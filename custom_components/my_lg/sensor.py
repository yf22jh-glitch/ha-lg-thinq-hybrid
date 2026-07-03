"""PAT-based sensors for air conditioners (humidity, temperature, filter)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MyLgConfigEntry
from .const import DEVICE_TYPE_AIR_CONDITIONER
from .coordinator import PatDeviceCoordinator
from .entity import MyLgEntity


@dataclass(frozen=True, kw_only=True)
class MyLgSensorDescription(SensorEntityDescription):
    """Sensor description with a getter into the status dict."""

    value_fn: Callable[[PatDeviceCoordinator], float | None]


AC_SENSORS: tuple[MyLgSensorDescription, ...] = (
    MyLgSensorDescription(
        key="humidity",
        translation_key="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.get("airQualitySensor", "humidity"),
    ),
    MyLgSensorDescription(
        key="current_temperature",
        translation_key="current_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.get("temperature", "currentTemperature"),
    ),
    MyLgSensorDescription(
        key="filter_remaining",
        translation_key="filter_remaining",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda c: c.get("filterInfo", "filterRemainPercent"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    entities: list[MyLgSensor] = []
    for coordinator in entry.runtime_data.coordinators.values():
        if coordinator.device_type != DEVICE_TYPE_AIR_CONDITIONER:
            continue
        for desc in AC_SENSORS:
            if desc.value_fn(coordinator) is not None:
                entities.append(MyLgSensor(coordinator, desc))
    async_add_entities(entities)


class MyLgSensor(MyLgEntity, SensorEntity):
    """A single value read from the device status dict."""

    entity_description: MyLgSensorDescription

    def __init__(
        self, coordinator: PatDeviceCoordinator, description: MyLgSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        return self.entity_description.value_fn(self.coordinator)
