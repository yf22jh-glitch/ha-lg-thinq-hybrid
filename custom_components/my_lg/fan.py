"""Air purifier as a HA fan entity (power + wind strength preset)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MyLgConfigEntry
from .const import DEVICE_TYPE_AIR_PURIFIER
from .coordinator import PatDeviceCoordinator
from .entity import MyLgEntity

POWER_ON = "POWER_ON"
POWER_OFF = "POWER_OFF"
WIND_STRENGTHS = ["LOW", "MID", "HIGH", "AUTO"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    entities = [
        MyLgAirPurifierFan(coordinator)
        for coordinator in entry.runtime_data.coordinators.values()
        if coordinator.device_type == DEVICE_TYPE_AIR_PURIFIER
    ]
    async_add_entities(entities)


class MyLgAirPurifierFan(MyLgEntity, FanEntity):
    """LG air purifier."""

    _attr_name = None
    _attr_preset_modes = WIND_STRENGTHS
    _attr_supported_features = (
        FanEntityFeature.PRESET_MODE
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator: PatDeviceCoordinator) -> None:
        super().__init__(coordinator, "fan")

    @property
    def is_on(self) -> bool:
        return self._get("operation", "airPurifierOperationMode") == POWER_ON

    @property
    def preset_mode(self) -> str | None:
        return self._get("airFlow", "windStrength")

    async def _control(self, payload: dict[str, Any]) -> None:
        await self.coordinator.async_control(payload)
        self.coordinator.handle_mqtt_status(payload)  # optimistic

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        await self._control({"operation": {"airPurifierOperationMode": POWER_ON}})
        if preset_mode:
            await self.async_set_preset_mode(preset_mode)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._control({"operation": {"airPurifierOperationMode": POWER_OFF}})

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        await self._control({"airFlow": {"windStrength": preset_mode}})
