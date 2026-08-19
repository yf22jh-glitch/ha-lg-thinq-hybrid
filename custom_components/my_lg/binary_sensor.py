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
