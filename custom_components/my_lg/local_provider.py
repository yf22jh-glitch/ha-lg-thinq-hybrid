"""Strict read-only provider for the Rethink Local pilot state feed.

This module deliberately has no Home Assistant or MQTT dependency.  It owns the
subscriber-side contract and cursor fencing, while transport and entity routing
remain independently testable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

_LOGGER = logging.getLogger(__name__)

LOCAL_PROVIDER_MODE_DISABLED = "disabled"
LOCAL_PROVIDER_MODE_SHADOW = "shadow"

OPT_LOCAL_PROVIDER_MODE = "local_provider_mode"
OPT_LOCAL_PAT_DEVICE_ID = "local_pat_device_id"
OPT_LOCAL_BINDING_ID = "local_binding_id"
OPT_LOCAL_MQTT_PASSWORD = "local_mqtt_password"
OPT_LOCAL_BINDINGS = "local_bindings"

LOCAL_BINDING_SCHEMA_VERSION = 1
LOCAL_DHUM_WATER_TANK_PROFILE_ID = "dhum-water-tank-v1"

LOCAL_PILOT_PREFIX = "lg_rethink_pilot/v1"
LOCAL_WATER_TANK_FIELD = "water_tank.full"
WIDEQ_WATER_TANK_KEY = "airState.miscFuncState.watertankLight"

LOCAL_PROFILE_CATALOGUE_FILENAME = "pilot-profiles.v1.json"
LOCAL_PROFILE_CATALOGUE_DIGEST_FILENAME = "pilot-profiles.v1.sha256"

# The shadow runtime places a tighter boundary around the 64 KiB general
# decoder schema.  Keep the same 8 KiB subscriber limit here.
MAX_PAYLOAD_BYTES = 8 * 1024
MAX_FUTURE_SKEW = timedelta(minutes=5)
MAX_TOMBSTONED_GENERATIONS = 10_000
MAX_JSON_SAFE_INTEGER = 9_007_199_254_740_991

_BINDING_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{15,127}$")
_OPAQUE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")
_SEMANTIC_ID = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*$")
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
_FIELD_REQUIRED_KEYS = frozenset(
    {"value", "value_type", "observed_at", "confidence", "exposure"}
)
_FIELD_ALLOWED_KEYS = _FIELD_REQUIRED_KEYS | {"unit"}
_DIAGNOSTIC_KEYS = frozenset(
    {"rejected_frames", "unresolved_fields", "invalid_values", "unsupported_frames"}
)
_AVAILABILITY_KEYS = frozenset({"status", "session_id", "observed_at"})
_RUNTIME_AVAILABILITY_KEYS = frozenset({"status", "service_instance_id", "observed_at"})

_CATALOGUE_KEYS = frozenset({"schema_version", "semantics_revision", "profiles"})
_CATALOGUE_PROFILE_KEYS = frozenset(
    {
        "profile_id",
        "contract_revision",
        "supported_semantics_revisions",
        "model_id",
        "platform",
        "fields",
    }
)
_CATALOGUE_FIELD_REQUIRED_KEYS = frozenset(
    {"semantic_id", "value_type", "exposure", "confidence"}
)
_CATALOGUE_FIELD_ALLOWED_KEYS = _CATALOGUE_FIELD_REQUIRED_KEYS | {"unit"}
_CATALOGUE_DIGEST = re.compile(
    rb"^([0-9a-f]{64})  local/semantic/pilot-profiles\.v1\.json\n$"
)
MAX_PROFILE_CATALOGUE_BYTES = 256 * 1024


class LocalProviderContractError(ValueError):
    """An MQTT publication failed the pinned Local provider contract."""


class LocalProviderConfigurationError(ValueError):
    """Local shadow options are incomplete or outside the pilot boundary."""


@dataclass(frozen=True)
class LocalSemanticFieldContract:
    """One exact retained-field contract authorized for a Local profile."""

    value_type: Literal["boolean", "number", "string"]
    exposure: Literal["state", "diagnostic"]
    confidence: tuple[str, ...]
    unit: str | None = None

    def __post_init__(self) -> None:
        if self.value_type not in ("boolean", "number", "string"):
            raise ValueError("Local semantic field value type is invalid")
        if self.exposure not in ("state", "diagnostic"):
            raise ValueError("Local semantic field exposure is invalid")
        if not self.confidence or any(
            not isinstance(item, str) or not item or len(item) > 128
            for item in self.confidence
        ):
            raise ValueError("Local semantic field confidence is invalid")
        if self.unit is not None and (
            not isinstance(self.unit, str) or not self.unit or len(self.unit) > 16
        ):
            raise ValueError("Local semantic field unit is invalid")


@dataclass(frozen=True)
class LocalSemanticProfile:
    """Pinned model/platform/revision and exact semantic-field allowlist."""

    profile_id: str
    model_id: str
    platform: Literal["thinq1", "thinq2"]
    semantics_revision: int
    fields: Mapping[str, LocalSemanticFieldContract]
    contract_revision: int = 1
    supported_semantics_revisions: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not _OPAQUE_ID.fullmatch(self.profile_id):
            raise ValueError("Local semantic profile id is invalid")
        if not _OPAQUE_ID.fullmatch(self.model_id):
            raise ValueError("Local semantic model id is invalid")
        if self.platform not in ("thinq1", "thinq2"):
            raise ValueError("Local semantic platform is invalid")
        if (
            type(self.semantics_revision) is not int
            or self.semantics_revision < 1
            or self.semantics_revision > MAX_JSON_SAFE_INTEGER
        ):
            raise ValueError("Local semantic revision is invalid")
        if (
            type(self.contract_revision) is not int
            or self.contract_revision < 1
            or self.contract_revision > MAX_JSON_SAFE_INTEGER
        ):
            raise ValueError("Local semantic contract revision is invalid")
        supported = self.supported_semantics_revisions or (self.semantics_revision,)
        if (
            not isinstance(supported, tuple)
            or not supported
            or len(supported) > 32
            or len(set(supported)) != len(supported)
            or any(
                type(revision) is not int
                or revision < 1
                or revision > MAX_JSON_SAFE_INTEGER
                for revision in supported
            )
            or self.semantics_revision not in supported
        ):
            raise ValueError("Local semantic supported revisions are invalid")
        owned = dict(self.fields)
        if not owned or len(owned) > 256:
            raise ValueError("Local semantic profile fields are invalid")
        for semantic_id, contract in owned.items():
            if (
                not isinstance(semantic_id, str)
                or len(semantic_id) > 128
                or not _SEMANTIC_ID.fullmatch(semantic_id)
                or not isinstance(contract, LocalSemanticFieldContract)
            ):
                raise ValueError("Local semantic profile field is invalid")
        object.__setattr__(self, "fields", MappingProxyType(owned))
        object.__setattr__(self, "supported_semantics_revisions", supported)


@dataclass(frozen=True)
class LocalSemanticShadowField:
    """One validated Local value plus its non-secret evidence metadata."""

    value: bool | float | int | str
    value_type: Literal["boolean", "number", "string"]
    observed_at: datetime
    confidence: str
    exposure: Literal["state", "diagnostic"]
    unit: str | None = None


def _catalogue_error() -> None:
    raise RuntimeError("Bundled Rethink Local profile catalogue is invalid")


def _catalogue_object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _catalogue_error()
        result[key] = value
    return result


def _catalogue_integer(value: object) -> int:
    if type(value) is not int or value < 1 or value > MAX_JSON_SAFE_INTEGER:
        _catalogue_error()
    return value


def _load_local_semantic_profile_catalogue(
    catalogue_bytes: bytes,
    digest_bytes: bytes,
) -> tuple[int, Mapping[str, LocalSemanticProfile], str]:
    """Validate one exact generated catalogue and its sha256 sidecar."""
    if (
        not isinstance(catalogue_bytes, bytes)
        or not catalogue_bytes
        or len(catalogue_bytes) > MAX_PROFILE_CATALOGUE_BYTES
    ):
        _catalogue_error()
    digest_match = _CATALOGUE_DIGEST.fullmatch(digest_bytes)
    if digest_match is None:
        _catalogue_error()
    actual_digest = hashlib.sha256(catalogue_bytes).hexdigest()
    if digest_match.group(1).decode("ascii") != actual_digest:
        _catalogue_error()
    try:
        catalogue = json.loads(
            catalogue_bytes.decode("utf-8"),
            object_pairs_hook=_catalogue_object_without_duplicate_keys,
            parse_constant=lambda _value: _catalogue_error(),
        )
    except RuntimeError:
        raise
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        _catalogue_error()
    if not isinstance(catalogue, dict) or set(catalogue) != _CATALOGUE_KEYS:
        _catalogue_error()
    if catalogue["schema_version"] != 1 or type(catalogue["schema_version"]) is not int:
        _catalogue_error()
    semantics_revision = _catalogue_integer(catalogue["semantics_revision"])
    raw_profiles = catalogue["profiles"]
    if not isinstance(raw_profiles, list) or not raw_profiles or len(raw_profiles) > 64:
        _catalogue_error()

    profiles: dict[str, LocalSemanticProfile] = {}
    for raw_profile in raw_profiles:
        if (
            not isinstance(raw_profile, dict)
            or set(raw_profile) != _CATALOGUE_PROFILE_KEYS
        ):
            _catalogue_error()
        profile_id = raw_profile["profile_id"]
        model_id = raw_profile["model_id"]
        platform = raw_profile["platform"]
        if (
            not isinstance(profile_id, str)
            or not _OPAQUE_ID.fullmatch(profile_id)
            or profile_id in profiles
            or not isinstance(model_id, str)
            or not _OPAQUE_ID.fullmatch(model_id)
            or platform not in ("thinq1", "thinq2")
        ):
            _catalogue_error()
        contract_revision = _catalogue_integer(raw_profile["contract_revision"])
        raw_supported = raw_profile["supported_semantics_revisions"]
        if (
            not isinstance(raw_supported, list)
            or not raw_supported
            or len(raw_supported) > 32
        ):
            _catalogue_error()
        supported = tuple(_catalogue_integer(value) for value in raw_supported)
        if len(set(supported)) != len(supported) or semantics_revision not in supported:
            _catalogue_error()

        raw_fields = raw_profile["fields"]
        if not isinstance(raw_fields, list) or not raw_fields or len(raw_fields) > 256:
            _catalogue_error()
        fields: dict[str, LocalSemanticFieldContract] = {}
        for raw_field in raw_fields:
            if (
                not isinstance(raw_field, dict)
                or not _CATALOGUE_FIELD_REQUIRED_KEYS.issubset(raw_field)
                or not set(raw_field).issubset(_CATALOGUE_FIELD_ALLOWED_KEYS)
            ):
                _catalogue_error()
            semantic_id = raw_field["semantic_id"]
            confidence = raw_field["confidence"]
            unit = raw_field.get("unit")
            if (
                not isinstance(semantic_id, str)
                or not _SEMANTIC_ID.fullmatch(semantic_id)
                or semantic_id in fields
                or raw_field["value_type"] not in ("boolean", "number", "string")
                or raw_field["exposure"] != "state"
                or not isinstance(confidence, list)
                or not confidence
                or len(confidence) > 32
                or len(set(confidence)) != len(confidence)
                or any(not isinstance(item, str) for item in confidence)
                or (unit is not None and not isinstance(unit, str))
            ):
                _catalogue_error()
            try:
                fields[semantic_id] = LocalSemanticFieldContract(
                    value_type=raw_field["value_type"],
                    exposure=raw_field["exposure"],
                    confidence=tuple(confidence),
                    unit=unit,
                )
            except ValueError:
                _catalogue_error()
        try:
            profiles[profile_id] = LocalSemanticProfile(
                profile_id=profile_id,
                model_id=model_id,
                platform=platform,
                semantics_revision=semantics_revision,
                fields=fields,
                contract_revision=contract_revision,
                supported_semantics_revisions=supported,
            )
        except ValueError:
            _catalogue_error()
    return semantics_revision, MappingProxyType(profiles), actual_digest


def _load_bundled_local_semantic_profiles() -> tuple[
    int, Mapping[str, LocalSemanticProfile], str
]:
    directory = Path(__file__).resolve().parent
    try:
        return _load_local_semantic_profile_catalogue(
            (directory / LOCAL_PROFILE_CATALOGUE_FILENAME).read_bytes(),
            (directory / LOCAL_PROFILE_CATALOGUE_DIGEST_FILENAME).read_bytes(),
        )
    except OSError as err:
        raise RuntimeError(
            "Bundled Rethink Local profile catalogue is unavailable"
        ) from err


_PROFILE_CATALOGUE_LOCK = threading.Lock()
_PROFILE_CATALOGUE_CACHE: tuple[int, Mapping[str, LocalSemanticProfile], str] | None = (
    None
)


def _validate_compatibility_profile(
    profiles: Mapping[str, LocalSemanticProfile],
) -> None:
    """Fail closed unless the legacy one-field DHUM contract remains exact."""
    try:
        profile = profiles[LOCAL_DHUM_WATER_TANK_PROFILE_ID]
        contract = profile.fields[LOCAL_WATER_TANK_FIELD]
    except KeyError as err:
        raise RuntimeError(
            "Bundled Rethink Local profile catalogue lacks the compatibility profile"
        ) from err
    if (
        profile.model_id != "DHUM_056905_WW"
        or profile.platform != "thinq2"
        or contract.value_type != "boolean"
        or contract.exposure != "state"
        or contract.unit is not None
    ):
        _catalogue_error()


def load_local_semantic_profile_catalogue() -> tuple[
    int, Mapping[str, LocalSemanticProfile], str
]:
    """Load and validate the optional Local catalogue once, thread-safely.

    Importing ``my_lg`` must never depend on the optional Local artifact. Home
    Assistant callers that may hit the filesystem run this function through
    ``hass.async_add_executor_job``; successful results are immutable and
    process-cached. Failures are deliberately not cached so a repaired artifact
    can be adopted by a later config-entry reload.
    """
    global _PROFILE_CATALOGUE_CACHE

    cached = _PROFILE_CATALOGUE_CACHE
    if cached is not None:
        return cached
    with _PROFILE_CATALOGUE_LOCK:
        cached = _PROFILE_CATALOGUE_CACHE
        if cached is not None:
            return cached
        try:
            loaded = _load_bundled_local_semantic_profiles()
            _validate_compatibility_profile(loaded[1])
        except RuntimeError as err:
            raise LocalProviderConfigurationError(
                "Local profile catalogue is unavailable or invalid"
            ) from err
        _PROFILE_CATALOGUE_CACHE = loaded
        return loaded


def _local_semantic_profile(profile_id: str) -> LocalSemanticProfile:
    profiles = load_local_semantic_profile_catalogue()[1]
    try:
        return profiles[profile_id]
    except KeyError as err:
        raise LocalProviderConfigurationError(
            "Local provider profile is unsupported"
        ) from err


@dataclass(frozen=True)
class LocalShadowConfiguration:
    """Validated configuration for one exact read-only Local binding."""

    pat_device_id: str
    binding_id: str
    mqtt_username: str
    mqtt_password: str
    profile_id: str
    model_id: str
    platform: Literal["thinq1", "thinq2"]
    _profile: LocalSemanticProfile

    @property
    def profile(self) -> LocalSemanticProfile:
        return self._profile


_LOCAL_BINDING_KEYS = frozenset(
    {
        "schema_version",
        "mode",
        "profile_id",
        "model_id",
        "platform",
        "pat_device_id",
        "binding_id",
        "mqtt_password",
    }
)
_LEGACY_LOCAL_OPTION_KEYS = frozenset(
    {
        OPT_LOCAL_PROVIDER_MODE,
        OPT_LOCAL_PAT_DEVICE_ID,
        OPT_LOCAL_BINDING_ID,
        OPT_LOCAL_MQTT_PASSWORD,
    }
)


def _configuration_error(message: str) -> None:
    raise LocalProviderConfigurationError(message)


def _valid_password(value: object) -> bool:
    return isinstance(value, str) and bool(value) and len(value.encode("utf-8")) <= 1024


def _configuration_from_values(
    *,
    pat_device_id: object,
    binding_id: object,
    mqtt_password: object,
    profile_id: object,
    model_id: object,
    platform: object,
) -> LocalShadowConfiguration:
    if not isinstance(pat_device_id, str) or not _OPAQUE_ID.fullmatch(pat_device_id):
        _configuration_error("Local provider PAT device id is invalid")
    try:
        validated_binding_id = validate_binding_id(binding_id)
    except LocalProviderContractError as err:
        raise LocalProviderConfigurationError(str(err)) from err
    if not _valid_password(mqtt_password):
        _configuration_error("Local provider MQTT password is invalid")
    if not isinstance(profile_id, str):
        _configuration_error("Local provider profile is unsupported")
    profile = _local_semantic_profile(profile_id)
    if model_id != profile.model_id or platform != profile.platform:
        _configuration_error("Local provider profile model or platform does not match")
    return LocalShadowConfiguration(
        pat_device_id=pat_device_id,
        binding_id=validated_binding_id,
        mqtt_username=f"shadow-{validated_binding_id}",
        mqtt_password=mqtt_password,
        profile_id=profile.profile_id,
        model_id=profile.model_id,
        platform=profile.platform,
        _profile=profile,
    )


def _legacy_local_shadow_configuration(
    options: Mapping[str, object],
) -> LocalShadowConfiguration | None:
    mode = options.get(OPT_LOCAL_PROVIDER_MODE, LOCAL_PROVIDER_MODE_DISABLED)
    if mode == LOCAL_PROVIDER_MODE_DISABLED:
        return None
    if mode != LOCAL_PROVIDER_MODE_SHADOW:
        _configuration_error(
            "Local provider mode is unsupported during the shadow pilot"
        )
    profile = _local_semantic_profile(LOCAL_DHUM_WATER_TANK_PROFILE_ID)
    return _configuration_from_values(
        pat_device_id=options.get(OPT_LOCAL_PAT_DEVICE_ID),
        binding_id=options.get(OPT_LOCAL_BINDING_ID),
        mqtt_password=options.get(OPT_LOCAL_MQTT_PASSWORD),
        profile_id=LOCAL_DHUM_WATER_TANK_PROFILE_ID,
        model_id=profile.model_id,
        platform=profile.platform,
    )


def local_shadow_configuration(
    options: Mapping[str, object],
) -> LocalShadowConfiguration | None:
    """Validate the legacy one-DHUM option shape during migration."""
    return _legacy_local_shadow_configuration(options)


def _decode_binding_list(value: object) -> list[object]:
    if isinstance(value, str):
        if not value.strip():
            return []
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as err:
            raise LocalProviderConfigurationError(
                "Local provider bindings JSON is invalid"
            ) from err
    if not isinstance(value, list) or len(value) > 64:
        _configuration_error("Local provider bindings must be a bounded list")
    return list(value)


def _configuration_from_binding(value: object) -> LocalShadowConfiguration:
    if not isinstance(value, dict) or set(value) != _LOCAL_BINDING_KEYS:
        _configuration_error("Local provider binding keys are invalid")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != LOCAL_BINDING_SCHEMA_VERSION
    ):
        _configuration_error("Local provider binding schema is unsupported")
    if value["mode"] != LOCAL_PROVIDER_MODE_SHADOW:
        _configuration_error("Local provider binding mode is unsupported")
    return _configuration_from_values(
        pat_device_id=value["pat_device_id"],
        binding_id=value["binding_id"],
        mqtt_password=value["mqtt_password"],
        profile_id=value["profile_id"],
        model_id=value["model_id"],
        platform=value["platform"],
    )


def local_shadow_configurations(
    options: Mapping[str, object],
) -> tuple[LocalShadowConfiguration, ...]:
    """Return all exact bindings, accepting the legacy one-DHUM shape."""
    if OPT_LOCAL_BINDINGS not in options:
        legacy = _legacy_local_shadow_configuration(options)
        return () if legacy is None else (legacy,)
    if any(
        options.get(key) not in (None, "", LOCAL_PROVIDER_MODE_DISABLED)
        for key in _LEGACY_LOCAL_OPTION_KEYS
    ):
        _configuration_error("Local provider legacy and versioned options conflict")
    configs = tuple(
        _configuration_from_binding(item)
        for item in _decode_binding_list(options[OPT_LOCAL_BINDINGS])
    )
    pat_ids = [config.pat_device_id for config in configs]
    binding_ids = [config.binding_id for config in configs]
    if len(set(pat_ids)) != len(pat_ids):
        _configuration_error("Local provider PAT device bindings must be one-to-one")
    if len(set(binding_ids)) != len(binding_ids):
        _configuration_error("Local provider binding ids must be unique")
    return configs


def _configuration_dict(
    config: LocalShadowConfiguration, *, mask_password: bool = False
) -> dict[str, object]:
    return {
        "schema_version": LOCAL_BINDING_SCHEMA_VERSION,
        "mode": LOCAL_PROVIDER_MODE_SHADOW,
        "profile_id": config.profile_id,
        "model_id": config.model_id,
        "platform": config.platform,
        "pat_device_id": config.pat_device_id,
        "binding_id": config.binding_id,
        "mqtt_password": "" if mask_password else config.mqtt_password,
    }


def migrate_local_shadow_options(options: Mapping[str, object]) -> dict[str, object]:
    """Normalize legacy/current options into the versioned JSON-safe list."""
    configs = local_shadow_configurations(options)
    result = dict(options)
    for key in _LEGACY_LOCAL_OPTION_KEYS:
        result.pop(key, None)
    result[OPT_LOCAL_BINDINGS] = [_configuration_dict(config) for config in configs]
    return result


def local_bindings_for_form(options: Mapping[str, object]) -> str:
    """Render editable JSON without ever redisplaying stored MQTT passwords."""
    configs = local_shadow_configurations(options)
    return json.dumps(
        [_configuration_dict(config, mask_password=True) for config in configs],
        ensure_ascii=False,
        indent=2,
    )


def merge_local_shadow_options(
    submitted: Mapping[str, object], existing: Mapping[str, object]
) -> dict[str, object]:
    """Normalize OptionsFlow input while retaining only matching masked secrets."""
    result = dict(submitted)
    try:
        existing_configs = local_shadow_configurations(existing)
    except LocalProviderConfigurationError:
        # An invalid stored value must be repairable through OptionsFlow, but
        # none of its unvalidated secrets are eligible for implicit reuse.
        existing_configs = ()
        existing_is_valid = False
    else:
        existing_is_valid = True
    existing_passwords = {
        config.binding_id: config.mqtt_password for config in existing_configs
    }

    if OPT_LOCAL_BINDINGS in result:
        bindings = _decode_binding_list(result[OPT_LOCAL_BINDINGS])
        owned: list[object] = []
        for item in bindings:
            if not isinstance(item, dict):
                _configuration_error("Local provider binding is invalid")
            candidate = dict(item)
            if candidate.get("mqtt_password") in (None, ""):
                binding_id = candidate.get("binding_id")
                password = existing_passwords.get(binding_id)
                if password is not None:
                    candidate["mqtt_password"] = password
            owned.append(candidate)
        result[OPT_LOCAL_BINDINGS] = owned
    else:
        mode = result.get(OPT_LOCAL_PROVIDER_MODE, LOCAL_PROVIDER_MODE_DISABLED)
        if mode == LOCAL_PROVIDER_MODE_SHADOW and result.get(
            OPT_LOCAL_MQTT_PASSWORD
        ) in (None, ""):
            binding_id = result.get(OPT_LOCAL_BINDING_ID)
            password = existing_passwords.get(binding_id)
            if password is None and len(existing_configs) == 1:
                password = existing_configs[0].mqtt_password
            if (
                password is None
                and existing_is_valid
                and _valid_password(existing.get(OPT_LOCAL_MQTT_PASSWORD))
            ):
                password = existing[OPT_LOCAL_MQTT_PASSWORD]
            if password is not None:
                result[OPT_LOCAL_MQTT_PASSWORD] = password

    return migrate_local_shadow_options(result)


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


def _field_object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _contract_error(f"{name} must be an object")
    keys = set(value)
    if not _FIELD_REQUIRED_KEYS.issubset(keys) or not keys.issubset(
        _FIELD_ALLOWED_KEYS
    ):
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
    profile: LocalSemanticProfile,
    now: datetime,
) -> tuple[
    dict[str, Any],
    str,
    int,
    Mapping[str, LocalSemanticShadowField],
]:
    snapshot = _exact_object(_decode_payload(payload), _SNAPSHOT_KEYS, "snapshot")
    if type(snapshot["schema_version"]) is not int or snapshot["schema_version"] != 1:
        _contract_error("Local provider snapshot schema is unsupported")
    if (
        type(snapshot["semantics_revision"]) is not int
        or snapshot["semantics_revision"] != profile.semantics_revision
    ):
        _contract_error("Local provider semantics revision is unsupported")
    if snapshot["binding_id"] != expected_binding_id:
        _contract_error("Local provider binding does not match")
    if (
        snapshot["model_id"] != profile.model_id
        or snapshot["platform"] != profile.platform
    ):
        _contract_error("Local provider model or platform does not match")

    session_id = snapshot["session_id"]
    if not isinstance(session_id, str) or not _OPAQUE_ID.fullmatch(session_id):
        _contract_error("Local provider session id is invalid")
    sequence = snapshot["sequence"]
    if type(sequence) is not int or sequence < 1 or sequence > MAX_JSON_SAFE_INTEGER:
        _contract_error("Local provider sequence is invalid")
    published_at = _timestamp(snapshot["published_at"], "published_at", now)

    fields = snapshot["fields"]
    if not isinstance(fields, dict) or not fields or len(fields) > 256:
        _contract_error("Local provider snapshot fields are invalid")
    shadow_fields: dict[str, LocalSemanticShadowField] = {}
    for semantic_id, raw_field in fields.items():
        contract = profile.fields.get(semantic_id)
        if contract is None:
            _contract_error("Local provider semantic field is not authorized")
        field = _field_object(raw_field, f"semantic field {semantic_id}")
        value_type = field["value_type"]
        value = field["value"]
        if value_type != contract.value_type:
            _contract_error("Local provider semantic field type is unsupported")
        if (
            (value_type == "boolean" and type(value) is not bool)
            or (
                value_type == "number"
                and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                )
            )
            or (
                value_type == "string"
                and (not isinstance(value, str) or len(value) > 128)
            )
        ):
            _contract_error("Local provider semantic field value is invalid")
        if field["confidence"] not in contract.confidence:
            _contract_error("Local provider semantic field confidence is unsupported")
        if field["exposure"] != contract.exposure:
            _contract_error("Local provider semantic field exposure is unsupported")
        if field.get("unit") != contract.unit:
            _contract_error("Local provider semantic field unit is unsupported")
        observed_at = _timestamp(
            field["observed_at"], f"semantic field {semantic_id} observed_at", now
        )
        if observed_at > published_at:
            _contract_error("Local provider semantic observation is after publication")
        shadow_fields[semantic_id] = LocalSemanticShadowField(
            value=value,
            value_type=value_type,
            observed_at=observed_at,
            confidence=field["confidence"],
            exposure=field["exposure"],
            unit=field.get("unit"),
        )

    diagnostics = _exact_object(
        snapshot["diagnostics"], _DIAGNOSTIC_KEYS, "snapshot diagnostics"
    )
    for name, value in diagnostics.items():
        _safe_nonnegative_integer(value, f"snapshot diagnostics {name}")

    return snapshot, session_id, sequence, MappingProxyType(shadow_fields)


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


class LocalSemanticShadowProvider:
    """Consume one exact-profile Local feed without owning an HA entity."""

    mode = LOCAL_PROVIDER_MODE_SHADOW

    def __init__(
        self,
        binding_id: str,
        profile: LocalSemanticProfile,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.binding_id = validate_binding_id(binding_id)
        if not isinstance(profile, LocalSemanticProfile):
            raise TypeError("Local provider profile is invalid")
        self.profile = profile
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
        self._shadow_fields: Mapping[str, LocalSemanticShadowField] = MappingProxyType(
            {}
        )
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
    def profile_id(self) -> str:
        return self.profile.profile_id

    @property
    def model_id(self) -> str:
        return self.profile.model_id

    @property
    def platform(self) -> Literal["thinq1", "thinq2"]:
        return self.profile.platform

    @property
    def shadow_fields(self) -> Mapping[str, LocalSemanticShadowField]:
        """Return an immutable view of the last fully validated field set."""
        return self._shadow_fields

    def field_value(self, semantic_id: str) -> bool | float | int | str | None:
        """Return one diagnostic shadow value; never an operational owner."""
        field = self._shadow_fields.get(semantic_id)
        return None if field is None else field.value

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
            and bool(self._shadow_fields)
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
            snapshot, session_id, sequence, shadow_fields = _parse_state(
                publications[self.state_topic][0], self.binding_id, self.profile, now
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
            self._shadow_fields = shadow_fields
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
        snapshot, session_id, sequence, fields = _parse_state(
            payload, self.binding_id, self.profile, now
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
        self._shadow_fields = fields
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


class LocalWaterTankShadowProvider(LocalSemanticShadowProvider):
    """Compatibility facade for the existing one-DHUM water-tank resolver."""

    def __init__(
        self,
        binding_id: str,
        *,
        profile: LocalSemanticProfile | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        exact_profile = _local_semantic_profile(LOCAL_DHUM_WATER_TANK_PROFILE_ID)
        if profile is not None and profile != exact_profile:
            raise LocalProviderConfigurationError(
                "Local water-tank provider profile does not match"
            )
        super().__init__(binding_id, exact_profile, now=now)

    @property
    def shadow_value(self) -> bool | None:
        value = self.field_value(LOCAL_WATER_TANK_FIELD)
        return value if type(value) is bool else None


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
        self, local_provider: LocalSemanticShadowProvider | None = None
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
