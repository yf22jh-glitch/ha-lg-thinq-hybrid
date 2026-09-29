"""Stable Local ownership for existing composite/control entities."""

from __future__ import annotations

from dataclasses import dataclass

from .local_command import CLIMATE_POWER_ON_CAPABILITY, CLIMATE_TUPLE_CAPABILITY
from .local_control_composite_domain import (
    LocalControlCompositeDomainContract,
    LocalControlCompositeInputDomain,
)
from .local_provider import LocalSemanticShadowProvider
from .local_read_provider import TlvReadShadowProvider

LOCAL_AUTO_COMFORT_SEMANTIC = "comfort.preference_step"
LOCAL_CLIMATE_TARGET_SEMANTIC = "temperature.target_c"
_LOCAL_CLIMATE_REQUIRED_FIELDS = {
    "operation.power_requested": "boolean",
    "operation.mode": "string",
    "fan.mode": "string",
    LOCAL_CLIMATE_TARGET_SEMANTIC: "number",
    LOCAL_AUTO_COMFORT_SEMANTIC: "number",
}


@dataclass(frozen=True, slots=True)
class TlvReadOwnedField:
    """One field resolved without confusing ownership with live availability."""

    owner_configured: bool
    available: bool
    value: object | None


def tlv_read_owner_configured(
    provider: TlvReadShadowProvider | None,
    semantic_id: str | None,
) -> bool:
    """Return whether this exact provider contract contains the semantic.

    The profile is the sole model-routing authority. Callers provide the
    semantic used by the entity surface; there is deliberately no parallel
    model/semantic allowlist to drift from the reviewed contract.
    """
    return (
        provider is not None
        and semantic_id is not None
        and semantic_id in provider.profile.fields_by_semantic_id
    )


def resolve_tlv_read_owned_field(
    provider: TlvReadShadowProvider | None,
    semantic_id: str | None,
) -> TlvReadOwnedField:
    """Resolve one configured Local field without cloud or stale fallback.

    Contract presence fixes ownership. Temporary unavailability therefore
    returns a configured owner with no value; it is never permission to read a
    cached value or switch to PAT/WideQ.
    """
    if not tlv_read_owner_configured(provider, semantic_id):
        return TlvReadOwnedField(False, False, None)
    assert provider is not None and semantic_id is not None
    available = provider.field_available(semantic_id)
    return TlvReadOwnedField(
        True,
        available,
        provider.field_value(semantic_id) if available else None,
    )


def local_climate_control_domain(
    provider: LocalSemanticShadowProvider | None,
    model_id: str | None,
    composite_contract: LocalControlCompositeDomainContract | None,
) -> LocalControlCompositeInputDomain | None:
    """Return the one domain shared by both Local climate write capabilities."""
    if provider is None or composite_contract is None or not isinstance(model_id, str):
        return None
    if any(
        (contract := provider.profile.fields.get(semantic_id)) is None
        or contract.value_type != value_type
        for semantic_id, value_type in _LOCAL_CLIMATE_REQUIRED_FIELDS.items()
    ):
        return None
    tuple_capability = composite_contract.capability(
        model_id, CLIMATE_TUPLE_CAPABILITY
    )
    power_capability = composite_contract.capability(
        model_id, CLIMATE_POWER_ON_CAPABILITY
    )
    if (
        tuple_capability is None
        or power_capability is None
        or tuple_capability.input_domain != power_capability.input_domain
    ):
        return None
    return tuple_capability.input_domain


def local_climate_promoted_semantics(
    provider: LocalSemanticShadowProvider | None,
    model_id: str | None,
    composite_contract: LocalControlCompositeDomainContract | None,
) -> frozenset[str]:
    """Semantics already owned by exact Local climate/select surfaces."""
    domain = local_climate_control_domain(provider, model_id, composite_contract)
    if provider is None or domain is None:
        return frozenset()
    if (
        not domain.target_ranges_by_mode
        or "auto" not in domain.comfort_preference.applies_to_modes
    ):
        return frozenset()
    return frozenset(
        {LOCAL_CLIMATE_TARGET_SEMANTIC, LOCAL_AUTO_COMFORT_SEMANTIC}
    )


def local_auto_comfort_owner_configured(
    provider: LocalSemanticShadowProvider | None,
    model_id: str | None,
    composite_contract: LocalControlCompositeDomainContract | None,
) -> bool:
    """Return whether the exact Local AUTO preference select is materialized."""
    return LOCAL_AUTO_COMFORT_SEMANTIC in local_climate_promoted_semantics(
        provider, model_id, composite_contract
    )
