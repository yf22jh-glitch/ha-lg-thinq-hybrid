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
from typing import Any, Mapping

import aiohttp

CLIMATE_TUPLE_CAPABILITY = "climate.mode_fan_setpoint"
CLIMATE_POWER_ON_CAPABILITY = "climate.power_on_with_setpoint"
POWER_CAPABILITY = "operation.power_requested"
#: The two swing directions, as single-field settings. CST170 has a codec for both; CST570 has only
#: the vertical one - its 0x206 frames decode to an already-observed value, so no value has a frame
#: of its own. A model without a codec refuses and the request goes to the cloud.
SWING_VERTICAL_CAPABILITY = "swing.vertical_enabled"
SWING_HORIZONTAL_CAPABILITY = "swing.horizontal_enabled"

#: Home Assistant's own climate modes, in the bridge's vocabulary. `auto` is deliberately absent: the
#: setpoint tag carries something other than a temperature there, so a tuple naming one is meaningless.
#: `climate_tuple` refuses `auto` however it arrives, because the shadow will otherwise supply it for a
#: request that only meant to change the fan.
HVAC_TO_LOCAL_MODE = {
    "cool": "cool",
    "dry": "dry",
    "fan_only": "fan_only",
}

#: The mode in which the setpoint field means something else. Named here rather than only excluded
#: above, so the rule holds wherever a mode comes from.
UNWRITABLE_MODE = "auto"

#: The fan step while which the appliance reports a setpoint that is not its stored one. In 파워
#: 냉방풍 it reports `temperature.target_c` as 18 and returns the real target as soon as the fan
#: leaves `power`. Two rules follow, and the second is the one that matters: while the shadow says
#: `power`, its setpoint is not a reading to fill anything in from - so a request that only changes
#: the fan, which is how anyone leaves 파워 냉방풍, would otherwise have written that 18 as the
#: genuine setpoint and dropped the room seven degrees, silently.
UNWRITABLE_FAN = "power"

#: LG's own wind-strength names, in the bridge's vocabulary. `POWER` is deliberately absent: the only
#: observed frame carrying it is `cool|power|18C`, so a local `POWER` request would be encodable only
#: at one exact mode and temperature and would otherwise be refused after a round trip. The cloud takes
#: it directly.
WIND_STRENGTH_TO_LOCAL_FAN = {
    "LOW": "low",
    "MID": "medium",
    "HIGH": "high",
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
    """The local path cannot serve this request; the caller should use the cloud."""


class LocalCommandFailed(RuntimeError):
    """The bridge refused the command, or the appliance never acknowledged it."""


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


def climate_tuple(
    shadow: Mapping[str, Any],
    *,
    mode: str | None = None,
    fan: str | None = None,
    target_c: float | None = None,
    now: datetime | None = None,
) -> str:
    """`mode|fan|temperatureC`, taking whatever the request does not name from the appliance.

    Raises `LocalCommandUnavailable` when the shadow cannot supply a field the frame must carry - the
    appliance writes mode, fan and setpoint together, so a request to change one of them still has to
    state the other two, and stating a guess would silently move them.
    """
    now = now or datetime.now(timezone.utc)
    reported_mode = _fresh(shadow.get("operation.mode"), now)
    reported_fan = _fresh(shadow.get("fan.mode"), now)
    resolved_mode = mode if mode is not None else reported_mode
    resolved_fan = fan if fan is not None else reported_fan
    if target_c is not None:
        resolved_temp: Any = target_c
    elif reported_mode is None:
        # The meaning of 0x1fe depends on the mode: in AUTO it is not a temperature. Fields age
        # independently, so a fresh-looking target beside a stale/missing mode cannot be classified
        # and must not be restated as a setpoint. An explicit target above does not depend on it.
        raise LocalCommandUnavailable(
            "the appliance has not reported operation.mode recently enough to tell whether its "
            "setpoint field is a temperature"
        )
    elif reported_mode == UNWRITABLE_MODE:
        # Naming a writable destination mode does not change what the current reading means. In
        # AUTO, 0x1fe is not a temperature; only an explicitly requested target can supply one for
        # the tuple that leaves AUTO. Reusing the field here would turn a mode-only request into an
        # unrequested temperature change.
        raise LocalCommandUnavailable(
            "the appliance reports no temperature setpoint in auto mode, so leaving it locally "
            "requires an explicit target"
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
        # Not a reading, so it fills nothing in - and said as itself, because "not reported
        # recently enough" would send the next reader looking at freshness.
        raise LocalCommandUnavailable(
            "the appliance reports a placeholder setpoint while the fan is on power, "
            "so a request that does not state its own cannot be written"
        )
    else:
        resolved_temp = _fresh(shadow.get("temperature.target_c"), now)
    if resolved_fan == UNWRITABLE_FAN:
        # Asking for the power fan, or leaving it in place: the setpoint cannot be set while it is
        # on, and the frame must carry one, so there is no tuple to write either way.
        raise LocalCommandUnavailable(
            "the setpoint cannot be stated while the fan is on power, so no tuple can be written"
        )
    if resolved_mode == UNWRITABLE_MODE:
        # Whether the request named it or the shadow filled it in. In auto the setpoint tag carries
        # something other than a temperature, so a tuple stating one would write a number into a
        # field that means something else - and a request to change the fan would do it silently.
        raise LocalCommandUnavailable("auto mode has no setpoint to state, so no tuple can be written")
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

    async def async_send(self, device_id: str, capability: str, value: str) -> LocalCommandResult:
        try:
            async with self._session.post(
                f"{self._base_url}/control/{device_id}",
                json={"capability": capability, "value": value},
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
            raise LocalCommandFailed(f"the local bridge did not answer in time: {err}") from err

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
            raise LocalCommandFailed(detail)
        result = LocalCommandResult(str(fields.get("verdict") or ""), fields.get("state") or {})
        if result.verdict == UNREPORTED:
            # The frame went out and the appliance never accounted for it. Saying so is the point -
            # the alternative is Home Assistant showing a state nothing confirmed.
            raise LocalCommandFailed("the appliance did not report the change")
        if result.verdict not in KNOWN_VERDICTS:
            raise LocalCommandFailed(f"unexpected verdict {result.verdict!r}")
        # `unverifiable` is returned rather than raised. The frame went out; nothing that arrives
        # afterwards could tell a restatement from a fresh report, which is not a failure and is not
        # something to retry - and raising it made restating a setting the unit was already on look
        # like an error, whose obvious next move is to press it again.
        return result
