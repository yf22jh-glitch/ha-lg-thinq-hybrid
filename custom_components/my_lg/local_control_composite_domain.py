"""Pinned composable argument domains for Rethink Local climate controls.

Observed whole frames remain private per-binding evidence.  This additive
public artifact answers a different question: which independently decoded
arguments can the verified composite encoder combine for an already-attested
binding/capability.  It contains no device or binding identity.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .local_control_contract import (
    EXPECTED_LOCAL_CONTROL_TARGET_AUTHORITY_REVISION,
    EXPECTED_LOCAL_CONTROL_TARGET_AUTHORITY_SHA256,
)

LOCAL_CONTROL_COMPOSITE_DOMAIN_FILENAME = (
    "home-assistant-local-control-composite-domain.v1.json"
)
LOCAL_CONTROL_COMPOSITE_DOMAIN_DIGEST_FILENAME = (
    "home-assistant-local-control-composite-domain.v1.sha256"
)
LOCAL_CONTROL_COMPOSITE_DOMAIN_SCHEMA_VERSION = 1
EXPECTED_LOCAL_CONTROL_COMPOSITE_DOMAIN_ROOT_SHA256 = (
    "f5c7d3ce49c8bdad27b596793a7743fa6db8a8af432ef5def920bcfb4d7e4ded"
)

_HASH_DOMAIN = b"lg-rethink-local/home-assistant-local-control-composite-domain/v1\0"
_MAX_ARTIFACT_BYTES = 64 * 1024
_MAX_TEXT_BYTES = 128
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^ha-local-control-composite-domain-v1:[0-9a-f]{16}$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_CAPABILITY_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_TEMPERATURE_TUPLE = re.compile(
    r"^([^|]+)\|([^|]+)\|(-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?)C$"
)
_COMFORT_TUPLE = re.compile(r"^(auto)\|([^|]+)\|comfort:(-2|-1|0|1|2)$")
_PRIVATE_FIELD_MARKERS = (
    b"binding_id",
    b"evidence_role",
    b"observation_ref",
    b"frame_sha256",
    b"target_role",
    b"source_path",
    b"observedValues",
)


class LocalControlCompositeDomainError(RuntimeError):
    """The bundled composable control-domain artifact is invalid."""


def _invalid(message: str = "Bundled Local composite control domain is invalid") -> None:
    raise LocalControlCompositeDomainError(message)


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _invalid("Bundled Local composite control domain has duplicate keys")
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
    # The bridge hashes a fully canonical body, then appends the derived
    # revision and root fields.  Reproduce that exact serializer here instead
    # of sorting those two derived fields back into the body: byte identity
    # with the bridge artifact is part of the authority boundary.
    body = {
        key: value[key]
        for key in value
        if key not in {"revision", "rootSha256"}
    }
    serialized = {
        **_canonicalize(body),
        "revision": value.get("revision"),
        "rootSha256": value.get("rootSha256"),
    }
    return (
        json.dumps(
            serialized,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def _finite_number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(float(value))


def _text_values(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(
            not isinstance(item, str)
            or not item
            or len(item.encode("utf-8")) > _MAX_TEXT_BYTES
            for item in value
        )
        or value != sorted(value)
        or len(value) != len(set(value))
    ):
        _invalid("Bundled Local composite enum domain is invalid")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class LocalControlCompositeInterlock:
    """One reviewed cross-field requirement."""

    when_component: str
    equals: str
    require_component: str
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LocalControlCompositeTargetRange:
    """One mode's exact numeric target grid."""

    min_c: float
    max_c: float
    step_c: float


@dataclass(frozen=True, slots=True)
class LocalControlCompositeComfortRange:
    """The exact, unitless AUTO comfort-preference grid."""

    min_step: int
    max_step: int
    step: int
    applies_to_modes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LocalControlCompositeInputDomain:
    """One exact-model composable climate command domain."""

    modes: tuple[str, ...]
    fans: tuple[str, ...]
    target_ranges_by_mode: Mapping[str, LocalControlCompositeTargetRange]
    comfort_preference: LocalControlCompositeComfortRange
    interlocks: tuple[LocalControlCompositeInterlock, ...]
    # Additive release metadata; range describes the carried current value,
    # not permission to edit temperature in these modes.
    preserve_setpoint_modes: tuple[str, ...] = ()

    def target_range(self, mode: str) -> LocalControlCompositeTargetRange | None:
        """Return the authorized numeric grid for one decoded mode."""
        return self.target_ranges_by_mode.get(mode)

    def _components_authorized(self, mode: str, fan: str) -> bool:
        if mode not in self.modes or fan not in self.fans:
            return False
        components = {"mode": mode, "fan": fan}
        return all(
            components.get(interlock.when_component) != interlock.equals
            or components.get(interlock.require_component) in interlock.values
            for interlock in self.interlocks
        )

    def render_temperature(
        self, mode: str, fan: str, target_c: int | float
    ) -> str | None:
        """Render only the Celsius variant of the discriminated command."""
        if (
            not self._components_authorized(mode, fan)
            or type(target_c) not in (int, float)
            or not math.isfinite(float(target_c))
        ):
            return None
        target = float(target_c)
        target_range = self.target_range(mode)
        if (
            target_range is None
            or target < target_range.min_c
            or target > target_range.max_c
        ):
            return None
        steps = (target - target_range.min_c) / target_range.step_c
        if not math.isclose(steps, round(steps), rel_tol=0.0, abs_tol=1e-9):
            return None
        rendered_target = str(int(target)) if target.is_integer() else str(target)
        return f"{mode}|{fan}|{rendered_target}C"

    def render_comfort(
        self, mode: str, fan: str, preference: int
    ) -> str | None:
        """Render only the unitless AUTO comfort-preference variant."""
        domain = self.comfort_preference
        if (
            not self._components_authorized(mode, fan)
            or mode not in domain.applies_to_modes
            or type(preference) is not int
            or preference < domain.min_step
            or preference > domain.max_step
            or (preference - domain.min_step) % domain.step != 0
        ):
            return None
        return f"{mode}|{fan}|comfort:{preference}"

    # Keep the old method name for temperature-only callers while making it
    # impossible for them to accidentally authorize an AUTO comfort value.
    def render(self, mode: str, fan: str, target_c: int | float) -> str | None:
        return self.render_temperature(mode, fan, target_c)

    def authorizes(self, value: str) -> bool:
        """Validate an exact canonical request without a Cartesian allow-list."""
        if not isinstance(value, str) or len(value.encode("utf-8")) > 512:
            return False
        comfort_match = _COMFORT_TUPLE.fullmatch(value)
        if comfort_match is not None:
            mode, fan, preference_text = comfort_match.groups()
            return self.render_comfort(mode, fan, int(preference_text)) == value
        temperature_match = _TEMPERATURE_TUPLE.fullmatch(value)
        if temperature_match is None:
            return False
        mode, fan, target_text = temperature_match.groups()
        try:
            target = float(target_text)
        except ValueError:
            return False
        return self.render_temperature(mode, fan, target) == value


@dataclass(frozen=True, slots=True)
class LocalControlCompositeCapability:
    model_id: str
    capability_id: str
    input_domain: LocalControlCompositeInputDomain
    observed_layout_value_count: int


@dataclass(frozen=True)
class LocalControlCompositeDomainContract:
    root_sha256: str
    revision: str
    capabilities: Mapping[tuple[str, str], LocalControlCompositeCapability]

    def capability(
        self, model_id: str, capability_id: str
    ) -> LocalControlCompositeCapability | None:
        return self.capabilities.get((model_id, capability_id))

    def authorizes(self, model_id: str, capability_id: str, value: str) -> bool:
        descriptor = self.capability(model_id, capability_id)
        return descriptor is not None and descriptor.input_domain.authorizes(value)


def _parse_domain(raw: object) -> LocalControlCompositeInputDomain:
    if not isinstance(raw, dict) or set(raw) != {
        "kind",
        "format",
        "components",
        "interlocks",
    }:
        _invalid()
    if (
        raw["kind"] != "composite"
        or raw["format"] != "mode-dependent-climate-argument/v1"
    ):
        _invalid("Bundled Local composite format is invalid")
    components = raw["components"]
    if not isinstance(components, dict) or set(components) != {
        "mode",
        "fan",
        "targetC",
        "comfortPreference",
    }:
        _invalid()
    mode = components["mode"]
    fan = components["fan"]
    target = components["targetC"]
    comfort = components["comfortPreference"]
    if (
        not isinstance(mode, dict)
        or set(mode) != {"kind", "values"}
        or mode["kind"] != "enum"
        or not isinstance(fan, dict)
        or set(fan) != {"kind", "values"}
        or fan["kind"] != "enum"
        or not isinstance(target, dict)
        or set(target) != {"kind", "unit", "appliesToModes", "rangesByMode"}
        or target["kind"] != "number"
        or target["unit"] != "C"
        or not isinstance(target["rangesByMode"], dict)
        or not isinstance(comfort, dict)
        or set(comfort) != {"kind", "min", "max", "step", "appliesToModes"}
        or comfort["kind"] != "number"
        or type(comfort["min"]) is not int
        or type(comfort["max"]) is not int
        or type(comfort["step"]) is not int
        or comfort["step"] <= 0
        or comfort["max"] < comfort["min"]
    ):
        _invalid("Bundled Local composite component domain is invalid")
    modes = _text_values(mode["values"])
    fans = _text_values(fan["values"])
    target_modes = _text_values(target["appliesToModes"])
    comfort_modes = _text_values(comfort["appliesToModes"])
    raw_ranges = target["rangesByMode"]
    if (
        set(raw_ranges) != set(target_modes)
        or set(target_modes) & set(comfort_modes)
        or set(target_modes) | set(comfort_modes) != set(modes)
        or comfort_modes != ("auto",)
        or (comfort["max"] - comfort["min"]) % comfort["step"] != 0
    ):
        _invalid("Bundled Local composite target ranges do not match its modes")
    target_ranges: dict[str, LocalControlCompositeTargetRange] = {}
    for mode_name in target_modes:
        raw_range = raw_ranges[mode_name]
        if (
            not isinstance(raw_range, dict)
            or set(raw_range) != {"min", "max", "step"}
            or not all(
                _finite_number(raw_range.get(key))
                for key in ("min", "max", "step")
            )
            or raw_range["step"] <= 0
            or raw_range["max"] < raw_range["min"]
        ):
            _invalid("Bundled Local composite target range is invalid")
        steps = (float(raw_range["max"]) - float(raw_range["min"])) / float(
            raw_range["step"]
        )
        if not math.isclose(steps, round(steps), rel_tol=0.0, abs_tol=1e-9):
            _invalid("Bundled Local composite target range is not a complete grid")
        target_ranges[mode_name] = LocalControlCompositeTargetRange(
            min_c=float(raw_range["min"]),
            max_c=float(raw_range["max"]),
            step_c=float(raw_range["step"]),
        )

    raw_interlocks = raw["interlocks"]
    if not isinstance(raw_interlocks, list) or not raw_interlocks:
        _invalid("Bundled Local composite interlocks are missing")
    interlocks: list[LocalControlCompositeInterlock] = []
    for item in raw_interlocks:
        if not isinstance(item, dict) or set(item) != {"when", "require"}:
            _invalid()
        when = item["when"]
        require = item["require"]
        if (
            not isinstance(when, dict)
            or set(when) != {"component", "equals"}
            or when["component"] not in ("mode", "fan")
            or not isinstance(when["equals"], str)
            or not isinstance(require, dict)
            or set(require) != {"component", "values"}
            or require["component"] not in ("mode", "fan")
        ):
            _invalid("Bundled Local composite interlock is invalid")
        interlocks.append(
            LocalControlCompositeInterlock(
                when_component=when["component"],
                equals=when["equals"],
                require_component=require["component"],
                values=_text_values(require["values"]),
            )
        )
    return LocalControlCompositeInputDomain(
        modes=modes,
        fans=fans,
        target_ranges_by_mode=MappingProxyType(target_ranges),
        comfort_preference=LocalControlCompositeComfortRange(
            min_step=comfort["min"],
            max_step=comfort["max"],
            step=comfort["step"],
            applies_to_modes=comfort_modes,
        ),
        interlocks=tuple(interlocks),
    )


def _load_local_control_composite_domain(
    raw: bytes, digest: bytes
) -> LocalControlCompositeDomainContract:
    if (
        not isinstance(raw, bytes)
        or not raw
        or len(raw) > _MAX_ARTIFACT_BYTES
        or digest
        != f"{EXPECTED_LOCAL_CONTROL_COMPOSITE_DOMAIN_ROOT_SHA256}\n".encode("ascii")
        or any(marker in raw for marker in _PRIVATE_FIELD_MARKERS)
    ):
        _invalid("Bundled Local composite control-domain bytes are invalid")
    try:
        artifact = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=lambda _value: _invalid(),
        )
    except LocalControlCompositeDomainError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError) as err:
        raise LocalControlCompositeDomainError(
            "Bundled Local composite control domain is not canonical JSON"
        ) from err
    if not isinstance(artifact, dict) or set(artifact) != {
        "schemaVersion",
        "authority",
        "sourceTargetAuthorityRevision",
        "sourceTargetAuthoritySha256",
        "models",
        "stats",
        "revision",
        "rootSha256",
    }:
        _invalid()
    if raw != _canonical_pretty(artifact):
        _invalid("Bundled Local composite control-domain bytes are not canonical")
    unhashed = {
        key: value for key, value in artifact.items() if key not in {"revision", "rootSha256"}
    }
    computed_root = hashlib.sha256(_HASH_DOMAIN + _canonical_compact(unhashed)).hexdigest()
    if (
        artifact["schemaVersion"] != LOCAL_CONTROL_COMPOSITE_DOMAIN_SCHEMA_VERSION
        or artifact["authority"] != "decoder-model-contract-observed-layout"
        or artifact["sourceTargetAuthorityRevision"]
        != EXPECTED_LOCAL_CONTROL_TARGET_AUTHORITY_REVISION
        or artifact["sourceTargetAuthoritySha256"]
        != EXPECTED_LOCAL_CONTROL_TARGET_AUTHORITY_SHA256
        or artifact["rootSha256"] != computed_root
        or computed_root != EXPECTED_LOCAL_CONTROL_COMPOSITE_DOMAIN_ROOT_SHA256
        or not isinstance(artifact["revision"], str)
        or _REVISION.fullmatch(artifact["revision"]) is None
        or artifact["revision"] != f"ha-local-control-composite-domain-v1:{computed_root[:16]}"
    ):
        _invalid("Bundled Local composite control-domain pins do not agree")

    raw_models = artifact["models"]
    stats = artifact["stats"]
    if (
        not isinstance(raw_models, list)
        or not raw_models
        or not isinstance(stats, dict)
        or set(stats) != {"modelCount", "capabilityCount"}
        or stats["modelCount"] != len(raw_models)
    ):
        _invalid("Bundled Local composite control-domain accounting is invalid")
    capabilities: dict[tuple[str, str], LocalControlCompositeCapability] = {}
    previous_model = ""
    for model in raw_models:
        if not isinstance(model, dict) or set(model) != {"modelId", "capabilities"}:
            _invalid()
        model_id = model["modelId"]
        rows = model["capabilities"]
        if (
            not isinstance(model_id, str)
            or _MODEL_ID.fullmatch(model_id) is None
            or model_id <= previous_model
            or not isinstance(rows, list)
            or not rows
        ):
            _invalid("Bundled Local composite model inventory is invalid")
        previous_model = model_id
        previous_capability = ""
        for row in rows:
            if not isinstance(row, dict) or set(row) != {
                "capabilityId",
                "inputDomain",
                "observedLayoutValueCount",
            }:
                _invalid()
            capability_id = row["capabilityId"]
            count = row["observedLayoutValueCount"]
            if (
                not isinstance(capability_id, str)
                or _CAPABILITY_ID.fullmatch(capability_id) is None
                or capability_id <= previous_capability
                or type(count) is not int
                or count < 1
            ):
                _invalid("Bundled Local composite capability inventory is invalid")
            previous_capability = capability_id
            key = (model_id, capability_id)
            capabilities[key] = LocalControlCompositeCapability(
                model_id=model_id,
                capability_id=capability_id,
                input_domain=_parse_domain(row["inputDomain"]),
                observed_layout_value_count=count,
            )
    if stats["capabilityCount"] != len(capabilities):
        _invalid("Bundled Local composite capability count is invalid")
    return LocalControlCompositeDomainContract(
        root_sha256=computed_root,
        revision=artifact["revision"],
        capabilities=MappingProxyType(capabilities),
    )


_CONTRACT_CACHE: LocalControlCompositeDomainContract | None = None
_CONTRACT_LOCK = threading.Lock()


def load_local_control_composite_domain_contract() -> LocalControlCompositeDomainContract:
    """Load the one pinned composable domain without speculative compatibility."""
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
            loaded = _load_local_control_composite_domain(
                (directory / LOCAL_CONTROL_COMPOSITE_DOMAIN_FILENAME).read_bytes(),
                (directory / LOCAL_CONTROL_COMPOSITE_DOMAIN_DIGEST_FILENAME).read_bytes(),
            )
        except OSError as err:
            raise LocalControlCompositeDomainError(
                "Bundled Local composite control domain is unavailable"
            ) from err
        _CONTRACT_CACHE = loaded
        return loaded
