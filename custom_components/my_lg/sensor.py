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
from homeassistant.const import (
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MyLgConfigEntry
from .const import DEVICE_TYPE_AIR_CONDITIONER, DOMAIN
from .coordinator import PatDeviceCoordinator
from .coordinator_wideq import WideqCoordinator
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


@dataclass(frozen=True, kw_only=True)
class WideqSensorDescription(SensorEntityDescription):
    """Sensor description reading from a wideq snapshot dict (dotted keys)."""

    value_fn: Callable[[dict], float | None]


WIDEQ_AC_SENSORS: tuple[WideqSensorDescription, ...] = (
    WideqSensorDescription(
        key="energy_current",
        translation_key="energy_current",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.get("airState.energy.onCurrent"),
    ),
    WideqSensorDescription(
        key="energy_today",
        translation_key="energy_today",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda s: s.get("airState.energy.dailyTotal"),
    ),
    WideqSensorDescription(
        key="energy_month",
        translation_key="energy_month",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.get("airState.energy.monthlyTotal"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    data = entry.runtime_data
    entities: list[SensorEntity] = []
    for coordinator in data.coordinators.values():
        if coordinator.device_type != DEVICE_TYPE_AIR_CONDITIONER:
            continue
        for desc in AC_SENSORS:
            if desc.value_fn(coordinator) is not None:
                entities.append(MyLgSensor(coordinator, desc))
        # wideq-backed power/energy (only if wideq is configured).
        if data.wideq_coordinator is not None:
            for wdesc in WIDEQ_AC_SENSORS:
                entities.append(
                    WideqAcSensor(data.wideq_coordinator, coordinator, wdesc)
                )
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


class WideqAcSensor(CoordinatorEntity[WideqCoordinator], SensorEntity):
    """AC power/energy read from the wideq coordinator, mapped by device alias."""

    _attr_has_entity_name = True
    entity_description: WideqSensorDescription

    def __init__(
        self,
        wideq_coordinator: WideqCoordinator,
        pat_coordinator: PatDeviceCoordinator,
        description: WideqSensorDescription,
    ) -> None:
        super().__init__(wideq_coordinator)
        self._alias = pat_coordinator.alias
        self.entity_description = description
        self._attr_unique_id = f"{pat_coordinator.device_id}_{description.key}"
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
    def native_value(self) -> float | None:
        return self.entity_description.value_fn(
            self.coordinator.snapshot_for(self._alias)
        )
