"""Dehumidifier as a HA humidifier entity (state via PAT/MQTT, control via PAT)."""

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
from .const import DEVICE_TYPE_DEHUMIDIFIER
from .coordinator import PatDeviceCoordinator
from .entity import MyLgEntity

POWER_ON = "POWER_ON"
POWER_OFF = "POWER_OFF"

JOB_MODES = [
    "SMART_HUMIDITY",
    "RAPID_HUMIDITY",
    "QUIET_HUMIDITY",
    "CLOTHES_DRY",
    "INTENSIVE_DRY",
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    entities = [
        MyLgDehumidifier(coordinator)
        for coordinator in entry.runtime_data.coordinators.values()
        if coordinator.device_type == DEVICE_TYPE_DEHUMIDIFIER
    ]
    async_add_entities(entities)


class MyLgDehumidifier(MyLgEntity, HumidifierEntity):
    """LG dehumidifier."""

    _attr_name = None
    _attr_device_class = HumidifierDeviceClass.DEHUMIDIFIER
    _attr_supported_features = HumidifierEntityFeature.MODES
    _attr_min_humidity = 30
    _attr_max_humidity = 70
    _attr_available_modes = JOB_MODES

    def __init__(self, coordinator: PatDeviceCoordinator) -> None:
        super().__init__(coordinator, "dehumidifier")

    @property
    def is_on(self) -> bool:
        return self._get("operation", "dehumidifierOperationMode") == POWER_ON

    @property
    def current_humidity(self) -> float | None:
        return self._get("humidity", "currentHumidity")

    @property
    def target_humidity(self) -> float | None:
        return self._get("humidity", "targetHumidity")

    @property
    def mode(self) -> str | None:
        return self._get("dehumidifierJobMode", "currentJobMode")

    async def _control(self, payload: dict[str, Any]) -> None:
        await self.coordinator.api.async_post_device_control(
            self.coordinator.device_id, payload
        )
        self.coordinator.handle_mqtt_status(payload)  # optimistic

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._control({"operation": {"dehumidifierOperationMode": POWER_ON}})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._control({"operation": {"dehumidifierOperationMode": POWER_OFF}})

    async def async_set_humidity(self, humidity: int) -> None:
        # Device accepts 30..70 in steps of 5.
        value = max(30, min(70, round(humidity / 5) * 5))
        await self._control({"humidity": {"targetHumidity": value}})

    async def async_set_mode(self, mode: str) -> None:
        await self._control({"dehumidifierJobMode": {"currentJobMode": mode}})
