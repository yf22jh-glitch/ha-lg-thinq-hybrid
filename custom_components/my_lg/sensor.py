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
from .const import (
    DEVICE_TYPE_AIR_CONDITIONER,
    DEVICE_TYPE_STYLER,
    DEVICE_TYPE_WASHTOWER,
    DOMAIN,
)
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


def _wq(*path: str):
    """Getter navigating a nested wideq snapshot (e.g. snap['washer']['course'])."""

    def getter(snap: dict):
        node = snap
        for key in path:
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        return node

    return getter


def _energy(key: str, tkey: str, path: tuple[str, ...]) -> WideqSensorDescription:
    return WideqSensorDescription(
        key=key,
        translation_key=tkey,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_wq(*path),
    )


def _text(key: str, name: str, path: tuple[str, ...]) -> WideqSensorDescription:
    return WideqSensorDescription(key=key, name=name, value_fn=_wq(*path))


def _minutes(key: str, name: str, path: tuple[str, ...]) -> WideqSensorDescription:
    return WideqSensorDescription(
        key=key, name=name, native_unit_of_measurement="min", value_fn=_wq(*path)
    )


# All wideq-only (PAT cannot provide these). Snapshot is nested under
# washer/dryer/styler dicts (unlike AC's flat dotted keys).
WASHTOWER_SENSORS: tuple[WideqSensorDescription, ...] = (
    _text("washer_state", "Washer state", ("washer", "state")),
    _text("washer_course", "Washer course", ("washer", "course")),
    _text("washer_spin", "Washer spin", ("washer", "spin")),
    _text("washer_water_temp", "Washer water temp", ("washer", "temp")),
    _text("washer_water_level", "Washer water level", ("washer", "waterLevel")),
    _minutes("washer_remain", "Washer remaining", ("washer", "remainTimeMinute")),
    _text("washer_error", "Washer error", ("washer", "error")),
    _text("washer_door_lock", "Washer door lock", ("washer", "doorLock")),
    _text("washer_child_lock", "Washer child lock", ("washer", "childLock")),
    _energy("washer_energy", "washer_energy", ("washer", "accumulatedEnergyData")),
    _text("dryer_state", "Dryer state", ("dryer", "state")),
    _text("dryer_dry_level", "Dryer dry level", ("dryer", "dryLevel")),
    _minutes("dryer_remain", "Dryer remaining", ("dryer", "remainTimeMinute")),
    _text("dryer_duct_clogging", "Dryer duct clogging", ("dryer", "ductClogging")),
    _text("dryer_error", "Dryer error", ("dryer", "error")),
    _energy("dryer_energy", "dryer_energy", ("dryer", "accumulatedEnergyData")),
)

STYLER_SENSORS: tuple[WideqSensorDescription, ...] = (
    _text("styler_state", "State", ("styler", "state")),
    _text("styler_course", "Course", ("styler", "course")),
    _minutes("styler_remain", "Remaining", ("styler", "remainTimeMinute")),
    _text("styler_night_dry", "Night dry", ("styler", "nightDry")),
    _text("styler_door_lock", "Door lock", ("styler", "doorLock")),
    _text("styler_child_lock", "Child lock", ("styler", "childLock")),
    _text("styler_error", "Error", ("styler", "error")),
    _energy("styler_energy", "styler_energy", ("styler", "accumulatedEnergyData")),
)

WIDEQ_SENSORS_BY_TYPE: dict[str, tuple[WideqSensorDescription, ...]] = {
    DEVICE_TYPE_AIR_CONDITIONER: WIDEQ_AC_SENSORS,
    DEVICE_TYPE_WASHTOWER: WASHTOWER_SENSORS,
    DEVICE_TYPE_STYLER: STYLER_SENSORS,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    data = entry.runtime_data
    entities: list[SensorEntity] = []
    for coordinator in data.coordinators.values():
        # PAT sensors for air conditioners.
        if coordinator.device_type == DEVICE_TYPE_AIR_CONDITIONER:
            for desc in AC_SENSORS:
                if desc.value_fn(coordinator) is not None:
                    entities.append(MyLgSensor(coordinator, desc))
        # wideq-backed sensors (only if wideq is configured).
        if data.wideq_coordinator is not None:
            for wdesc in WIDEQ_SENSORS_BY_TYPE.get(coordinator.device_type, ()):
                entities.append(
                    WideqDeviceSensor(data.wideq_coordinator, coordinator, wdesc)
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


class WideqDeviceSensor(CoordinatorEntity[WideqCoordinator], SensorEntity):
    """A wideq-only value read from the wideq snapshot, mapped by device alias."""

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
