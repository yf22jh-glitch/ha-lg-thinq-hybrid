"""Binary sensors for fields the PAT API cannot provide (dehumidifier water tank)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MyLgConfigEntry
from .const import DEVICE_TYPE_DEHUMIDIFIER, DOMAIN
from .coordinator import PatDeviceCoordinator
from .coordinator_wideq import WideqCoordinator

# wideq snapshot key: 1.0 = tank full, 0.0 = ok. (PAT has no equivalent field;
# only the WATER_IS_FULL edge push — see §11.9.)
WATER_TANK_KEY = "airState.miscFuncState.watertankLight"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    data = entry.runtime_data
    if data.wideq_coordinator is None:
        return  # water tank comes from wideq; nothing to add without it
    entities = [
        WaterTankFullSensor(data.wideq_coordinator, coordinator)
        for coordinator in data.coordinators.values()
        if coordinator.device_type == DEVICE_TYPE_DEHUMIDIFIER
    ]
    async_add_entities(entities)


class WaterTankFullSensor(CoordinatorEntity[WideqCoordinator], BinarySensorEntity):
    """Dehumidifier water tank full (level state from wideq, self-clearing)."""

    _attr_has_entity_name = True
    _attr_translation_key = "water_tank_full"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        wideq_coordinator: WideqCoordinator,
        pat_coordinator: PatDeviceCoordinator,
    ) -> None:
        super().__init__(wideq_coordinator)
        self._alias = pat_coordinator.alias
        self._attr_unique_id = f"{pat_coordinator.device_id}_water_tank_full"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, pat_coordinator.device_id)},
            name=pat_coordinator.alias,
            manufacturer="LG",
            model=pat_coordinator.model or pat_coordinator.device_type,
        )

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self._alias in (self.coordinator.data or {})
        )

    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.snapshot_for(self._alias).get(WATER_TANK_KEY)
        if value is None:
            return None
        try:
            return int(float(value)) == 1
        except (TypeError, ValueError):
            return bool(value)
