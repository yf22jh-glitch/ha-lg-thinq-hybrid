"""Dehumidifier / humidifier as HA humidifier entities (state PAT, control PAT)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.humidifier import (
    HumidifierDeviceClass,
    HumidifierEntity,
    HumidifierEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MyLgConfigEntry
from .const import DEVICE_TYPE_DEHUMIDIFIER, DEVICE_TYPE_HUMIDIFIER
from .coordinator import PatDeviceCoordinator
from .entity import MyLgEntity

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
    },
    DEVICE_TYPE_HUMIDIFIER: {
        "op_key": "humidifierOperationMode",
        "job_group": "humidifierJobMode",
        "device_class": HumidifierDeviceClass.HUMIDIFIER,
        "modes": ["HUMIDIFY", "HUMIDIFY_AND_AIR_CLEAN", "AIR_CLEAN"],
        "current": ("airQualitySensor", "humidity"),
    },
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    entities = [
        MyLgHumidifier(coordinator, _CONFIG[coordinator.device_type])
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

    def __init__(self, coordinator: PatDeviceCoordinator, config: dict) -> None:
        super().__init__(coordinator, "humidifier")
        self._cfg = config
        self._attr_device_class = config["device_class"]
        self._attr_available_modes = config["modes"]

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

    async def _control(self, payload: dict[str, Any]) -> None:
        await self.coordinator.async_control(payload)
        self.coordinator.handle_mqtt_status(payload)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._control({"operation": {self._cfg["op_key"]: POWER_ON}})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._control({"operation": {self._cfg["op_key"]: POWER_OFF}})

    async def async_set_humidity(self, humidity: int) -> None:
        value = max(30, min(70, round(humidity / 5) * 5))
        await self._control({"humidity": {"targetHumidity": value}})

    async def async_set_mode(self, mode: str) -> None:
        await self._control({self._cfg["job_group"]: {"currentJobMode": mode}})
