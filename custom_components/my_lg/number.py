"""Refrigerator target-temperature Number entities (PAT write).

Only compartments that report a numeric ``targetTemperature`` are settable —
kimchi fridges expose storage-mode enums (FREEZER/KIMCHI/…) and are read-only.

Write payload mirrors the thinqconnect SDK: the value goes to the
``temperatureInUnits`` resource with the compartment's ``locationName``:
    {"temperatureInUnits": {"locationName": "FRIDGE", "targetTemperatureC": 3}}
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MyLgConfigEntry
from .const import DEVICE_TYPE_REFRIGERATOR
from .coordinator import PatDeviceCoordinator
from .entity import MyLgEntity

# Fallback °C ranges when the profile doesn't pin them per compartment.
_FALLBACK_RANGE: dict[str, tuple[int, int]] = {
    "FRIDGE": (1, 7),
    "FREEZER": (-23, -15),
    "CONVERTIBLE": (-23, 7),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up refrigerator target-temperature numbers."""
    entities: list[NumberEntity] = []
    for coord in entry.runtime_data.coordinators.values():
        if coord.device_type != DEVICE_TYPE_REFRIGERATOR:
            continue
        for item in coord.get("temperature") or []:
            if not isinstance(item, dict):
                continue
            loc = item.get("locationName")
            if loc and isinstance(item.get("targetTemperature"), (int, float)):
                entities.append(MyLgFridgeTargetTemp(coord, loc))
    async_add_entities(entities)


class MyLgFridgeTargetTemp(MyLgEntity, NumberEntity):
    """A refrigerator compartment target temperature (settable)."""

    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_step = 1

    def __init__(self, coordinator: PatDeviceCoordinator, location: str) -> None:
        super().__init__(coordinator, f"{location.lower()}_target_temp")
        self._location = location
        self._attr_translation_key = f"{location.lower()}_target_temp"
        lo, hi = _FALLBACK_RANGE.get(location, (-23, 7))
        self._attr_native_min_value = lo
        self._attr_native_max_value = hi

    @property
    def native_value(self) -> float | None:
        value = self.coordinator.get_location(
            "temperature", self._location, "targetTemperature"
        )
        return value if isinstance(value, (int, float)) else None

    async def async_set_native_value(self, value: float) -> None:
        payload: dict[str, Any] = {
            "temperatureInUnits": {
                "locationName": self._location,
                "targetTemperatureC": int(value),
            }
        }
        await self.coordinator.async_control(payload)
        # Status temperature is a location-keyed list; a genuine value arrives
        # via the next DEVICE_STATUS push, so no optimistic merge here.
