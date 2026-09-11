"""Common read-only Home Assistant contract for Rethink Local semantics."""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Iterator
from typing import Literal

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory

from .const import DOMAIN
from .coordinator import PatDeviceCoordinator
from .local_provider import (
    LocalSemanticFieldContract,
    LocalSemanticShadowField,
    LocalSemanticShadowProvider,
)
from .local_read_provider import (
    TlvReadFieldContract,
    TlvReadShadowProvider,
    TlvReadValue,
)

# These profile fields already have the same user-facing state on a PAT entity.
# Keep this map exact and deliberately small: a merely similar field is still
# registered as a disabled Local semantic entity rather than silently discarded.
_PAT_DUPLICATE_SEMANTICS: dict[str, frozenset[str]] = {
    "dhum-core-state-v1": frozenset(
        {
            "operation.power_requested",
            "operation.mode",
            "fan.mode",
            "humidity.current_pct",
            "humidity.target_pct",
        }
    ),
    "dhum-core-state-v2": frozenset(
        {
            "operation.power_requested",
            "operation.mode",
            "fan.mode",
            "humidity.current_pct",
            "humidity.target_pct",
        }
    ),
    "styler-core-state-v1": frozenset({"cycle.state"}),
    "styler-core-state-v2": frozenset({"cycle.state"}),
    "dishwasher-core-state-v1": frozenset(
        {
            "cycle.state",
            "cycle.course",
            "cycle.total_min",
            "cycle.remaining_min",
            "door.open",
            "consumable.rinse_aid_refill_required",
        }
    ),
    "kimchi-aabb-core-state-v1": frozenset({"filter.one_touch_enabled"}),
    "kimchi-thinq1-core-state-v1": frozenset({"filter.one_touch_enabled"}),
    "oven-core-state-v1": frozenset({"oven.upper.state", "oven.upper.remaining_s"}),
    "air-core-state-v1": frozenset(
        {
            "operation.power_requested",
            "operation.mode",
            "fan.mode",
            "temperature.current_c",
            "humidity.current_pct",
            "air_quality.pm1_ug_m3",
            "air_quality.pm2_5_ug_m3",
            "air_quality.pm10_ug_m3",
        }
    ),
    "humidifier-core-state-v1": frozenset(
        {
            "operation.power_requested",
            "operation.mode",
            "fan.mode",
            "temperature.current_c",
            "humidity.current_pct",
            "humidity.target_pct",
            "air_quality.pm1_ug_m3",
            "air_quality.pm2_5_ug_m3",
            "air_quality.pm10_ug_m3",
            "auto_operation.enabled",
            "sleep_mode.enabled",
            "hygienic_dry.mode",
            "mood_light.enabled",
        }
    ),
    "cst170-core-state-v1": frozenset(
        {
            "operation.power_requested",
            "operation.mode",
            "fan.mode",
            "temperature.current_c",
            "temperature.target_c",
            "humidity.current_pct",
            "swing.vertical_enabled",
            "swing.horizontal_enabled",
            "energy_saving.enabled",
        }
    ),
    "cst570-core-state-v1": frozenset(
        {
            "operation.power_requested",
            "operation.mode",
            "fan.mode",
            "temperature.current_c",
            "temperature.target_c",
            "humidity.current_pct",
            "swing.vertical_enabled",
            "swing.horizontal_enabled",
            "energy_saving.enabled",
        }
    ),
    "washtower-core-state-v1": frozenset(
        {
            "washer.cycle.state",
            "washer.cycle.remaining_min",
            "dryer.cycle.state",
            "dryer.cycle.remaining_min",
        }
    ),
    "cooktop-left-front-state-v1": frozenset(
        {"burner.left_front.state", "burner.left_front.power_level"}
    ),
}

# These are duplicates only when the optional WideQ platform is present.
_WIDEQ_DUPLICATE_SEMANTICS: dict[str, frozenset[str]] = {
    "dhum-water-tank-v1": frozenset({"water_tank.full"}),
    "dhum-core-state-v1": frozenset({"water_tank.full"}),
    "dhum-core-state-v2": frozenset({"water_tank.full"}),
    "styler-core-state-v1": frozenset({"cycle.course"}),
    "styler-core-state-v2": frozenset({"cycle.course"}),
    "cst170-core-state-v1": frozenset({"comfort_energy_saving.enabled"}),
    "cst570-core-state-v1": frozenset({"comfort_energy_saving.enabled"}),
}


def local_semantic_unique_id(pat_device_id: str, semantic_id: str) -> str:
    """Return a stable source-qualified HA unique id bounded to 128 chars."""
    candidate = f"{pat_device_id}_local_semantic_{semantic_id}"
    if len(candidate) <= 128:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:16]
    prefix_budget = 128 - len("_local_semantic__") - len(digest)
    owned_prefix = pat_device_id[: max(1, prefix_budget // 2)]
    semantic_budget = prefix_budget - len(owned_prefix)
    owned_semantic = semantic_id[: max(1, semantic_budget)]
    return f"{owned_prefix}_local_semantic_{owned_semantic}_{digest}"


def iter_local_semantic_contracts(
    provider: LocalSemanticShadowProvider,
    value_type: Literal["boolean", "number", "string"],
    *,
    wideq_configured: bool,
    established_semantics: Collection[str] | None = None,
    excluded_semantics: Collection[str] = (),
    overlay_duplicates: bool = False,
) -> Iterator[tuple[str, LocalSemanticFieldContract]]:
    """Yield every exact profile field owned by one HA read-only domain."""
    for semantic_id, contract in provider.profile.fields.items():
        duplicate = None
        if not overlay_duplicates:
            duplicate = (
                local_semantic_duplicate_source(
                    provider,
                    semantic_id,
                    wideq_configured=wideq_configured,
                )
                if established_semantics is None
                else ("pat" if semantic_id in established_semantics else None)
            )
        if (
            contract.value_type == value_type
            and semantic_id not in excluded_semantics
            and duplicate is None
        ):
            yield semantic_id, contract


def iter_tlv_read_contracts(
    provider: TlvReadShadowProvider,
    domain: Literal["binary_sensor", "sensor", "event"],
    *,
    established_semantics: Collection[str] = (),
    excluded_semantics: Collection[str] = (),
    overlay_duplicates: bool = False,
) -> Iterator[tuple[str, TlvReadFieldContract]]:
    """Yield full-feed descriptors not owned by an actually created HA entity."""
    for contract in provider.profile.fields:
        established = contract.semantic_id in established_semantics
        authorized_overlay = (
            overlay_duplicates
            and contract.owner == "none"
            and contract.enabled_by_default
        )
        if (
            contract.domain == domain
            and contract.semantic_id not in excluded_semantics
            and (not established or authorized_overlay)
        ):
            yield contract.semantic_id, contract


def local_semantic_duplicate_source(
    provider: LocalSemanticShadowProvider,
    semantic_id: str,
    *,
    wideq_configured: bool,
) -> Literal["pat", "wideq"] | None:
    """Return the established owner for one exact duplicate, if it has one."""
    if semantic_id in _PAT_DUPLICATE_SEMANTICS.get(provider.profile_id, ()):
        return "pat"
    if wideq_configured and semantic_id in _WIDEQ_DUPLICATE_SEMANTICS.get(
        provider.profile_id, ()
    ):
        return "wideq"
    return None


class LocalSemanticEntityMixin:
    """Shared identity, availability, and provider listener for Local entities."""

    _attr_has_entity_name = True
    # Local is a parallel source during the pilot. Register every non-duplicate
    # field, but leave it opt-in so an uncertain semantic cannot clutter or
    # silently replace the established PAT/WideQ entity.
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        provider: LocalSemanticShadowProvider,
        pat_coordinator: PatDeviceCoordinator,
        semantic_id: str,
        contract: LocalSemanticFieldContract,
    ) -> None:
        super().__init__()
        if provider.profile.fields.get(semantic_id) is not contract:
            raise ValueError("Local semantic entity contract is not profile-owned")
        self._provider = provider
        self._semantic_id = semantic_id
        self._contract = contract
        self._attr_name = f"Local · {semantic_id}"
        self._attr_unique_id = local_semantic_unique_id(
            pat_coordinator.device_id, semantic_id
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, pat_coordinator.device_id)},
            name=pat_coordinator.alias,
            manufacturer="LG",
            model=pat_coordinator.model or pat_coordinator.device_type,
        )
        self._attr_entity_category = (
            EntityCategory.DIAGNOSTIC if contract.exposure == "diagnostic" else None
        )
        # Only a profile with a real measured SLA needs periodic clock checks.
        # This poll performs no I/O; all actual values still arrive by listener.
        self._attr_should_poll = provider.profile.freshness_max_age_ms is not None
        self._remove_provider_listener = None

    @property
    def semantic_id(self) -> str:
        return self._semantic_id

    @property
    def _shadow_field(self) -> LocalSemanticShadowField | None:
        return self._provider.shadow_fields.get(self._semantic_id)

    @property
    def available(self) -> bool:
        return self._provider.semantic_field_available(self._semantic_id)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        field = self._shadow_field
        attributes: dict[str, object] = {
            "semantic_id": self._semantic_id,
            "profile_id": self._provider.profile_id,
            "value_type": self._contract.value_type,
            "fresh": self._provider.semantic_field_fresh(self._semantic_id),
        }
        if field is not None:
            attributes.update(
                {
                    "observed_at": field.observed_at.isoformat(),
                    "confidence": field.confidence,
                }
            )
        return attributes

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._remove_provider_listener = self._provider.async_add_listener(
            self._handle_provider_update
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_provider_listener is not None:
            self._remove_provider_listener()
            self._remove_provider_listener = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_provider_update(self) -> None:
        self.async_write_ha_state()


class TlvReadEntityMixin:
    """Shared HA identity and lifecycle for one complete-feed descriptor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        provider: TlvReadShadowProvider,
        pat_coordinator: PatDeviceCoordinator,
        semantic_id: str,
        contract: TlvReadFieldContract,
    ) -> None:
        super().__init__()
        if provider.profile.fields_by_semantic_id.get(semantic_id) is not contract:
            raise ValueError("TLV read entity contract is not profile-owned")
        self._provider = provider
        self._semantic_id = semantic_id
        self._contract = contract
        self._attr_name = f"Local · {contract.label_ko}"
        # Preserve the pilot semantic unique-id namespace so a field promoted
        # into the complete artifact keeps its registry identity after reload.
        self._attr_unique_id = local_semantic_unique_id(
            pat_coordinator.device_id, semantic_id
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, pat_coordinator.device_id)},
            name=pat_coordinator.alias,
            manufacturer="LG",
            model=pat_coordinator.model or pat_coordinator.device_type,
        )
        self._attr_entity_category = (
            EntityCategory.DIAGNOSTIC
            if contract.entity_category == "diagnostic"
            else None
        )
        self._attr_entity_registry_enabled_default = contract.enabled_by_default
        self._attr_should_poll = False
        self._remove_provider_listener = None

    @property
    def semantic_id(self) -> str:
        return self._semantic_id

    @property
    def _read_field(self) -> TlvReadValue | None:
        return self._provider.fields.get(self._semantic_id)

    @property
    def available(self) -> bool:
        return self._provider.field_available(self._semantic_id)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        field = self._read_field
        attributes: dict[str, object] = {
            "semantic_id": self._semantic_id,
            "descriptor_key": self._contract.descriptor_key,
            "profile_id": self._provider.profile.profile_id,
            "value_types": list(self._contract.value_types),
        }
        if field is not None:
            attributes.update(
                {
                    "observed_at": field.observed_at.isoformat(),
                    "confidence": field.confidence,
                }
            )
        return attributes

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._remove_provider_listener = self._provider.async_add_listener(
            self._handle_provider_update
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_provider_listener is not None:
            self._remove_provider_listener()
            self._remove_provider_listener = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_provider_update(self) -> None:
        self.async_write_ha_state()
