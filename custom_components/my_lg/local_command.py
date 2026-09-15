"""Send a command to an appliance through the local Rethink bridge.

Home Assistant speaks LG's ThinQ Connect vocabulary; the bridge speaks the vocabulary its own packet
decoder produces. Translating between them would mean maintaining a third table that has to stay in
step with two others, and getting it wrong is not visible - a claim in the wrong spelling simply never
confirms.

So this does not translate. The local shadow already reports `operation.mode` and `fan.mode` in exactly
the vocabulary the bridge accepts, so a request that changes one field takes the others from the
shadow and substitutes only what changed. The single mapping that remains is Home Assistant's own
HVACMode, which is not LG's and is small enough to be read at a glance.

The bridge decides whether a command may be sent, encodes it from frames observed on the wire, records
it before sending, and answers with what the appliance itself reported. Nothing here re-implements any
of that; it composes the request and reports the verdict honestly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from urllib.parse import quote
from typing import Any, Mapping

import aiohttp
from .local_control_confirmed_features import APPLIANCE_SETTING_MODELS, APPLIANCE_VALUE_MODELS, APPLIANCE_VALUE_OPTIONS
from .local_water_dnd import WINDOW as WATER_DND_WINDOW, is_canonical_window
from .local_water_parameters import SCHEMAS as WATER_PARAMETER_SCHEMAS, is_canonical_parameter
from .local_washer_options import CAPABILITY as WASHER_PROGRAM, is_canonical_program

CLIMATE_TUPLE_CAPABILITY = "climate.mode_fan_setpoint"
CLIMATE_POWER_ON_CAPABILITY = "climate.power_on_with_setpoint"
POWER_CAPABILITY = "operation.power_requested"
_CLIMATE_CAPABILITIES = frozenset(
    (CLIMATE_TUPLE_CAPABILITY, CLIMATE_POWER_ON_CAPABILITY)
)
_CLIMATE_COMMON_FIELDS = (
    "operation.mode",
    "fan.mode",
)
TEMPERATURE_TARGET_SEMANTIC = "temperature.target_c"
COMFORT_PREFERENCE_SEMANTIC = "comfort.preference_step"
#: The two swing directions, as single-field settings. CST170 has a codec for both; CST570 has only
#: the vertical one - its 0x206 frames decode to an already-observed value, so no value has a frame
#: of its own. A model without a codec refuses and the request goes to the cloud.
SWING_VERTICAL_CAPABILITY = "swing.vertical_enabled"
SWING_HORIZONTAL_CAPABILITY = "swing.horizontal_enabled"

#: Legacy hybrid routing only advertises the modes it already supported locally. The Local-native
#: climate below has a separate map because its pinned model domain, not cloud fallback behavior,
#: decides whether AUTO exists.
HVAC_TO_LOCAL_MODE = {
    "cool": "cool",
    "dry": "dry",
    "fan_only": "fan_only",
}
LOCAL_NATIVE_HVAC_TO_MODE = {**HVAC_TO_LOCAL_MODE, "auto": "auto"}

#: Modes whose target grid differs from the ordinary COOL/DRY/FAN_ONLY carry-over. Crossing this
#: boundary must use a target that was observed in the destination mode or one the user supplied;
#: otherwise a mode-only request would silently let the appliance clamp a foreign target.
MODE_SPECIFIC_TARGET = "auto"

#: The fan step while which the appliance reports a setpoint that is not its stored one. In 파워
#: 냉방풍 it reports `temperature.target_c` as 18 and returns the real target as soon as the fan
#: leaves `power`. Two rules follow, and the second is the one that matters: while the shadow says
#: `power`, its setpoint is not a reading to fill anything in from - so a request that only changes
#: the fan, which is how anyone leaves 파워 냉방풍, would otherwise have written that 18 as the
#: genuine setpoint and dropped the room seven degrees, silently.
UNWRITABLE_FAN = "power"

#: LG's own wind-strength names, in the bridge's vocabulary.  The decoder is
#: the authority for the actual Local domain; this small table only translates
#: Home Assistant's display spelling.  POWER is safe to offer because the
#: bridge's composite authority constrains it to cooling and deliberately does
#: not claim the temporary 18 C readback as a changed setpoint.
WIND_STRENGTH_TO_LOCAL_FAN = {
    "LOW": "low",
    "MID": "medium",
    "HIGH": "high",
    "POWER": "power",
    "AUTO": "auto",
}

#: How old a shadow field may be and still fill a field the request does not change. These ACs report
#: their full tuple every 190-300 seconds even when nothing changes, so the bridge's 60-second
#: acknowledgement ceiling cannot be reused as an input ceiling: it made a healthy local path reject
#: ordinary steady-state commands most of the time. Two observed maximum report windows are accepted;
#: beyond that the cloud receives the partial intent instead of this integration restating old values.
MAX_SHADOW_AGE = timedelta(minutes=10)

#: The bridge's management API, on the loopback of the machine Home Assistant shares with it. The
#: control endpoint takes no credential there, unlike the lifecycle relay's, which carries a bearer
#: token; `deploy/nginx/` also proxies this listener from a public TLS port behind shared Basic Auth,
#: so "loopback" describes how this client reaches it and not who else can.
LOCAL_BRIDGE_BASE_URL = "http://127.0.0.1:44401"

#: Verdicts the bridge returns. Only the first two mean the appliance itself confirmed the outcome.
CONFIRMED = ("confirmed", "already")
#: The frame went out and the appliance never accounted for it.
UNREPORTED = "unreported"
#: The frame went out and nothing that arrives could confirm or deny it.
UNVERIFIABLE = "unverifiable"
KNOWN_VERDICTS = (*CONFIRMED, UNVERIFIABLE)


@dataclass(frozen=True)
class SwingWrite:
    """One swing direction, as both vocabularies name it."""

    field: str
    capability: str
    enabled: bool


def swing_writes(*, horizontal: bool | None, vertical: bool | None) -> tuple[SwingWrite, ...]:
    """The writes one swing selection means, with each direction's two names kept together.

    Each argument is what that direction should become, or None where the appliance does not have
    it. Assembled here rather than inline at the call site because the two directions differ only
    by which strings go where, and a swap would send a horizontal selection to the vertical vane
    with nothing to notice it - while which Home Assistant swing mode implies which direction stays
    with the constants that name them.
    """
    writes = []
    if horizontal is not None:
        writes.append(SwingWrite("rotateLeftRight", SWING_HORIZONTAL_CAPABILITY, horizontal))
    if vertical is not None:
        writes.append(SwingWrite("rotateUpDown", SWING_VERTICAL_CAPABILITY, vertical))
    return tuple(writes)


#: Failures that happen before a request is on the wire: the connection could not be made, the
#: connect phase timed out, or the URL was never usable. Everything else - a timeout once the
#: request is out, a connection dropping mid-request - leaves it unknown whether the bridge ran the
#: handler, and the bridge records and sends a frame before it waits for the appliance, so "unknown"
#: has to mean "assume it was sent".
_NOTHING_WAS_SENT = (
    aiohttp.ClientConnectorError,
    aiohttp.ConnectionTimeoutError,
    aiohttp.InvalidURL,
)

#: The one gateway answer that means the request was never forwarded. 502 and 504 do not, and the
#: status alone cannot separate their causes: nginx answers 502 both when it could not connect at
#: all and when the upstream closed AFTER receiving the request, and 504 when it forwarded and gave
#: up waiting - which is exactly the window in which the bridge has already sent the frame and is
#: waiting for the appliance. Unable to tell, this assumes the frame went out, because a second
#: command to a machine that heats and spins is the worse of the two mistakes. It costs little here:
#: this client reaches the bridge directly on the loopback, where a bridge that is down answers with
#: a refused connection - which IS offered to the cloud - and no gateway status appears at all.
_NOT_FORWARDED_STATUS = 503


def reflects_the_appliance(outcome: "LocalCommandResult | None") -> bool:
    """Whether Home Assistant may show the requested state as though it took effect.

    Three outcomes, three answers. `None` means the local path did not serve it, so the caller sent
    it to the cloud and the usual optimistic update applies. A confirmed local write means the
    appliance itself said so. Anything else went out with nothing able to confirm it, and showing a
    state the appliance never acknowledged would be a guess - the next report is a second away.
    """
    return outcome is None or outcome.confirmed


class LocalCommandUnavailable(RuntimeError):
    """The local path refused before a frame could reach the appliance."""


class LocalCommandFailed(RuntimeError):
    """The bridge returned an invalid or explicit terminal failure."""


class LocalCommandPending(RuntimeError):
    """A command may have reached the wire and must reconcile from readback."""


@dataclass(frozen=True)
class LocalCommandResult:
    verdict: str
    state: Mapping[str, Any]

    @property
    def confirmed(self) -> bool:
        return self.verdict in CONFIRMED


def _fresh(field: Any, now: datetime) -> Any:
    """A shadow value, or None when it is too old to build a command from."""
    if field is None:
        return None
    observed_at = getattr(field, "observed_at", None)
    if not isinstance(observed_at, datetime):
        return None
    if now - observed_at > MAX_SHADOW_AGE:
        return None
    return getattr(field, "value", None)


def _validated_expected_state(
    expected_state: Mapping[str, Any] | None,
) -> dict[str, str | int | float]:
    """Return the exact climate compare object accepted by the bridge."""
    if not isinstance(expected_state, Mapping):
        raise LocalCommandUnavailable(
            "a climate command requires the exact current mode, fan and mode-dependent argument"
        )
    mode = expected_state.get("operation.mode")
    fan = expected_state.get("fan.mode")
    argument_semantic = (
        COMFORT_PREFERENCE_SEMANTIC
        if mode == "auto"
        else TEMPERATURE_TARGET_SEMANTIC
    )
    expected_keys = {*_CLIMATE_COMMON_FIELDS, argument_semantic}
    if set(expected_state) != expected_keys:
        raise LocalCommandUnavailable(
            "a climate command requires exactly one mode-dependent argument"
        )
    argument = expected_state.get(argument_semantic)
    if type(mode) is not str or type(fan) is not str:
        raise LocalCommandUnavailable(
            "a climate command requires a string mode and string fan"
        )
    if (
        type(argument) not in (int, float)
        or not math.isfinite(float(argument))
        or (
            argument_semantic == COMFORT_PREFERENCE_SEMANTIC
            and (type(argument) is not int or argument < -2 or argument > 2)
        )
    ):
        raise LocalCommandUnavailable(
            "a climate command requires a valid numeric mode-dependent argument"
        )
    return {
        "operation.mode": mode,
        "fan.mode": fan,
        argument_semantic: argument,
    }


def climate_state_fields(mode: object) -> tuple[str, str, str]:
    """Return the exact current-state variant for one decoded mode."""
    return (
        *_CLIMATE_COMMON_FIELDS,
        (
            COMFORT_PREFERENCE_SEMANTIC
            if mode == "auto"
            else TEMPERATURE_TARGET_SEMANTIC
        ),
    )


def climate_expected_state(
    shadow: Mapping[str, Any], now: datetime | None = None
) -> dict[str, str | int | float]:
    """The appliance tuple HA used before applying the requested changes."""
    now = now or datetime.now(timezone.utc)
    mode = _fresh(shadow.get("operation.mode"), now)
    fields = climate_state_fields(mode)
    return _validated_expected_state(
        {
            semantic_id: _fresh(shadow.get(semantic_id), now)
            for semantic_id in fields
        }
    )


def climate_tuple(
    shadow: Mapping[str, Any],
    *,
    mode: str | None = None,
    fan: str | None = None,
    target_c: float | None = None,
    comfort_preference: int | None = None,
    retained_target_c: float | None = None,
    retained_comfort_preference: int | None = None,
    now: datetime | None = None,
) -> str:
    """Render the mode-discriminated tuple without mixing temperature and AUTO comfort.

    Raises `LocalCommandUnavailable` when the shadow cannot supply a field the frame must carry - the
    appliance writes mode, fan and setpoint together, so a request to change one of them still has to
    state the other two, and stating a guess would silently move them.
    """
    now = now or datetime.now(timezone.utc)
    reported_mode = _fresh(shadow.get("operation.mode"), now)
    reported_fan = _fresh(shadow.get("fan.mode"), now)
    resolved_mode = mode if mode is not None else reported_mode
    resolved_fan = fan if fan is not None else reported_fan
    if target_c is not None and comfort_preference is not None:
        raise LocalCommandUnavailable(
            "a climate command cannot set temperature and AUTO comfort together"
        )
    if resolved_mode == "auto":
        if target_c is not None:
            raise LocalCommandUnavailable(
                "AUTO takes a comfort preference, not a Celsius target"
            )
        if resolved_fan == UNWRITABLE_FAN:
            raise LocalCommandUnavailable(
                "power fan is available only in cooling mode"
            )
        resolved_preference: Any
        if comfort_preference is not None:
            resolved_preference = comfort_preference
        elif reported_mode == "auto" and reported_fan != UNWRITABLE_FAN:
            resolved_preference = _fresh(
                shadow.get(COMFORT_PREFERENCE_SEMANTIC), now
            )
        else:
            resolved_preference = retained_comfort_preference
        missing = [
            name
            for name, value in (
                ("operation.mode", resolved_mode),
                ("fan.mode", resolved_fan),
                (COMFORT_PREFERENCE_SEMANTIC, resolved_preference),
            )
            if value is None
        ]
        if missing:
            raise LocalCommandUnavailable(
                "the appliance has not reported "
                + ", ".join(missing)
                + " recently enough to write from"
            )
        if (
            type(resolved_preference) is not int
            or resolved_preference < -2
            or resolved_preference > 2
        ):
            raise LocalCommandUnavailable(
                "the reported AUTO comfort preference is not a whole step from -2 to 2"
            )
        return f"auto|{resolved_fan}|comfort:{resolved_preference}"

    if comfort_preference is not None:
        raise LocalCommandUnavailable(
            "AUTO comfort preference is valid only in AUTO mode"
        )
    if resolved_mode in ('dry', 'fan_only'):
        # These modes carry the live Celsius target but do not offer a target
        # editor. AUTO/power carriers are not temperatures that can be preserved.
        current_temp = _fresh(shadow.get('temperature.target_c'), now)
        if (target_c is not None or reported_mode not in ('cool', 'dry', 'fan_only')
                or reported_fan not in ('very low', 'low', 'medium', 'high', 'auto')
                or type(current_temp) not in (int, float)):
            raise LocalCommandUnavailable(
                'dry/fan modes preserve a fresh ordinary setpoint; temperature editing or AUTO/power carry-over is unavailable'
            )
    if target_c is not None and resolved_fan == UNWRITABLE_FAN:
        # The frame carries a byte in the target slot, but the appliance does
        # not apply it while power fan is active and reports a temporary 18 C.
        # Accepting a target request here would promise a setting that cannot
        # be confirmed or even observed.
        raise LocalCommandUnavailable(
            "temperature cannot be changed while the fan is on power"
        )
    if target_c is not None:
        resolved_temp: Any = target_c
    elif reported_mode is None:
        # Target ranges are mode-dependent. Fields age independently, so a fresh-looking target
        # beside a stale/missing mode cannot be placed on the right grid and must not be restated.
        # An explicit target above does not depend on the old mode.
        raise LocalCommandUnavailable(
            "the appliance has not reported operation.mode recently enough to classify its setpoint"
        )
    elif reported_fan is None:
        # Whether the reported setpoint is real or the power-fan placeholder is a question about
        # the fan, so without a fresh fan reading the setpoint cannot be classified at all - and an
        # unclassified 18 is the one that would be written as genuine. Per-field freshness is real:
        # the publisher timestamps each semantic field on its own.
        raise LocalCommandUnavailable(
            "the appliance has not reported fan.mode recently enough to tell whether its setpoint is real"
        )
    elif reported_fan == UNWRITABLE_FAN:
        # 18 C is a temporary display value, not the retained setpoint.  The
        # router remembers only a previously observed non-power target and may
        # provide it here so leaving/restarting power fan cannot overwrite the
        # user's real target.  With no such observation we refuse rather than
        # guess.
        resolved_temp = retained_target_c
    elif MODE_SPECIFIC_TARGET in {resolved_mode, reported_mode}:
        # AUTO's carrier is not a temperature. Leaving AUTO, or crossing between ordinary modes,
        # therefore uses only a target observed for the destination mode rather than reusing a
        # different mode's argument.
        resolved_temp = retained_target_c
    else:
        resolved_temp = _fresh(shadow.get("temperature.target_c"), now)
    if resolved_fan == UNWRITABLE_FAN and resolved_mode != "cool":
        # 파워 냉방풍 is an app-declared cooling-only combination.  Do not
        # coerce either component behind the user's back.
        raise LocalCommandUnavailable(
            "power fan is available only in cooling mode"
        )
    missing = [
        name
        for name, value in (
            ("operation.mode", resolved_mode),
            ("fan.mode", resolved_fan),
            ("temperature.target_c", resolved_temp),
        )
        if value is None
    ]
    if missing:
        raise LocalCommandUnavailable(
            "the appliance has not reported " + ", ".join(missing) + " recently enough to write from"
        )
    try:
        temperature = float(resolved_temp)
    except (TypeError, ValueError) as err:
        # A shadow field that is not a number at all. Raised as unavailable rather than escaping,
        # so the request reaches the cloud instead of neither path.
        raise LocalCommandUnavailable(f"the reported setpoint is not a temperature: {resolved_temp!r}") from err
    rendered = int(temperature) if temperature.is_integer() else temperature
    return f"{resolved_mode}|{resolved_fan}|{rendered}C"


class LocalCommandClient:
    """Posts one semantic command to the bridge and reports what the appliance said."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str = LOCAL_BRIDGE_BASE_URL,
        timeout_s: float = 30.0,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)

    async def async_appliance_setting_state(self, device_id: str, capability: str) -> bool | str | None:
        """Read exact own-connection settings; never query the appliance or cloud."""
        model = APPLIANCE_SETTING_MODELS.get(capability) or APPLIANCE_VALUE_MODELS.get(capability)
        if model is None:
            return None
        async with self._session.get(
            f"{self._base_url}/control/home-assistant/{quote(device_id, safe='')}/appliance-settings-state",
            timeout=aiohttp.ClientTimeout(total=5), allow_redirects=False,
        ) as response:
            if response.status != 200:
                return None
            body = await response.json()
        if (not isinstance(body, dict) or body.get('schema_version') != 1
                or body.get('model_id') != model or not isinstance(body.get('values'), dict)):
            return None
        value = body['values'].get(capability)
        if capability == WASHER_PROGRAM:
            return value if is_canonical_program(value) else None
        if capability == WATER_DND_WINDOW:
            return value if is_canonical_window(value) else None
        if capability in WATER_PARAMETER_SCHEMAS:
            return value if is_canonical_parameter(capability,value) else None
        if capability in APPLIANCE_VALUE_MODELS:
            return value if isinstance(value,str) and value in APPLIANCE_VALUE_OPTIONS[capability] else None
        return value if type(value) is bool else None

    async def async_air_extra_state(self, device_id: str, capability: str) -> bool | None:
        """Own-connection cached value only; no device query or cloud fallback."""
        if capability not in ('clean_dry.enabled', 'rapid_operation.enabled'):
            return None
        async with self._session.get(
            f"{self._base_url}/control/home-assistant/{quote(device_id, safe='')}/air-extra-state",
            timeout=aiohttp.ClientTimeout(total=5), allow_redirects=False,
        ) as response:
            if response.status != 200:
                return None
            body = await response.json()
        if (not isinstance(body, dict) or body.get('schema_version') != 1
                or body.get('model_id') != 'AIR_910604_WW' or not isinstance(body.get('values'), dict)):
            return None
        value = body['values'].get(capability)
        return value if type(value) is bool else None

    async def async_vacuum_auto_emptying_state(self, device_id: str) -> bool | None:
        """Read a bridge-cached own-device scalar; never poll LG or send a packet."""
        async with self._session.get(
            f"{self._base_url}/control/home-assistant/{quote(device_id, safe='')}/vacuum-auto-emptying-state",
            timeout=aiohttp.ClientTimeout(total=5), allow_redirects=False,
        ) as response:
            if response.status != 200:
                return None
            body = await response.json()
        if (not isinstance(body, dict) or body.get('schema_version') != 1
                or body.get('model_id') != 'HWWA9X3C_F2U'):
            return None
        return body.get('enabled') if type(body.get('enabled')) is bool else None

    async def async_vacuum_reservation_state(self, device_id: str) -> dict | None:
        from .local_vacuum_reservation import MODEL, ENABLED, SCHEDULE, is_canonical_schedule
        async with self._session.get(
            f"{self._base_url}/control/home-assistant/{quote(device_id, safe='')}/vacuum-reservation-state",
            timeout=aiohttp.ClientTimeout(total=5), allow_redirects=False,
        ) as response:
            if response.status != 200:
                return None
            body = await response.json()
        if not isinstance(body, dict) or body.get('schema_version') != 1 or body.get('model_id') != MODEL or not isinstance(body.get('values'), dict):
            return None
        values = body['values']
        enabled, schedule = values.get(ENABLED), values.get(SCHEDULE)
        return {ENABLED: enabled if type(enabled) is bool else None,
                SCHEDULE: schedule if schedule == 'unset' or is_canonical_schedule(schedule) else None}

    async def async_send(
        self,
        device_id: str,
        capability: str,
        value: str,
        *,
        expected_state: Mapping[str, Any] | None = None,
    ) -> LocalCommandResult:
        if capability in _CLIMATE_CAPABILITIES:
            request_expected_state = _validated_expected_state(expected_state)
        elif expected_state is not None:
            raise LocalCommandUnavailable(
                "only climate tuple commands may carry expected_state"
            )
        else:
            request_expected_state = None
        request = {"capability": capability, "value": value}
        if request_expected_state is not None:
            request["expected_state"] = request_expected_state
        try:
            async with self._session.post(
                f"{self._base_url}/control/home-assistant/{device_id}",
                json=request,
                timeout=self._timeout,
                # 307/308 preserve POST. Following one could replay a single Home Assistant
                # request at a second endpoint, violating the one-command invariant.
                allow_redirects=False,
            ) as response:
                status = response.status
                try:
                    body = await response.json(content_type=None)
                except (ValueError, aiohttp.ClientError):
                    # A proxy answering with a page, or the connection dropping while the body is
                    # read. Either way a status line arrived, which is what decides whether the
                    # request may be made again.
                    body = None
        except _NOTHING_WAS_SENT as err:
            raise LocalCommandUnavailable(f"the local bridge could not be reached: {err}") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            # A request that went out and then failed. The bridge records a frame and sends it
            # before it waits up to twenty seconds for the appliance to report, so a timeout is
            # exactly the case where the write most likely DID happen - offering it to the cloud
            # would be the second command.
            raise LocalCommandPending(
                f"the local bridge did not answer in time: {err}"
            ) from err

        # A body that is valid JSON but not an object - a proxy's page, a route mismatch - would
        # otherwise raise AttributeError out of this method, past both the exceptions callers
        # handle, and the request would reach neither the appliance nor the cloud.
        fields = body if isinstance(body, dict) else {}
        detail = str(fields.get("error") or f"the bridge answered {status}")
        if 400 <= status < 500:
            # Nothing reached the appliance. The bridge records a frame before sending it and
            # turns every failure after that into a verdict, so a 4xx is always a refusal made
            # before the write - which is what makes falling back to the cloud safe rather than
            # a second command for the same request.
            raise LocalCommandUnavailable(detail)
        if status == _NOT_FORWARDED_STATUS:
            # Nothing in front of the bridge had anywhere to forward it, so the endpoint never ran.
            raise LocalCommandUnavailable(detail)
        if status >= 500:
            # Unknown: something threw where the endpoint expected nothing to. Whether a frame
            # went out cannot be told from here, so this is not offered to the cloud.
            raise LocalCommandPending(detail)
        result = LocalCommandResult(str(fields.get("verdict") or ""), fields.get("state") or {})
        if result.verdict == UNREPORTED:
            # The frame went out and the appliance never accounted for it. Saying so is the point -
            # the alternative is Home Assistant showing a state nothing confirmed.
            raise LocalCommandPending("the appliance did not report the change")
        if result.verdict not in KNOWN_VERDICTS:
            raise LocalCommandFailed(f"unexpected verdict {result.verdict!r}")
        # `unverifiable` is returned rather than raised. The frame went out; nothing that arrives
        # afterwards could tell a restatement from a fresh report, which is not a failure and is not
        # something to retry - and raising it made restating a setting the unit was already on look
        # like an error, whose obvious next move is to press it again.
        return result
