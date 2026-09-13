"""Dehumidifier / humidifier cards with exact local-first scalar commands."""

from __future__ import annotations

from typing import Any

from homeassistant.components.humidifier import (
    HumidifierDeviceClass,
    HumidifierEntity,
    HumidifierEntityFeature,
)
from homeassistant.core import HomeAssistant

from . import MyLgConfigEntry
from .compat import AddConfigEntryEntitiesCallback
from .const import DEVICE_TYPE_DEHUMIDIFIER, DEVICE_TYPE_HUMIDIFIER
from .coordinator import PatDeviceCoordinator
from .entity import MyLgEntity
from .local_control_native import async_native_local_control, native_local_available
from .local_control_router import LocalControlRouter

POWER_ON = "POWER_ON"
POWER_OFF = "POWER_OFF"

# Per device-type wiring (operation resource key, job-mode group, modes, ...).
_CONFIG: dict[str, dict[str, Any]] = {
    DEVICE_TYPE_DEHUMIDIFIER: {
        "op_key": "dehumidifierOperationMode",
        "job_group": "dehumidifierJobMode",
        "device_class": HumidifierDeviceClass.DEHUMIDIFIER,
        "modes": [
            "SMART_HUMIDITY",
            "RAPID_HUMIDITY",
            "QUIET_HUMIDITY",
            "CLOTHES_DRY",
            "INTENSIVE_DRY",
        ],
        "current": ("humidity", "currentHumidity"),
        "local_modes": {"SMART_HUMIDITY": "smart", "RAPID_HUMIDITY": "jet", "QUIET_HUMIDITY": "silent", "CLOTHES_DRY": "laundry", "INTENSIVE_DRY": "intensive"},
    },
    DEVICE_TYPE_HUMIDIFIER: {
        "op_key": "humidifierOperationMode",
        "job_group": "humidifierJobMode",
        "device_class": HumidifierDeviceClass.HUMIDIFIER,
        "modes": ["HUMIDIFY", "HUMIDIFY_AND_AIR_CLEAN", "AIR_CLEAN"],
        "current": ("airQualitySensor", "humidity"),
        "local_modes": {"HUMIDIFY_AND_AIR_CLEAN": "humidify+clean", "AIR_CLEAN": "air clean"},
    },
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    entities = [
        MyLgHumidifier(coordinator, _CONFIG[coordinator.device_type], entry.runtime_data.local_control)
        for coordinator in entry.runtime_data.coordinators.values()
        if coordinator.device_type in _CONFIG
    ]
    async_add_entities(entities)


class MyLgHumidifier(MyLgEntity, HumidifierEntity):
    """LG (de)humidifier."""

    _attr_name = None
    _attr_supported_features = HumidifierEntityFeature.MODES
    _attr_min_humidity = 30
    _attr_max_humidity = 70

    def __init__(self, coordinator: PatDeviceCoordinator, config: dict, local_control: LocalControlRouter | None = None) -> None:
        super().__init__(coordinator, "humidifier")
        self._cfg = config
        self._local_control = local_control
        self._attr_device_class = config["device_class"]
        self._attr_available_modes = config["modes"]

    @property
    def available(self) -> bool:
        return native_local_available(self._local_control, self.coordinator.device_id,
                                      'operation.power_requested', 'operation.mode', 'humidity.target_pct') or super().available

    @property
    def is_on(self) -> bool:
        return self._get("operation", self._cfg["op_key"]) == POWER_ON

    @property
    def current_humidity(self) -> float | None:
        return self._get(*self._cfg["current"])

    @property
    def target_humidity(self) -> float | None:
        return self._get("humidity", "targetHumidity")

    @property
    def mode(self) -> str | None:
        return self._get(self._cfg["job_group"], "currentJobMode")

    async def _control(self, payload: dict[str, Any], capability: str, value: str | None) -> None:
        if await async_native_local_control(self._local_control, self.coordinator.device_id, capability, value):
            return
        await self.coordinator.async_control(payload)
        self.coordinator.handle_mqtt_status(payload)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._control({"operation": {self._cfg["op_key"]: POWER_ON}}, "operation.power_requested", "true")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._control({"operation": {self._cfg["op_key"]: POWER_OFF}}, "operation.power_requested", "false")

    async def async_set_humidity(self, humidity: int) -> None:
        value = max(30, min(70, round(humidity / 5) * 5))
        await self._control({"humidity": {"targetHumidity": value}}, "humidity.target_pct", str(value))

    async def async_set_mode(self, mode: str) -> None:
        if mode not in self._cfg['modes']:
            raise ValueError('Unsupported humidity mode')
        await self._control({self._cfg["job_group"]: {"currentJobMode": mode}}, "operation.mode", self._cfg['local_modes'].get(mode))
