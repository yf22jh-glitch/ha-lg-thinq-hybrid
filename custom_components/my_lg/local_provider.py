"""Strict read-only provider for the Rethink Local pilot state feed.

This module deliberately has no Home Assistant or MQTT dependency.  It owns the
subscriber-side contract and cursor fencing, while transport and entity routing
remain independently testable.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

_LOGGER = logging.getLogger(__name__)

LOCAL_PROVIDER_MODE_DISABLED = "disabled"
LOCAL_PROVIDER_MODE_SHADOW = "shadow"

OPT_LOCAL_PROVIDER_MODE = "local_provider_mode"
OPT_LOCAL_PAT_DEVICE_ID = "local_pat_device_id"
OPT_LOCAL_BINDING_ID = "local_binding_id"
OPT_LOCAL_MQTT_PASSWORD = "local_mqtt_password"

LOCAL_PILOT_PREFIX = "lg_rethink_pilot/v1"
LOCAL_MODEL_ID = "DHUM_056905_WW"
LOCAL_PLATFORM = "thinq2"
LOCAL_SEMANTICS_REVISION = 26
LOCAL_WATER_TANK_FIELD = "water_tank.full"
LOCAL_WATER_TANK_CONFIDENCE = (
    "confirmed-exact-device-bidirectional-local-interlock-correlation"
)
WIDEQ_WATER_TANK_KEY = "airState.miscFuncState.watertankLight"

# The shadow runtime places a tighter boundary around the 64 KiB general
# decoder schema.  Keep the same 8 KiB subscriber limit here.
MAX_PAYLOAD_BYTES = 8 * 1024
MAX_FUTURE_SKEW = timedelta(minutes=5)
MAX_TOMBSTONED_GENERATIONS = 10_000
MAX_JSON_SAFE_INTEGER = 9_007_199_254_740_991

_BINDING_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{15,127}$")
_OPAQUE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")
_SERVICE_INSTANCE_ID = re.compile(r"^[a-f0-9]{32}$")
_ISO_TIMESTAMP = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d{1,3}))?(Z|[+-](\d{2}):(\d{2}))$"
)

_SNAPSHOT_KEYS = frozenset(
    {
        "schema_version",
        "semantics_revision",
        "binding_id",
        "model_id",
        "platform",
        "session_id",
        "sequence",
        "published_at",
        "fields",
        "diagnostics",
    }
)
_FIELD_KEYS = frozenset(
    {"value", "value_type", "observed_at", "confidence", "exposure"}
)
_DIAGNOSTIC_KEYS = frozenset(
    {"rejected_frames", "unresolved_fields", "invalid_values", "unsupported_frames"}
)
_AVAILABILITY_KEYS = frozenset({"status", "session_id", "observed_at"})
_RUNTIME_AVAILABILITY_KEYS = frozenset({"status", "service_instance_id", "observed_at"})


class LocalProviderContractError(ValueError):
    """An MQTT publication failed the pinned Local provider contract."""


class LocalProviderConfigurationError(ValueError):
    """Local shadow options are incomplete or outside the pilot boundary."""


@dataclass(frozen=True)
class LocalShadowConfiguration:
    """Validated configuration for one exact dehumidifier pilot binding."""

    pat_device_id: str
    binding_id: str
    mqtt_username: str
    mqtt_password: str


def local_shadow_configuration(
    options: Mapping[str, object],
) -> LocalShadowConfiguration | None:
    """Validate disabled/shadow options; no preferred mode exists yet."""
    mode = options.get(OPT_LOCAL_PROVIDER_MODE, LOCAL_PROVIDER_MODE_DISABLED)
    if mode == LOCAL_PROVIDER_MODE_DISABLED:
        return None
    if mode != LOCAL_PROVIDER_MODE_SHADOW:
        raise LocalProviderConfigurationError(
            "Local provider mode is unsupported during the shadow pilot"
        )
    pat_device_id = options.get(OPT_LOCAL_PAT_DEVICE_ID)
    if not isinstance(pat_device_id, str) or not _OPAQUE_ID.fullmatch(pat_device_id):
        raise LocalProviderConfigurationError("Local provider PAT device id is invalid")
    try:
        binding_id = validate_binding_id(options.get(OPT_LOCAL_BINDING_ID))
    except LocalProviderContractError as err:
        raise LocalProviderConfigurationError(str(err)) from err
    password = options.get(OPT_LOCAL_MQTT_PASSWORD)
    if (
        not isinstance(password, str)
        or not password
        or len(password.encode("utf-8")) > 1024
    ):
        raise LocalProviderConfigurationError("Local provider MQTT password is invalid")
    return LocalShadowConfiguration(
        pat_device_id=pat_device_id,
        binding_id=binding_id,
        mqtt_username=f"shadow-{binding_id}",
        mqtt_password=password,
    )


def merge_local_shadow_options(
    submitted: Mapping[str, object], existing: Mapping[str, object]
) -> dict[str, object]:
    """Normalize OptionsFlow input without exposing or silently clearing its secret."""
    result = dict(submitted)
    mode = result.get(OPT_LOCAL_PROVIDER_MODE, LOCAL_PROVIDER_MODE_DISABLED)
    if mode == LOCAL_PROVIDER_MODE_DISABLED:
        for key in (
            OPT_LOCAL_PAT_DEVICE_ID,
            OPT_LOCAL_BINDING_ID,
            OPT_LOCAL_MQTT_PASSWORD,
        ):
            result.pop(key, None)
        return result

    if result.get(OPT_LOCAL_MQTT_PASSWORD) in (None, ""):
        password = existing.get(OPT_LOCAL_MQTT_PASSWORD)
        if isinstance(password, str) and password:
            result[OPT_LOCAL_MQTT_PASSWORD] = password
        else:
            result.pop(OPT_LOCAL_MQTT_PASSWORD, None)
    local_shadow_configuration(result)
    return result


def _contract_error(message: str) -> None:
    raise LocalProviderContractError(message)


def validate_binding_id(value: object) -> str:
    """Return one exact pilot binding id or fail closed."""
    if not isinstance(value, str) or not _BINDING_ID.fullmatch(value):
        _contract_error("Local provider binding id is invalid")
    return value


def _exact_object(value: object, keys: frozenset[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        _contract_error(f"{name} keys are invalid")
    return value


def _reject_json_constant(value: str) -> None:
    _contract_error(f"Local provider JSON constant is invalid: {value}")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _contract_error("Local provider JSON has duplicate keys")
        result[key] = value
    return result


def _decode_payload(payload: object) -> dict[str, Any]:
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        _contract_error("Local provider payload must be bytes")
    owned = bytes(payload)
    if not owned or len(owned) > MAX_PAYLOAD_BYTES:
        _contract_error("Local provider payload is empty or oversized")
    try:
        text = owned.decode("utf-8")
    except UnicodeDecodeError:
        _contract_error("Local provider payload is not UTF-8")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except LocalProviderContractError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError):
        _contract_error("Local provider payload is not valid JSON")
    if not isinstance(value, dict):
        _contract_error("Local provider payload must be a JSON object")
    return value


def _canonical_payload(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _utc_now(now: Callable[[], datetime]) -> datetime:
    value = now()
    if not isinstance(value, datetime) or value.tzinfo is None:
        _contract_error("Local provider clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _timestamp(value: object, name: str, now: datetime) -> datetime:
    if not isinstance(value, str):
        _contract_error(f"{name} is invalid")
    match = _ISO_TIMESTAMP.fullmatch(value)
    if match is None:
        _contract_error(f"{name} is invalid")
    zone = match.group(8)
    zone_hour = int(match.group(9) or 0)
    zone_minute = int(match.group(10) or 0)
    if zone != "Z" and (
        zone_hour > 14 or zone_minute > 59 or (zone_hour == 14 and zone_minute != 0)
    ):
        _contract_error(f"{name} is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if zone == "Z" else value)
    except ValueError:
        _contract_error(f"{name} is invalid")
    parsed = parsed.astimezone(timezone.utc)
    if parsed > now + MAX_FUTURE_SKEW:
        _contract_error(f"{name} is too far in the future")
    return parsed


def _safe_nonnegative_integer(value: object, name: str) -> int:
    if (
        type(value) is not int or value < 0 or value > MAX_JSON_SAFE_INTEGER
    ):  # bool is deliberately not an int here
        _contract_error(f"{name} is invalid")
    return value


def _parse_state(
    payload: object,
    expected_binding_id: str,
    now: datetime,
) -> tuple[dict[str, Any], str, int, bool]:
    snapshot = _exact_object(_decode_payload(payload), _SNAPSHOT_KEYS, "snapshot")
    if type(snapshot["schema_version"]) is not int or snapshot["schema_version"] != 1:
        _contract_error("Local provider snapshot schema is unsupported")
    if (
        type(snapshot["semantics_revision"]) is not int
        or snapshot["semantics_revision"] != LOCAL_SEMANTICS_REVISION
    ):
        _contract_error("Local provider semantics revision is unsupported")
    if snapshot["binding_id"] != expected_binding_id:
        _contract_error("Local provider binding does not match")
    if snapshot["model_id"] != LOCAL_MODEL_ID or snapshot["platform"] != LOCAL_PLATFORM:
        _contract_error("Local provider model or platform does not match")

    session_id = snapshot["session_id"]
    if not isinstance(session_id, str) or not _OPAQUE_ID.fullmatch(session_id):
        _contract_error("Local provider session id is invalid")
    sequence = snapshot["sequence"]
    if type(sequence) is not int or sequence < 1 or sequence > MAX_JSON_SAFE_INTEGER:
        _contract_error("Local provider sequence is invalid")
    published_at = _timestamp(snapshot["published_at"], "published_at", now)

    fields = _exact_object(
        snapshot["fields"], frozenset({LOCAL_WATER_TANK_FIELD}), "snapshot fields"
    )
    field = _exact_object(
        fields[LOCAL_WATER_TANK_FIELD], _FIELD_KEYS, "water tank field"
    )
    if field["value_type"] != "boolean" or type(field["value"]) is not bool:
        _contract_error("Local provider water tank value is not boolean")
    if field["confidence"] != LOCAL_WATER_TANK_CONFIDENCE:
        _contract_error("Local provider water tank confidence is unsupported")
    if field["exposure"] != "state":
        _contract_error("Local provider water tank exposure is unsupported")
    observed_at = _timestamp(field["observed_at"], "water tank observed_at", now)
    if observed_at > published_at:
        _contract_error("Local provider water tank observation is after publication")

    diagnostics = _exact_object(
        snapshot["diagnostics"], _DIAGNOSTIC_KEYS, "snapshot diagnostics"
    )
    for name, value in diagnostics.items():
        _safe_nonnegative_integer(value, f"snapshot diagnostics {name}")

    return snapshot, session_id, sequence, field["value"]


def _parse_availability(
    payload: object,
    now: datetime,
) -> tuple[dict[str, Any], str, str, datetime]:
    value = _exact_object(
        _decode_payload(payload), _AVAILABILITY_KEYS, "device availability"
    )
    status = value["status"]
    if status not in ("online", "offline"):
        _contract_error("Local provider device availability is invalid")
    session_id = value["session_id"]
    if not isinstance(session_id, str) or not _OPAQUE_ID.fullmatch(session_id):
        _contract_error("Local provider availability session id is invalid")
    observed_at = _timestamp(
        value["observed_at"], "device availability observed_at", now
    )
    return value, status, session_id, observed_at


def _parse_runtime_availability(
    payload: object,
    now: datetime,
) -> tuple[dict[str, Any], str, str, datetime]:
    value = _exact_object(
        _decode_payload(payload),
        _RUNTIME_AVAILABILITY_KEYS,
        "runtime availability",
    )
    status = value["status"]
    if status not in ("online", "offline"):
        _contract_error("Local provider runtime availability is invalid")
    service_instance_id = value["service_instance_id"]
    if not isinstance(service_instance_id, str) or not _SERVICE_INSTANCE_ID.fullmatch(
        service_instance_id
    ):
        _contract_error("Local provider service instance id is invalid")
    observed_at = _timestamp(
        value["observed_at"], "runtime availability observed_at", now
    )
    return value, status, service_instance_id, observed_at


class LocalWaterTankShadowProvider:
    """Consume one exact read-only Local feed without owning an HA entity."""

    mode = LOCAL_PROVIDER_MODE_SHADOW

    def __init__(
        self,
        binding_id: str,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.binding_id = validate_binding_id(binding_id)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.state_topic = f"{LOCAL_PILOT_PREFIX}/state/{self.binding_id}"
        self.availability_topic = f"{LOCAL_PILOT_PREFIX}/availability/{self.binding_id}"
        self.runtime_availability_topic = (
            f"{LOCAL_PILOT_PREFIX}/runtime/{self.binding_id}/availability"
        )
        self.topics = (
            self.state_topic,
            self.availability_topic,
            self.runtime_availability_topic,
        )

        self._transport_ready = False
        self._session_id: str | None = None
        self._sequence = 0
        self._state_payload: str | None = None
        self._shadow_value: bool | None = None
        self._device_status = "unknown"
        self._device_availability_payload: str | None = None
        self._device_availability_at: datetime | None = None
        self._tombstoned_sessions: set[str] = set()

        self._service_instance_id: str | None = None
        self._runtime_status = "unknown"
        self._runtime_payload: str | None = None
        self._runtime_availability_at: datetime | None = None
        self._tombstoned_service_instances: set[str] = set()
        self._rejected_messages = 0

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def shadow_value(self) -> bool | None:
        """Return the diagnostic Local value, never the operational HA value."""
        return self._shadow_value

    @property
    def rejected_messages(self) -> int:
        return self._rejected_messages

    @property
    def transport_ready(self) -> bool:
        return self._transport_ready

    @property
    def shadow_healthy(self) -> bool:
        """Return whether all three read-only feed fences currently agree."""
        return (
            self._transport_ready
            and self._shadow_value is not None
            and self._session_id is not None
            and self._device_status == "online"
            and self._runtime_status == "online"
        )

    def set_transport_ready(self, ready: bool) -> None:
        if type(ready) is not bool:
            raise TypeError("Local provider transport readiness must be boolean")
        self._transport_ready = ready

    def ingest(
        self,
        topic: str,
        payload: object,
        *,
        qos: int,
        retained: bool,
    ) -> bool:
        """Validate and atomically apply one exact MQTT publication."""
        try:
            if type(qos) is not int or qos != 1:
                _contract_error("Local provider requires MQTT QoS 1")
            if type(retained) is not bool:
                _contract_error("Local provider retained flag is invalid")
            now = _utc_now(self._now)
            if topic == self.state_topic:
                return self._ingest_state(payload, now)
            if topic == self.availability_topic:
                return self._ingest_device_availability(payload, now)
            if topic == self.runtime_availability_topic:
                return self._ingest_runtime_availability(payload, now)
            _contract_error("Local provider topic is not authorized")
        except LocalProviderContractError:
            self._rejected_messages += 1
            raise

    def ingest_retained_final_current(
        self,
        publications: Mapping[str, tuple[object, int, bool]],
    ) -> bool:
        """Atomically adopt one complete retained-only set after a reconnect."""
        return self._ingest_final_current(publications, require_retained=True)

    def ingest_bootstrap_final_current(
        self,
        publications: Mapping[str, tuple[object, int, bool]],
    ) -> bool:
        """Atomically adopt a complete retained set plus exact live repairs.

        MQTT marks the initial subscription replay as retained, but later
        retained writes delivered to an existing subscriber normally arrive
        with ``retain=False``. Those exact QoS 1 messages may repair an
        initially inconsistent retained candidate set before transport-ready.
        """
        return self._ingest_final_current(publications, require_retained=False)

    def _ingest_final_current(
        self,
        publications: Mapping[str, tuple[object, int, bool]],
        *,
        require_retained: bool,
    ) -> bool:
        """Validate and atomically apply one complete bootstrap candidate.

        A subscriber can miss the publisher's retained ``offline`` while its
        socket is down.  A complete, exact three-topic set is therefore the
        only path allowed to advance to a new session/service generation
        without observing that intermediate publication live.
        """
        try:
            if self._transport_ready:
                _contract_error(
                    "Local provider final-current recovery requires a disconnected "
                    "transport"
                )
            if set(publications) != set(self.topics):
                _contract_error(
                    "Local provider retained final-current set is incomplete"
                )
            for _payload, qos, retained in publications.values():
                if (
                    type(qos) is not int
                    or qos != 1
                    or type(retained) is not bool
                    or (require_retained and not retained)
                ):
                    _contract_error(
                        "Local provider final-current requires exact MQTT QoS 1"
                    )

            now = _utc_now(self._now)
            snapshot, session_id, sequence, shadow_value = _parse_state(
                publications[self.state_topic][0], self.binding_id, now
            )
            availability, device_status, availability_session, device_at = (
                _parse_availability(publications[self.availability_topic][0], now)
            )
            runtime, runtime_status, service_instance_id, runtime_at = (
                _parse_runtime_availability(
                    publications[self.runtime_availability_topic][0], now
                )
            )
            if availability_session != session_id:
                _contract_error(
                    "Local provider final-current availability session does not match"
                )

            state_canonical = _canonical_payload(snapshot)
            availability_canonical = _canonical_payload(availability)
            runtime_canonical = _canonical_payload(runtime)
            session_changed = self._session_id not in (None, session_id)
            service_changed = self._service_instance_id not in (
                None,
                service_instance_id,
            )

            if session_id in self._tombstoned_sessions:
                _contract_error("Local provider final-current session was superseded")
            if not session_changed and self._session_id is not None:
                if sequence < self._sequence:
                    _contract_error("Local provider final-current sequence regressed")
                if (
                    sequence == self._sequence
                    and state_canonical != self._state_payload
                ):
                    _contract_error("Local provider final-current cursor collided")
                if (
                    self._device_availability_at is not None
                    and device_at < self._device_availability_at
                ):
                    _contract_error(
                        "Local provider final-current device availability regressed"
                    )
            if service_instance_id in self._tombstoned_service_instances:
                _contract_error(
                    "Local provider final-current service instance was superseded"
                )
            runtime_exact_replay = runtime_canonical == self._runtime_payload
            runtime_lwt_regression = False
            if (
                not service_changed
                and self._service_instance_id is not None
                and self._runtime_availability_at is not None
                and runtime_at < self._runtime_availability_at
                and not runtime_exact_replay
            ):
                # MQTT fixes the LWT payload before CONNECT. Its offline
                # observed_at therefore predates the online publication even
                # though the broker delivers it later on a crash. Accept only
                # that fail-closed online -> offline edge and retain the prior
                # timestamp as the ordering high-water.
                runtime_lwt_regression = (
                    self._runtime_status == "online" and runtime_status == "offline"
                )
                if not runtime_lwt_regression:
                    _contract_error(
                        "Local provider final-current runtime availability regressed"
                    )
            if (
                session_changed
                and len(self._tombstoned_sessions) >= MAX_TOMBSTONED_GENERATIONS
            ):
                _contract_error("Local provider session tombstone bound is exhausted")
            if (
                service_changed
                and len(self._tombstoned_service_instances)
                >= MAX_TOMBSTONED_GENERATIONS
            ):
                _contract_error("Local provider service tombstone bound is exhausted")

            changed = (
                session_changed
                or service_changed
                or state_canonical != self._state_payload
                or availability_canonical != self._device_availability_payload
                or runtime_canonical != self._runtime_payload
            )
            if session_changed and self._session_id is not None:
                self._tombstoned_sessions.add(self._session_id)
            if service_changed and self._service_instance_id is not None:
                self._tombstoned_service_instances.add(self._service_instance_id)

            self._session_id = session_id
            self._sequence = sequence
            self._state_payload = state_canonical
            self._shadow_value = shadow_value
            self._device_status = device_status
            self._device_availability_payload = availability_canonical
            self._device_availability_at = device_at
            self._service_instance_id = service_instance_id
            self._runtime_status = runtime_status
            self._runtime_payload = runtime_canonical
            if (
                service_changed
                or self._runtime_availability_at is None
                or runtime_at >= self._runtime_availability_at
            ):
                self._runtime_availability_at = runtime_at
            return changed
        except LocalProviderContractError:
            self._rejected_messages += 1
            raise

    def _ingest_state(self, payload: object, now: datetime) -> bool:
        snapshot, session_id, sequence, value = _parse_state(
            payload, self.binding_id, now
        )
        canonical = _canonical_payload(snapshot)
        if session_id in self._tombstoned_sessions:
            _contract_error("Local provider session was superseded")
        if self._session_id is not None and session_id == self._session_id:
            if sequence < self._sequence:
                _contract_error("Local provider sequence regressed")
            if sequence == self._sequence:
                if canonical == self._state_payload:
                    return False
                _contract_error("Local provider cursor collided")
        elif self._session_id is not None:
            if self._device_status != "offline":
                _contract_error("Local provider session rotation requires offline")
            if len(self._tombstoned_sessions) >= MAX_TOMBSTONED_GENERATIONS:
                _contract_error("Local provider session tombstone bound is exhausted")

        if session_id != self._session_id:
            if self._session_id is not None:
                self._tombstoned_sessions.add(self._session_id)
            self._device_status = "unknown"
            self._device_availability_payload = None
            self._device_availability_at = None
        self._session_id = session_id
        self._sequence = sequence
        self._state_payload = canonical
        self._shadow_value = value
        return True

    def _ingest_device_availability(self, payload: object, now: datetime) -> bool:
        value, status, session_id, observed_at = _parse_availability(payload, now)
        if self._session_id is None or session_id != self._session_id:
            _contract_error("Local provider availability session does not match")
        canonical = _canonical_payload(value)
        if (
            self._device_availability_at is not None
            and observed_at < self._device_availability_at
        ):
            _contract_error("Local provider device availability regressed")
        if status == self._device_status:
            if canonical == self._device_availability_payload:
                return False
            _contract_error("Local provider duplicate device availability changed")
        self._device_status = status
        self._device_availability_payload = canonical
        self._device_availability_at = observed_at
        return True

    def _ingest_runtime_availability(self, payload: object, now: datetime) -> bool:
        value, status, service_instance_id, observed_at = _parse_runtime_availability(
            payload, now
        )
        canonical = _canonical_payload(value)
        if service_instance_id in self._tombstoned_service_instances:
            _contract_error("Local provider service instance was superseded")
        runtime_lwt_regression = False
        if self._service_instance_id is not None:
            if service_instance_id != self._service_instance_id:
                if self._runtime_status != "offline":
                    _contract_error(
                        "Local provider service rotation requires runtime offline"
                    )
                if (
                    len(self._tombstoned_service_instances)
                    >= MAX_TOMBSTONED_GENERATIONS
                ):
                    _contract_error(
                        "Local provider service tombstone bound is exhausted"
                    )
            else:
                if canonical == self._runtime_payload:
                    return False
                if (
                    self._runtime_availability_at is not None
                    and observed_at < self._runtime_availability_at
                ):
                    # See retained final-current handling above. A same-service
                    # offline edge is the only allowed timestamp regression.
                    runtime_lwt_regression = (
                        self._runtime_status == "online" and status == "offline"
                    )
                    if not runtime_lwt_regression:
                        _contract_error("Local provider runtime availability regressed")
                if status == self._runtime_status:
                    if canonical == self._runtime_payload:
                        return False
                    _contract_error(
                        "Local provider duplicate runtime availability changed"
                    )

        if (
            service_instance_id != self._service_instance_id
            and self._service_instance_id is not None
        ):
            self._tombstoned_service_instances.add(self._service_instance_id)
        self._service_instance_id = service_instance_id
        self._runtime_status = status
        self._runtime_payload = canonical
        if not runtime_lwt_regression:
            self._runtime_availability_at = observed_at
        return True


def parse_wideq_water_tank_value(value: object) -> bool | None:
    """Parse only the exact WideQ 0/1 domain; never guess unknown values."""
    if type(value) is bool:
        return None
    if value in (0, 0.0, "0", "0.0"):
        return False
    if value in (1, 1.0, "1", "1.0"):
        return True
    return None


class WaterTankProviderResolver:
    """Resolve the existing entity while Local remains observational only."""

    def __init__(
        self, local_provider: LocalWaterTankShadowProvider | None = None
    ) -> None:
        self.local_provider = local_provider
        self.mode = (
            LOCAL_PROVIDER_MODE_SHADOW
            if local_provider is not None
            else LOCAL_PROVIDER_MODE_DISABLED
        )
        self.invalid_wideq_values = 0

    def available(self, wideq_device_available: bool) -> bool:
        """Preserve the existing WideQ availability owner in shadow mode."""
        return bool(wideq_device_available)

    def resolve(self, wideq_snapshot: object) -> bool | None:
        """Preserve WideQ as operational owner while Local is shadow-only."""
        if not isinstance(wideq_snapshot, Mapping):
            self._note_invalid_wideq_value()
            return None
        if WIDEQ_WATER_TANK_KEY not in wideq_snapshot:
            return None
        value = parse_wideq_water_tank_value(wideq_snapshot[WIDEQ_WATER_TANK_KEY])
        if value is None:
            self._note_invalid_wideq_value()
        return value

    def _note_invalid_wideq_value(self) -> None:
        """Record unknown input without logging its potentially sensitive value."""
        self.invalid_wideq_values += 1
        count = self.invalid_wideq_values
        if count == 1 or count % 100 == 0:
            _LOGGER.warning(
                "WideQ water-tank provider returned an unsupported value; "
                "state left unavailable (count=%d)",
                count,
            )
