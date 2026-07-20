"""PAT-based sensors for air conditioners (humidity, temperature, filter)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MyLgConfigEntry
from .compat import AddConfigEntryEntitiesCallback
from .const import (
    DEVICE_TYPE_AIR_CONDITIONER,
    DEVICE_TYPE_AIR_PURIFIER,
    DEVICE_TYPE_COOKTOP,
    DEVICE_TYPE_DISH_WASHER,
    DEVICE_TYPE_HUMIDIFIER,
    DEVICE_TYPE_KIMCHI_REFRIGERATOR,
    DEVICE_TYPE_OVEN,
    DEVICE_TYPE_REFRIGERATOR,
    DEVICE_TYPE_STYLER,
    DEVICE_TYPE_WASHTOWER,
    DEVICE_TYPE_WATER_PURIFIER,
    DOMAIN,
)
from .coordinator import PatDeviceCoordinator
from .coordinator_wideq import WideqCoordinator
from .entity import MyLgEntity
from .raw_sensor import RawSensorManager


@dataclass(frozen=True, kw_only=True)
class MyLgSensorDescription(SensorEntityDescription):
    """Sensor description with a getter into the status dict."""

    value_fn: Callable[[PatDeviceCoordinator], float | None]
    # Profile property group that indicates support. When set, the sensor is
    # created if the device profile advertises this group even while the device
    # is offline (status value currently None) — otherwise an offline-at-startup
    # device would silently lose the entity until the next reload.
    profile_group: str | None = None


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


def _pm(key: str, tkey: str, field: str, dclass: SensorDeviceClass) -> MyLgSensorDescription:
    return MyLgSensorDescription(
        key=key,
        translation_key=tkey,
        device_class=dclass,
        native_unit_of_measurement=CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
        state_class=SensorStateClass.MEASUREMENT,
        profile_group="airQualitySensor",
        value_fn=lambda c, f=field: c.get("airQualitySensor", f),
    )


_HUMIDITY = MyLgSensorDescription(
    key="humidity",
    translation_key="humidity",
    device_class=SensorDeviceClass.HUMIDITY,
    native_unit_of_measurement=PERCENTAGE,
    state_class=SensorStateClass.MEASUREMENT,
    profile_group="airQualitySensor",
    value_fn=lambda c: c.get("airQualitySensor", "humidity"),
)
_TOTAL_POLLUTION = MyLgSensorDescription(
    key="total_pollution",
    translation_key="total_pollution",
    state_class=SensorStateClass.MEASUREMENT,
    profile_group="airQualitySensor",
    value_fn=lambda c: c.get("airQualitySensor", "totalPollution"),
)

AIR_PURIFIER_SENSORS: tuple[MyLgSensorDescription, ...] = (
    _pm("pm1", "pm1", "PM1", SensorDeviceClass.PM1),
    _pm("pm2_5", "pm2_5", "PM2", SensorDeviceClass.PM25),
    _pm("pm10", "pm10", "PM10", SensorDeviceClass.PM10),
    _HUMIDITY,
    _TOTAL_POLLUTION,
    MyLgSensorDescription(
        key="odor",
        translation_key="odor",
        state_class=SensorStateClass.MEASUREMENT,
        profile_group="airQualitySensor",
        value_fn=lambda c: c.get("airQualitySensor", "odor"),
    ),
    MyLgSensorDescription(
        key="filter_remaining",
        translation_key="filter_remaining",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        profile_group="filterInfo",
        value_fn=lambda c: c.get("filterInfo", "filterRemainPercent"),
    ),
)

HUMIDIFIER_SENSORS: tuple[MyLgSensorDescription, ...] = (
    _pm("pm1", "pm1", "PM1", SensorDeviceClass.PM1),
    _pm("pm2_5", "pm2_5", "PM2", SensorDeviceClass.PM25),
    _pm("pm10", "pm10", "PM10", SensorDeviceClass.PM10),
    _HUMIDITY,
    _TOTAL_POLLUTION,
    MyLgSensorDescription(
        key="current_temperature",
        translation_key="current_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.get("airQualitySensor", "temperature"),
    ),
)

def _text(key: str, group: str, field: str) -> MyLgSensorDescription:
    return MyLgSensorDescription(
        key=key, translation_key=key,
        value_fn=lambda c, g=group, f=field: c.get(g, f),
    )


def _loc_text(key: str, group: str, location: str, field: str) -> MyLgSensorDescription:
    return MyLgSensorDescription(
        key=key, translation_key=key,
        value_fn=lambda c, g=group, loc=location, f=field: c.get_location(g, loc, f),
    )


def _temp_loc(key: str, location: str) -> MyLgSensorDescription:
    return MyLgSensorDescription(
        key=key, translation_key=key,
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c, loc=location: c.get_location(
            "temperature", loc, "targetTemperature"
        ),
    )


def _timer(key: str, hkey: str, mkey: str) -> MyLgSensorDescription:
    def fn(c, h=hkey, m=mkey):
        hv = c.get("timer", h)
        mv = c.get("timer", m)
        if hv is None and mv is None:
            return None
        return (hv or 0) * 60 + (mv or 0)

    return MyLgSensorDescription(
        key=key, translation_key=key, native_unit_of_measurement="min", value_fn=fn
    )


REFRIGERATOR_SENSORS: tuple[MyLgSensorDescription, ...] = (
    _temp_loc("fridge_temp", "FRIDGE"),
    _temp_loc("freezer_temp", "FREEZER"),
    _text("fresh_air_filter", "refrigeration", "freshAirFilter"),
)

# Kimchi fridges vary by model: some report TOP/MIDDLE/BOTTOM compartments,
# others LEFT/RIGHT/MIDDLE/BOTTOM. Enumerate the locations the device actually
# reports instead of hardcoding, so LEFT/RIGHT compartments aren't dropped.
_KIMCHI_LOCATIONS = ("TOP", "MIDDLE", "BOTTOM", "LEFT", "RIGHT")


def _kimchi_descriptions(coord: PatDeviceCoordinator) -> tuple[MyLgSensorDescription, ...]:
    present = {
        item.get("locationName")
        for item in (coord.get("temperature") or [])
        if isinstance(item, dict)
    }
    descs = [
        _loc_text(f"{loc.lower()}_mode", "temperature", loc, "targetTemperature")
        for loc in _KIMCHI_LOCATIONS
        if loc in present
    ]
    descs.append(_text("one_touch_filter", "refrigeration", "oneTouchFilter"))
    return tuple(descs)

DISHWASHER_SENSORS: tuple[MyLgSensorDescription, ...] = (
    _text("current_status", "runState", "currentState"),
    _text("current_course", "dishWashingCourse", "currentDishWashingCourse"),
    _timer("remaining", "remainHour", "remainMinute"),
    _timer("total_time", "totalHour", "totalMinute"),
)

WATER_PURIFIER_SENSORS: tuple[MyLgSensorDescription, ...] = (
    _text("cock_state", "runState", "cockState"),
    _text("sterilizing_state", "runState", "sterilizingState"),
)

def _mins(h, m):
    if h is None and m is None:
        return None
    return (h or 0) * 60 + (m or 0)


def _named(key: str, name: str, value_fn, **kw) -> MyLgSensorDescription:
    return MyLgSensorDescription(key=key, name=name, value_fn=value_fn, **kw)


OVEN_SENSORS: tuple[MyLgSensorDescription, ...] = (
    _named("oven_status", "Status", lambda c: c.get_zone("UPPER", "runState", "currentState")),
    _named(
        "oven_target_temp", "Target temperature",
        lambda c: c.get_zone("UPPER", "temperature", "targetTemperature"),
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    _named(
        "oven_remaining", "Remaining",
        lambda c: _mins(c.get_zone("UPPER", "timer", "remainHour"), c.get_zone("UPPER", "timer", "remainMinute")),
        native_unit_of_measurement="min",
    ),
)


def _zone(loc: str, label: str) -> tuple[MyLgSensorDescription, ...]:
    return (
        _named(
            f"{loc.lower()}_state",
            f"{label} state",
            lambda c, location=loc: c.get_zone(
                location, "cookingZone", "currentState"
            ),
        ),
        _named(
            f"{loc.lower()}_power",
            f"{label} power level",
            lambda c, location=loc: c.get_zone(location, "power", "powerLevel"),
            state_class=SensorStateClass.MEASUREMENT,
        ),
    )


COOKTOP_SENSORS: tuple[MyLgSensorDescription, ...] = (
    *_zone("LEFT_FRONT", "Left front"),
    *_zone("RIGHT_FRONT", "Right front"),
    *_zone("LEFT_REAR", "Left rear"),
    *_zone("RIGHT_REAR", "Right rear"),
)

WASHTOWER_PAT_SENSORS: tuple[MyLgSensorDescription, ...] = (
    _named("washer_status", "Washer status", lambda c: c.get("washer", "runState", "currentState")),
    _named("washer_remaining", "Washer remaining",
           lambda c: _mins(c.get("washer", "timer", "remainHour"), c.get("washer", "timer", "remainMinute")),
           native_unit_of_measurement="min"),
    _named("washer_total", "Washer total time",
           lambda c: _mins(c.get("washer", "timer", "totalHour"), c.get("washer", "timer", "totalMinute")),
           native_unit_of_measurement="min"),
    _named("washer_cycles", "Washer cycles", lambda c: c.get("washer", "cycle", "cycleCount"),
           state_class=SensorStateClass.TOTAL_INCREASING),
    _named("dryer_status", "Dryer status", lambda c: c.get("dryer", "runState", "currentState")),
    _named("dryer_remaining", "Dryer remaining",
           lambda c: _mins(c.get("dryer", "timer", "remainHour"), c.get("dryer", "timer", "remainMinute")),
           native_unit_of_measurement="min"),
    _named("dryer_total", "Dryer total time",
           lambda c: _mins(c.get("dryer", "timer", "totalHour"), c.get("dryer", "timer", "totalMinute")),
           native_unit_of_measurement="min"),
)

STYLER_PAT_SENSORS: tuple[MyLgSensorDescription, ...] = (
    _named("styler_status", "Status", lambda c: c.get("runState", "currentState")),
    _named("styler_remaining", "Remaining",
           lambda c: _mins(c.get("timer", "remainHour"), c.get("timer", "remainMinute")),
           native_unit_of_measurement="min"),
    _named("styler_total", "Total time",
           lambda c: _mins(c.get("timer", "totalHour"), c.get("timer", "totalMinute")),
           native_unit_of_measurement="min"),
)

PAT_SENSORS_BY_TYPE: dict[str, tuple[MyLgSensorDescription, ...]] = {
    DEVICE_TYPE_AIR_CONDITIONER: AC_SENSORS,
    DEVICE_TYPE_AIR_PURIFIER: AIR_PURIFIER_SENSORS,
    DEVICE_TYPE_HUMIDIFIER: HUMIDIFIER_SENSORS,
    DEVICE_TYPE_REFRIGERATOR: REFRIGERATOR_SENSORS,
    DEVICE_TYPE_DISH_WASHER: DISHWASHER_SENSORS,
    DEVICE_TYPE_WATER_PURIFIER: WATER_PURIFIER_SENSORS,
    DEVICE_TYPE_OVEN: OVEN_SENSORS,
    DEVICE_TYPE_COOKTOP: COOKTOP_SENSORS,
    DEVICE_TYPE_WASHTOWER: WASHTOWER_PAT_SENSORS,
    DEVICE_TYPE_STYLER: STYLER_PAT_SENSORS,
}

# Device types whose sensor set depends on the device's reported layout and so
# must be built per-device (see _kimchi_descriptions).
DYNAMIC_PAT_SENSORS: dict[str, Callable[[PatDeviceCoordinator], tuple[MyLgSensorDescription, ...]]] = {
    DEVICE_TYPE_KIMCHI_REFRIGERATOR: _kimchi_descriptions,
}


@dataclass(frozen=True, kw_only=True)
class WideqSensorDescription(SensorEntityDescription):
    """Sensor description reading from a wideq snapshot dict (dotted keys)."""

    value_fn: Callable[[dict], float | None]
    history_key: str | None = None


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
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda s: None,
        history_key="today",
    ),
    WideqSensorDescription(
        key="energy_month",
        translation_key="energy_month",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda s: None,
        history_key="month",
    ),
)

WIDEQ_REFRIGERATOR_SENSORS: tuple[WideqSensorDescription, ...] = (
    WideqSensorDescription(
        key="energy_today",
        translation_key="energy_today",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda s: None,
        history_key="today",
    ),
    WideqSensorDescription(
        key="energy_month",
        translation_key="energy_month",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda s: None,
        history_key="month",
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


def _wenergy(key: str, tkey: str, path: tuple[str, ...]) -> WideqSensorDescription:
    return WideqSensorDescription(
        key=key,
        translation_key=tkey,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_wq(*path),
    )


def _wtext(key: str, name: str, path: tuple[str, ...]) -> WideqSensorDescription:
    return WideqSensorDescription(key=key, name=name, value_fn=_wq(*path))


def _wminutes(key: str, name: str, path: tuple[str, ...]) -> WideqSensorDescription:
    return WideqSensorDescription(
        key=key, name=name, native_unit_of_measurement="min", value_fn=_wq(*path)
    )


# All wideq-only (PAT cannot provide these). Snapshot is nested under
# washer/dryer/styler dicts (unlike AC's flat dotted keys).
WASHTOWER_SENSORS: tuple[WideqSensorDescription, ...] = (
    _wtext("washer_state", "Washer state", ("washer", "state")),
    _wtext("washer_course", "Washer course", ("washer", "course")),
    _wtext("washer_spin", "Washer spin", ("washer", "spin")),
    _wtext("washer_water_temp", "Washer water temp", ("washer", "temp")),
    _wtext("washer_water_level", "Washer water level", ("washer", "waterLevel")),
    _wminutes("washer_remain", "Washer remaining", ("washer", "remainTimeMinute")),
    _wtext("washer_error", "Washer error", ("washer", "error")),
    _wtext("washer_door_lock", "Washer door lock", ("washer", "doorLock")),
    _wtext("washer_child_lock", "Washer child lock", ("washer", "childLock")),
    _wenergy("washer_energy", "washer_energy", ("washer", "accumulatedEnergyData")),
    _wtext("dryer_state", "Dryer state", ("dryer", "state")),
    _wtext("dryer_dry_level", "Dryer dry level", ("dryer", "dryLevel")),
    _wminutes("dryer_remain", "Dryer remaining", ("dryer", "remainTimeMinute")),
    _wtext("dryer_duct_clogging", "Dryer duct clogging", ("dryer", "ductClogging")),
    _wtext("dryer_error", "Dryer error", ("dryer", "error")),
    _wenergy("dryer_energy", "dryer_energy", ("dryer", "accumulatedEnergyData")),
)

STYLER_SENSORS: tuple[WideqSensorDescription, ...] = (
    _wtext("styler_state", "State", ("styler", "state")),
    _wtext("styler_course", "Course", ("styler", "course")),
    _wminutes("styler_remain", "Remaining", ("styler", "remainTimeMinute")),
    _wtext("styler_night_dry", "Night dry", ("styler", "nightDry")),
    _wtext("styler_door_lock", "Door lock", ("styler", "doorLock")),
    _wtext("styler_child_lock", "Child lock", ("styler", "childLock")),
    _wtext("styler_error", "Error", ("styler", "error")),
    _wenergy("styler_energy", "styler_energy", ("styler", "accumulatedEnergyData")),
)

WIDEQ_SENSORS_BY_TYPE: dict[str, tuple[WideqSensorDescription, ...]] = {
    DEVICE_TYPE_AIR_CONDITIONER: WIDEQ_AC_SENSORS,
    DEVICE_TYPE_REFRIGERATOR: WIDEQ_REFRIGERATOR_SENSORS,
    DEVICE_TYPE_KIMCHI_REFRIGERATOR: WIDEQ_REFRIGERATOR_SENSORS,
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
        descs = PAT_SENSORS_BY_TYPE.get(coordinator.device_type, ())
        builder = DYNAMIC_PAT_SENSORS.get(coordinator.device_type)
        if builder is not None:
            descs = descs + builder(coordinator)
        # Create a sensor when the device reports the field now, or when its
        # profile advertises the capability (so offline-at-startup devices keep
        # their entities instead of losing them until the next reload).
        for desc in descs:
            if desc.value_fn(coordinator) is not None or (
                desc.profile_group is not None
                and coordinator.supports(desc.profile_group)
            ):
                entities.append(MyLgSensor(coordinator, desc))
        # wideq-backed sensors (only if wideq is configured).
        if data.wideq_coordinator is not None:
            for wdesc in WIDEQ_SENSORS_BY_TYPE.get(coordinator.device_type, ()):
                entities.append(
                    WideqDeviceSensor(data.wideq_coordinator, coordinator, wdesc)
                )
    async_add_entities(entities)

    # The complete audited RAW inventory is registered disabled by default.
    # Catalog paths make entities available before the deliberately delayed
    # first WideQ poll; listeners add genuinely new firmware fields later
    # without triggering any additional network request.
    manager = RawSensorManager(
        list(data.coordinators.values()), data.wideq_coordinator, async_add_entities
    )
    manager.add_new()
    for coordinator in data.coordinators.values():
        entry.async_on_unload(coordinator.async_add_listener(manager.add_new))
    if data.wideq_coordinator is not None:
        entry.async_on_unload(data.wideq_coordinator.async_add_listener(manager.add_new))


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
        # Cumulative energy meters must not vanish when the device drops out of
        # the wideq snapshot (e.g. AC powered off); a gap would break long-term
        # statistics. Cache the last reading and keep reporting it.
        self._is_energy = description.device_class == SensorDeviceClass.ENERGY
        self._last_value: float | None = None

    @property
    def available(self) -> bool:
        if self.entity_description.history_key is not None:
            return self.coordinator.energy_history_available(
                self._alias, self.entity_description.history_key
            )
        if self._alias in (self.coordinator.data or {}):
            return True
        # energy: stay available on cached value even while device is absent
        return self._is_energy and self._last_value is not None

    @property
    def native_value(self) -> float | None:
        if self.entity_description.history_key is not None:
            return self.coordinator.energy_history_value(
                self._alias, self.entity_description.history_key
            )

        value = self.entity_description.value_fn(
            self.coordinator.snapshot_for(self._alias)
        )
        if value is not None:
            if self._is_energy:
                self._last_value = value
            return value
        # device absent/None: hold last energy reading (cumulative), else None
        return self._last_value if self._is_energy else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(self.coordinator.diagnostic_attributes)
        if self.entity_description.history_key is not None:
            attrs.update(self.coordinator.energy_history_attributes(self._alias))
        elif self.entity_description.key in {
            "washer_energy",
            "dryer_energy",
            "styler_energy",
        }:
            attrs.update(
                {
                    "energy_source": "wideq_snapshot",
                    "energy_scope": "current_or_last_cycle",
                }
            )
        return attrs
