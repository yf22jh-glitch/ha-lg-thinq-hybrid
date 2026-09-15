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

LOCAL_PILOT_PREFIX = "lg_rethink_local/v1"
LOCAL_WATER_TANK_FIELD = "water_tank.full"
WIDEQ_WATER_TANK_KEY = "airState.waterTank.full"

LOCAL_PROFILE_CATALOGUE_FILENAME = "pilot-profiles.v1.json"
LOCAL_PROFILE_CATALOGUE_DIGEST_FILENAME = "pilot-profiles.v1.sha256"

# The shadow runtime places a tighter boundary around the 64 KiB general
# decoder schema.  Keep the same 8 KiB subscriber limit here.
MAX_PAYLOAD_BYTES = 8 * 1024
MAX_FUTURE_SKEW = timedelta(minutes=5)
CONTROL_PRESENCE_LIVE_TTL = timedelta(seconds=240)
_CONTROL_PRESENCE_EXPIRY_EPSILON_SECONDS = 0.001
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

_SNAPSHOT_REQUIRED_KEYS = frozenset(
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
# Tombstones retracting a retained value; only profiles that declare
# `authoritative_invalidations` may send them.
_SNAPSHOT_OPTIONAL_KEYS = frozenset({"invalidated_fields"})
# An identity-bound snapshot names the appliance it came from and proves it.
_SNAPSHOT_IDENTITY_KEYS = frozenset({"binding_generation", "pat_device_id_proof_sha256"})
_SNAPSHOT_COHORT_KEYS = frozenset({"cohort_generation"})
_SNAPSHOT_KEYS_BY_SCHEMA = {
    1: _SNAPSHOT_REQUIRED_KEYS,
    2: _SNAPSHOT_REQUIRED_KEYS | _SNAPSHOT_IDENTITY_KEYS,
    3: _SNAPSHOT_REQUIRED_KEYS | _SNAPSHOT_IDENTITY_KEYS | _SNAPSHOT_COHORT_KEYS,
}
_INVALIDATION_KEYS = frozenset({"observed_at", "confidence"})
_PROOF_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FIELD_REQUIRED_KEYS = frozenset(
    {"value", "value_type", "observed_at", "confidence", "exposure"}
)
_FIELD_ALLOWED_KEYS = _FIELD_REQUIRED_KEYS | {"unit"}
_DIAGNOSTIC_KEYS = frozenset(
    {"rejected_frames", "unresolved_fields", "invalid_values", "unsupported_frames"}
)
_AVAILABILITY_REQUIRED_KEYS = frozenset({"status", "session_id", "observed_at"})
_AVAILABILITY_KEYS_BY_SCHEMA = {
    1: _AVAILABILITY_REQUIRED_KEYS,
    2: _AVAILABILITY_REQUIRED_KEYS | _SNAPSHOT_IDENTITY_KEYS | {"state_sequence"},
    3: _AVAILABILITY_REQUIRED_KEYS
    | _SNAPSHOT_IDENTITY_KEYS
    | {"state_sequence", "schema_version", "cohort_generation"},
}
_RUNTIME_AVAILABILITY_KEYS = frozenset({"status", "service_instance_id", "observed_at"})
_CONTROL_PRESENCE_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "evidence",
        "binding_generation",
        "sequence",
        "pat_device_id_proof_sha256",
        "profile_id",
        "service_instance_id",
        "observed_at",
        "valid_until",
    }
)

_CATALOGUE_KEYS = frozenset({"schema_version", "semantics_revision", "profiles"})
_CATALOGUE_PROFILE_REQUIRED_KEYS = frozenset(
    {
        "profile_id",
        "contract_revision",
        "supported_semantics_revisions",
        "model_id",
        "platform",
        "fields",
    }
)
# `availability_policy` governs how the publisher decides a device is online and
# needs nothing from us; `authoritative_invalidations` does, because only those
# profiles may send the tombstones that retract a retained value.
_CATALOGUE_PROFILE_ALLOWED_KEYS = _CATALOGUE_PROFILE_REQUIRED_KEYS | {
    "availability_policy",
    "authoritative_invalidations",
    # A measured cadence pinned as a freshness SLA. A rejected key aborts the
    # entire catalogue, not one profile, so every binding in Home Assistant would
    # die at setup rather than the one profile that changed.
    "freshness_max_age_ms",
}
# How the publisher decides a device is online. `device-report` judges it by the
# appliance's own periodic report rather than by the age of its state, for
# appliances that can be alive and unchanged for days.
_AVAILABILITY_POLICY_ATTESTED_SESSION = "attested-session"
_AVAILABILITY_POLICY_DEVICE_REPORT = "device-report"
_AVAILABILITY_POLICIES = frozenset(
    {
        _AVAILABILITY_POLICY_ATTESTED_SESSION,
        _AVAILABILITY_POLICY_DEVICE_REPORT,
    }
)
_CATALOGUE_FIELD_REQUIRED_KEYS = frozenset(
    {"semantic_id", "value_type", "exposure", "confidence"}
)
_CATALOGUE_FIELD_ALLOWED_KEYS = _CATALOGUE_FIELD_REQUIRED_KEYS | {"unit", "allowed_values"}
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
    allowed_values: tuple[object, ...] | None = None

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
        if self.allowed_values is not None and (
            not self.allowed_values
            or len(self.allowed_values) > 64
            or len(set(map(repr, self.allowed_values))) != len(self.allowed_values)
        ):
            raise ValueError("Local semantic field allowed values are invalid")


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
    availability_policy: str | None = None
    authoritative_invalidations: bool = False
    freshness_max_age_ms: int | None = None

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


@dataclass(frozen=True)
class _ControlPresencePublication:
    """One validated state-independent appliance liveness publication."""

    canonical: str
    status: Literal["online", "offline"]
    binding_generation: int
    sequence: int
    service_instance_id: str
    observed_at: datetime
    valid_until: datetime | None


@dataclass(frozen=True)
class _RuntimeFinalCurrentCandidate:
    canonical: str
    status: Literal["online", "offline"]
    service_instance_id: str
    observed_at: datetime
    service_changed: bool
    lwt_regression: bool
    changed: bool


@dataclass(frozen=True)
class _SemanticFinalCurrentCandidate:
    session_id: str
    sequence: int
    state_canonical: str
    state_published_at: datetime
    state_availability_coordinate: tuple[int, int | None, int] | None
    shadow_fields: Mapping[str, LocalSemanticShadowField]
    device_status: str
    availability_canonical: str
    device_availability_at: datetime
    device_availability_coordinate: tuple[int, int | None, int] | None
    binding_generation: int | None
    cohort_generation: int | None
    cohort_advanced: bool
    session_changed: bool
    legacy_session_changed: bool
    changed: bool


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
            or not _CATALOGUE_PROFILE_REQUIRED_KEYS.issubset(raw_profile)
            or not set(raw_profile).issubset(_CATALOGUE_PROFILE_ALLOWED_KEYS)
        ):
            _catalogue_error()
        availability_policy = raw_profile.get("availability_policy")
        authoritative_invalidations = raw_profile.get("authoritative_invalidations")
        freshness_max_age_ms = raw_profile.get("freshness_max_age_ms")
        if (availability_policy is not None and availability_policy not in _AVAILABILITY_POLICIES) or (
            authoritative_invalidations is not None and authoritative_invalidations is not True
        ):
            _catalogue_error()
        if freshness_max_age_ms is not None and (
            type(freshness_max_age_ms) is not int
            or freshness_max_age_ms < 1
            or freshness_max_age_ms > MAX_JSON_SAFE_INTEGER
        ):
            _catalogue_error()
        # Mirror the publisher's two rules rather than pairing the keys. A window
        # with no policy is the field's original purpose - a measured cadence pinned
        # as a freshness SLA - and refusing it would drop every Local binding in the
        # house the first time someone pins one, including bindings that had nothing
        # to do with the profile that changed.
        if freshness_max_age_ms is not None and availability_policy not in (
            None,
            _AVAILABILITY_POLICY_DEVICE_REPORT,
        ):
            _catalogue_error()
        if availability_policy == _AVAILABILITY_POLICY_DEVICE_REPORT and freshness_max_age_ms is None:
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
            allowed_values = raw_field.get("allowed_values")
            if allowed_values is not None and (
                not isinstance(allowed_values, list)
                or not allowed_values
                or len(allowed_values) > 64
                or any(
                    not isinstance(item, (str, int, float, bool)) or isinstance(item, bool) is not (raw_field["value_type"] == "boolean")
                    for item in allowed_values
                )
            ):
                _catalogue_error()
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
                    allowed_values=None if allowed_values is None else tuple(allowed_values),
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
                availability_policy=availability_policy,
                authoritative_invalidations=authoritative_invalidations is True,
                freshness_max_age_ms=freshness_max_age_ms,
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
    require_identity: bool = False

    @property
    def profile(self) -> LocalSemanticProfile:
        return self._profile


_LOCAL_BINDING_REQUIRED_KEYS = frozenset(
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
# Whether this binding's publisher has been migrated to an identity-bound
# manifest. It is opt-in and flipped in the same window as the manifests: a
# publisher still on the legacy contract emits no proof at all, and demanding one
# from it would reject every message instead of merely leaving it unverified.
_LOCAL_BINDING_ALLOWED_KEYS = _LOCAL_BINDING_REQUIRED_KEYS | {"require_identity"}
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
    require_identity: bool = False,
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
        require_identity=require_identity,
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
    if (
        not isinstance(value, dict)
        or not _LOCAL_BINDING_REQUIRED_KEYS.issubset(value)
        or not set(value).issubset(_LOCAL_BINDING_ALLOWED_KEYS)
    ):
        _configuration_error("Local provider binding keys are invalid")
    require_identity = value.get("require_identity", False)
    if require_identity is not True and require_identity is not False:
        _configuration_error("Local provider binding identity requirement is invalid")
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
        require_identity=require_identity,
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
    value: dict[str, object] = {
        "schema_version": LOCAL_BINDING_SCHEMA_VERSION,
        "mode": LOCAL_PROVIDER_MODE_SHADOW,
        "profile_id": config.profile_id,
        "model_id": config.model_id,
        "platform": config.platform,
        "pat_device_id": config.pat_device_id,
        "binding_id": config.binding_id,
        "mqtt_password": "" if mask_password else config.mqtt_password,
    }
    if config.require_identity:
        value["require_identity"] = True
    return value


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


def _parsed_timestamp(value: object, name: str) -> datetime:
    """Parse the publisher's timestamp grammar without imposing a time role."""
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
    return parsed.astimezone(timezone.utc)


def _timestamp(value: object, name: str, now: datetime) -> datetime:
    parsed = _parsed_timestamp(value, name)
    if parsed > now + MAX_FUTURE_SKEW:
        _contract_error(f"{name} is too far in the future")
    return parsed


def _safe_nonnegative_integer(value: object, name: str) -> int:
    if (
        type(value) is not int or value < 0 or value > MAX_JSON_SAFE_INTEGER
    ):  # bool is deliberately not an int here
        _contract_error(f"{name} is invalid")
    return value


_PAT_IDENTITY_PROOF_DOMAIN = b"lg-rethink-pilot/pat-device-identity-proof/v1\0"


def local_pat_device_identity_proof(
    binding_id: str, model_id: str, platform: str, pat_device_id: str
) -> str:
    """Recompute the publisher's domain-separated PAT identity proof.

    This is the point of an identity-bound publication: without it a binding is
    trusted purely because it published on the topic we happen to subscribe to,
    and a mis-paired appliance would merge its state into another appliance's
    entities silently. Verified byte-for-byte against the publisher's own
    proofs for every live binding.
    """

    digest = hashlib.sha256()
    digest.update(_PAT_IDENTITY_PROOF_DOMAIN)
    for name, value in (
        ("binding_id", binding_id),
        ("model_id", model_id),
        ("platform", platform),
        ("pat_device_id", pat_device_id.lower()),
    ):
        encoded = value.encode("utf-8")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(encoded)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(encoded)
        digest.update(b"\0")
    return digest.hexdigest()


def _identity_fields(
    value: Mapping[str, Any], name: str, expected_proof: str | None
) -> None:
    """Validate the identity a schema 2/3 publication carries."""

    generation = value["binding_generation"]
    if type(generation) is not int or generation < 1 or generation > MAX_JSON_SAFE_INTEGER:
        _contract_error(f"Local provider {name} binding generation is invalid")
    proof = value["pat_device_id_proof_sha256"]
    if not isinstance(proof, str) or not _PROOF_SHA256.fullmatch(proof):
        _contract_error(f"Local provider {name} identity proof is invalid")
    if expected_proof is not None and proof != expected_proof:
        _contract_error(f"Local provider {name} identity proof does not match this binding")
    cohort = value.get("cohort_generation")
    if cohort is not None and (
        type(cohort) is not int or cohort < 1 or cohort > MAX_JSON_SAFE_INTEGER
    ):
        _contract_error(f"Local provider {name} cohort generation is invalid")


def _parse_state(
    payload: object,
    expected_binding_id: str,
    profile: LocalSemanticProfile,
    now: datetime,
    expected_proof: str | None = None,
    require_identity: bool = False,
) -> tuple[
    dict[str, Any],
    str,
    int,
    Mapping[str, LocalSemanticShadowField],
    datetime,
]:
    decoded = _decode_payload(payload)
    if not isinstance(decoded, dict):
        _contract_error("snapshot keys are invalid")
    schema_version = decoded.get("schema_version")
    if type(schema_version) is not int or schema_version not in _SNAPSHOT_KEYS_BY_SCHEMA:
        _contract_error("Local provider snapshot schema is unsupported")
    allowed = _SNAPSHOT_KEYS_BY_SCHEMA[schema_version]
    if not allowed.issubset(decoded) or not set(decoded).issubset(
        allowed | _SNAPSHOT_OPTIONAL_KEYS
    ):
        _contract_error("snapshot keys are invalid")
    snapshot = decoded
    if schema_version != 1:
        _identity_fields(snapshot, "snapshot", expected_proof)
    elif require_identity:
        # Once a binding's publisher is known to be identity-bound, a publication
        # without a proof is a downgrade and must not be taken. Before that
        # migration the publisher genuinely has no proof to send, so demanding one
        # would reject every message rather than leave it unverified.
        _contract_error("Local provider snapshot is not identity-bound")
    # A profile declares the revisions its contract holds across, and the publisher
    # pins any one of them. Comparing against the catalogue's single current
    # revision instead rejects a publisher that is a revision behind - including
    # the publisher we just rolled back to, which is exactly when this side has to
    # keep working.
    if (
        type(snapshot["semantics_revision"]) is not int
        or snapshot["semantics_revision"] not in profile.supported_semantics_revisions
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
    raw_invalidated = snapshot.get("invalidated_fields")
    if not isinstance(fields, dict) or (
        not fields and not isinstance(raw_invalidated, dict)
    ):
        # The publisher allows either half to be empty, never both: a snapshot that
        # retracts every field carries `fields: {}` and the tombstones, and refusing
        # it leaves the retracted values on display forever.
        _contract_error("Local provider snapshot fields are invalid")
    if len(fields) + (len(raw_invalidated) if isinstance(raw_invalidated, dict) else 0) > 256:
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
        if contract.allowed_values is not None and value not in contract.allowed_values:
            _contract_error("Local provider semantic field value is outside its allowlist")
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

    invalidated = raw_invalidated
    if invalidated is not None:
        if not isinstance(invalidated, dict) or not invalidated:
            # The publisher refuses to emit an empty map, so accepting one here
            # would accept something it never sends.
            _contract_error("Local provider snapshot invalidations are invalid")
        if not profile.authoritative_invalidations:
            _contract_error("Local provider field invalidations are not authorized by this profile")
        for semantic_id, raw_invalidation in invalidated.items():
            contract = profile.fields.get(semantic_id)
            if contract is None:
                _contract_error("Local provider semantic invalidation is not authorized")
            if semantic_id in shadow_fields:
                _contract_error("Local provider retracted a field it also published")
            invalidation = _exact_object(
                raw_invalidation, _INVALIDATION_KEYS, f"semantic invalidation {semantic_id}"
            )
            if invalidation["confidence"] not in contract.confidence:
                _contract_error("Local provider semantic invalidation confidence is unsupported")
            observed_at = _timestamp(
                invalidation["observed_at"],
                f"semantic invalidation {semantic_id} observed_at",
                now,
            )
            if observed_at > published_at:
                _contract_error("Local provider semantic invalidation is after publication")

    diagnostics = _exact_object(
        snapshot["diagnostics"], _DIAGNOSTIC_KEYS, "snapshot diagnostics"
    )
    for name, value in diagnostics.items():
        _safe_nonnegative_integer(value, f"snapshot diagnostics {name}")

    return (
        snapshot,
        session_id,
        sequence,
        MappingProxyType(shadow_fields),
        published_at,
    )


def _parse_availability(
    payload: object,
    now: datetime,
    expected_proof: str | None = None,
    require_identity: bool = False,
) -> tuple[dict[str, Any], str, str, datetime]:
    decoded = _decode_payload(payload)
    if not isinstance(decoded, dict):
        _contract_error("device availability keys are invalid")
    schema_version = decoded.get("schema_version")
    if schema_version is None:
        schema_version = 2 if "pat_device_id_proof_sha256" in decoded else 1
    if type(schema_version) is not int or schema_version not in _AVAILABILITY_KEYS_BY_SCHEMA:
        _contract_error("Local provider availability schema is unsupported")
    value = _exact_object(
        decoded, _AVAILABILITY_KEYS_BY_SCHEMA[schema_version], "device availability"
    )
    if schema_version != 1:
        _identity_fields(value, "availability", expected_proof)
        state_sequence = value["state_sequence"]
        if (
            type(state_sequence) is not int
            or state_sequence < 1
            or state_sequence > MAX_JSON_SAFE_INTEGER
        ):
            _contract_error("Local provider availability state sequence is invalid")
    elif require_identity:
        _contract_error("Local provider availability is not identity-bound")
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


def _parse_control_presence(
    payload: object,
    now: datetime,
    profile: LocalSemanticProfile,
    expected_proof: str | None,
) -> _ControlPresencePublication:
    """Validate authenticated device liveness without consulting state coordinates."""
    value = _exact_object(
        _decode_payload(payload),
        _CONTROL_PRESENCE_KEYS,
        "control presence",
    )
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        _contract_error("Local provider control presence schema is unsupported")
    _identity_fields(value, "control presence", expected_proof)
    sequence = value["sequence"]
    if (
        type(sequence) is not int
        or sequence < 1
        or sequence > MAX_JSON_SAFE_INTEGER
    ):
        _contract_error("Local provider control presence sequence is invalid")
    if value["profile_id"] != profile.profile_id:
        _contract_error("Local provider control presence profile does not match")

    expected_evidence = profile.availability_policy
    if (
        expected_evidence not in _AVAILABILITY_POLICIES
        or value["evidence"] != expected_evidence
    ):
        _contract_error("Local provider control presence evidence does not match")
    status = value["status"]
    if status not in ("online", "offline"):
        _contract_error("Local provider control presence status is invalid")
    service_instance_id = value["service_instance_id"]
    if not isinstance(service_instance_id, str) or not _SERVICE_INSTANCE_ID.fullmatch(
        service_instance_id
    ):
        _contract_error("Local provider control presence service instance is invalid")
    observed_at = _timestamp(
        value["observed_at"], "control presence observed_at", now
    )

    raw_valid_until = value["valid_until"]
    valid_until: datetime | None
    if expected_evidence == _AVAILABILITY_POLICY_ATTESTED_SESSION:
        if profile.platform != "thinq2" or raw_valid_until is not None:
            _contract_error("Local provider attested-session validity is invalid")
        valid_until = None
    else:
        if (
            profile.platform != "thinq1"
            or profile.freshness_max_age_ms is None
            or not isinstance(raw_valid_until, str)
        ):
            _contract_error("Local provider device-report validity is invalid")
        # A lease boundary is intentionally in the future while an appliance is online, so it
        # uses the same exact timestamp grammar without the publication-time future-skew fence.
        valid_until = _parsed_timestamp(
            raw_valid_until, "control presence valid_until"
        )
        freshness = timedelta(milliseconds=profile.freshness_max_age_ms)
        if status == "online":
            if valid_until != observed_at + freshness:
                _contract_error("Local provider device-report lease is invalid")
        elif valid_until > observed_at:
            # Startup offline uses observed_at itself; expiry/offline may retain the earlier lease
            # boundary, but an offline marker can never promise future liveness.
            _contract_error("Local provider offline device-report lease is invalid")

    return _ControlPresencePublication(
        canonical=_canonical_payload(value),
        status=status,
        binding_generation=value["binding_generation"],
        sequence=sequence,
        service_instance_id=service_instance_id,
        observed_at=observed_at,
        valid_until=valid_until,
    )


class LocalSemanticShadowProvider:
    """Consume one exact-profile Local feed without owning an HA entity."""

    mode = LOCAL_PROVIDER_MODE_SHADOW

    def __init__(
        self,
        binding_id: str,
        profile: LocalSemanticProfile,
        *,
        pat_device_id: str | None = None,
        require_identity: bool = False,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.binding_id = validate_binding_id(binding_id)
        if not isinstance(profile, LocalSemanticProfile):
            raise TypeError("Local provider profile is invalid")
        self.profile = profile
        # With the PAT identity in hand we can recompute the proof the publisher
        # signs its snapshots with, which is what turns "published on our topic"
        # into "came from this exact appliance".
        self.expected_proof = (
            None
            if pat_device_id is None
            else local_pat_device_identity_proof(
                self.binding_id, profile.model_id, profile.platform, pat_device_id
            )
        )
        if require_identity and self.expected_proof is None:
            raise LocalProviderConfigurationError(
                "Local provider cannot require an identity it has no PAT device id to check"
            )
        self.require_identity = require_identity
        # The proof covers the appliance, not which generation of the binding
        # published. A retained snapshot from a superseded generation therefore
        # carries a byte-identical, valid proof; only refusing to move backwards
        # makes the field mean anything.
        self._binding_generation: int | None = None
        # Wire schema 3 orders an independent state cursor by cohort before
        # session and sequence.  Keep its scalar high-water even when a retained
        # delete clears the current snapshot; otherwise that delete could revive
        # an already superseded cohort with a reset sequence.
        self._cohort_generation: int | None = None
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.state_topic = f"{LOCAL_PILOT_PREFIX}/state/{self.binding_id}"
        self.availability_topic = f"{LOCAL_PILOT_PREFIX}/availability/{self.binding_id}"
        self.runtime_availability_topic = (
            f"{LOCAL_PILOT_PREFIX}/runtime/{self.binding_id}/availability"
        )
        self.presence_topic = f"{LOCAL_PILOT_PREFIX}/presence/{self.binding_id}"
        self._presence_enabled = self.profile.availability_policy in _AVAILABILITY_POLICIES
        semantic_topics = (
            self.state_topic,
            self.availability_topic,
            self.runtime_availability_topic,
        )
        # The legacy water-tank compatibility feed predates an authenticated liveness policy and
        # remains a three-topic read-only shadow. Every production control profile declares one of
        # the two evidence policies and therefore bootstraps the exact fourth presence topic.
        self.topics = semantic_topics + (
            (self.presence_topic,) if self._presence_enabled else ()
        )

        self._transport_ready = False
        self._session_id: str | None = None
        self._sequence = 0
        self._state_payload: str | None = None
        # MQTT delivers state and its exact completion marker separately. Keep
        # an uncommitted candidate out of the observable shadow until both pass
        # the existing final-current validator; never invent a new observation.
        self._pending_live_state: (
            tuple[bytes, tuple[int, int | None, int], str] | None
        ) = None
        self._state_published_at: datetime | None = None
        self._state_availability_coordinate: tuple[int, int | None, int] | None = (
            None
        )
        self._shadow_fields: Mapping[str, LocalSemanticShadowField] = MappingProxyType(
            {}
        )
        self._device_status = "unknown"
        self._device_availability_payload: str | None = None
        self._device_availability_at: datetime | None = None
        self._device_availability_coordinate: tuple[int, int | None, int] | None = (
            None
        )
        self._tombstoned_sessions: set[str] = set()

        self._service_instance_id: str | None = None
        self._runtime_status = "unknown"
        self._runtime_payload: str | None = None
        self._runtime_availability_at: datetime | None = None
        self._tombstoned_service_instances: set[str] = set()

        self._presence_binding_generation: int | None = None
        self._presence_sequence = 0
        self._presence_status = "unknown"
        self._presence_payload: str | None = None
        self._presence_service_instance_id: str | None = None
        self._presence_observed_at: datetime | None = None
        self._presence_valid_until: datetime | None = None
        self._presence_live_received_at: datetime | None = None
        self._read_authority_expiry_notified: (
            tuple[tuple[int, str], datetime] | None
        ) = None
        self._tombstoned_presence_service_instances: set[str] = set()
        # A transport reconnect invalidates operational use of the in-memory semantic tuple until
        # that connection supplies an exact state+availability pair. Cursor high-waters remain so
        # the same retained pair can be safely re-adopted without opening replay.
        self._control_state_current = False
        self._semantic_transport_current = False
        self._rejected_messages = 0
        self._listeners: list[Callable[[], None]] = []

    def _binding_generation_candidate(
        self, snapshot: Mapping[str, Any]
    ) -> int | None:
        """Validate, but do not commit, one snapshot generation."""
        generation = snapshot.get("binding_generation")
        if generation is None:
            return None
        if (
            self._binding_generation is not None
            and generation < self._binding_generation
        ):
            _contract_error("Local provider binding generation moved backwards")
        return generation

    def _cohort_advanced_candidate(
        self,
        snapshot: Mapping[str, Any],
        session_id: str,
        binding_generation: int | None,
    ) -> bool:
        """Validate schema 3's generation/cohort cursor without mutating it."""
        cohort_generation = snapshot.get("cohort_generation")
        if cohort_generation is None:
            return False
        if (
            binding_generation is not None
            and self._binding_generation is not None
            and binding_generation > self._binding_generation
        ):
            # A binding generation is an outer, separately reset identity
            # contract. Its existing session/final-current fences decide whether
            # the transition is allowed; only then may its cohort high-water
            # start again.
            return False
        if self._cohort_generation is None:
            # The first V3 snapshot still enters through the legacy
            # session/sequence fences. A cohort becomes an independent cursor
            # only after this provider has accepted a V3 cohort high-water;
            # otherwise a V2 -> V3 shape change could smuggle in a reset.
            return False
        if cohort_generation < self._cohort_generation:
            _contract_error("Local provider cohort generation regressed")
        if cohort_generation > self._cohort_generation:
            return True
        if self._session_id is None:
            _contract_error("Local provider cohort was superseded")
        if session_id != self._session_id:
            _contract_error(
                "Local provider cohort cursor collides with a different session"
            )
        return False

    @staticmethod
    def _snapshot_availability_coordinate(
        snapshot: Mapping[str, Any], sequence: int
    ) -> tuple[int, int | None, int] | None:
        generation = snapshot.get("binding_generation")
        if generation is None:
            return None
        return generation, snapshot.get("cohort_generation"), sequence

    @staticmethod
    def _availability_coordinate(
        availability: Mapping[str, Any],
    ) -> tuple[int, int | None, int] | None:
        generation = availability.get("binding_generation")
        if generation is None:
            return None
        return (
            generation,
            availability.get("cohort_generation"),
            availability["state_sequence"],
        )

    @classmethod
    def _assert_availability_describes_snapshot(
        cls,
        availability: Mapping[str, Any],
        snapshot: Mapping[str, Any],
        sequence: int,
    ) -> tuple[int, int | None, int] | None:
        state_coordinate = cls._snapshot_availability_coordinate(snapshot, sequence)
        availability_coordinate = cls._availability_coordinate(availability)
        if availability_coordinate != state_coordinate:
            _contract_error(
                "Local provider availability does not describe this snapshot"
            )
        return availability_coordinate

    def _availability_describes_current_state(self) -> bool:
        # Schema 1 has no state coordinate, so its session-bound marker remains
        # compatible as None == None only while its observation is causally at
        # or after the current state publication. Schema 2/3 additionally match
        # every identity coordinate.
        return (
            self._session_id is not None
            and self._device_availability_payload is not None
            and self._device_availability_coordinate
            == self._state_availability_coordinate
            and self._state_published_at is not None
            and self._device_availability_at is not None
            and self._device_availability_at >= self._state_published_at
        )

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def binding_generation(self) -> int | None:
        """Return the accepted V2/V3 binding generation for read cross-fencing."""
        return self._binding_generation

    @property
    def cohort_generation(self) -> int | None:
        """Return the accepted V3 publication cohort for read cross-fencing."""
        return self._cohort_generation

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

    def async_add_listener(
        self, update_callback: Callable[[], None]
    ) -> Callable[[], None]:
        """Subscribe to committed state, availability, and transport changes."""
        if not callable(update_callback):
            raise TypeError("Local provider update listener must be callable")
        self._listeners.append(update_callback)

        def remove_listener() -> None:
            try:
                self._listeners.remove(update_callback)
            except ValueError:
                pass

        return remove_listener

    def _notify_listeners(self) -> None:
        for update_callback in tuple(self._listeners):
            try:
                update_callback()
            except Exception:  # noqa: BLE001 - one HA entity must not block the feed
                _LOGGER.exception("Rethink Local provider update listener failed")

    def _finish_update(self, changed: bool) -> bool:
        if changed:
            self._notify_listeners()
        return changed

    def field_value(self, semantic_id: str) -> bool | float | int | str | None:
        """Return one validated Local value; the consumer owns routing policy."""
        field = self._shadow_fields.get(semantic_id)
        return None if field is None else field.value

    def semantic_field_fresh_until(self, semantic_id: str) -> datetime | None:
        """Return this profile's exact freshness deadline for one retained field."""
        field = self._shadow_fields.get(semantic_id)
        max_age_ms = self.profile.freshness_max_age_ms
        if field is None or max_age_ms is None:
            return None
        return field.observed_at + timedelta(milliseconds=max_age_ms)

    def semantic_field_fresh(self, semantic_id: str) -> bool:
        """Return whether one present field remains inside the profile freshness SLA."""
        if semantic_id not in self._shadow_fields:
            return False
        fresh_until = self.semantic_field_fresh_until(semantic_id)
        return fresh_until is None or _utc_now(self._now) <= fresh_until

    def semantic_field_available(self, semantic_id: str) -> bool:
        """Return HA-safe availability for one exact semantic field."""
        return self.shadow_healthy and self.semantic_field_fresh(semantic_id)

    @property
    def rejected_messages(self) -> int:
        return self._rejected_messages

    @property
    def transport_ready(self) -> bool:
        return self._transport_ready

    @property
    def control_presence_enabled(self) -> bool:
        """Whether this profile owns the independent authenticated presence topic."""
        return self._presence_enabled

    @property
    def shadow_healthy(self) -> bool:
        """Return whether all three read-only feed fences currently agree."""
        return (
            self._transport_ready
            and self._semantic_transport_current
            and bool(self._shadow_fields)
            and self._session_id is not None
            and self._device_status == "online"
            and self._availability_describes_current_state()
            and self._runtime_status == "online"
        )

    @property
    def read_publication_authority(self) -> tuple[int, str] | None:
        """Return the authenticated live service identity for read publications.

        Full-read publication does not require a pilot semantic snapshot.  Its
        authority is the independently authenticated presence generation and
        runtime service instance; the read feed owns its cohort/source cursor.
        """
        if (
            not self.control_alive
            or self._presence_binding_generation is None
            or self._presence_service_instance_id is None
        ):
            return None
        return (
            self._presence_binding_generation,
            self._presence_service_instance_id,
        )

    @property
    def read_publication_authority_expiry(
        self,
    ) -> tuple[tuple[int, str], datetime] | None:
        """Return the current live-presence epoch and its inclusive deadline."""
        if (
            not self._presence_enabled
            or self.expected_proof is None
            or not self._transport_ready
            or self._presence_status != "online"
            or self._runtime_status != "online"
            or self._presence_binding_generation is None
            or self._presence_service_instance_id is None
            or self._presence_service_instance_id != self._service_instance_id
            or self._presence_live_received_at is None
        ):
            return None
        deadline = self._presence_live_received_at + CONTROL_PRESENCE_LIVE_TTL
        if self._presence_valid_until is not None:
            deadline = min(deadline, self._presence_valid_until)
        return (
            (
                self._presence_binding_generation,
                self._presence_service_instance_id,
            ),
            deadline,
        )

    def read_publication_authority_expiry_delay(
        self, expected: tuple[tuple[int, str], datetime]
    ) -> float | None:
        """Return the event-loop delay for one still-current expiry epoch."""
        if (
            self.read_publication_authority_expiry != expected
            or self._read_authority_expiry_notified == expected
        ):
            return None
        return max(
            0.0,
            (expected[1] - _utc_now(self._now)).total_seconds()
            + _CONTROL_PRESENCE_EXPIRY_EPSILON_SECONDS,
        )

    def expire_read_publication_authority(
        self, expected: tuple[tuple[int, str], datetime]
    ) -> bool:
        """Notify listeners once when an unchanged presence epoch expires."""
        if (
            self.read_publication_authority_expiry != expected
            or _utc_now(self._now) <= expected[1]
            or self._read_authority_expiry_notified == expected
        ):
            return False
        self._read_authority_expiry_notified = expected
        self._notify_listeners()
        return True

    @property
    def control_alive(self) -> bool:
        """Return whether an authenticated appliance presence can receive a command.

        This intentionally says nothing about semantic state. Composite commands still inspect
        their exact fields and observation times, while an exact stateless command may proceed
        before the appliance has emitted any state at all.
        """
        expiry = self.read_publication_authority_expiry
        return expiry is not None and _utc_now(self._now) <= expiry[1]

    @property
    def control_state_ready(self) -> bool:
        """Whether composite control state belongs to the live binding generation."""
        return (
            self._presence_enabled
            and self._semantic_transport_current
            and self._control_state_current
            and self._presence_binding_generation is not None
            and self._binding_generation == self._presence_binding_generation
            and self._state_payload is not None
            and self._state_published_at is not None
            and self._presence_observed_at is not None
            and self._state_published_at >= self._presence_observed_at
            and bool(self._shadow_fields)
        )

    def control_fields_ready(self, semantic_ids: tuple[str, ...]) -> bool:
        """Return whether every composite input was observed in this presence epoch.

        A reducer publication can be new while carrying forward fields last observed before the
        appliance reconnected.  Publication time alone therefore cannot authorize a composite
        write: each field the encoder will restate must have appliance evidence at or after the
        authenticated presence edge.
        """
        presence_floor = self._presence_observed_at
        if (
            not semantic_ids
            or not self.control_alive
            or not self.control_state_ready
            or presence_floor is None
        ):
            return False
        return all(
            (field := self._shadow_fields.get(semantic_id)) is not None
            and field.observed_at >= presence_floor
            for semantic_id in semantic_ids
        )

    def set_transport_ready(self, ready: bool) -> None:
        if type(ready) is not bool:
            raise TypeError("Local provider transport readiness must be boolean")
        before = (
            self._transport_ready,
            self._control_state_current,
            self._semantic_transport_current,
            self._presence_live_received_at,
        )
        self._transport_ready = ready
        if not ready:
            self._pending_live_state = None
            self._control_state_current = False
            self._semantic_transport_current = False
            self._presence_live_received_at = None
        after = (
            self._transport_ready,
            self._control_state_current,
            self._semantic_transport_current,
            self._presence_live_received_at,
        )
        if after != before:
            self._notify_listeners()

    def _validate_presence_candidate(
        self, presence: _ControlPresencePublication
    ) -> tuple[bool, bool, bool]:
        """Return changed/generation-advanced/service-changed without mutating state."""
        generation_advanced = (
            self._presence_binding_generation is not None
            and presence.binding_generation > self._presence_binding_generation
        )
        if (
            self._presence_binding_generation is not None
            and presence.binding_generation < self._presence_binding_generation
        ):
            _contract_error("Local provider control presence generation regressed")
        if (
            not generation_advanced
            and presence.service_instance_id
            in self._tombstoned_presence_service_instances
        ):
            _contract_error("Local provider control presence service was superseded")

        service_changed = (
            self._presence_service_instance_id is not None
            and presence.service_instance_id != self._presence_service_instance_id
        )
        if (
            not generation_advanced
            and not service_changed
            and self._presence_service_instance_id is not None
        ):
            if presence.sequence < self._presence_sequence:
                _contract_error("Local provider control presence sequence regressed")
            if presence.sequence == self._presence_sequence:
                if presence.canonical == self._presence_payload:
                    return False, False, False
                _contract_error("Local provider control presence cursor collided")
        if (
            service_changed
            and len(self._tombstoned_presence_service_instances)
            >= MAX_TOMBSTONED_GENERATIONS
        ):
            _contract_error("Local provider presence service tombstone bound is exhausted")
        return True, generation_advanced, service_changed

    def _apply_presence_candidate(
        self,
        presence: _ControlPresencePublication,
        *,
        generation_advanced: bool,
        service_changed: bool,
    ) -> None:
        evidence_edge = (
            generation_advanced
            or service_changed
            or self._presence_sequence != presence.sequence
            or self._presence_status != presence.status
            or self._presence_observed_at != presence.observed_at
        )
        if service_changed and self._presence_service_instance_id is not None:
            self._tombstoned_presence_service_instances.add(
                self._presence_service_instance_id
            )
        if generation_advanced:
            # Binding generation is the outer identity cursor; it may deliberately reuse the
            # publisher process id, so its accepted service cannot remain tombstoned.
            self._tombstoned_presence_service_instances.discard(
                presence.service_instance_id
            )
        self._presence_binding_generation = presence.binding_generation
        self._presence_sequence = presence.sequence
        self._presence_status = presence.status
        self._presence_payload = presence.canonical
        self._presence_service_instance_id = presence.service_instance_id
        self._presence_observed_at = presence.observed_at
        self._presence_valid_until = presence.valid_until
        # A process heartbeat republishes the exact same physical evidence. It refreshes the
        # live-delivery TTL separately and must not erase a current tuple. Any validated cursor
        # advance, changed status/service/generation, or evidence timestamp is a real edge and
        # closes composite state even if two physical events share a millisecond timestamp.
        if evidence_edge:
            self._control_state_current = False

    def _apply_presence_delivery(self, now: datetime, retained: bool) -> bool:
        """Record proof that this MQTT connection saw a live publisher delivery."""
        received_at = None if retained else now
        changed = received_at != self._presence_live_received_at
        self._presence_live_received_at = received_at
        return changed

    def _ingest_control_presence(
        self, payload: object, now: datetime, retained: bool
    ) -> bool:
        presence = _parse_control_presence(
            payload, now, self.profile, self.expected_proof
        )
        changed, generation_advanced, service_changed = (
            self._validate_presence_candidate(presence)
        )
        runtime_handover_changed = self._apply_live_presence_runtime_handover(
            presence, retained=retained
        )
        if changed:
            self._apply_presence_candidate(
                presence,
                generation_advanced=generation_advanced,
                service_changed=service_changed,
            )
        return (
            self._apply_presence_delivery(now, retained)
            or changed
            or runtime_handover_changed
        )

    def _ingest_presence_retained_delete(self) -> bool:
        # Preserve the cursor high-water and service id so a delayed retained replay cannot revive
        # the deleted online marker. A genuinely new marker for the same service needs a higher
        # sequence and is still allowed.
        changed = (
            self._presence_status != "offline"
            or self._presence_payload is not None
            or self._presence_valid_until is not None
        )
        self._presence_status = "offline"
        self._presence_payload = None
        self._presence_valid_until = None
        self._presence_live_received_at = None
        self._control_state_current = False
        return changed

    def ingest_live_semantic_publication(
        self, topic: str, payload: object, *, qos: int, retained: bool
    ) -> bool:
        """Commit V3 live state+availability atomically, using existing checks.

        The last *completed* pair remains visible, with its original field
        timestamps and liveness fences. Pending data cannot grant new state or
        refresh freshness. Offline, tombstone and transport events are immediate.
        Bootstrap and the legacy per-message ingest API keep their old contract.
        """
        if topic not in (
            self.state_topic, self.availability_topic
        ) or self._is_retained_delete(payload):
            changed = self.ingest(topic, payload, qos=qos, retained=retained)
            if self._is_retained_delete(payload) or not self.shadow_healthy:
                self._pending_live_state = None
            return changed
        try:
            if type(qos) is not int or qos != 1 or type(retained) is not bool:
                _contract_error(
                    "Local provider live publication transport flags are invalid"
                )
            now = _utc_now(self._now)
            if topic == self.state_topic:
                snapshot, session, sequence, _, _ = _parse_state(
                    payload, self.binding_id, self.profile, now,
                    self.expected_proof, self.require_identity,
                )
                if snapshot["schema_version"] != 3:
                    return self._finish_update(self._ingest_state(payload, now))
                self._ingest_state(payload, now, validate_only=True)
                coordinate = self._snapshot_availability_coordinate(snapshot, sequence)
                assert coordinate is not None
                canonical = _canonical_payload(snapshot).encode()
                pending = self._pending_live_state
                if pending is not None:
                    if coordinate < pending[1]:
                        _contract_error("Local provider pending live state regressed")
                    if coordinate[:2] == pending[1][:2] and session != pending[2]:
                        _contract_error("Local provider pending live session collided")
                    if coordinate == pending[1] and canonical != pending[0]:
                        _contract_error("Local provider pending live cursor collided")
                self._pending_live_state = (canonical, coordinate, session)
                return False
            availability, status, session, _ = _parse_availability(
                payload, now, self.expected_proof, self.require_identity
            )
        except LocalProviderContractError:
            self._rejected_messages += 1
            raise
        pending = self._pending_live_state
        if (
            pending is not None
            and self._availability_coordinate(availability) == pending[1]
            and session == pending[2]
        ):
            changed = self.ingest_semantic_bootstrap_final_current(
                {
                    self.state_topic: (pending[0], qos, retained),
                    self.availability_topic: (payload, qos, retained),
                }
            )
            self._pending_live_state = None
            return changed
        changed = self.ingest(topic, payload, qos=qos, retained=retained)
        if status == "offline":
            self._pending_live_state = None
        return changed

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
                if self._is_retained_delete(payload):
                    changed = self._ingest_state_retained_delete()
                else:
                    changed = self._ingest_state(payload, now)
            elif topic == self.availability_topic:
                if self._is_retained_delete(payload):
                    changed = self._ingest_availability_retained_delete()
                else:
                    changed = self._ingest_device_availability(payload, now)
            elif topic == self.runtime_availability_topic:
                if self._is_retained_delete(payload):
                    changed = self._ingest_runtime_retained_delete()
                else:
                    changed = self._ingest_runtime_availability(payload, now, retained)
            elif self._presence_enabled and topic == self.presence_topic:
                if self._is_retained_delete(payload):
                    changed = self._ingest_presence_retained_delete()
                else:
                    changed = self._ingest_control_presence(payload, now, retained)
            else:
                _contract_error("Local provider topic is not authorized")
        except LocalProviderContractError:
            self._rejected_messages += 1
            raise
        return self._finish_update(changed)

    @staticmethod
    def _is_retained_delete(payload: object) -> bool:
        """Recognize the empty MQTT payload used to retract a retained value."""
        return isinstance(payload, (bytes, bytearray, memoryview)) and not payload

    def _tombstone_current_session_for_retained_delete(self) -> bool:
        session_id = self._session_id
        if session_id is None or session_id in self._tombstoned_sessions:
            return False
        if len(self._tombstoned_sessions) >= MAX_TOMBSTONED_GENERATIONS:
            _contract_error("Local provider session tombstone bound is exhausted")
        self._tombstoned_sessions.add(session_id)
        return True

    def _clear_device_availability_for_retained_delete(self) -> bool:
        changed = (
            self._device_status != "offline"
            or self._device_availability_payload is not None
            or self._device_availability_at is not None
            or self._device_availability_coordinate is not None
        )
        self._device_status = "offline"
        self._device_availability_payload = None
        self._device_availability_at = None
        self._device_availability_coordinate = None
        return changed

    def _validate_runtime_tombstone_capacity(
        self, *service_instance_ids: str | None
    ) -> None:
        additions = {
            service_instance_id
            for service_instance_id in service_instance_ids
            if service_instance_id is not None
            and service_instance_id not in self._tombstoned_service_instances
        }
        if (
            len(self._tombstoned_service_instances) + len(additions)
            > MAX_TOMBSTONED_GENERATIONS
        ):
            _contract_error("Local provider service tombstone bound is exhausted")

    def _apply_live_presence_runtime_handover(
        self,
        presence: _ControlPresencePublication,
        *,
        retained: bool,
    ) -> bool:
        """Let authenticated live presence fence a stale retained runtime owner."""
        if (
            retained
            or self._service_instance_id is None
            or self._service_instance_id == presence.service_instance_id
        ):
            return False
        self._validate_runtime_tombstone_capacity(self._service_instance_id)
        return self._ingest_runtime_retained_delete()

    def _ingest_runtime_retained_delete(self) -> bool:
        """Fail closed while preserving a fence against the deleted service."""
        service_instance_id = self._service_instance_id
        if (
            service_instance_id is not None
            and service_instance_id not in self._tombstoned_service_instances
        ):
            self._validate_runtime_tombstone_capacity(service_instance_id)
            self._tombstoned_service_instances.add(service_instance_id)
        changed = (
            self._runtime_status != "offline"
            or self._runtime_payload is not None
            or self._runtime_availability_at is not None
            or self._presence_live_received_at is not None
        )
        self._runtime_status = "offline"
        self._runtime_payload = None
        self._runtime_availability_at = None
        self._presence_live_received_at = None
        self._control_state_current = False
        return changed

    def _ingest_state_retained_delete(self) -> bool:
        changed = self._tombstone_current_session_for_retained_delete()
        self._control_state_current = False
        self._semantic_transport_current = False
        changed = self._clear_device_availability_for_retained_delete() or changed
        changed = (
            self._session_id is not None
            or self._sequence != 0
            or self._state_payload is not None
            or self._state_published_at is not None
            or self._state_availability_coordinate is not None
            or bool(self._shadow_fields)
            or changed
        )
        self._session_id = None
        self._sequence = 0
        self._state_payload = None
        self._state_published_at = None
        self._state_availability_coordinate = None
        self._shadow_fields = MappingProxyType({})
        return changed

    def _ingest_availability_retained_delete(self) -> bool:
        changed = self._tombstone_current_session_for_retained_delete()
        self._control_state_current = False
        self._semantic_transport_current = False
        return self._clear_device_availability_for_retained_delete() or changed

    @staticmethod
    def _validate_final_current_qos(
        publications: Mapping[str, tuple[object, int, bool]],
        *,
        require_retained: bool,
    ) -> None:
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

    def _semantic_final_current_candidate(
        self,
        publications: Mapping[str, tuple[object, int, bool]],
        now: datetime,
    ) -> _SemanticFinalCurrentCandidate:
        snapshot, session_id, sequence, shadow_fields, state_published_at = (
            _parse_state(
                publications[self.state_topic][0],
                self.binding_id,
                self.profile,
                now,
                self.expected_proof,
                self.require_identity,
            )
        )
        availability, device_status, availability_session, device_at = (
            _parse_availability(
                publications[self.availability_topic][0],
                now,
                self.expected_proof,
                self.require_identity,
            )
        )
        if availability_session != session_id:
            _contract_error(
                "Local provider final-current availability session does not match"
            )
        if device_at < state_published_at:
            _contract_error(
                "Local provider availability predates the snapshot it describes"
            )
        availability_coordinate = self._assert_availability_describes_snapshot(
            availability, snapshot, sequence
        )
        binding_generation = self._binding_generation_candidate(snapshot)
        cohort_advanced = self._cohort_advanced_candidate(
            snapshot, session_id, binding_generation
        )
        state_coordinate = self._snapshot_availability_coordinate(
            snapshot, sequence
        )
        state_canonical = _canonical_payload(snapshot)
        availability_canonical = _canonical_payload(availability)
        session_changed = self._session_id not in (None, session_id)

        if session_id in self._tombstoned_sessions and not cohort_advanced:
            _contract_error("Local provider final-current session was superseded")
        if (
            not cohort_advanced
            and not session_changed
            and self._session_id is not None
        ):
            if sequence < self._sequence:
                _contract_error("Local provider final-current sequence regressed")
            if sequence == self._sequence and state_canonical != self._state_payload:
                _contract_error("Local provider final-current cursor collided")
            if (
                self._device_availability_at is not None
                and device_at < self._device_availability_at
            ):
                _contract_error(
                    "Local provider final-current device availability regressed"
                )
        legacy_session_changed = session_changed and not cohort_advanced
        if (
            legacy_session_changed
            and len(self._tombstoned_sessions) >= MAX_TOMBSTONED_GENERATIONS
        ):
            _contract_error("Local provider session tombstone bound is exhausted")

        return _SemanticFinalCurrentCandidate(
            session_id=session_id,
            sequence=sequence,
            state_canonical=state_canonical,
            state_published_at=state_published_at,
            state_availability_coordinate=state_coordinate,
            shadow_fields=shadow_fields,
            device_status=device_status,
            availability_canonical=availability_canonical,
            device_availability_at=device_at,
            device_availability_coordinate=availability_coordinate,
            binding_generation=binding_generation,
            cohort_generation=snapshot.get("cohort_generation"),
            cohort_advanced=cohort_advanced,
            session_changed=session_changed,
            legacy_session_changed=legacy_session_changed,
            changed=(
                session_changed
                or state_canonical != self._state_payload
                or availability_canonical != self._device_availability_payload
            ),
        )

    def _apply_semantic_final_current(
        self, candidate: _SemanticFinalCurrentCandidate
    ) -> None:
        if candidate.legacy_session_changed and self._session_id is not None:
            self._tombstoned_sessions.add(self._session_id)
        if candidate.cohort_advanced:
            self._tombstoned_sessions.discard(candidate.session_id)
        self._session_id = candidate.session_id
        self._sequence = candidate.sequence
        self._state_payload = candidate.state_canonical
        self._state_published_at = candidate.state_published_at
        self._state_availability_coordinate = (
            candidate.state_availability_coordinate
        )
        self._shadow_fields = candidate.shadow_fields
        self._device_status = candidate.device_status
        self._device_availability_payload = candidate.availability_canonical
        self._device_availability_at = candidate.device_availability_at
        self._device_availability_coordinate = (
            candidate.device_availability_coordinate
        )
        if candidate.binding_generation is not None:
            self._binding_generation = candidate.binding_generation
        if candidate.cohort_generation is not None:
            self._cohort_generation = candidate.cohort_generation
        self._control_state_current = True
        self._semantic_transport_current = True

    def _runtime_final_current_candidate(
        self,
        runtime: Mapping[str, Any],
        runtime_status: str,
        service_instance_id: str,
        runtime_at: datetime,
    ) -> _RuntimeFinalCurrentCandidate:
        runtime_canonical = _canonical_payload(runtime)
        service_changed = self._service_instance_id not in (
            None,
            service_instance_id,
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
            runtime_lwt_regression = (
                self._runtime_status == "online" and runtime_status == "offline"
            )
            if not runtime_lwt_regression:
                _contract_error(
                    "Local provider final-current runtime availability regressed"
                )
        if (
            service_changed
            and len(self._tombstoned_service_instances)
            >= MAX_TOMBSTONED_GENERATIONS
        ):
            _contract_error("Local provider service tombstone bound is exhausted")
        return _RuntimeFinalCurrentCandidate(
            canonical=runtime_canonical,
            status=runtime_status,
            service_instance_id=service_instance_id,
            observed_at=runtime_at,
            service_changed=service_changed,
            lwt_regression=runtime_lwt_regression,
            changed=(
                service_changed or runtime_canonical != self._runtime_payload
            ),
        )

    def _apply_runtime_final_current(
        self, candidate: _RuntimeFinalCurrentCandidate
    ) -> None:
        if candidate.service_changed and self._service_instance_id is not None:
            self._tombstoned_service_instances.add(self._service_instance_id)
        self._service_instance_id = candidate.service_instance_id
        self._runtime_status = candidate.status
        self._runtime_payload = candidate.canonical
        if (
            candidate.service_changed
            or self._runtime_availability_at is None
            or candidate.observed_at >= self._runtime_availability_at
        ):
            self._runtime_availability_at = candidate.observed_at
        if candidate.changed:
            self._control_state_current = False

    def ingest_control_bootstrap_final_current(
        self,
        publications: Mapping[str, tuple[object, int, bool]],
    ) -> bool:
        """Atomically adopt presence+runtime even when no state topic exists yet."""
        try:
            if not self._presence_enabled:
                _contract_error("Local provider has no control presence contract")
            if self._transport_ready:
                _contract_error(
                    "Local provider control bootstrap requires a disconnected transport"
                )
            expected = {self.runtime_availability_topic, self.presence_topic}
            if set(publications) != expected:
                _contract_error(
                    "Local provider control bootstrap set is incomplete"
                )
            self._validate_final_current_qos(
                publications, require_retained=False
            )
            now = _utc_now(self._now)
            runtime, runtime_status, service_instance_id, runtime_at = (
                _parse_runtime_availability(
                    publications[self.runtime_availability_topic][0], now
                )
            )
            runtime_candidate = self._runtime_final_current_candidate(
                runtime, runtime_status, service_instance_id, runtime_at
            )
            presence = _parse_control_presence(
                publications[self.presence_topic][0],
                now,
                self.profile,
                self.expected_proof,
            )
            presence_changed, generation_advanced, presence_service_changed = (
                self._validate_presence_candidate(presence)
            )
            presence_retained = publications[self.presence_topic][2]
            if (
                not presence_retained
                and runtime_candidate.service_instance_id
                != presence.service_instance_id
            ):
                _contract_error(
                    "Local provider buffered runtime does not match live presence"
                )
            self._validate_runtime_tombstone_capacity(
                self._service_instance_id
                if runtime_candidate.service_changed
                else None,
            )
            self._apply_runtime_final_current(runtime_candidate)
            if presence_changed:
                self._apply_presence_candidate(
                    presence,
                    generation_advanced=generation_advanced,
                    service_changed=presence_service_changed,
                )
            presence_delivery_changed = self._apply_presence_delivery(
                now, presence_retained
            )
            changed = (
                runtime_candidate.changed
                or presence_changed
                or presence_delivery_changed
            )
        except LocalProviderContractError:
            self._rejected_messages += 1
            raise
        return self._finish_update(changed)

    def ingest_semantic_bootstrap_final_current(
        self,
        publications: Mapping[str, tuple[object, int, bool]],
    ) -> bool:
        """Atomically adopt the optional state+availability pair in either order."""
        try:
            expected = {self.state_topic, self.availability_topic}
            if set(publications) != expected:
                _contract_error(
                    "Local provider semantic bootstrap set is incomplete"
                )
            self._validate_final_current_qos(
                publications, require_retained=False
            )
            candidate = self._semantic_final_current_candidate(
                publications, _utc_now(self._now)
            )
            if (
                self._presence_enabled
                and self._presence_binding_generation is not None
                and candidate.binding_generation
                != self._presence_binding_generation
            ):
                _contract_error(
                    "Local provider semantic bootstrap generation does not match presence"
                )
            self._apply_semantic_final_current(candidate)
            changed = candidate.changed
        except LocalProviderContractError:
            self._rejected_messages += 1
            raise
        return self._finish_update(changed)

    def ingest_retained_final_current(
        self,
        publications: Mapping[str, tuple[object, int, bool]],
    ) -> bool:
        """Atomically adopt one complete retained-only set after a reconnect."""
        return self._finish_update(
            self._ingest_final_current(publications, require_retained=True)
        )

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
        return self._finish_update(
            self._ingest_final_current(publications, require_retained=False)
        )

    def _ingest_final_current(
        self,
        publications: Mapping[str, tuple[object, int, bool]],
        *,
        require_retained: bool,
    ) -> bool:
        """Validate and atomically apply one complete bootstrap candidate.

        A subscriber can miss the publisher's retained ``offline`` while its
        socket is down.  A complete exact topic set is therefore the
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
            snapshot, session_id, sequence, shadow_fields, state_published_at = _parse_state(
                publications[self.state_topic][0],
                self.binding_id,
                self.profile,
                now,
                self.expected_proof,
                self.require_identity,
            )
            availability, device_status, availability_session, device_at = (
                _parse_availability(
                    publications[self.availability_topic][0],
                    now,
                    self.expected_proof,
                    self.require_identity,
                )
            )
            runtime, runtime_status, service_instance_id, runtime_at = (
                _parse_runtime_availability(
                    publications[self.runtime_availability_topic][0], now
                )
            )
            presence: _ControlPresencePublication | None = None
            presence_changed = False
            presence_generation_advanced = False
            presence_service_changed = False
            presence_retained = True
            if self._presence_enabled:
                presence = _parse_control_presence(
                    publications[self.presence_topic][0],
                    now,
                    self.profile,
                    self.expected_proof,
                )
                (
                    presence_changed,
                    presence_generation_advanced,
                    presence_service_changed,
                ) = self._validate_presence_candidate(presence)
                presence_retained = publications[self.presence_topic][2]
                if (
                    not presence_retained
                    and service_instance_id != presence.service_instance_id
                ):
                    _contract_error(
                        "Local provider buffered runtime does not match live presence"
                    )
            if availability_session != session_id:
                _contract_error(
                    "Local provider final-current availability session does not match"
                )
            if device_at < state_published_at:
                _contract_error(
                    "Local provider availability predates the snapshot it describes"
                )
            availability_coordinate = self._assert_availability_describes_snapshot(
                availability, snapshot, sequence
            )
            binding_generation = self._binding_generation_candidate(snapshot)
            if (
                presence is not None
                and binding_generation != presence.binding_generation
            ):
                _contract_error(
                    "Local provider final-current state generation does not match presence"
                )
            cohort_advanced = self._cohort_advanced_candidate(
                snapshot, session_id, binding_generation
            )
            state_coordinate = self._snapshot_availability_coordinate(
                snapshot, sequence
            )
            state_canonical = _canonical_payload(snapshot)
            availability_canonical = _canonical_payload(availability)
            runtime_canonical = _canonical_payload(runtime)
            session_changed = self._session_id not in (None, session_id)
            service_changed = self._service_instance_id not in (
                None,
                service_instance_id,
            )

            if session_id in self._tombstoned_sessions and not cohort_advanced:
                _contract_error("Local provider final-current session was superseded")
            if (
                not cohort_advanced
                and not session_changed
                and self._session_id is not None
            ):
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
            legacy_session_changed = session_changed and not cohort_advanced
            if (
                legacy_session_changed
                and len(self._tombstoned_sessions) >= MAX_TOMBSTONED_GENERATIONS
            ):
                _contract_error("Local provider session tombstone bound is exhausted")
            if (
                service_changed
                and len(self._tombstoned_service_instances)
                >= MAX_TOMBSTONED_GENERATIONS
            ):
                _contract_error("Local provider service tombstone bound is exhausted")
            self._validate_runtime_tombstone_capacity(
                self._service_instance_id if service_changed else None,
            )

            changed = (
                session_changed
                or service_changed
                or presence_changed
                or state_canonical != self._state_payload
                or availability_canonical != self._device_availability_payload
                or runtime_canonical != self._runtime_payload
            )
            if legacy_session_changed and self._session_id is not None:
                self._tombstoned_sessions.add(self._session_id)
            if cohort_advanced:
                # A higher cohort supersedes by its scalar high-water, including
                # when it deliberately reuses the publisher session id.
                self._tombstoned_sessions.discard(session_id)
            if service_changed and self._service_instance_id is not None:
                self._tombstoned_service_instances.add(self._service_instance_id)

            self._session_id = session_id
            self._sequence = sequence
            self._state_payload = state_canonical
            self._state_published_at = state_published_at
            self._state_availability_coordinate = state_coordinate
            self._shadow_fields = shadow_fields
            self._device_status = device_status
            self._device_availability_payload = availability_canonical
            self._device_availability_at = device_at
            self._device_availability_coordinate = availability_coordinate
            if binding_generation is not None:
                self._binding_generation = binding_generation
            cohort_generation = snapshot.get("cohort_generation")
            if cohort_generation is not None:
                self._cohort_generation = cohort_generation
            self._service_instance_id = service_instance_id
            self._runtime_status = runtime_status
            self._runtime_payload = runtime_canonical
            if (
                service_changed
                or self._runtime_availability_at is None
                or runtime_at >= self._runtime_availability_at
            ):
                self._runtime_availability_at = runtime_at
            if presence is not None and presence_changed:
                self._apply_presence_candidate(
                    presence,
                    generation_advanced=presence_generation_advanced,
                    service_changed=presence_service_changed,
                )
            if presence is not None:
                changed = (
                    self._apply_presence_delivery(
                        now, presence_retained
                    )
                    or changed
                )
            self._control_state_current = True
            self._semantic_transport_current = True
            return changed
        except LocalProviderContractError:
            self._rejected_messages += 1
            raise

    def _ingest_state(
        self, payload: object, now: datetime, *, validate_only: bool = False
    ) -> bool:
        snapshot, session_id, sequence, fields, published_at = _parse_state(
            payload, self.binding_id, self.profile, now, self.expected_proof, self.require_identity
        )
        binding_generation = self._binding_generation_candidate(snapshot)
        cohort_advanced = self._cohort_advanced_candidate(
            snapshot, session_id, binding_generation
        )
        state_coordinate = self._snapshot_availability_coordinate(snapshot, sequence)
        canonical = _canonical_payload(snapshot)
        if session_id in self._tombstoned_sessions and not cohort_advanced:
            _contract_error("Local provider session was superseded")
        if cohort_advanced:
            pass
        elif self._session_id is not None and session_id == self._session_id:
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

        if validate_only:
            return True
        if cohort_advanced or session_id != self._session_id:
            if self._session_id is not None and not cohort_advanced:
                self._tombstoned_sessions.add(self._session_id)
            if cohort_advanced:
                self._tombstoned_sessions.discard(session_id)
            self._device_status = "unknown"
            self._device_availability_payload = None
            self._device_availability_at = None
            self._device_availability_coordinate = None
        self._session_id = session_id
        self._sequence = sequence
        self._state_payload = canonical
        self._state_published_at = published_at
        self._state_availability_coordinate = state_coordinate
        self._shadow_fields = fields
        if binding_generation is not None:
            self._binding_generation = binding_generation
        cohort_generation = snapshot.get("cohort_generation")
        if cohort_generation is not None:
            self._cohort_generation = cohort_generation
        self._control_state_current = False
        return True

    def _ingest_device_availability(self, payload: object, now: datetime) -> bool:
        value, status, session_id, observed_at = _parse_availability(
            payload, now, self.expected_proof, self.require_identity
        )
        if session_id in self._tombstoned_sessions:
            _contract_error("Local provider availability session was superseded")
        if self._session_id is None or session_id != self._session_id:
            _contract_error("Local provider availability session does not match")
        coordinate = self._availability_coordinate(value)
        if coordinate != self._state_availability_coordinate:
            _contract_error(
                "Local provider availability does not describe the current snapshot"
            )
        if self._state_published_at is None or observed_at < self._state_published_at:
            _contract_error(
                "Local provider availability predates the snapshot it describes"
            )
        canonical = _canonical_payload(value)
        if (
            self._device_availability_at is not None
            and observed_at < self._device_availability_at
        ):
            _contract_error("Local provider device availability regressed")
        if status == self._device_status:
            if canonical == self._device_availability_payload:
                return False
            # The publisher deliberately refreshes an unchanged status after a
            # newer identity-bound state. Only that stale -> exact transition is
            # a valid same-status publication; changes at one cursor still collide.
            if self._availability_describes_current_state():
                _contract_error(
                    "Local provider duplicate device availability changed"
                )
        self._device_status = status
        self._device_availability_payload = canonical
        self._device_availability_at = observed_at
        self._device_availability_coordinate = coordinate
        # This is coordinate/current proof, not liveness. Offline is still a valid exact marker;
        # independent authenticated presence decides whether a command may be attempted.
        self._control_state_current = True
        self._semantic_transport_current = True
        return True

    def _ingest_runtime_availability(
        self, payload: object, now: datetime, retained: bool
    ) -> bool:
        value, status, service_instance_id, observed_at = _parse_runtime_availability(
            payload, now
        )
        canonical = _canonical_payload(value)
        if service_instance_id in self._tombstoned_service_instances:
            _contract_error("Local provider service instance was superseded")
        runtime_lwt_regression = False
        if self._service_instance_id is not None:
            if service_instance_id != self._service_instance_id:
                presence_authorized_live_handover = (
                    not retained
                    and self._presence_payload is not None
                    and self._presence_service_instance_id == service_instance_id
                )
                if (
                    self._runtime_status != "offline"
                    and not presence_authorized_live_handover
                ):
                    _contract_error(
                        "Local provider service rotation requires runtime offline"
                    )
                self._validate_runtime_tombstone_capacity(
                    self._service_instance_id
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
        self._control_state_current = False
        return True


class LocalWaterTankShadowProvider(LocalSemanticShadowProvider):
    """Compatibility facade for the existing one-DHUM water-tank resolver."""

    def __init__(
        self,
        binding_id: str,
        *,
        profile: LocalSemanticProfile | None = None,
        pat_device_id: str | None = None,
        require_identity: bool = False,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        exact_profile = _local_semantic_profile(LOCAL_DHUM_WATER_TANK_PROFILE_ID)
        if profile is not None and profile != exact_profile:
            raise LocalProviderConfigurationError(
                "Local water-tank provider profile does not match"
            )
        # This facade covers the DHUM binding, whose publisher is the one that
        # builds its contract from a declared PAT identity and signs every
        # publication with it. Dropping the identity here left the only proof this
        # system actually emits unchecked.
        super().__init__(
            binding_id,
            exact_profile,
            pat_device_id=pat_device_id,
            require_identity=require_identity,
            now=now,
        )

    @property
    def shadow_value(self) -> bool | None:
        value = self.field_value(LOCAL_WATER_TANK_FIELD)
        return value if type(value) is bool else None


def parse_wideq_water_tank_value(value: object) -> bool | None:
    """Parse only the reviewed WideQ tank-state domain."""
    if type(value) is bool:
        return None
    if value in (0, 0.0, "0", "0.0"):
        return False
    if value in (1, 1.0, "1", "1.0", 2, 2.0, "2", "2.0"):
        return True
    return None


class WaterTankProviderResolver:
    """Keep one entity identity while routing its state to one exact owner."""

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
        """Use Local exclusively when its provider was configured."""
        if self.local_provider is not None:
            return self.local_provider.semantic_field_available(
                LOCAL_WATER_TANK_FIELD
            )
        return bool(wideq_device_available)

    def resolve(self, wideq_snapshot: object) -> bool | None:
        """Resolve Local, or WideQ only when no Local provider exists."""
        if self.local_provider is not None:
            value = self.local_provider.field_value(LOCAL_WATER_TANK_FIELD)
            return value if type(value) is bool else None
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
