"""Binary sensors for fields the PAT API cannot provide (dehumidifier water tank)."""

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MyLgConfigEntry
from .compat import AddConfigEntryEntitiesCallback
from .const import (
    DEVICE_TYPE_DEHUMIDIFIER,
    DEVICE_TYPE_DISH_WASHER,
    DEVICE_TYPE_REFRIGERATOR,
    DOMAIN,
)
from .coordinator import PatDeviceCoordinator
from .coordinator_wideq import WideqCoordinator
from .entity import MyLgEntity
from .local_provider import (
    WIDEQ_WATER_TANK_KEY,
    LocalSemanticShadowProvider,
    WaterTankProviderResolver,
)

# wideq snapshot key: 1.0 = tank full, 0.0 = ok. (PAT has no equivalent field;
# only the WATER_IS_FULL edge push — see §11.9.)
WATER_TANK_KEY = WIDEQ_WATER_TANK_KEY


@dataclass(frozen=True, kw_only=True)
class MyLgBinaryDescription(BinarySensorEntityDescription):
    """PAT binary sensor with an is_on getter."""

    is_on_fn: Callable[[PatDeviceCoordinator], bool | None]


def _door_flat(c: PatDeviceCoordinator) -> bool | None:
    v = c.get("doorStatus", "doorState")
    return None if v is None else v == "OPEN"


def _door_loc(location: str) -> Callable[[PatDeviceCoordinator], bool | None]:
    def fn(c: PatDeviceCoordinator) -> bool | None:
        v = c.get_location("doorStatus", location, "doorState")
        return None if v is None else v == "OPEN"

    return fn


PAT_BINARY_BY_TYPE: dict[str, tuple[MyLgBinaryDescription, ...]] = {
    DEVICE_TYPE_REFRIGERATOR: (
        MyLgBinaryDescription(
            key="door",
            translation_key="door",
            device_class=BinarySensorDeviceClass.DOOR,
            is_on_fn=_door_loc("MAIN"),
        ),
    ),
    DEVICE_TYPE_DISH_WASHER: (
        MyLgBinaryDescription(
            key="door",
            translation_key="door",
            device_class=BinarySensorDeviceClass.DOOR,
            is_on_fn=_door_flat,
        ),
        MyLgBinaryDescription(
            key="rinse_refill",
            translation_key="rinse_refill",
            device_class=BinarySensorDeviceClass.PROBLEM,
            is_on_fn=lambda c: (
                None
                if (v := c.get("dishWashingStatus", "rinseRefill")) is None
                else bool(v)
            ),
        ),
    ),
}


@dataclass(frozen=True, kw_only=True)
class LocalSemanticBinaryDescription(BinarySensorEntityDescription):
    """One read-only boolean explicitly promoted by an exact Local profile."""

    semantic_id: str


LOCAL_BINARY_BY_PROFILE: dict[str, tuple[LocalSemanticBinaryDescription, ...]] = {
    "dhum-core-state-v2": (
        LocalSemanticBinaryDescription(
            key="status_display",
            translation_key="status_display",
            semantic_id="display.enabled",
        ),
        LocalSemanticBinaryDescription(
            key="operation_blocked",
            translation_key="operation_blocked",
            semantic_id="operation.blocked",
            device_class=BinarySensorDeviceClass.PROBLEM,
            entity_registry_enabled_default=False,
        ),
    ),
    "air-tower-core-state-v1": (
        LocalSemanticBinaryDescription(
            key="ai_energy_saving",
            translation_key="ai_energy_saving",
            semantic_id="energy_saving.ai_enabled",
        ),
    ),
    "styler-core-state-v2": (
        LocalSemanticBinaryDescription(
            key="current_time_display",
            translation_key="current_time_display",
            semantic_id="display.current_time_enabled",
        ),
        LocalSemanticBinaryDescription(
            key="no_interrupt",
            translation_key="no_interrupt",
            semantic_id="option.no_interrupt_enabled",
        ),
    ),
    "wireless-vacuum-core-state-v1": (
        LocalSemanticBinaryDescription(
            key="vacuum_ai_suction_adjustment",
            translation_key="vacuum_ai_suction_adjustment",
            semantic_id="suction.ai_adjustment_enabled",
        ),
        LocalSemanticBinaryDescription(
            key="vacuum_battery_life_extension",
            translation_key="vacuum_battery_life_extension",
            semantic_id="battery.life_extension_enabled",
        ),
        LocalSemanticBinaryDescription(
            key="vacuum_auto_stop_and_go",
            translation_key="vacuum_auto_stop_and_go",
            semantic_id="operation.auto_stop_and_go_enabled",
        ),
        LocalSemanticBinaryDescription(
            key="vacuum_mop_suction",
            translation_key="vacuum_mop_suction",
            semantic_id="mop.suction_enabled",
        ),
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    data = entry.runtime_data
    entities: list[BinarySensorEntity] = []
    # wideq-backed water tank (dehumidifier).
    if data.wideq_coordinator is not None:
        entities += [
            WaterTankFullSensor(
                data.wideq_coordinator,
                coordinator,
                data.local_providers.get(coordinator.device_id),
            )
            for coordinator in data.coordinators.values()
            if coordinator.device_type == DEVICE_TYPE_DEHUMIDIFIER
        ]
    # Fields promoted from exact Rethink profiles are separate read-only
    # entities. They share the PAT device identity, while existing PAT/WideQ
    # entities and controls retain their current providers and precedence.
    for pat_device_id, provider in data.local_providers.items():
        coordinator = data.coordinators.get(pat_device_id)
        if (
            coordinator is None
            or coordinator.device_id != pat_device_id
            or coordinator.model != provider.model_id
        ):
            continue
        for desc in LOCAL_BINARY_BY_PROFILE.get(provider.profile_id, ()):
            contract = provider.profile.fields.get(desc.semantic_id)
            if (
                provider.profile.availability_policy == "attested-session"
                and contract is not None
                and contract.value_type == "boolean"
                and contract.exposure == "state"
            ):
                entities.append(
                    LocalSemanticBinarySensor(provider, coordinator, desc)
                )
    # PAT binary sensors (door, rinse refill, ...).
    for coordinator in data.coordinators.values():
        for desc in PAT_BINARY_BY_TYPE.get(coordinator.device_type, ()):
            if desc.is_on_fn(coordinator) is not None:
                entities.append(MyLgBinarySensor(coordinator, desc))
    async_add_entities(entities)


class MyLgBinarySensor(MyLgEntity, BinarySensorEntity):
    entity_description: MyLgBinaryDescription

    def __init__(
        self, coordinator: PatDeviceCoordinator, description: MyLgBinaryDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.is_on_fn(self.coordinator)


class LocalSemanticBinarySensor(BinarySensorEntity):
    """One fail-closed boolean sourced only from an exact Local profile."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: LocalSemanticBinaryDescription

    def __init__(
        self,
        provider: LocalSemanticShadowProvider,
        pat_coordinator: PatDeviceCoordinator,
        description: LocalSemanticBinaryDescription,
    ) -> None:
        self._provider = provider
        self.entity_description = description
        self._attr_unique_id = f"{pat_coordinator.device_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, pat_coordinator.device_id)},
            name=pat_coordinator.alias,
            manufacturer="LG",
            model=pat_coordinator.model or pat_coordinator.device_type,
        )

    async def async_added_to_hass(self) -> None:
        """Publish accepted Local state and availability changes immediately."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._provider.async_add_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        return (
            self._provider.shadow_healthy
            and type(self._provider.field_value(self.entity_description.semantic_id))
            is bool
        )

    @property
    def is_on(self) -> bool | None:
        if not self.available:
            return None
        value = self._provider.field_value(self.entity_description.semantic_id)
        return value if type(value) is bool else None


class WaterTankFullSensor(CoordinatorEntity[WideqCoordinator], BinarySensorEntity):
    """Dehumidifier water tank full (level state from wideq, self-clearing)."""

    _attr_has_entity_name = True
    _attr_translation_key = "water_tank_full"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        wideq_coordinator: WideqCoordinator,
        pat_coordinator: PatDeviceCoordinator,
        local_provider: LocalSemanticShadowProvider | None = None,
    ) -> None:
        super().__init__(wideq_coordinator)
        self._device_id = pat_coordinator.device_id
        self._provider_resolver = WaterTankProviderResolver(local_provider)
        self._attr_unique_id = f"{pat_coordinator.device_id}_water_tank_full"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, pat_coordinator.device_id)},
            name=pat_coordinator.alias,
            manufacturer="LG",
            model=pat_coordinator.model or pat_coordinator.device_type,
        )

    @property
    def available(self) -> bool:
        return self._provider_resolver.available(
            self._device_id in (self.coordinator.data or {})
        )

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return self.coordinator.diagnostic_attributes

    @property
    def is_on(self) -> bool | None:
        return self._provider_resolver.resolve(
            self.coordinator.snapshot_for(self._device_id)
        )
