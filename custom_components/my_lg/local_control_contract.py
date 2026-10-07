"""Pinned Home Assistant entity contract for exact Rethink Local controls.

The public artifact describes model/capability surfaces only. Materialization
joins it to configured appliance identities; an optional explicit per-binding
scope can restrict that set. Actual writes still use the bound command router.
This module has no Home Assistant dependency so its fail-closed contract can be
tested in isolation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Union

LOCAL_CONTROL_ENTITY_CONTRACT_FILENAME = (
    "home-assistant-local-control-entity-contract.v1.json"
)
LOCAL_CONTROL_ENTITY_CONTRACT_DIGEST_FILENAME = (
    "home-assistant-local-control-entity-contract.v1.sha256"
)
LOCAL_CONTROL_ELIGIBILITY_OPTION = "local_control_eligibility"
LOCAL_CONTROL_ENTITY_CONTRACT_SCHEMA_VERSION = 1
LOCAL_CONTROL_ELIGIBILITY_SCHEMA_VERSION = 3
EXPECTED_LOCAL_CONTROL_ENTITY_CONTRACT_ROOT_SHA256 = (
    "0c424972c46f489e8e353e2743790cf6cd3c4f44666830b37aebc09a80554a70"
)
EXPECTED_LOCAL_CONTROL_CHECKPOINT_REVISION = (
    "ha-local-control-authority-checkpoint-v3:98695b8d3012d057"
)
EXPECTED_LOCAL_CONTROL_CHECKPOINT_SHA256 = (
    "98695b8d3012d05719d776360f31521454bfcdebb22df4ad90405b27dd51df06"
)
EXPECTED_LOCAL_CONTROL_TARGET_AUTHORITY_REVISION = (
    "ha-local-control-target-authority-v3:46d0b93596df5a1b"
)
EXPECTED_LOCAL_CONTROL_TARGET_AUTHORITY_SHA256 = (
    "46d0b93596df5a1b94525f127c9c7eb204b6b1e32b8261f95359be1efef50f4d"
)
_HASH_DOMAIN = b"lg-rethink-local/home-assistant-local-control-entity-contract/v1\0"
_MAX_ARTIFACT_BYTES = 512 * 1024
_MAX_VALUES_PER_ENTRY = 256

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_BINDING_ID = re.compile(r"^(?!shadow-)[A-Za-z0-9][A-Za-z0-9_-]{15,127}$")
_SEMANTIC_ID = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*$")
_ENTITY_KEY = re.compile(r"^local_[a-z0-9_]+$")
_FORBIDDEN_ZERO_EVIDENCE_VALUES = frozenset(
    {
        ("CST_170004_WW", "climate.mode_fan_setpoint", "cool|high|21C"),
        ("CST_170004_WW", "climate.mode_fan_setpoint", "cool|high|22C"),
        ("CST_170004_WW", "climate.mode_fan_setpoint", "cool|low|22C"),
        ("CST_170004_WW", "climate.mode_fan_setpoint", "cool|medium|22C"),
    }
)

_ARTIFACT_KEYS = frozenset(
    {
        "schemaVersion",
        "revision",
        "rootSha256",
        "authority",
        "deploymentAuthority",
        "promotionDecision",
        "fleetDeviceCount",
        "models",
        "entities",
        "excludedCapabilities",
        "excludedValues",
        "stats",
    }
)
_PROMOTION_KEYS = frozenset(
    {
        "revision",
        "decision",
        "evidenceAuthority",
        "defaultEnabled",
        "pilotRequiredBeforeEnablement",
        "privateBindingEligibilityRequired",
        "requiresPrivateTargetAuthorityGate",
    }
)
_MODEL_KEYS = frozenset(
    {"modelId", "fleetUnitCount", "includedEntityCount", "excludedCapabilityCount"}
)
_ENTITY_KEYS = frozenset(
    {
        "key",
        "modelId",
        "capabilityId",
        "homeAssistantEntityKey",
        "labelKo",
        "entityDomain",
        "inputDomain",
        "supportedValues",
        "safetyClass",
        "commandSemantics",
        "parameterless",
        "oneShot",
        "exactStateSemantic",
        "exposure",
        "entityCategory",
        "diagnostic",
        "valueMappings",
        "materializationPolicy",
        "fallbackPolicy",
        "defaultEnabledPolicy",
        "existingHomeAssistantOwner",
    }
)
_VALUE_MAPPING_KEYS = frozenset({"homeAssistantValue", "localRequestValue"})
_MATERIALIZATION_KEYS = frozenset(
    {"factoryEligible", "action", "duplicatePrevention", "privateBindingEligibility"}
)
_FALLBACK_KEYS = frozenset({"prewireRefusal", "postwireFailure"})
_DEFAULT_POLICY_KEYS = frozenset(
    {"appliesWhenMaterializedAsNewEntity", "enabled", "reason"}
)
_OWNER_KEYS = frozenset({"exists", "surfaces"})
_OWNER_SURFACE_KEYS = frozenset(
    {"domain", "entityKey", "dashboardReferenced", "localWriteCoverage"}
)
_EXCLUDED_CAPABILITY_KEYS = frozenset({"key", "modelId", "capabilityId", "reason"})
_EXCLUDED_VALUE_KEYS = frozenset({"key", "modelId", "capabilityId", "value", "reason"})
_STATS_KEYS = frozenset(
    {
        "fleetDeviceCount",
        "registryModelCount",
        "representedModelCount",
        "entityCount",
        "supportedValueCount",
        "switchCount",
        "selectCount",
        "numberCount",
        "buttonCount",
        "existingOwnerCount",
        "existingOwnerSurfaceCount",
        "dashboardReferencedOwnerCount",
        "factoryEntityCount",
        "naivePhysicalEntityInstanceCount",
        "bindingEligiblePhysicalEntityInstanceCount",
        "bindingBlockedPhysicalEntityInstanceCount",
        "naivePhysicalValueInstanceCount",
        "bindingEligiblePhysicalValueInstanceCount",
        "bindingBlockedPhysicalValueInstanceCount",
        "stateSemanticCount",
        "excludedCapabilityCount",
        "excludedValueCount",
    }
)
_EXPECTED_STATS = MappingProxyType(
    {
        "fleetDeviceCount": 18,
        "registryModelCount": 16,
        "representedModelCount": 12,
        "entityCount": 107,
        "supportedValueCount": 318,
        "switchCount": 47,
        "selectCount": 50,
        "numberCount": 8,
        "buttonCount": 2,
        "existingOwnerCount": 29,
        "existingOwnerSurfaceCount": 31,
        "dashboardReferencedOwnerCount": 27,
        "factoryEntityCount": 78,
        "naivePhysicalEntityInstanceCount": 139,
        "bindingEligiblePhysicalEntityInstanceCount": 139,
        "bindingBlockedPhysicalEntityInstanceCount": 0,
        "naivePhysicalValueInstanceCount": 395,
        "bindingEligiblePhysicalValueInstanceCount": 395,
        "bindingBlockedPhysicalValueInstanceCount": 0,
        "stateSemanticCount": 103,
        "excludedCapabilityCount": 15,
        "excludedValueCount": 76,
    }
)

Primitive = Union[bool, int, float, str]  # noqa: UP007 - Python 3.9 parser support


class LocalControlEntityContractError(RuntimeError):
    """The bundled public entity contract is not the exact reviewed release."""


class LocalControlEligibilityError(ValueError):
    """The private binding eligibility option is malformed or incompatible."""


def _contract_error(
    message: str = "Bundled Local control entity contract is invalid",
) -> None:
    raise LocalControlEntityContractError(message)


def _eligibility_error(message: str = "Local control eligibility is invalid") -> None:
    raise LocalControlEligibilityError(message)


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _contract_error("Bundled Local control entity contract has duplicate keys")
        result[key] = value
    return result


def _canonicalize(value: Any) -> Any:
    if isinstance(value, list):
        return [_canonicalize(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonicalize(value[key]) for key in sorted(value)}
    return value


def _canonical_compact(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        _canonicalize(dict(value)),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_pretty(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            _canonicalize(dict(value)),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def _positive_int(value: object) -> bool:
    return type(value) is int and 1 <= value <= 9_007_199_254_740_991


def _nonnegative_int(value: object) -> bool:
    return type(value) is int and 0 <= value <= 9_007_199_254_740_991


def _primitive(value: object) -> bool:
    return type(value) in (bool, int, float, str) and not (
        type(value) is float and not math.isfinite(value)
    )


def _primitive_key(value: Primitive) -> tuple[type[Primitive], str]:
    return type(value), _local_request_value(value)


def _local_request_value(value: Primitive) -> str:
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is float and value.is_integer():
        return str(int(value))
    return str(value)


@dataclass(frozen=True)
class LocalControlValueMapping:
    """One exact HA-facing value to Local request string."""

    home_assistant_value: Primitive
    local_request_value: str


@dataclass(frozen=True)
class LocalControlEntityDescriptor:
    """One exact-model Local control surface from the pinned public contract."""

    key: str
    model_id: str
    capability_id: str
    home_assistant_entity_key: str
    label_ko: str
    entity_domain: Literal["switch", "select", "number", "button", "text"]
    input_kind: Literal["boolean", "enum", "number"]
    supported_values: tuple[Primitive, ...]
    value_mappings: tuple[LocalControlValueMapping, ...]
    exact_state_semantic: str | None
    factory_eligible: bool
    existing_owner: bool
    one_shot: bool
    number_min: int | float | None = None
    number_max: int | float | None = None
    number_step: int | float | None = None
    unit: str | None = None
    parameter_schema: str | None = None
    draft_only: bool = False
    request_from: str | None = None
    parameter_pattern: str | None = None

    @property
    def exact_local_request_values(self) -> tuple[str, ...]:
        return tuple(item.local_request_value for item in self.value_mappings)

    @property
    def home_assistant_values(self) -> tuple[Primitive, ...]:
        return tuple(item.home_assistant_value for item in self.value_mappings)


@dataclass(frozen=True)
class LocalControlEntityContract:
    """Immutable current contract and exact-model descriptor indices."""

    root_sha256: str
    revision: str
    descriptors: tuple[LocalControlEntityDescriptor, ...]
    descriptors_by_model: Mapping[str, tuple[LocalControlEntityDescriptor, ...]]
    model_fleet_counts: Mapping[str, int]
    stats: Mapping[str, int]


@dataclass(frozen=True)
class LocalControlBindingEligibility:
    """Validated exact values authorized for one private binding."""

    binding_id: str
    values_by_capability: Mapping[str, tuple[str, ...]]


def _validate_model_rows(
    value: object,
) -> tuple[dict[str, int], dict[str, tuple[int, int]]]:
    if not isinstance(value, list) or len(value) != 16:
        _contract_error("Bundled Local control model inventory is invalid")
    fleet_counts: dict[str, int] = {}
    accounting: dict[str, tuple[int, int]] = {}
    previous = ""
    for row in value:
        if not isinstance(row, dict) or set(row) != _MODEL_KEYS:
            _contract_error()
        model_id = row["modelId"]
        if (
            not isinstance(model_id, str)
            or _OPAQUE_ID.fullmatch(model_id) is None
            or model_id <= previous
            or not _positive_int(row["fleetUnitCount"])
            or not _nonnegative_int(row["includedEntityCount"])
            or not _nonnegative_int(row["excludedCapabilityCount"])
        ):
            _contract_error("Bundled Local control model inventory is invalid")
        previous = model_id
        fleet_counts[model_id] = row["fleetUnitCount"]
        accounting[model_id] = (
            row["includedEntityCount"],
            row["excludedCapabilityCount"],
        )
    if sum(fleet_counts.values()) != 18:
        _contract_error("Bundled Local control fleet inventory is not exactly 18")
    return fleet_counts, accounting


def _validate_number_grid(values: list[Primitive], raw: Mapping[str, Any]) -> bool:
    if set(raw) != {"kind", "min", "max", "step", "unit", "values"}:
        return False
    minimum, maximum, step = raw.get("min"), raw.get("max"), raw.get("step")
    if (
        type(minimum) not in (int, float)
        or type(maximum) not in (int, float)
        or type(step) not in (int, float)
        or not all(math.isfinite(float(item)) for item in (minimum, maximum, step))
        or step <= 0
        or maximum < minimum
        or raw.get("unit") is not None
        and (
            not isinstance(raw["unit"], str)
            or not raw["unit"]
            or len(raw["unit"].encode("utf-8")) > 32
        )
    ):
        return False
    expected: list[int | float] = []
    current = minimum
    # The current contract uses finite decimal/integer grids.  This bounded
    # reconstruction prevents HA Number from accepting unobserved holes.
    for _ in range(_MAX_VALUES_PER_ENTRY + 1):
        if current > maximum:
            break
        expected.append(current)
        current += step
    return values == expected and len(expected) <= _MAX_VALUES_PER_ENTRY


def _descriptor(raw: object, *, editable_labels: bool = False) -> LocalControlEntityDescriptor:
    if not isinstance(raw, dict) or set(raw) != _ENTITY_KEYS:
        _contract_error()
    model_id = raw["modelId"]
    capability_id = raw["capabilityId"]
    key = raw["key"]
    entity_key = raw["homeAssistantEntityKey"]
    domain = raw["entityDomain"]
    supported = raw["supportedValues"]
    input_domain = raw["inputDomain"]
    mappings = raw["valueMappings"]
    if (
        not isinstance(model_id, str)
        or _OPAQUE_ID.fullmatch(model_id) is None
        or not isinstance(capability_id, str)
        or _SEMANTIC_ID.fullmatch(capability_id) is None
        or key != f"{model_id}|{capability_id}"
        or not isinstance(entity_key, str)
        or _ENTITY_KEY.fullmatch(entity_key) is None
        or entity_key != "local_" + re.sub(r"[^a-z0-9]+", "_", capability_id).strip("_")
        or domain not in ("switch", "select", "number", "button")
        or not isinstance(raw["labelKo"], str)
        or not editable_labels and not re.search(r"[\uac00-\ud7a3]", raw["labelKo"])
        or not raw["labelKo"].strip()
        or len(raw["labelKo"].encode("utf-8")) > 256
        or not isinstance(supported, list)
        or not supported
        or len(supported) > _MAX_VALUES_PER_ENTRY
        or not all(_primitive(item) for item in supported)
        or len({_primitive_key(item) for item in supported}) != len(supported)
        or not isinstance(input_domain, dict)
        or not isinstance(mappings, list)
        or len(mappings) != len(supported)
    ):
        _contract_error("Bundled Local control descriptor identity is invalid")

    kind = input_domain.get("kind")
    if kind in ("boolean", "enum"):
        if set(input_domain) != {"kind", "values"}:
            _contract_error()
    elif kind == "number":
        if set(input_domain) != {"kind", "min", "max", "step", "unit", "values"}:
            _contract_error()
    else:
        _contract_error()
    if input_domain.get("values") != supported:
        _contract_error("Bundled Local control input values drifted")
    if (
        kind == "boolean"
        and not all(type(item) is bool for item in supported)
        or kind == "enum"
        and not all(isinstance(item, str) for item in supported)
        or kind == "number"
        and not all(type(item) in (int, float) for item in supported)
    ):
        _contract_error()

    if domain == "switch" and kind != "boolean":
        _contract_error()
    if domain == "number" and (
        kind != "number" or not _validate_number_grid(supported, input_domain)
    ):
        _contract_error("Bundled Local control number grid is incomplete")
    if domain == "select" and kind not in ("enum", "number"):
        _contract_error()
    if domain == "button" and len(supported) != 1:
        _contract_error()

    parsed_mappings: list[LocalControlValueMapping] = []
    for index, mapping in enumerate(mappings):
        if not isinstance(mapping, dict) or set(mapping) != _VALUE_MAPPING_KEYS:
            _contract_error()
        local_value = mapping["localRequestValue"]
        if (
            not isinstance(local_value, str)
            or not local_value
            or len(local_value.encode("utf-8")) > 2048
            or local_value != _local_request_value(supported[index])
        ):
            _contract_error("Bundled Local control value mapping is invalid")
        expected_ha_value: Primitive
        if domain == "switch":
            expected_ha_value = "on" if supported[index] is True else "off"
        elif domain == "button":
            expected_ha_value = "press"
        elif domain == "select":
            expected_ha_value = _local_request_value(supported[index])
        else:
            expected_ha_value = supported[index]
        if editable_labels and domain == "select":
            # Display text is editable; the exact reported/request value pair
            # above still determines the command. Never encode the UI label.
            label = mapping["homeAssistantValue"]
            if (not isinstance(label, str) or not label.strip()
                    or len(label.encode("utf-8")) > 256
                    or any(ord(char) < 32 for char in label)
                    or any(item.home_assistant_value == label for item in parsed_mappings)):
                _contract_error("Local control choice labels are invalid")
            expected_ha_value = label
        if mapping["homeAssistantValue"] != expected_ha_value:
            _contract_error("Bundled Local control HA value mapping drifted")
        parsed_mappings.append(
            LocalControlValueMapping(
                home_assistant_value=expected_ha_value,
                local_request_value=local_value,
            )
        )

    materialization = raw["materializationPolicy"]
    owner = raw["existingHomeAssistantOwner"]
    fallback = raw["fallbackPolicy"]
    default_policy = raw["defaultEnabledPolicy"]
    if (
        not isinstance(materialization, dict)
        or set(materialization) != _MATERIALIZATION_KEYS
        or not isinstance(owner, dict)
        or set(owner) != _OWNER_KEYS
        or not isinstance(fallback, dict)
        or set(fallback) != _FALLBACK_KEYS
        or not isinstance(default_policy, dict)
        or set(default_policy) != _DEFAULT_POLICY_KEYS
    ):
        _contract_error()
    factory = materialization["factoryEligible"]
    owner_exists = owner["exists"]
    if (
        type(factory) is not bool
        or type(owner_exists) is not bool
        or factory is owner_exists
    ):
        _contract_error("Bundled Local control owner policy is invalid")
    if materialization != {
        "factoryEligible": factory,
        "action": "create-disabled-entity" if factory else "reuse-existing-owner",
        "duplicatePrevention": "model-and-capability-owner-key",
        "privateBindingEligibility": "required-before-create-or-local-route",
    }:
        _contract_error()
    surfaces = owner["surfaces"]
    if not isinstance(surfaces, list) or bool(surfaces) is not owner_exists:
        _contract_error()
    for surface in surfaces:
        if (
            not isinstance(surface, dict)
            or set(surface) != _OWNER_SURFACE_KEYS
            or surface["domain"] not in ("climate", "switch", "select", "button")
            or not isinstance(surface["entityKey"], str)
            or not surface["entityKey"]
            or type(surface["dashboardReferenced"]) is not bool
            or surface["localWriteCoverage"]
            not in ("all-supported-values", "on-only-with-cloud-off")
        ):
            _contract_error()
    if fallback != {
        "prewireRefusal": (
            "cloud-fallback-through-existing-owner"
            if owner_exists
            else "remain-unavailable-without-cloud-owner"
        ),
        "postwireFailure": "surface-error-never-cloud-retry",
    }:
        _contract_error()
    if (
        default_policy
        != {
            "appliesWhenMaterializedAsNewEntity": True,
            "enabled": False,
            "reason": "new-control-opt-in-after-pilot",
        }
    ):
        _contract_error()

    state_semantic = raw["exactStateSemantic"]
    if state_semantic is not None and (
        not isinstance(state_semantic, str)
        or _SEMANTIC_ID.fullmatch(state_semantic) is None
    ):
        _contract_error()
    if factory and state_semantic is None:
        _contract_error("New Local control owner lacks exact state acknowledgement")
    if (
        raw["safetyClass"] not in ("low", "guarded")
        or raw["commandSemantics"]
        != ("one-shot" if domain == "button" else "set-state")
        or raw["parameterless"] is not (domain == "button")
        or raw["oneShot"] is not (domain == "button")
        or raw["exposure"] != "config"
        or raw["entityCategory"] != "config"
        or raw["diagnostic"] is not False
    ):
        _contract_error()
    if factory and domain == "switch" and supported != [False, True]:
        _contract_error("A new Local switch lacks both safe polarities")

    return LocalControlEntityDescriptor(
        key=key,
        model_id=model_id,
        capability_id=capability_id,
        home_assistant_entity_key=entity_key,
        label_ko=raw["labelKo"],
        entity_domain=domain,
        input_kind=kind,
        supported_values=tuple(supported),
        value_mappings=tuple(parsed_mappings),
        exact_state_semantic=state_semantic,
        factory_eligible=factory,
        existing_owner=owner_exists,
        one_shot=domain == "button",
        number_min=input_domain.get("min"),
        number_max=input_domain.get("max"),
        number_step=input_domain.get("step"),
        unit=input_domain.get("unit"),
    )


def _validate_exclusions(
    capabilities: object,
    values: object,
    descriptors: tuple[LocalControlEntityDescriptor, ...],
    model_ids: set[str],
) -> tuple[dict[str, int], int]:
    if not isinstance(capabilities, list) or not isinstance(values, list):
        _contract_error()
    included = {
        (item.model_id, item.capability_id, value)
        for item in descriptors
        for value in item.exact_local_request_values
    }
    per_model: dict[str, int] = {model_id: 0 for model_id in model_ids}
    previous = ""
    capability_keys: set[str] = set()
    for row in capabilities:
        if not isinstance(row, dict) or set(row) != _EXCLUDED_CAPABILITY_KEYS:
            _contract_error()
        model_id, capability_id, key = row["modelId"], row["capabilityId"], row["key"]
        if (
            model_id not in model_ids
            or not isinstance(capability_id, str)
            or _SEMANTIC_ID.fullmatch(capability_id) is None
            or key != f"{model_id}|{capability_id}"
            or key <= previous
            or key in capability_keys
            or row["reason"]
            not in (
                "non-exact-evidence",
                "prohibited-by-default",
                "unresolved-domain",
                "unverifiable-state-ack",
            )
        ):
            _contract_error("Bundled Local control capability exclusions are invalid")
        previous = key
        capability_keys.add(key)
        per_model[model_id] += 1
    previous = ""
    value_keys: set[str] = set()
    forbidden_zero_evidence_values: set[tuple[str, str, str]] = set()
    for row in values:
        if not isinstance(row, dict) or set(row) != _EXCLUDED_VALUE_KEYS:
            _contract_error()
        model_id, capability_id, key, value = (
            row["modelId"],
            row["capabilityId"],
            row["key"],
            row["value"],
        )
        if (
            model_id not in model_ids
            or not isinstance(capability_id, str)
            or _SEMANTIC_ID.fullmatch(capability_id) is None
            or not isinstance(key, str)
            or not key.startswith(f"{model_id}|{capability_id}|")
            or key <= previous
            or key in value_keys
            or not _primitive(value)
            or row["reason"]
            not in (
                "send-gate-or-codec-refused",
                "unsafe-power-awaiting-current-raw-proof",
                "unverifiable-state-ack",
                "zero-observation-evidence-absent",
            )
            or (model_id, capability_id, _local_request_value(value)) in included
        ):
            _contract_error("Bundled Local control value exclusions are invalid")
        previous = key
        value_keys.add(key)
        if row["reason"] == "zero-observation-evidence-absent":
            forbidden_zero_evidence_values.add(
                (model_id, capability_id, _local_request_value(value))
            )
    if forbidden_zero_evidence_values != _FORBIDDEN_ZERO_EVIDENCE_VALUES:
        _contract_error("Bundled Local control zero-evidence exclusions drifted")
    return per_model, len(value_keys)


def _load_local_control_entity_contract(
    raw: bytes, digest: bytes
) -> LocalControlEntityContract:
    """Validate exact canonical bytes, pins, closed keys and accounting."""
    if (
        not isinstance(raw, bytes)
        or not raw
        or len(raw) > _MAX_ARTIFACT_BYTES
        or digest
        != f"{EXPECTED_LOCAL_CONTROL_ENTITY_CONTRACT_ROOT_SHA256}\n".encode("ascii")
    ):
        _contract_error("Bundled Local control entity contract digest is invalid")
    try:
        artifact = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=lambda _value: _contract_error(),
        )
    except LocalControlEntityContractError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
    ) as err:
        raise LocalControlEntityContractError(
            "Bundled Local control entity contract is not canonical JSON"
        ) from err
    if not isinstance(artifact, dict) or set(artifact) != _ARTIFACT_KEYS:
        _contract_error()
    if raw != _canonical_pretty(artifact):
        _contract_error("Bundled Local control entity contract bytes are not canonical")
    unhashed = {
        key: value
        for key, value in artifact.items()
        if key not in {"revision", "rootSha256"}
    }
    computed_root = hashlib.sha256(
        _HASH_DOMAIN + _canonical_compact(unhashed)
    ).hexdigest()
    if (
        artifact["schemaVersion"] != LOCAL_CONTROL_ENTITY_CONTRACT_SCHEMA_VERSION
        or artifact["authority"] != "verified-v3-semantic-checkpoint"
        or artifact["deploymentAuthority"] is not True
        or artifact["fleetDeviceCount"] != 18
        or artifact["rootSha256"] != computed_root
        or computed_root != EXPECTED_LOCAL_CONTROL_ENTITY_CONTRACT_ROOT_SHA256
        or artifact["revision"] != f"ha-local-control-entity-v1:{computed_root[:16]}"
    ):
        _contract_error("Bundled Local control entity contract pins do not agree")
    if (
        not isinstance(artifact["promotionDecision"], dict)
        or set(artifact["promotionDecision"]) != _PROMOTION_KEYS
        or artifact["promotionDecision"]
        != {
            "revision": 1,
            "decision": "approved-for-disabled-home-assistant-entity-materialization",
            "evidenceAuthority": "exact-codec-send-gate-and-verified-prestate-endpoint",
            "defaultEnabled": False,
            "pilotRequiredBeforeEnablement": True,
            "privateBindingEligibilityRequired": True,
            "requiresPrivateTargetAuthorityGate": True,
        }
    ):
        _contract_error("Bundled Local control promotion policy drifted")
    fleet_counts, model_accounting = _validate_model_rows(artifact["models"])

    raw_entities = artifact["entities"]
    if not isinstance(raw_entities, list) or len(raw_entities) != 107:
        _contract_error()
    descriptors = tuple(_descriptor(item) for item in raw_entities)
    if [item.key for item in descriptors] != sorted(
        item.key for item in descriptors
    ) or len({item.key for item in descriptors}) != len(descriptors):
        _contract_error("Bundled Local control descriptors are not uniquely sorted")
    if any(item.model_id not in fleet_counts for item in descriptors):
        _contract_error()
    per_model_included = {
        model_id: sum(item.model_id == model_id for item in descriptors)
        for model_id in fleet_counts
    }
    per_model_excluded, excluded_value_count = _validate_exclusions(
        artifact["excludedCapabilities"],
        artifact["excludedValues"],
        descriptors,
        set(fleet_counts),
    )
    if (
        any(
            model_accounting[model_id]
            != (per_model_included[model_id], per_model_excluded[model_id])
            for model_id in fleet_counts
        )
        # The frozen v3 checkpoint's statistic predates the four additive
        # zero-observation audit rows. Their exact set is validated above, so
        # accept precisely 76 counted exclusions plus those four explicit rows.
        or excluded_value_count
        != 76 + len(_FORBIDDEN_ZERO_EVIDENCE_VALUES)
    ):
        _contract_error("Bundled Local control model accounting drifted")

    stats = artifact["stats"]
    if (
        not isinstance(stats, dict)
        or set(stats) != _STATS_KEYS
        or any(not _nonnegative_int(value) for value in stats.values())
        or stats != dict(_EXPECTED_STATS)
    ):
        _contract_error("Bundled Local control statistics drifted")
    actual = {
        "entityCount": len(descriptors),
        "supportedValueCount": sum(len(item.supported_values) for item in descriptors),
        "switchCount": sum(item.entity_domain == "switch" for item in descriptors),
        "selectCount": sum(item.entity_domain == "select" for item in descriptors),
        "numberCount": sum(item.entity_domain == "number" for item in descriptors),
        "buttonCount": sum(item.entity_domain == "button" for item in descriptors),
        "existingOwnerCount": sum(item.existing_owner for item in descriptors),
        "factoryEntityCount": sum(item.factory_eligible for item in descriptors),
        "stateSemanticCount": sum(
            item.exact_state_semantic is not None for item in descriptors
        ),
    }
    if any(stats[key] != value for key, value in actual.items()):
        _contract_error("Bundled Local control descriptor accounting drifted")
    if any(
        "|power|" in value
        for item in descriptors
        for value in item.exact_local_request_values
    ):
        _contract_error("Unsafe POWER placeholder entered Local entity authority")

    by_model = MappingProxyType(
        {
            model_id: tuple(item for item in descriptors if item.model_id == model_id)
            for model_id in fleet_counts
        }
    )
    return LocalControlEntityContract(
        root_sha256=computed_root,
        revision=artifact["revision"],
        descriptors=descriptors,
        descriptors_by_model=by_model,
        model_fleet_counts=MappingProxyType(dict(fleet_counts)),
        stats=MappingProxyType(dict(stats)),
    )


_CONTRACT_CACHE: LocalControlEntityContract | None = None
_CONTRACT_LOCK = threading.Lock()


def load_local_control_entity_contract(path: Path | None = None) -> LocalControlEntityContract:
    """Use DB controls only for explicitly piloted models."""
    feature_database = (
        Path(__file__).resolve().parents[2] / "my_lg_features.sqlite3"
        if path is None else path
    )
    if not feature_database.is_file():
        return _load_bundled_local_control_entity_contract()
    try:
        connection = sqlite3.connect(f"{feature_database.as_uri()}?mode=ro", uri=True)
        try:
            if (
                connection.execute("PRAGMA application_id").fetchone()[0] != 0x4C474646
                or connection.execute("PRAGMA user_version").fetchone()[0] != 2
            ):
                _contract_error("Local feature database layout is invalid")
            rollout = connection.execute(
                "SELECT model_id, enabled FROM model_rollout"
            ).fetchall()
            selected = frozenset(row[0] for row in rollout if row[1])
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise LocalControlEntityContractError(
            "Local feature database rollout is unavailable"
        ) from error
    if not selected:
        return _load_bundled_local_control_entity_contract()
    dynamic = load_local_control_entity_contract_from_database(feature_database)
    if len(selected) == len(rollout):
        return dynamic
    baseline = _load_bundled_local_control_entity_contract()
    descriptors = tuple(
        descriptor for descriptor in baseline.descriptors
        if descriptor.model_id not in selected
    ) + tuple(
        descriptor for descriptor in dynamic.descriptors
        if descriptor.model_id in selected
    )
    by_model = MappingProxyType({
        model_id: tuple(descriptor for descriptor in descriptors if descriptor.model_id == model_id)
        for model_id in sorted(set(baseline.descriptors_by_model) | selected)
    })
    digest = hashlib.sha256(json.dumps(
        [descriptor.key for descriptor in descriptors], separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return LocalControlEntityContract(
        root_sha256=digest,
        revision=f"feature-db:{digest[:16]}",
        descriptors=descriptors,
        descriptors_by_model=by_model,
        model_fleet_counts=MappingProxyType({
            **{model_id: count for model_id, count in baseline.model_fleet_counts.items()
               if model_id not in selected},
            **{model_id: 1 for model_id in by_model if model_id in selected},
        }),
        stats=MappingProxyType({"entityCount": len(descriptors)}),
    )


def _load_bundled_local_control_entity_contract() -> LocalControlEntityContract:
    """Cache the old control surface for models not yet on the database."""
    global _CONTRACT_CACHE
    cached = _CONTRACT_CACHE
    if cached is not None:
        return cached
    with _CONTRACT_LOCK:
        cached = _CONTRACT_CACHE
        if cached is not None:
            return cached
        directory = Path(__file__).resolve().parent
        try:
            loaded = _load_local_control_entity_contract(
                (directory / LOCAL_CONTROL_ENTITY_CONTRACT_FILENAME).read_bytes(),
                (
                    directory / LOCAL_CONTROL_ENTITY_CONTRACT_DIGEST_FILENAME
                ).read_bytes(),
            )
        except OSError as err:
            raise LocalControlEntityContractError(
                "Bundled Local control entity contract is unavailable"
            ) from err
        _CONTRACT_CACHE = loaded
        return loaded


def load_local_control_entity_contract_from_database(
    path: Path,
) -> LocalControlEntityContract:
    """Read model/control declarations without a fleet-wide release hash gate.

    The command router remains the only sender. A database entry describes an
    existing codec's HA surface; it does not contain executable wire payloads.
    """
    if not path.is_file() or path.is_symlink():
        raise LocalControlEntityContractError("Local feature database is unavailable")
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            if (
                connection.execute("PRAGMA application_id").fetchone()[0]
                != 0x4C474646
                or connection.execute("PRAGMA user_version").fetchone()[0] != 2
            ):
                _contract_error("Local feature database layout is invalid")
            rows = connection.execute(
                "SELECT f.model_id, f.feature_id, f.definition_json FROM features f "
                "WHERE f.channel = 'control-entity' AND f.enabled = 1 "
                "AND NOT EXISTS (SELECT 1 FROM features d WHERE d.model_id=f.model_id "
                "AND d.feature_id=f.feature_id AND d.enabled=0 "
                "AND d.channel IN ('control-entity', 'confirmed-control')) "
                "ORDER BY f.model_id, f.feature_id"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise LocalControlEntityContractError(
            "Local feature database could not be read"
        ) from error
    descriptors: list[LocalControlEntityDescriptor] = []
    for row in rows:
        try:
            definition = json.loads(row["definition_json"])
            if (
                not isinstance(definition, dict)
                or definition.get("modelId") != row["model_id"]
                or definition.get("capabilityId") != row["feature_id"]
            ):
                raise ValueError("Control feature identity is invalid")
            # The bridge loads trusted model code; HA only consumes the menu.
            # A handler filename is not an entity attribute or a new pin.
            presentation = {k: v for k, v in definition.items() if k != "runtimeHandler"}
            draft = presentation.pop('draftOnly', False)
            request_from = presentation.pop('requestFrom', None)
            pattern = presentation.pop('parameterPattern', None)
            if type(draft) is not bool:
                raise ValueError('draftOnly must be boolean')
            if draft or request_from is not None:
                if draft and request_from is not None:
                    raise ValueError('Draft and action must be separate')
                if not isinstance(pattern, str) or not 1 <= len(pattern) <= 256:
                    raise ValueError('A bounded parameter pattern is required')
                re.compile(pattern)
                if draft:
                    if presentation.get('entityDomain') != 'text':
                        raise ValueError('A draft must be a text input')
                    # Reuse the scalar presentation validator; this row never
                    # dispatches its example value or claims appliance state.
                    presentation['entityDomain'] = 'select'
                elif (presentation.get('entityDomain') != 'button'
                      or not isinstance(request_from, str)
                      or _SEMANTIC_ID.fullmatch(request_from) is None):
                    raise ValueError('An action must reference a named draft')
            elif pattern is not None:
                raise ValueError('Parameter pattern has no draft/action')
            descriptor = _descriptor(presentation, editable_labels=True)
            descriptors.append(replace(descriptor,
                entity_domain='text' if draft else descriptor.entity_domain,
                draft_only=draft, request_from=request_from, parameter_pattern=pattern))
        except (LocalControlEntityContractError, TypeError, ValueError, KeyError):
            # A bad row cannot remove another model's already working controls.
            logging.getLogger(__name__).warning(
                "Local feature database skipped invalid control %s for model %s",
                row["feature_id"], row["model_id"],
            )
            continue
    by_model = MappingProxyType({
        model_id: tuple(item for item in descriptors if item.model_id == model_id)
        for model_id in sorted({item.model_id for item in descriptors})
    })
    digest = hashlib.sha256(
        json.dumps(
            [item.key for item in descriptors], separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return LocalControlEntityContract(
        root_sha256=digest,  # Diagnostic receipt only in database mode.
        revision=f"feature-db:{digest[:16]}",
        descriptors=tuple(descriptors),
        descriptors_by_model=by_model,
        model_fleet_counts=MappingProxyType({model_id: 1 for model_id in by_model}),
        stats=MappingProxyType({"entityCount": len(descriptors)}),
    )


def resolve_local_control_binding_eligibility(
    options: Mapping[str, object],
    contract: LocalControlEntityContract,
    binding_models: Mapping[str, str],
) -> Mapping[str, LocalControlBindingEligibility]:
    """Derive supported controls from registered models, or honor an explicit scope.

    Registration needs no second installer-maintained copy of the model's
    capability list. Unknown models get no controls. Explicit legacy scopes
    remain restrictive; stale/invalid scopes never fall back to automatic mode.
    This creates owners, not commands: routing, value validation and command
    acknowledgement remain unchanged, as do disabled-by-default policies.
    """
    value = options.get(LOCAL_CONTROL_ELIGIBILITY_OPTION)
    if contract.revision.startswith("feature-db:") and value is not None:
        # Legacy private scopes remain restrictive, but their old release hash
        # no longer disables an unrelated model when one DB row changes.
        if not isinstance(value, dict) or not isinstance(value.get("bindings"), list):
            _eligibility_error()
        scoped: dict[str, LocalControlBindingEligibility] = {}
        for raw_binding in value["bindings"]:
            if not isinstance(raw_binding, dict):
                _eligibility_error()
            binding_id = raw_binding.get("binding_id")
            if (
                not isinstance(binding_id, str)
                or binding_id not in binding_models
                or binding_id in scoped
                or not isinstance(raw_binding.get("entries"), list)
            ):
                _eligibility_error()
            descriptors = {
                item.capability_id: item
                for item in contract.descriptors_by_model.get(binding_models[binding_id], ())
            }
            allowed: dict[str, tuple[str, ...]] = {}
            for entry in raw_binding["entries"]:
                if not isinstance(entry, dict):
                    _eligibility_error()
                capability_id = entry.get("capability_id")
                exact_values = entry.get("exact_values")
                descriptor = descriptors.get(capability_id)
                if descriptor is None:
                    continue
                if (
                    not isinstance(exact_values, list)
                    or not exact_values
                    or any(not isinstance(item, str) for item in exact_values)
                ):
                    _eligibility_error()
                intersection = tuple(
                    item for item in descriptor.exact_local_request_values
                    if item in exact_values
                )
                if intersection:
                    allowed[capability_id] = intersection
            scoped[binding_id] = LocalControlBindingEligibility(
                binding_id=binding_id,
                values_by_capability=MappingProxyType(allowed),
            )
        return MappingProxyType(scoped)
    if LOCAL_CONTROL_ELIGIBILITY_OPTION not in options:
        automatic: dict[str, LocalControlBindingEligibility] = {}
        for binding_id, model_id in binding_models.items():
            descriptors = contract.descriptors_by_model.get(model_id, ())
            if not descriptors:
                continue
            if not isinstance(binding_id, str) or _BINDING_ID.fullmatch(binding_id) is None:
                _eligibility_error()
            automatic[binding_id] = LocalControlBindingEligibility(
                binding_id=binding_id,
                values_by_capability=MappingProxyType({
                    descriptor.capability_id: descriptor.exact_local_request_values
                    for descriptor in descriptors
                }),
            )
        return MappingProxyType(automatic)
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "contract_sha256",
        "checkpoint_revision",
        "checkpoint_sha256",
        "target_authority_revision",
        "target_authority_sha256",
        "bindings",
    }:
        _eligibility_error()
    if value["schema_version"] != LOCAL_CONTROL_ELIGIBILITY_SCHEMA_VERSION:
        _eligibility_error()
    if (
        value["contract_sha256"] != contract.root_sha256
        or value["checkpoint_revision"] != EXPECTED_LOCAL_CONTROL_CHECKPOINT_REVISION
        or value["checkpoint_sha256"] != EXPECTED_LOCAL_CONTROL_CHECKPOINT_SHA256
        or value["target_authority_revision"]
        != EXPECTED_LOCAL_CONTROL_TARGET_AUTHORITY_REVISION
        or value["target_authority_sha256"]
        != EXPECTED_LOCAL_CONTROL_TARGET_AUTHORITY_SHA256
    ):
        # The private document is a materialization/value-membership hint, not
        # outbound send authority. Any stale or mixed pin disables the entire
        # Local generic surface atomically while independent reads stay alive.
        return MappingProxyType({})

    raw_bindings = value["bindings"]
    if not isinstance(raw_bindings, list) or not isinstance(binding_models, Mapping):
        _eligibility_error()

    raw_binding_ids = [
        row.get("binding_id") if isinstance(row, dict) else None for row in raw_bindings
    ]
    if any(
        not isinstance(binding_id, str)
        or _BINDING_ID.fullmatch(binding_id) is None
        or binding_id not in binding_models
        for binding_id in raw_binding_ids
    ):
        _eligibility_error()
    if raw_binding_ids != sorted(set(raw_binding_ids)):
        _eligibility_error()

    parsed: dict[str, LocalControlBindingEligibility] = {}
    for raw_binding in raw_bindings:
        if not isinstance(raw_binding, dict) or set(raw_binding) != {
            "binding_id",
            "entries",
        }:
            _eligibility_error()
        binding_id = raw_binding["binding_id"]
        entries = raw_binding["entries"]
        if (
            not isinstance(binding_id, str)
            or _BINDING_ID.fullmatch(binding_id) is None
            or binding_id in parsed
            or binding_id not in binding_models
            or not isinstance(entries, list)
        ):
            _eligibility_error()
        model_id = binding_models[binding_id]
        if model_id not in contract.model_fleet_counts:
            _eligibility_error()
        descriptors = contract.descriptors_by_model.get(model_id, ())
        if len(entries) != len(descriptors):
            # Validate this selected appliance's capabilities, not the size of
            # the house. Omitted bindings get no generic owners or permission;
            # adding/skipping another appliance cannot disable this one.
            _eligibility_error()
        values_by_capability: dict[str, tuple[str, ...]] = {}
        for entry, descriptor in zip(entries, descriptors):
            if not isinstance(entry, dict) or set(entry) != {
                "capability_id",
                "exact_values",
            }:
                _eligibility_error()
            capability_id = entry["capability_id"]
            exact_values = entry["exact_values"]
            if (
                capability_id != descriptor.capability_id
                or capability_id in values_by_capability
                or not isinstance(exact_values, list)
                or not exact_values
                or len(exact_values) > _MAX_VALUES_PER_ENTRY
                or any(
                    not isinstance(item, str)
                    or not item
                    or len(item.encode("utf-8")) > 2048
                    for item in exact_values
                )
                or len(set(exact_values)) != len(exact_values)
                or tuple(exact_values) != descriptor.exact_local_request_values
            ):
                _eligibility_error()
            values_by_capability[capability_id] = tuple(exact_values)
        parsed[binding_id] = LocalControlBindingEligibility(
            binding_id=binding_id,
            values_by_capability=MappingProxyType(values_by_capability),
        )

    return MappingProxyType(parsed)


def eligible_factory_descriptors(
    contract: LocalControlEntityContract,
    eligibility: Mapping[str, LocalControlBindingEligibility],
    *,
    binding_id: str,
    model_id: str,
    domain: Literal["switch", "select", "number", "button", "text"] | None = None,
) -> tuple[LocalControlEntityDescriptor, ...]:
    """Return only exact private/public intersections for one physical binding."""
    binding = eligibility.get(binding_id)
    if binding is None:
        return ()
    descriptors = contract.descriptors_by_model.get(model_id, ())
    return tuple(
        descriptor
        for descriptor in descriptors
        if descriptor.factory_eligible
        and (domain is None or descriptor.entity_domain == domain)
        and binding.values_by_capability.get(descriptor.capability_id)
        == descriptor.exact_local_request_values
    )


def local_control_value_authorized(
    contract: LocalControlEntityContract,
    eligibility: Mapping[str, LocalControlBindingEligibility],
    *,
    binding_id: str,
    model_id: str,
    capability_id: str,
    local_request_value: str,
) -> bool:
    """Check the private per-value gate immediately before a Local route."""
    binding = eligibility.get(binding_id)
    if binding is None:
        return False
    descriptors = contract.descriptors_by_model.get(model_id, ())
    descriptor = next(
        (item for item in descriptors if item.capability_id == capability_id),
        None,
    )
    allowed = binding.values_by_capability.get(capability_id)
    if descriptor is not None and descriptor.draft_only:
        return False  # UI-only draft is never a device command.
    if descriptor is not None and descriptor.request_from is not None:
        draft = next((d for d in descriptors if d.capability_id == descriptor.request_from), None)
        return (allowed is not None and allowed == descriptor.exact_local_request_values
                and draft is not None and draft.draft_only
                and isinstance(local_request_value, str) and len(local_request_value) <= 160
                and re.fullmatch(draft.parameter_pattern, local_request_value) is not None
                and re.fullmatch(descriptor.parameter_pattern, local_request_value) is not None)
    if descriptor is not None and descriptor.parameter_schema is not None:
        from .local_vacuum_reservation import MODEL, SCHEDULE, SCHEMA, is_canonical_schedule
        from .local_water_dnd import MODEL as WATER_MODEL, WINDOW, SCHEMA as WATER_SCHEMA, is_canonical_window
        from .local_water_parameters import SCHEMAS as WATER_SCHEMAS, is_canonical_parameter
        from .local_washer_options import MODEL as WASHER_MODEL, CAPABILITY as WASHER_PROGRAM, SCHEMA as WASHER_SCHEMA, is_canonical_program
        from .local_dryer_options import MODEL as DRYER_MODEL, CAPABILITY as DRYER_PROGRAM, SCHEMA as DRYER_SCHEMA, is_canonical_program as is_dryer_program
        if model_id == DRYER_MODEL and capability_id == DRYER_PROGRAM and descriptor.parameter_schema == DRYER_SCHEMA:
            return (allowed == descriptor.exact_local_request_values and allowed is not None
                    and is_dryer_program(local_request_value))
        from .local_styler_options import MODEL as STYLER_MODEL, CAPABILITY as STYLER_PROGRAM, SCHEMA as STYLER_SCHEMA, is_canonical_program as is_styler_program
        if model_id == STYLER_MODEL and capability_id == STYLER_PROGRAM and descriptor.parameter_schema == STYLER_SCHEMA:
            return (allowed == descriptor.exact_local_request_values and allowed is not None
                    and is_styler_program(local_request_value))
        from .local_styler_dnd import RESERVATION as STYLER_DND_RESERVATION, SCHEMA as STYLER_DND_SCHEMA, is_canonical_reservation
        if model_id == STYLER_MODEL and capability_id == STYLER_DND_RESERVATION and descriptor.parameter_schema == STYLER_DND_SCHEMA:
            return (allowed == descriptor.exact_local_request_values and allowed is not None
                    and is_canonical_reservation(local_request_value))
        if model_id == WASHER_MODEL and capability_id == WASHER_PROGRAM and descriptor.parameter_schema == WASHER_SCHEMA:
            return (allowed == descriptor.exact_local_request_values and allowed is not None
                    and is_canonical_program(local_request_value))
        if model_id == WATER_MODEL and capability_id in WATER_SCHEMAS and descriptor.parameter_schema == WATER_SCHEMAS[capability_id]:
            return (allowed == descriptor.exact_local_request_values and allowed is not None
                    and is_canonical_parameter(capability_id, local_request_value))
        if model_id == WATER_MODEL and capability_id == WINDOW and descriptor.parameter_schema == WATER_SCHEMA:
            return (allowed == descriptor.exact_local_request_values and allowed is not None
                    and is_canonical_window(local_request_value))
        return (model_id == MODEL and capability_id == SCHEDULE and descriptor.parameter_schema == SCHEMA
                and allowed == descriptor.exact_local_request_values and allowed is not None
                and is_canonical_schedule(local_request_value))
    return (
        descriptor is not None
        and allowed is not None
        and local_request_value in allowed
        and local_request_value in descriptor.exact_local_request_values
    )


def local_control_capability_authorized(
    contract: LocalControlEntityContract,
    eligibility: Mapping[str, LocalControlBindingEligibility],
    *,
    binding_id: str,
    model_id: str,
    capability_id: str,
) -> bool:
    """Prove this exact binding owns a capability with retained exact evidence.

    The exact values remain evidence that the binding/model/capability target is
    real.  A separately pinned composite-domain artifact may authorize new
    component combinations; this predicate never widens scalar capabilities.
    """
    binding = eligibility.get(binding_id)
    if binding is None:
        return False
    descriptor = next(
        (
            item
            for item in contract.descriptors_by_model.get(model_id, ())
            if item.capability_id == capability_id
        ),
        None,
    )
    allowed = binding.values_by_capability.get(capability_id)
    return (
        descriptor is not None
        and allowed is not None
        and tuple(allowed) == descriptor.exact_local_request_values
        and bool(allowed)
    )


def local_control_authorized_values(
    contract: LocalControlEntityContract,
    eligibility: Mapping[str, LocalControlBindingEligibility],
    *,
    binding_id: str,
    model_id: str,
    capability_id: str,
) -> tuple[str, ...]:
    """Return the exact private/public value intersection for one capability."""
    binding = eligibility.get(binding_id)
    if binding is None:
        return ()
    descriptor = next(
        (
            item
            for item in contract.descriptors_by_model.get(model_id, ())
            if item.capability_id == capability_id
        ),
        None,
    )
    allowed = binding.values_by_capability.get(capability_id)
    if (
        descriptor is None
        or allowed is None
        or tuple(allowed) != descriptor.exact_local_request_values
    ):
        return ()
    return descriptor.exact_local_request_values
