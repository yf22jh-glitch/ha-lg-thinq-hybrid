"""Air purifier as a HA fan entity (power + wind strength preset)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant

from . import MyLgConfigEntry
from .compat import AddConfigEntryEntitiesCallback
from .const import DEVICE_TYPE_AIR_PURIFIER
from .coordinator import PatDeviceCoordinator
from .entity import MyLgEntity
from .local_control_native import async_native_local_control, native_local_available
from .local_control_router import LocalControlRouter

POWER_ON = "POWER_ON"
POWER_OFF = "POWER_OFF"
WIND_STRENGTHS = ["LOW", "MID", "HIGH", "AUTO"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    entities = [
        MyLgAirPurifierFan(coordinator, entry.runtime_data.local_control)
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

    def __init__(self, coordinator: PatDeviceCoordinator, local_control: LocalControlRouter | None = None) -> None:
        super().__init__(coordinator, "fan")
        self._local_control = local_control

    @property
    def available(self) -> bool:
        return native_local_available(self._local_control, self.coordinator.device_id,
                                      'operation.power_requested', 'fan.mode') or super().available

    @property
    def is_on(self) -> bool:
        return self._get("operation", "airPurifierOperationMode") == POWER_ON

    @property
    def preset_mode(self) -> str | None:
        return self._get("airFlow", "windStrength")

    async def _control(self, payload: dict[str, Any], capability: str, value: str) -> None:
        if await async_native_local_control(self._local_control, self.coordinator.device_id, capability, value):
            return
        await self.coordinator.async_control(payload)
        self.coordinator.handle_mqtt_status(payload)  # optimistic

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        await self._control({"operation": {"airPurifierOperationMode": POWER_ON}}, "operation.power_requested", "true")
        if preset_mode:
            await self.async_set_preset_mode(preset_mode)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._control({"operation": {"airPurifierOperationMode": POWER_OFF}}, "operation.power_requested", "false")

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode not in WIND_STRENGTHS:
            raise ValueError('Unsupported purifier fan mode')
        await self._control({"airFlow": {"windStrength": preset_mode}}, "fan.mode", preset_mode.lower())
