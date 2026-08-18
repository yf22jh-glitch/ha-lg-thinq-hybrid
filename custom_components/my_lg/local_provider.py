"""Strict read-only provider for the Rethink Local pilot state feed.

This module deliberately has no Home Assistant or MQTT dependency.  It owns the
subscriber-side contract and cursor fencing, while transport and entity routing
remain independently testable.
"""

from __future__ import annotations

import hashlib
import hmac
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
OPT_LOCAL_SCHEMA_ONE_ROLLBACK_CONFIRMATION = (
    "local_schema_one_rollback_confirmation"
)

LOCAL_BINDING_SCHEMA_VERSION = 3
LOCAL_IDENTITY_BINDING_SCHEMA_VERSION = 2
LOCAL_LEGACY_BINDING_SCHEMA_VERSION = 1
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
MAX_COHORT_GENERATION = 1_999_999_999_998

_BINDING_ID = re.compile(r"^(?!shadow-)[a-zA-Z0-9][a-zA-Z0-9_-]{15,127}$", re.I)
_OPAQUE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")
_PAT_DEVICE_ID = re.compile(
    r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.I
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SEMANTIC_ID = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*$")
_SERVICE_INSTANCE_ID = re.compile(r"^[a-f0-9]{32}$")
_PUBLICATION_SESSION_ID = re.compile(r"^[a-f0-9]{32}$")
_ISO_TIMESTAMP = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d{1,3}))?(Z|[+-](\d{2}):(\d{2}))$"
)

_SNAPSHOT_V1_KEYS = frozenset(
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
_SNAPSHOT_V2_KEYS = _SNAPSHOT_V1_KEYS | {
    "binding_generation",
    "pat_device_id_proof_sha256",
}
_SNAPSHOT_V3_KEYS = _SNAPSHOT_V2_KEYS | {"cohort_generation"}
_SNAPSHOT_OPTIONAL_KEYS = frozenset({"invalidated_fields"})
_FIELD_REQUIRED_KEYS = frozenset(
    {"value", "value_type", "observed_at", "confidence", "exposure"}
)
_FIELD_ALLOWED_KEYS = _FIELD_REQUIRED_KEYS | {"unit"}
_INVALIDATION_KEYS = frozenset({"observed_at", "confidence"})
_DIAGNOSTIC_KEYS = frozenset(
    {"rejected_frames", "unresolved_fields", "invalid_values", "unsupported_frames"}
)
_AVAILABILITY_V1_KEYS = frozenset({"status", "session_id", "observed_at"})
_AVAILABILITY_V2_KEYS = _AVAILABILITY_V1_KEYS | {
    "binding_generation",
    "pat_device_id_proof_sha256",
    "state_sequence",
}
_AVAILABILITY_V3_KEYS = _AVAILABILITY_V2_KEYS | {
    "cohort_generation",
    "schema_version",
}
_IDENTITY_KEYS = frozenset(
    {
        "schema_version",
        "binding_id",
        "binding_generation",
        "model_id",
        "platform",
        "pat_device_id_proof_sha256",
    }
)
_RUNTIME_AVAILABILITY_KEYS = frozenset({"status", "service_instance_id", "observed_at"})

_PAT_DEVICE_PROOF_DOMAIN = b"lg-rethink-pilot/pat-device-identity-proof/v1\0"

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
_CATALOGUE_PROFILE_ALLOWED_KEYS = _CATALOGUE_PROFILE_REQUIRED_KEYS | {
    "authoritative_invalidations",
    "availability_policy",
}
_CATALOGUE_FIELD_REQUIRED_KEYS = frozenset(
    {"semantic_id", "value_type", "exposure", "confidence"}
)
_CATALOGUE_FIELD_ALLOWED_KEYS = _CATALOGUE_FIELD_REQUIRED_KEYS | {
    "unit",
    "allowed_values",
}
_CATALOGUE_DIGEST = re.compile(
    rb"^([0-9a-f]{64})  local/semantic/pilot-profiles\.v1\.json\n$"
)
MAX_PROFILE_CATALOGUE_BYTES = 256 * 1024


class LocalProviderContractError(ValueError):
    """An MQTT publication failed the pinned Local provider contract."""


class LocalProviderConfigurationError(ValueError):
    """Local shadow options are incomplete or outside the pilot boundary."""


def _is_unicode_scalar_string(value: object) -> bool:
    """Return whether a string can be represented as strict UTF-8.

    Python's JSON decoder deliberately accepts escaped lone surrogates even
    though they are not Unicode scalar values and cannot be emitted as valid
    UTF-8.  Keep those values out of every Local contract boundary.
    """
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return True


def _json_strings_are_unicode_scalars(value: object) -> bool:
    """Validate every JSON object key and string value without recursion."""
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            if not _is_unicode_scalar_string(current):
                return False
        elif isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return True


@dataclass(frozen=True)
class LocalSemanticFieldContract:
    """One exact retained-field contract authorized for a Local profile."""

    value_type: Literal["boolean", "number", "string"]
    exposure: Literal["state", "diagnostic"]
    confidence: tuple[str, ...]
    unit: str | None = None
    allowed_values: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.value_type not in ("boolean", "number", "string"):
            raise ValueError("Local semantic field value type is invalid")
        if self.exposure not in ("state", "diagnostic"):
            raise ValueError("Local semantic field exposure is invalid")
        if not self.confidence or any(
            not _is_unicode_scalar_string(item) or not item or len(item) > 128
            for item in self.confidence
        ):
            raise ValueError("Local semantic field confidence is invalid")
        if self.unit is not None and (
            not _is_unicode_scalar_string(self.unit)
            or not self.unit
            or len(self.unit) > 16
        ):
            raise ValueError("Local semantic field unit is invalid")
        if self.allowed_values is not None:
            if (
                self.value_type != "string"
                or not isinstance(self.allowed_values, tuple)
                or not self.allowed_values
                or len(self.allowed_values) > 64
                or len(set(self.allowed_values)) != len(self.allowed_values)
                or any(
                    not _is_unicode_scalar_string(item)
                    or not item
                    or len(item) > 128
                    for item in self.allowed_values
                )
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
    authoritative_invalidations: bool = False
    availability_policy: Literal["attested-session"] | None = None

    def __post_init__(self) -> None:
        if not _OPAQUE_ID.fullmatch(self.profile_id):
            raise ValueError("Local semantic profile id is invalid")
        if not _OPAQUE_ID.fullmatch(self.model_id):
            raise ValueError("Local semantic model id is invalid")
        if self.platform not in ("thinq1", "thinq2"):
            raise ValueError("Local semantic platform is invalid")
        if type(self.authoritative_invalidations) is not bool:
            raise ValueError("Local semantic invalidation policy is invalid")
        if self.availability_policy not in (None, "attested-session"):
            raise ValueError("Local semantic availability policy is invalid")
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
class LocalPilotIdentityExpectation:
    """Owner-configured equality claim for one exact PAT/Local binding.

    The digest is deliberately not authentication material.  It only lets the
    subscriber compare the retained Local claim with the PAT identity that the
    operator explicitly paired in this config entry.
    """

    binding_id: str
    binding_generation: int
    model_id: str
    platform: Literal["thinq1", "thinq2"]
    pat_device_id_proof_sha256: str

    def __post_init__(self) -> None:
        if not _BINDING_ID.fullmatch(self.binding_id):
            raise ValueError("Local identity binding id is invalid")
        if (
            type(self.binding_generation) is not int
            or self.binding_generation < 1
            or self.binding_generation > MAX_JSON_SAFE_INTEGER
        ):
            raise ValueError("Local identity binding generation is invalid")
        if not _OPAQUE_ID.fullmatch(self.model_id):
            raise ValueError("Local identity model id is invalid")
        if self.platform not in ("thinq1", "thinq2"):
            raise ValueError("Local identity platform is invalid")
        if not _SHA256.fullmatch(self.pat_device_id_proof_sha256):
            raise ValueError("Local identity proof is invalid")


def _proof_part(digest: Any, name: str, value: str) -> None:
    encoded = value.encode("utf-8")
    digest.update(name.encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(len(encoded)).encode("ascii"))
    digest.update(b"\0")
    digest.update(encoded)
    digest.update(b"\0")


def create_local_pat_device_identity_proof(
    *,
    binding_id: str,
    model_id: str,
    platform: Literal["thinq1", "thinq2"],
    pat_device_id: str,
) -> str:
    """Mirror Rethink's domain-separated, length-prefixed equality digest."""
    try:
        validated_binding_id = validate_binding_id(binding_id)
    except LocalProviderContractError as err:
        raise LocalProviderConfigurationError(str(err)) from err
    if (
        not isinstance(model_id, str)
        or not _OPAQUE_ID.fullmatch(model_id)
        or platform not in ("thinq1", "thinq2")
        or not isinstance(pat_device_id, str)
        or not _PAT_DEVICE_ID.fullmatch(pat_device_id)
    ):
        raise LocalProviderConfigurationError(
            "Local provider PAT identity proof input is invalid"
        )
    digest = hashlib.sha256(_PAT_DEVICE_PROOF_DOMAIN)
    _proof_part(digest, "binding_id", validated_binding_id)
    _proof_part(digest, "model_id", model_id)
    _proof_part(digest, "platform", platform)
    _proof_part(digest, "pat_device_id", pat_device_id.lower())
    return digest.hexdigest()


def _binding_contains_device_id_fragment(binding_id: str, device_id: str) -> bool:
    """Mirror Rethink's privacy boundary for public MQTT binding names."""
    normalized_binding = re.sub(r"[^a-z0-9]", "", binding_id.lower())
    private_parts = device_id.lower().split("-")
    fragments = [re.sub(r"[^a-z0-9]", "", device_id.lower()), *private_parts]
    return any(
        len(fragment) >= 4 and fragment in normalized_binding
        for fragment in fragments
    )


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
    except (
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
    ):
        _catalogue_error()
    if not _json_strings_are_unicode_scalars(catalogue):
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
        profile_id = raw_profile["profile_id"]
        model_id = raw_profile["model_id"]
        platform = raw_profile["platform"]
        authoritative_invalidations = raw_profile.get(
            "authoritative_invalidations", False
        )
        availability_policy = raw_profile.get("availability_policy")
        if (
            not isinstance(profile_id, str)
            or not _OPAQUE_ID.fullmatch(profile_id)
            or profile_id in profiles
            or not isinstance(model_id, str)
            or not _OPAQUE_ID.fullmatch(model_id)
            or platform not in ("thinq1", "thinq2")
            or type(authoritative_invalidations) is not bool
            or (
                "authoritative_invalidations" in raw_profile
                and not authoritative_invalidations
            )
            or (
                "availability_policy" in raw_profile
                and availability_policy is None
            )
            or availability_policy not in (None, "attested-session")
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
            if (
                not isinstance(semantic_id, str)
                or not _SEMANTIC_ID.fullmatch(semantic_id)
                or semantic_id in fields
                or raw_field["value_type"] not in ("boolean", "number", "string")
                or raw_field["exposure"] != "state"
                or not isinstance(confidence, list)
                or not confidence
                or len(confidence) > 32
                or any(not isinstance(item, str) for item in confidence)
                or len(set(confidence)) != len(confidence)
                or ("unit" in raw_field and unit is None)
                or (unit is not None and not isinstance(unit, str))
                or ("allowed_values" in raw_field and allowed_values is None)
                or (
                    allowed_values is not None
                    and (
                        not isinstance(allowed_values, list)
                        or not allowed_values
                        or len(allowed_values) > 64
                        or any(
                            not isinstance(item, str) or not item
                            for item in allowed_values
                        )
                        or len(set(allowed_values)) != len(allowed_values)
                    )
                )
            ):
                _catalogue_error()
            try:
                fields[semantic_id] = LocalSemanticFieldContract(
                    value_type=raw_field["value_type"],
                    exposure=raw_field["exposure"],
                    confidence=tuple(confidence),
                    unit=unit,
                    allowed_values=(
                        tuple(allowed_values)
                        if allowed_values is not None
                        else None
                    ),
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
                authoritative_invalidations=authoritative_invalidations,
                availability_policy=availability_policy,
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
    schema_version: Literal[1, 2, 3]
    identity_expectation: LocalPilotIdentityExpectation | None
    _profile: LocalSemanticProfile

    @property
    def profile(self) -> LocalSemanticProfile:
        return self._profile

    @property
    def snapshot_schema_version(self) -> Literal[1, 2, 3]:
        """Return the exact MQTT snapshot schema pinned by this config."""
        return self.schema_version

    @property
    def publication_plan_revision(self) -> Literal[1, 2]:
        """Return the retained publication ordering contract revision."""
        return 2 if self.schema_version == LOCAL_BINDING_SCHEMA_VERSION else 1


_LOCAL_BINDING_V1_KEYS = frozenset(
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
_LOCAL_BINDING_V2_KEYS = _LOCAL_BINDING_V1_KEYS | {"binding_generation"}
_LOCAL_BINDING_V3_KEYS = _LOCAL_BINDING_V2_KEYS
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
    return (
        isinstance(value, str)
        and _is_unicode_scalar_string(value)
        and bool(value)
        and len(value.encode("utf-8")) <= 1024
    )


def _configuration_from_values(
    *,
    pat_device_id: object,
    binding_id: object,
    mqtt_password: object,
    profile_id: object,
    model_id: object,
    platform: object,
    schema_version: Literal[1, 2, 3],
    binding_generation: object | None = None,
) -> LocalShadowConfiguration:
    if not isinstance(pat_device_id, str) or not _OPAQUE_ID.fullmatch(pat_device_id):
        _configuration_error("Local provider PAT device id is invalid")
    canonical_pat_device_id = pat_device_id
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
    identity_expectation: LocalPilotIdentityExpectation | None = None
    if schema_version == LOCAL_LEGACY_BINDING_SCHEMA_VERSION:
        # Transitional compatibility for the already deployed read-only fleet.
        # Generic manifest-v2 publishers emit snapshot schema 1 and do not have
        # an identity claim.  They may remain attached only through the shadow
        # path; schema 2/3 remains mandatory for any identity-bound promotion.
        if binding_generation is not None:
            _configuration_error("Local provider legacy binding generation is invalid")
    elif schema_version in (
        LOCAL_IDENTITY_BINDING_SCHEMA_VERSION,
        LOCAL_BINDING_SCHEMA_VERSION,
    ):
        if not _PAT_DEVICE_ID.fullmatch(pat_device_id):
            _configuration_error(
                "Local provider identity-bound PAT device id must be a UUID"
            )
        canonical_pat_device_id = pat_device_id.lower()
        if _binding_contains_device_id_fragment(
            validated_binding_id, canonical_pat_device_id
        ):
            _configuration_error(
                "Local provider binding id must not expose a PAT identity fragment"
            )
        if (
            type(binding_generation) is not int
            or binding_generation < 1
            or binding_generation > MAX_JSON_SAFE_INTEGER
        ):
            _configuration_error("Local provider binding generation is invalid")
        identity_expectation = LocalPilotIdentityExpectation(
            binding_id=validated_binding_id,
            binding_generation=binding_generation,
            model_id=profile.model_id,
            platform=profile.platform,
            pat_device_id_proof_sha256=create_local_pat_device_identity_proof(
                binding_id=validated_binding_id,
                model_id=profile.model_id,
                platform=profile.platform,
                pat_device_id=canonical_pat_device_id,
            ),
        )
    else:
        _configuration_error("Local provider binding schema is unsupported")
    return LocalShadowConfiguration(
        pat_device_id=canonical_pat_device_id,
        binding_id=validated_binding_id,
        mqtt_username=f"shadow-{validated_binding_id}",
        mqtt_password=mqtt_password,
        profile_id=profile.profile_id,
        model_id=profile.model_id,
        platform=profile.platform,
        schema_version=schema_version,
        identity_expectation=identity_expectation,
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
        schema_version=LOCAL_LEGACY_BINDING_SCHEMA_VERSION,
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
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as err:
            raise LocalProviderConfigurationError(
                "Local provider bindings JSON is invalid"
            ) from err
    if not isinstance(value, list) or len(value) > 64:
        _configuration_error("Local provider bindings must be a bounded list")
    return list(value)


def _configuration_from_binding(value: object) -> LocalShadowConfiguration:
    if not isinstance(value, dict):
        _configuration_error("Local provider binding keys are invalid")
    schema_version = value.get("schema_version")
    if type(schema_version) is not int or schema_version not in (
        LOCAL_LEGACY_BINDING_SCHEMA_VERSION,
        LOCAL_IDENTITY_BINDING_SCHEMA_VERSION,
        LOCAL_BINDING_SCHEMA_VERSION,
    ):
        _configuration_error("Local provider binding schema is unsupported")
    expected_keys = {
        LOCAL_LEGACY_BINDING_SCHEMA_VERSION: _LOCAL_BINDING_V1_KEYS,
        LOCAL_IDENTITY_BINDING_SCHEMA_VERSION: _LOCAL_BINDING_V2_KEYS,
        LOCAL_BINDING_SCHEMA_VERSION: _LOCAL_BINDING_V3_KEYS,
    }[schema_version]
    if set(value) != expected_keys:
        _configuration_error("Local provider binding keys are invalid")
    if value["mode"] != LOCAL_PROVIDER_MODE_SHADOW:
        _configuration_error("Local provider binding mode is unsupported")
    return _configuration_from_values(
        pat_device_id=value["pat_device_id"],
        binding_id=value["binding_id"],
        mqtt_password=value["mqtt_password"],
        profile_id=value["profile_id"],
        model_id=value["model_id"],
        platform=value["platform"],
        schema_version=schema_version,
        binding_generation=value.get("binding_generation"),
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
    pat_ids = [config.pat_device_id.lower() for config in configs]
    binding_ids = [config.binding_id for config in configs]
    if len(set(pat_ids)) != len(pat_ids):
        _configuration_error("Local provider PAT device bindings must be one-to-one")
    if len(set(binding_ids)) != len(binding_ids):
        _configuration_error("Local provider binding ids must be unique")
    return configs


def isolated_local_shadow_configurations(
    options: Mapping[str, object],
) -> tuple[tuple[LocalShadowConfiguration, ...], int]:
    """Return independently valid startup bindings and their rejection count.

    OptionsFlow continues to use :func:`local_shadow_configurations` and rejects
    the whole submitted document.  Runtime startup is deliberately narrower:
    one corrupt stored binding must not detach unrelated read-only shadows.
    Duplicate PAT anchors or public binding ids are fail-closed by rejecting
    every member of the ambiguous group.
    """
    if OPT_LOCAL_BINDINGS not in options:
        config = _legacy_local_shadow_configuration(options)
        return (() if config is None else (config,)), 0
    if any(
        options.get(key) not in (None, "", LOCAL_PROVIDER_MODE_DISABLED)
        for key in _LEGACY_LOCAL_OPTION_KEYS
    ):
        _configuration_error("Local provider legacy and versioned options conflict")

    raw_bindings = _decode_binding_list(options[OPT_LOCAL_BINDINGS])
    parsed: list[LocalShadowConfiguration] = []
    rejected = 0
    for item in raw_bindings:
        try:
            parsed.append(_configuration_from_binding(item))
        except LocalProviderConfigurationError:
            rejected += 1

    pat_counts: dict[str, int] = {}
    binding_counts: dict[str, int] = {}
    for config in parsed:
        pat_key = config.pat_device_id.lower()
        pat_counts[pat_key] = pat_counts.get(pat_key, 0) + 1
        binding_counts[config.binding_id] = binding_counts.get(config.binding_id, 0) + 1
    unambiguous = tuple(
        config
        for config in parsed
        if pat_counts[config.pat_device_id.lower()] == 1
        and binding_counts[config.binding_id] == 1
    )
    return unambiguous, rejected + len(parsed) - len(unambiguous)


def _configuration_dict(
    config: LocalShadowConfiguration, *, mask_password: bool = False
) -> dict[str, object]:
    return {
        "schema_version": config.schema_version,
        "mode": LOCAL_PROVIDER_MODE_SHADOW,
        "profile_id": config.profile_id,
        "model_id": config.model_id,
        "platform": config.platform,
        "pat_device_id": config.pat_device_id,
        "binding_id": config.binding_id,
        **(
            {"binding_generation": config.identity_expectation.binding_generation}
            if config.identity_expectation is not None
            else {}
        ),
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


def local_schema_one_rollback_confirmation(binding_id: object) -> str:
    """Return the exact one-use phrase for one schema 3 -> 1 rollback."""
    try:
        validated_binding_id = validate_binding_id(binding_id)
    except LocalProviderContractError as err:
        raise LocalProviderConfigurationError(str(err)) from err
    return (
        "I CONFIRM V3 SERVICE STOPPED AND RETAINED TOPICS RESET; "
        f"ROLLBACK {validated_binding_id} FROM SCHEMA 3 TO SCHEMA 1"
    )


def merge_local_shadow_options(
    submitted: Mapping[str, object], existing: Mapping[str, object]
) -> dict[str, object]:
    """Normalize OptionsFlow input while retaining only matching masked secrets."""
    result = dict(submitted)
    rollback_confirmation = result.pop(
        OPT_LOCAL_SCHEMA_ONE_ROLLBACK_CONFIRMATION, ""
    )
    if not isinstance(rollback_confirmation, str) or not _is_unicode_scalar_string(
        rollback_confirmation
    ):
        _configuration_error(
            "Local provider schema-one rollback confirmation is invalid"
        )
    try:
        existing_configs = local_shadow_configurations(existing)
    except LocalProviderConfigurationError:
        # An invalid stored value must be repairable through OptionsFlow, but
        # none of its unvalidated secrets are eligible for implicit reuse.
        existing_configs = ()
        existing_is_valid = False
        try:
            existing_identity_configs = isolated_local_shadow_configurations(
                existing
            )[0]
        except LocalProviderConfigurationError:
            existing_identity_configs = ()
    else:
        existing_is_valid = True
        existing_identity_configs = existing_configs
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

    normalized = migrate_local_shadow_options(result)
    new_configs = local_shadow_configurations(normalized)
    prior_by_binding = {
        config.binding_id: config for config in existing_identity_configs
    }
    schema_one_rollbacks: list[str] = []
    for config in new_configs:
        prior = prior_by_binding.get(config.binding_id)
        if prior is None:
            continue
        prior_generation = (
            prior.identity_expectation.binding_generation
            if prior.identity_expectation is not None
            else None
        )
        generation = (
            config.identity_expectation.binding_generation
            if config.identity_expectation is not None
            else None
        )
        identity_changed = (
            prior.schema_version,
            prior.pat_device_id.lower(),
            prior.profile_id,
            prior.model_id,
            prior.platform,
            prior_generation,
        ) != (
            config.schema_version,
            config.pat_device_id.lower(),
            config.profile_id,
            config.model_id,
            config.platform,
            generation,
        )
        if not identity_changed:
            continue
        schema_one_rollback = (
            prior.schema_version == LOCAL_BINDING_SCHEMA_VERSION
            and config.schema_version == LOCAL_LEGACY_BINDING_SCHEMA_VERSION
            and prior.pat_device_id.lower() == config.pat_device_id.lower()
            and prior.profile_id == config.profile_id
            and prior.model_id == config.model_id
            and prior.platform == config.platform
            and prior_generation is not None
            and generation is None
        )
        if schema_one_rollback:
            schema_one_rollbacks.append(config.binding_id)
        else:
            _configuration_error(
                "Local provider binding identity reuse is forbidden; use a new binding id"
            )
    if schema_one_rollbacks:
        if (
            len(schema_one_rollbacks) != 1
            or rollback_confirmation
            != local_schema_one_rollback_confirmation(schema_one_rollbacks[0])
        ):
            _configuration_error(
                "Local provider schema-one rollback requires its exact one-use confirmation"
            )
    elif rollback_confirmation:
        _configuration_error(
            "Local provider schema-one rollback confirmation was not consumed"
        )
    return normalized


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
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        _contract_error("Local provider payload is not valid JSON")
    if not isinstance(value, dict):
        _contract_error("Local provider payload must be a JSON object")
    if not _json_strings_are_unicode_scalars(value):
        _contract_error("Local provider payload contains a non-scalar string")
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
    identity_expectation: LocalPilotIdentityExpectation | None,
    snapshot_schema_version: Literal[1, 2, 3],
    now: datetime,
) -> tuple[
    dict[str, Any],
    str,
    int,
    int | None,
    datetime,
    Mapping[str, LocalSemanticShadowField],
]:
    snapshot = _decode_payload(payload)
    expected_keys = {
        1: _SNAPSHOT_V1_KEYS,
        2: _SNAPSHOT_V2_KEYS,
        3: _SNAPSHOT_V3_KEYS,
    }[snapshot_schema_version]
    snapshot_keys = set(snapshot)
    if not expected_keys.issubset(snapshot_keys) or not snapshot_keys.issubset(
        expected_keys | _SNAPSHOT_OPTIONAL_KEYS
    ):
        _contract_error("snapshot keys are invalid")
    if (
        type(snapshot["schema_version"]) is not int
        or snapshot["schema_version"] != snapshot_schema_version
    ):
        _contract_error("Local provider snapshot schema is unsupported")
    if (
        type(snapshot["semantics_revision"]) is not int
        or snapshot["semantics_revision"]
        not in profile.supported_semantics_revisions
    ):
        _contract_error("Local provider semantics revision is unsupported")
    if snapshot["binding_id"] != expected_binding_id:
        _contract_error("Local provider binding does not match")
    if (
        snapshot["model_id"] != profile.model_id
        or snapshot["platform"] != profile.platform
    ):
        _contract_error("Local provider model or platform does not match")
    if snapshot_schema_version >= 2:
        if identity_expectation is None:
            _contract_error("Local provider identity-bound snapshot lacks an expectation")
        if (
            type(snapshot["binding_generation"]) is not int
            or snapshot["binding_generation"]
            != identity_expectation.binding_generation
            or not isinstance(snapshot["pat_device_id_proof_sha256"], str)
            or not _SHA256.fullmatch(snapshot["pat_device_id_proof_sha256"])
            or not hmac.compare_digest(
                snapshot["pat_device_id_proof_sha256"],
                identity_expectation.pat_device_id_proof_sha256,
            )
        ):
            _contract_error("Local provider snapshot identity does not match")

    session_id = snapshot["session_id"]
    session_pattern = (
        _PUBLICATION_SESSION_ID
        if snapshot_schema_version == 3
        else _OPAQUE_ID
    )
    if not isinstance(session_id, str) or not session_pattern.fullmatch(session_id):
        _contract_error("Local provider session id is invalid")
    sequence = snapshot["sequence"]
    if type(sequence) is not int or sequence < 1 or sequence > MAX_JSON_SAFE_INTEGER:
        _contract_error("Local provider sequence is invalid")
    cohort_generation: int | None = None
    if snapshot_schema_version == 3:
        cohort_generation = snapshot["cohort_generation"]
        if (
            type(cohort_generation) is not int
            or cohort_generation < 1
            or cohort_generation > MAX_COHORT_GENERATION
        ):
            _contract_error("Local provider cohort generation is invalid")
    published_at = _timestamp(snapshot["published_at"], "published_at", now)

    fields = snapshot["fields"]
    invalidated_fields = snapshot.get("invalidated_fields", {})
    if not isinstance(fields, dict) or len(fields) > 256:
        _contract_error("Local provider snapshot fields are invalid")
    if (
        not isinstance(invalidated_fields, dict)
        or len(invalidated_fields) > 256
        or ("invalidated_fields" in snapshot and not invalidated_fields)
        or len(fields) + len(invalidated_fields) < 1
        or len(fields) + len(invalidated_fields) > 256
    ):
        _contract_error("Local provider snapshot field invalidations are invalid")
    if invalidated_fields and not profile.authoritative_invalidations:
        _contract_error(
            "Local provider snapshot field invalidations are not authorized"
        )
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
                    or (
                        isinstance(value, int)
                        and abs(value) > MAX_JSON_SAFE_INTEGER
                    )
                    or (isinstance(value, float) and not math.isfinite(value))
                )
            )
            or (
                value_type == "string"
                and (not _is_unicode_scalar_string(value) or len(value) > 128)
            )
        ):
            _contract_error("Local provider semantic field value is invalid")
        if field["confidence"] not in contract.confidence:
            _contract_error("Local provider semantic field confidence is unsupported")
        if field["exposure"] != contract.exposure:
            _contract_error("Local provider semantic field exposure is unsupported")
        if field.get("unit") != contract.unit:
            _contract_error("Local provider semantic field unit is unsupported")
        if (
            contract.allowed_values is not None
            and value not in contract.allowed_values
        ):
            _contract_error("Local provider semantic field value is unsupported")
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

    for semantic_id, raw_invalidation in invalidated_fields.items():
        contract = profile.fields.get(semantic_id)
        if contract is None or semantic_id in fields:
            _contract_error("Local provider semantic invalidation is not authorized")
        invalidation = _exact_object(
            raw_invalidation,
            _INVALIDATION_KEYS,
            f"semantic invalidation {semantic_id}",
        )
        if invalidation["confidence"] not in contract.confidence:
            _contract_error(
                "Local provider semantic invalidation confidence is unsupported"
            )
        invalidated_at = _timestamp(
            invalidation["observed_at"],
            f"semantic invalidation {semantic_id} observed_at",
            now,
        )
        if invalidated_at > published_at:
            _contract_error(
                "Local provider semantic invalidation is after publication"
            )

    diagnostics = _exact_object(
        snapshot["diagnostics"], _DIAGNOSTIC_KEYS, "snapshot diagnostics"
    )
    for name, value in diagnostics.items():
        _safe_nonnegative_integer(value, f"snapshot diagnostics {name}")

    return (
        snapshot,
        session_id,
        sequence,
        cohort_generation,
        published_at,
        MappingProxyType(shadow_fields),
    )


def _parse_availability(
    payload: object,
    identity_expectation: LocalPilotIdentityExpectation | None,
    snapshot_schema_version: Literal[1, 2, 3],
    now: datetime,
) -> tuple[dict[str, Any], str, str, datetime, int | None, int | None]:
    expected_keys = {
        1: _AVAILABILITY_V1_KEYS,
        2: _AVAILABILITY_V2_KEYS,
        3: _AVAILABILITY_V3_KEYS,
    }[snapshot_schema_version]
    value = _exact_object(
        _decode_payload(payload), expected_keys, "device availability"
    )
    status = value["status"]
    if status not in ("online", "offline"):
        _contract_error("Local provider device availability is invalid")
    session_id = value["session_id"]
    session_pattern = (
        _PUBLICATION_SESSION_ID
        if snapshot_schema_version == 3
        else _OPAQUE_ID
    )
    if not isinstance(session_id, str) or not session_pattern.fullmatch(session_id):
        _contract_error("Local provider availability session id is invalid")
    observed_at = _timestamp(
        value["observed_at"], "device availability observed_at", now
    )
    state_sequence: int | None = None
    cohort_generation: int | None = None
    if snapshot_schema_version >= 2:
        if identity_expectation is None:
            _contract_error(
                "Local provider identity-bound availability lacks an expectation"
            )
        if (
            type(value["binding_generation"]) is not int
            or value["binding_generation"]
            != identity_expectation.binding_generation
            or not isinstance(value["pat_device_id_proof_sha256"], str)
            or not _SHA256.fullmatch(value["pat_device_id_proof_sha256"])
            or not hmac.compare_digest(
                value["pat_device_id_proof_sha256"],
                identity_expectation.pat_device_id_proof_sha256,
            )
        ):
            _contract_error("Local provider availability identity does not match")
        state_sequence = value["state_sequence"]
        if (
            type(state_sequence) is not int
            or state_sequence < 1
            or state_sequence > MAX_JSON_SAFE_INTEGER
        ):
            _contract_error("Local provider availability state sequence is invalid")
    if snapshot_schema_version == 3:
        if type(value["schema_version"]) is not int or value["schema_version"] != 3:
            _contract_error("Local provider availability schema is unsupported")
        cohort_generation = value["cohort_generation"]
        if (
            type(cohort_generation) is not int
            or cohort_generation < 1
            or cohort_generation > MAX_COHORT_GENERATION
        ):
            _contract_error("Local provider availability cohort generation is invalid")
    return (
        value,
        status,
        session_id,
        observed_at,
        state_sequence,
        cohort_generation,
    )


def _parse_identity(
    payload: object,
    expectation: LocalPilotIdentityExpectation,
) -> dict[str, Any]:
    value = _exact_object(_decode_payload(payload), _IDENTITY_KEYS, "identity")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["binding_id"] != expectation.binding_id
        or type(value["binding_generation"]) is not int
        or value["binding_generation"] != expectation.binding_generation
        or value["model_id"] != expectation.model_id
        or value["platform"] != expectation.platform
        or not isinstance(value["pat_device_id_proof_sha256"], str)
        or not _SHA256.fullmatch(value["pat_device_id_proof_sha256"])
        or not hmac.compare_digest(
            value["pat_device_id_proof_sha256"],
            expectation.pat_device_id_proof_sha256,
        )
    ):
        _contract_error("Local provider identity claim does not match configured PAT owner")
    return value


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
        identity_expectation: LocalPilotIdentityExpectation | None = None,
        snapshot_schema_version: Literal[1, 2, 3] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.binding_id = validate_binding_id(binding_id)
        if not isinstance(profile, LocalSemanticProfile):
            raise TypeError("Local provider profile is invalid")
        if identity_expectation is not None and (
            not isinstance(identity_expectation, LocalPilotIdentityExpectation)
            or identity_expectation.binding_id != self.binding_id
            or identity_expectation.model_id != profile.model_id
            or identity_expectation.platform != profile.platform
        ):
            raise LocalProviderConfigurationError(
                "Local provider identity expectation does not match its profile"
            )
        self.profile = profile
        self.identity_expectation = identity_expectation
        if snapshot_schema_version is None:
            snapshot_schema_version = 1 if identity_expectation is None else 2
        if (
            type(snapshot_schema_version) is not int
            or snapshot_schema_version not in (1, 2, 3)
            or (snapshot_schema_version == 1) != (identity_expectation is None)
        ):
            raise LocalProviderConfigurationError(
                "Local provider snapshot schema does not match its identity contract"
            )
        self.snapshot_schema_version = snapshot_schema_version
        self.publication_plan_revision = 2 if snapshot_schema_version == 3 else 1
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.state_topic = f"{LOCAL_PILOT_PREFIX}/state/{self.binding_id}"
        self.availability_topic = f"{LOCAL_PILOT_PREFIX}/availability/{self.binding_id}"
        self.runtime_availability_topic = (
            f"{LOCAL_PILOT_PREFIX}/runtime/{self.binding_id}/availability"
        )
        self.identity_topic = f"{LOCAL_PILOT_PREFIX}/identity/{self.binding_id}"
        base_topics = (
            self.state_topic,
            self.availability_topic,
            self.runtime_availability_topic,
        )
        self.topics = (
            base_topics
            if self.identity_expectation is None
            else (*base_topics, self.identity_topic)
        )

        self._transport_ready = False
        self._session_id: str | None = None
        self._sequence = 0
        self._cohort_generation: int | None = None
        self._cohort_generation_high_water: int | None = None
        self._state_payload: str | None = None
        self._state_published_at: datetime | None = None
        self._shadow_fields: Mapping[str, LocalSemanticShadowField] = MappingProxyType(
            {}
        )
        self._device_status = "unknown"
        self._device_availability_payload: str | None = None
        self._device_availability_at: datetime | None = None
        self._device_availability_state_sequence: int | None = None
        self._tombstoned_sessions: set[str] = set()

        self._service_instance_id: str | None = None
        self._runtime_status = "unknown"
        self._runtime_payload: str | None = None
        self._runtime_availability_at: datetime | None = None
        self._tombstoned_service_instances: set[str] = set()
        self._identity_payload: str | None = None
        self._identity_claim_matches = self.identity_expectation is None
        self._cohort_reset_fence_exhausted = False
        self._rejected_messages = 0
        self._listeners: set[Callable[[], None]] = set()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def cohort_generation(self) -> int | None:
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
        """Return whether every configured read-only feed fence currently agrees."""
        return (
            self._transport_ready
            and self._identity_claim_matches
            and self._state_payload is not None
            and self._session_id is not None
            and self._device_status == "online"
            and (
                self.identity_expectation is None
                or self._device_availability_state_sequence == self._sequence
            )
            and (
                self.snapshot_schema_version < 3
                or (
                    self._cohort_generation is not None
                    and self._service_instance_id == self._session_id
                    and self._state_published_at is not None
                    and self._device_availability_at is not None
                    and self._device_availability_at >= self._state_published_at
                )
            )
            and self._runtime_status == "online"
        )

    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register one event-loop state listener and return its remover."""
        if not callable(listener):
            raise TypeError("Local provider listener must be callable")
        self._listeners.add(listener)

        def remove() -> None:
            self._listeners.discard(listener)

        return remove

    def _notify_listeners(self) -> None:
        """Notify HA-facing listeners without letting one break ingestion."""
        for listener in tuple(self._listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001 - isolate entity callback failures
                _LOGGER.exception("Rethink Local provider listener failed")

    def set_transport_ready(self, ready: bool) -> None:
        if type(ready) is not bool:
            raise TypeError("Local provider transport readiness must be boolean")
        if ready == self._transport_ready:
            return
        self._transport_ready = ready
        self._notify_listeners()

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
            if isinstance(payload, (bytes, bytearray, memoryview)) and not payload:
                changed = self._ingest_retained_tombstone(topic)
                if changed:
                    self._notify_listeners()
                return changed
            now = _utc_now(self._now)
            if topic == self.state_topic:
                changed = self._ingest_state(payload, now)
            elif topic == self.availability_topic:
                changed = self._ingest_device_availability(payload, now)
            elif topic == self.runtime_availability_topic:
                changed = self._ingest_runtime_availability(payload, now)
            elif (
                self.identity_expectation is not None
                and topic == self.identity_topic
            ):
                changed = self._ingest_identity(payload)
            else:
                _contract_error("Local provider topic is not authorized")
            if changed:
                self._notify_listeners()
            return changed
        except LocalProviderContractError:
            self._rejected_messages += 1
            raise

    def _remember_tombstoned_generation(self) -> None:
        """Fence the current session/service before erasing a retained cohort."""
        candidates = [(self._service_instance_id, self._tombstoned_service_instances)]
        # Schema v3 uses an O(1) monotonic cohort high-water. The high-water is
        # deliberately retained across MQTT deletions, so a cleared retained
        # cohort cannot be revived by a delayed state from the same generation.
        if self.snapshot_schema_version < 3:
            candidates.insert(0, (self._session_id, self._tombstoned_sessions))
        for current, tombstones in candidates:
            if current is None or current in tombstones:
                continue
            if len(tombstones) >= MAX_TOMBSTONED_GENERATIONS:
                self._cohort_reset_fence_exhausted = True
                continue
            tombstones.add(current)

    def _clear_retained_cohort(self) -> bool:
        """Atomically discard state, device availability, identity, and runtime."""
        changed = any(
            (
                self._session_id is not None,
                self._state_payload is not None,
                bool(self._shadow_fields),
                self._device_status != "unknown",
                self._device_availability_payload is not None,
                self._service_instance_id is not None,
                self._runtime_status != "unknown",
                self._runtime_payload is not None,
                self._identity_payload is not None,
                self._identity_claim_matches != (self.identity_expectation is None),
            )
        )
        self._remember_tombstoned_generation()
        self._session_id = None
        self._sequence = 0
        self._cohort_generation = None
        self._state_payload = None
        self._state_published_at = None
        self._shadow_fields = MappingProxyType({})
        self._device_status = "unknown"
        self._device_availability_payload = None
        self._device_availability_at = None
        self._device_availability_state_sequence = None
        self._service_instance_id = None
        self._runtime_status = "unknown"
        self._runtime_payload = None
        self._runtime_availability_at = None
        self._identity_payload = None
        self._identity_claim_matches = self.identity_expectation is None
        return changed

    def _ingest_retained_tombstone(self, topic: str) -> bool:
        """Apply an MQTT retained deletion as a fail-closed cohort reset."""
        authorized = {self.state_topic, self.availability_topic}
        if self.identity_expectation is not None:
            authorized.add(self.identity_topic)
        if topic == self.runtime_availability_topic:
            # The publisher does not normally delete its LWT, but a broker-side
            # deletion must still make the shadow unavailable immediately.
            return self._clear_retained_cohort()
        if topic not in authorized:
            _contract_error("Local provider tombstone topic is not authorized")
        return self._clear_retained_cohort()

    def ingest_retained_final_current(
        self,
        publications: Mapping[str, tuple[object, int, bool]],
    ) -> bool:
        """Atomically adopt one complete retained-only set after a reconnect."""
        changed = self._ingest_final_current(publications, require_retained=True)
        if changed:
            self._notify_listeners()
        return changed

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
        changed = self._ingest_final_current(publications, require_retained=False)
        if changed:
            self._notify_listeners()
        return changed

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
            if self._cohort_reset_fence_exhausted:
                _contract_error("Local provider generation fence is exhausted")
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
            (
                snapshot,
                session_id,
                sequence,
                cohort_generation,
                state_published_at,
                shadow_fields,
            ) = _parse_state(
                publications[self.state_topic][0],
                self.binding_id,
                self.profile,
                self.identity_expectation,
                self.snapshot_schema_version,
                now,
            )
            (
                availability,
                device_status,
                availability_session,
                device_at,
                availability_state_sequence,
                availability_cohort_generation,
            ) = _parse_availability(
                publications[self.availability_topic][0],
                self.identity_expectation,
                self.snapshot_schema_version,
                now,
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
            if (
                self.identity_expectation is not None
                and availability_state_sequence != sequence
            ):
                _contract_error(
                    "Local provider final-current availability state sequence "
                    "does not match"
                )
            if (
                self.snapshot_schema_version == 3
                and availability_cohort_generation != cohort_generation
            ):
                _contract_error(
                    "Local provider final-current availability cohort does not match"
                )
            if self.snapshot_schema_version == 3:
                if service_instance_id != session_id:
                    _contract_error(
                        "Local provider final-current publication session does not "
                        "match runtime"
                    )
                if device_at < state_published_at:
                    _contract_error(
                        "Local provider final-current availability predates state"
                    )

            identity_canonical: str | None = None
            if self.identity_expectation is not None:
                identity = _parse_identity(
                    publications[self.identity_topic][0], self.identity_expectation
                )
                identity_canonical = _canonical_payload(identity)

            state_canonical = _canonical_payload(snapshot)
            availability_canonical = _canonical_payload(availability)
            runtime_canonical = _canonical_payload(runtime)
            session_changed = self._session_id not in (None, session_id)
            cohort_changed = (
                self.snapshot_schema_version == 3
                and self._cohort_generation is not None
                and cohort_generation != self._cohort_generation
            )
            service_changed = self._service_instance_id not in (
                None,
                service_instance_id,
            )

            if (
                self.snapshot_schema_version < 3
                and session_id in self._tombstoned_sessions
            ):
                _contract_error("Local provider final-current session was superseded")
            if self.snapshot_schema_version == 3:
                high_water = self._cohort_generation_high_water
                assert cohort_generation is not None
                if high_water is not None and cohort_generation < high_water:
                    _contract_error("Local provider final-current cohort regressed")
                if (
                    high_water is not None
                    and cohort_generation == high_water
                ):
                    if self._session_id is None:
                        _contract_error(
                            "Local provider final-current cohort was cleared"
                        )
                    if session_changed:
                        _contract_error(
                            "Local provider final-current cohort session collided"
                        )
                    if sequence < self._sequence:
                        _contract_error(
                            "Local provider final-current sequence regressed"
                        )
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
                # A complete disconnected final-current set may resume after a
                # broker reconnect with a new process session, but only at a
                # strictly higher cohort. The exact state+availability pair is
                # applied atomically below, so no prior offline edge is needed.
            elif not session_changed and self._session_id is not None:
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
                self.snapshot_schema_version < 3
                and session_changed
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
                or cohort_changed
                or service_changed
                or state_canonical != self._state_payload
                or availability_canonical != self._device_availability_payload
                or runtime_canonical != self._runtime_payload
                or identity_canonical != self._identity_payload
            )
            if (
                self.snapshot_schema_version < 3
                and session_changed
                and self._session_id is not None
            ):
                self._tombstoned_sessions.add(self._session_id)
            if service_changed and self._service_instance_id is not None:
                self._tombstoned_service_instances.add(self._service_instance_id)

            self._session_id = session_id
            self._sequence = sequence
            self._cohort_generation = cohort_generation
            if cohort_generation is not None:
                self._cohort_generation_high_water = cohort_generation
            self._state_payload = state_canonical
            self._state_published_at = state_published_at
            self._shadow_fields = shadow_fields
            self._device_status = device_status
            self._device_availability_payload = availability_canonical
            self._device_availability_at = device_at
            self._device_availability_state_sequence = availability_state_sequence
            self._service_instance_id = service_instance_id
            self._runtime_status = runtime_status
            self._runtime_payload = runtime_canonical
            self._identity_payload = identity_canonical
            self._identity_claim_matches = self.identity_expectation is None or (
                identity_canonical is not None
            )
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
        if self._cohort_reset_fence_exhausted:
            _contract_error("Local provider generation fence is exhausted")
        (
            snapshot,
            session_id,
            sequence,
            cohort_generation,
            published_at,
            fields,
        ) = _parse_state(
            payload,
            self.binding_id,
            self.profile,
            self.identity_expectation,
            self.snapshot_schema_version,
            now,
        )
        canonical = _canonical_payload(snapshot)
        session_changed = session_id != self._session_id
        cohort_changed = False
        if self.snapshot_schema_version == 3:
            assert cohort_generation is not None
            high_water = self._cohort_generation_high_water
            if high_water is not None and cohort_generation < high_water:
                _contract_error("Local provider cohort generation regressed")
            if high_water is not None and cohort_generation == high_water:
                if self._session_id is None:
                    _contract_error("Local provider cohort was cleared")
                if session_changed:
                    _contract_error("Local provider cohort session collided")
                if sequence < self._sequence:
                    _contract_error("Local provider sequence regressed")
                if sequence == self._sequence:
                    if canonical == self._state_payload:
                        return False
                    _contract_error("Local provider cursor collided")
            cohort_changed = high_water is not None and cohort_generation > high_water
        else:
            if session_id in self._tombstoned_sessions:
                _contract_error("Local provider session was superseded")
            if self._session_id is not None and not session_changed:
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
                self._tombstoned_sessions.add(self._session_id)

        state_advanced = (
            not session_changed
            and not cohort_changed
            and self.identity_expectation is not None
            and sequence > self._sequence
        )
        if session_changed or cohort_changed or state_advanced:
            self._device_status = "unknown"
            self._device_availability_payload = None
            self._device_availability_state_sequence = None
        if session_changed or cohort_changed:
            self._device_availability_at = None
        self._session_id = session_id
        self._sequence = sequence
        self._cohort_generation = cohort_generation
        if cohort_generation is not None:
            self._cohort_generation_high_water = cohort_generation
        self._state_payload = canonical
        self._state_published_at = published_at
        self._shadow_fields = fields
        return True

    def _ingest_device_availability(self, payload: object, now: datetime) -> bool:
        if self._cohort_reset_fence_exhausted:
            _contract_error("Local provider generation fence is exhausted")
        (
            value,
            status,
            session_id,
            observed_at,
            state_sequence,
            cohort_generation,
        ) = (
            _parse_availability(
                payload,
                self.identity_expectation,
                self.snapshot_schema_version,
                now,
            )
        )
        if self._session_id is None or session_id != self._session_id:
            _contract_error("Local provider availability session does not match")
        if self.identity_expectation is not None and state_sequence != self._sequence:
            _contract_error(
                "Local provider availability state sequence does not match"
            )
        if (
            self.snapshot_schema_version == 3
            and cohort_generation != self._cohort_generation
        ):
            _contract_error("Local provider availability cohort does not match")
        if self.snapshot_schema_version == 3 and (
            self._state_published_at is None
            or observed_at < self._state_published_at
        ):
            _contract_error("Local provider availability predates state")
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
        self._device_availability_state_sequence = state_sequence
        return True

    def _ingest_identity(self, payload: object) -> bool:
        if self._cohort_reset_fence_exhausted:
            _contract_error("Local provider generation fence is exhausted")
        expectation = self.identity_expectation
        if expectation is None:
            _contract_error("Local provider identity topic is not authorized")
        value = _parse_identity(payload, expectation)
        canonical = _canonical_payload(value)
        if canonical == self._identity_payload:
            return False
        if self._identity_payload is not None:
            _contract_error("Local provider identity claim changed in place")
        self._identity_payload = canonical
        self._identity_claim_matches = True
        return True

    def _ingest_runtime_availability(self, payload: object, now: datetime) -> bool:
        if self._cohort_reset_fence_exhausted:
            _contract_error("Local provider generation fence is exhausted")
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
        identity_expectation: LocalPilotIdentityExpectation | None = None,
        snapshot_schema_version: Literal[1, 2, 3] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        exact_profile = _local_semantic_profile(LOCAL_DHUM_WATER_TANK_PROFILE_ID)
        if profile is not None and profile != exact_profile:
            raise LocalProviderConfigurationError(
                "Local water-tank provider profile does not match"
            )
        super().__init__(
            binding_id,
            exact_profile,
            identity_expectation=identity_expectation,
            snapshot_schema_version=snapshot_schema_version,
            now=now,
        )

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
