"""PAT-based sensors for air conditioners (humidity, temperature, filter)."""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
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
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import MyLgConfigEntry
from .compat import AddConfigEntryEntitiesCallback, UnitOfDensity
from .const import (
    AIR_PURIFIER_ENERGY_HISTORY_MODELS,
    DEVICE_TYPE_AIR_CONDITIONER,
    DEVICE_TYPE_AIR_PURIFIER,
    DEVICE_TYPE_COOKTOP,
    DEVICE_TYPE_DEHUMIDIFIER,
    DEVICE_TYPE_DISH_WASHER,
    DEVICE_TYPE_HUMIDIFIER,
    DEVICE_TYPE_KIMCHI_REFRIGERATOR,
    DEVICE_TYPE_OVEN,
    DEVICE_TYPE_REFRIGERATOR,
    DEVICE_TYPE_STYLER,
    DEVICE_TYPE_WASHTOWER,
    DEVICE_TYPE_WATER_PURIFIER,
    DOMAIN,
    OPT_LOCAL_READ_DUPLICATE_OVERLAY,
)
from .coordinator import PatDeviceCoordinator
from .coordinator_wideq import WideqCoordinator
from .entity import MyLgEntity
from .local_entity import (
    LocalSemanticEntityMixin,
    TlvReadEntityMixin,
    iter_local_semantic_contracts,
    iter_tlv_read_contracts,
    local_semantic_unique_id,
)
from .local_energy_provider import CumulativeEnergyShadowProvider
from .local_provider import (
    LocalSemanticFieldContract,
    LocalSemanticShadowProvider,
)
from .local_read_owner import local_climate_promoted_semantics
from .local_read_provider import (
    TLV_READ_DIAGNOSTIC_KEYS,
    TlvReadFieldContract,
    TlvReadShadowProvider,
)
from .power_save import ac_power_save_attributes, ac_power_save_mode
from .raw_sensor import RawSensorManager

_LOGGER = logging.getLogger(__name__)


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
        native_unit_of_measurement=UnitOfDensity.MICROGRAMS_PER_CUBIC_METER,
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

    value_fn: Callable[[dict], Any]
    attribute_fn: Callable[[dict], dict[str, Any]] | None = None
    history_key: str | None = None


WIDEQ_AC_SENSORS: tuple[WideqSensorDescription, ...] = (
    WideqSensorDescription(
        key="power_save_mode",
        translation_key="power_save_mode",
        device_class=SensorDeviceClass.ENUM,
        options=[
            "off",
            "general",
            "comfortable",
            "dehumidification",
            "mixed",
        ],
        value_fn=ac_power_save_mode,
        attribute_fn=ac_power_save_attributes,
    ),
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

WIDEQ_ENERGY_HISTORY_SENSORS: tuple[WideqSensorDescription, ...] = (
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

# Refrigerators use a different ThinQ Web route but expose the same period
# totals and therefore share the entity descriptions.
WIDEQ_REFRIGERATOR_SENSORS = WIDEQ_ENERGY_HISTORY_SENSORS


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
    """Describe a current/last-cycle energy counter.

    ThinQ snapshot ``accumulatedEnergyData`` resets between appliance cycles
    and may be missed while the device is offline.  It is useful as a display
    value, but it is not a monotonic meter and therefore must not participate
    in Home Assistant long-term statistics as ``total_increasing``.
    """
    return WideqSensorDescription(
        key=key,
        translation_key=tkey,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        value_fn=_wq(*path),
    )


def _wtext(key: str, name: str, path: tuple[str, ...]) -> WideqSensorDescription:
    return WideqSensorDescription(key=key, name=name, value_fn=_wq(*path))


def _wminutes(key: str, name: str, path: tuple[str, ...]) -> WideqSensorDescription:
    return WideqSensorDescription(
        key=key, name=name, native_unit_of_measurement="min", value_fn=_wq(*path)
    )


def _wnonnegative_count(*path: str):
    """Read one resettable non-negative count from a WideQ snapshot."""

    getter = _wq(*path)

    def read(snap: dict) -> int | None:
        value = getter(snap)
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value >= 0 else None
        if isinstance(value, str) and value.isascii() and value.isdecimal():
            return int(value)
        return None

    return read


# D121110 does not carry tclCount in its local AABB frames.  ThinQ's retained
# account snapshot does: the exact path changed 52 -> 0 at the 2026-09-06
# MACHINE_CLEAN completion boundary.  Keep this cloud-owned read separate from
# the local read contract instead of inventing a local byte mapping.
DISHWASHER_WIDEQ_SENSORS: tuple[WideqSensorDescription, ...] = (
    WideqSensorDescription(
        key="dishwasher_tub_clean_count",
        translation_key="dishwasher_tub_clean_count",
        icon="mdi:dishwasher-alert",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_wnonnegative_count("dishwasher", "tclCount"),
    ),
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
    DEVICE_TYPE_DEHUMIDIFIER: WIDEQ_ENERGY_HISTORY_SENSORS,
    DEVICE_TYPE_REFRIGERATOR: WIDEQ_REFRIGERATOR_SENSORS,
    DEVICE_TYPE_KIMCHI_REFRIGERATOR: WIDEQ_REFRIGERATOR_SENSORS,
    DEVICE_TYPE_COOKTOP: WIDEQ_ENERGY_HISTORY_SENSORS,
    DEVICE_TYPE_OVEN: WIDEQ_ENERGY_HISTORY_SENSORS,
    DEVICE_TYPE_WATER_PURIFIER: WIDEQ_ENERGY_HISTORY_SENSORS,
    DEVICE_TYPE_DISH_WASHER: DISHWASHER_WIDEQ_SENSORS,
    DEVICE_TYPE_WASHTOWER: WASHTOWER_SENSORS,
    DEVICE_TYPE_STYLER: STYLER_SENSORS + WIDEQ_ENERGY_HISTORY_SENSORS,
}

# Energy history is model-gated rather than device-type-gated.  The tower
# purifier has a verified non-zero ``periodicEnergyData`` feed; the ordinary
# purifier shares the same device type but returns only placeholder zeroes.
WIDEQ_SENSORS_BY_MODEL: dict[str, tuple[WideqSensorDescription, ...]] = {
    model: WIDEQ_ENERGY_HISTORY_SENSORS
    for model in AIR_PURIFIER_ENERGY_HISTORY_MODELS
}


# Only exact same-domain/unit representations belong here.  A climate
# attribute, a boolean rendered as an ON/OFF text sensor, or seconds rendered
# as minutes deliberately remains a complete-feed entity.
_PAT_SENSOR_SEMANTICS: dict[str, dict[str, str]] = {
    DEVICE_TYPE_AIR_CONDITIONER: {
        "humidity": "humidity.current_pct",
        "current_temperature": "temperature.current_c",
    },
    DEVICE_TYPE_AIR_PURIFIER: {
        "pm1": "air_quality.pm1_ug_m3",
        "pm2_5": "air_quality.pm2_5_ug_m3",
        "pm10": "air_quality.pm10_ug_m3",
        "humidity": "humidity.current_pct",
    },
    DEVICE_TYPE_HUMIDIFIER: {
        "pm1": "air_quality.pm1_ug_m3",
        "pm2_5": "air_quality.pm2_5_ug_m3",
        "pm10": "air_quality.pm10_ug_m3",
        "humidity": "humidity.current_pct",
        "current_temperature": "temperature.current_c",
    },
    DEVICE_TYPE_DISH_WASHER: {
        "current_status": "cycle.state",
        "current_course": "cycle.course",
        "remaining": "cycle.remaining_min",
        "total_time": "cycle.total_min",
    },
    DEVICE_TYPE_OVEN: {"oven_status": "oven.upper.state"},
    DEVICE_TYPE_COOKTOP: {
        "left_front_state": "burner.left_front.state",
        "left_front_power": "burner.left_front.power_level",
        "left_rear_state": "burner.left_rear.state",
        "left_rear_power": "burner.left_rear.power_level",
        "right_front_state": "burner.right_front.state",
        "right_front_power": "burner.right_front.power_level",
    },
    DEVICE_TYPE_WASHTOWER: {
        "washer_status": "washer.cycle.state",
        "washer_remaining": "washer.cycle.remaining_min",
        "dryer_status": "dryer.cycle.state",
        "dryer_remaining": "dryer.cycle.remaining_min",
    },
    DEVICE_TYPE_STYLER: {"styler_status": "cycle.state"},
}
_WIDEQ_SENSOR_SEMANTICS = {
    "styler_course": "cycle.course",
    "washer_state": "washer.cycle.state",
    "washer_remain": "washer.cycle.remaining_min",
    "washer_child_lock": "washer.lock.child_enabled",
    "dryer_state": "dryer.cycle.state",
    "dryer_remain": "dryer.cycle.remaining_min",
}

# Legacy entity IDs used by dashboards can take an exact Local state without
# changing their registry identity. Keep this model-scoped: similar-looking
# values from another appliance are not evidence that its wire is identical.
_LOCAL_LEGACY_SENSOR_SEMANTICS: dict[tuple[str, str], str] = {
    ("WTL_KPK_BDH_KR_01", "washer_state"): "washer.cycle.state",
    ("WTL_KPK_BDH_KR_01", "washer_remain"): "washer.cycle.remaining_min",
    ("WTL_KPK_BDH_KR_01", "washer_child_lock"): "washer.lock.child_enabled",
    ("WTL_KPK_BDH_KR_01", "dryer_remain"): "dryer.cycle.remaining_min",
    ("ST_R_ETH01Y_", "styler_state"): "cycle.state",
    ("ST_R_ETH01Y_", "styler_course"): "cycle.course",
}

_LOCAL_LEGACY_SENSOR_TYPES = {
    "washer_state": "string",
    "washer_remain": "number",
    "washer_child_lock": "boolean",
    "dryer_remain": "number",
    "styler_state": "string",
    "styler_course": "string",
}

# The producer's reviewed local labels are not the old WideQ vocabulary.
# Translate only exact known labels; an unknown wire code stays unavailable.
_WASHER_LOCAL_TO_WIDEQ_STATE = {
    "power off": "POWEROFF",
    "initial": "INITIAL",
    "pause": "PAUSE",
    "detecting": "DETECTING",
    "filling": "ADD_DRAIN",
    "detecting detergent amount": "DETERGENT_AMOUNT",
    "soaking": "SOAK",
    "prewash": "PREWASH",
    "washing": "RUNNING",
    "rinsing": "RINSING",
    "rinse hold": "RINSEHOLD",
    "spinning": "SPINNING",
    "drying": "DRYING",
    "wash complete": "END",
    "wrinkle care": "REFRESHING",
    "error": "ERROR_AUTO_OFF",
    "anti-freeze standby": "FROZEN_PREVENT_INITIAL",
    "anti-freeze pause": "FROZEN_PREVENT_PAUSE",
    "anti-freeze running": "FROZEN_PREVENT_RUNNING",
    "audible diagnosis": "AUDIBLE_DIAGNOSIS",
    "auto detergent pause": "AUTO_DT_OPEN_PAUSE",
    "setting": "CONFIRM_START_FOR_CONTROL",
    "recognizing garment": "CLOTHING_RECOGNITION",
    "detergent input": "DETERGENT_INPUT",
    "softener input": "SOFTENER_INPUT",
    "detecting soil": "POLLUTION_DETECTING",
    "tub cleaning": "TUB_CLEANING",
    "complete/remote maintain": "END_REMOTE_MAINTAIN_ON",
    "steam": "STEAM",
    "laundry care": "LAUNDRYCARE",
    "dispenser cleaning": "EZDISPENSE_CLEANING",
    "awaiting completion": "END_WAITING",
}

_STYLER_LOCAL_TO_WIDEQ_STATE = {
    "power_off": "POWEROFF",
    "initial": "INITIAL",
    "pause": "PAUSE",
    "complete": "COMPLETE",
    "reserved": "RESERVED",
    "end_remote_maintain_on": "END_REMOTE_MAINTAIN_ON",
    "detecting": "DETECTING",
    "presteam": "PRESTEAM",
    "steam_spray": "STEAM_SPRAY",
    "drying": "DRYING",
    "dehume": "DEHUME",
    "steamer_running": "STEAMER_RUNNING",
}

# Full-read rows for these exact AABB locators are independently selectable in
# the feature database. They retain the legacy unique IDs when enabled; no
# guessed cloud value is synthesized for an unknown wire code.
_LOCAL_LEGACY_READ_SEMANTICS: dict[tuple[str, str], str] = {
    ("WTL_KPK_BDH_KR_01", "washer_energy"): "washer.cycle.energy_wh",
    ("WTL_KPK_BDH_KR_01", "dryer_energy"): "dryer.cycle.energy_wh",
    ("WTL_KPK_BDH_KR_01", "washer_course"): "diagnostic.washer.course_raw",
    ("WTL_KPK_BDH_KR_01", "washer_spin"): "diagnostic.washer.spin_setting_raw",
    ("WTL_KPK_BDH_KR_01", "washer_water_temp"): "diagnostic.washer.wash_temperature_raw",
    ("WTL_KPK_BDH_KR_01", "washer_water_level"): "diagnostic.washer.water_level_raw",
    ("WTL_KPK_BDH_KR_01", "washer_error"): "washer.error.code_raw",
    ("WTL_KPK_BDH_KR_01", "washer_door_lock"): "diagnostic.washer.lock_detection_options_bitmap_raw",
    ("WTL_KPK_BDH_KR_01", "dryer_state"): "diagnostic.dryer.state_raw",
    ("WTL_KPK_BDH_KR_01", "dryer_dry_level"): "diagnostic.dryer.dry_level_raw",
    ("WTL_KPK_BDH_KR_01", "dryer_duct_clogging"): "diagnostic.dryer.vent_blockage_raw",
    ("WTL_KPK_BDH_KR_01", "dryer_error"): "dryer.error.code_raw",
    ("ST_R_ETH01Y_", "styler_remain"): "cycle.remaining_min",
    ("ST_R_ETH01Y_", "styler_door_lock"): "lock.door_enabled",
    ("ST_R_ETH01Y_", "styler_night_dry"): "option.night_dry_enabled",
    ("ST_R_ETH01Y_", "styler_energy"): "diagnostic.cycle.course_spend_power_raw",
}
_LOCAL_LEGACY_READ_TYPES = {
    key: (("boolean",) if key in {"styler_door_lock", "styler_night_dry"} else ("number",))
    for _model, key in _LOCAL_LEGACY_READ_SEMANTICS
}

_WASHER_COURSE_RAW_TO_WIDEQ = {
    8: "BABYCARE", 27: "DUVET", 46: "NORMAL", 55: "RINSE_SPIN",
    74: "SPEEDWASH", 76: "SPEEDBOIL", 78: "SPIN_ONLY", 84: "TOWELS",
    85: "TUB_CLEAN", 94: "WOOL", 95: "SINGLE_SHIRTS", 114: "AI_COURSE",
    6: "ANSIMCOLD", 18: "COLORCARE", 56: "RINSEONLY", 65: "SILENT",
    70: "SOAK", 79: "SPORTS_WEARS", 89: "WASHONLY", 106: "RAINY_DAY",
    108: "SHIRT", 109: "SINGLE_GARMENTS", 113: "SWEAT_STAIN",
}
_WASHER_SPIN_RAW_TO_WIDEQ = {
    0: "NO_SPIN", 1: "SPIN_400", 2: "SPIN_600", 4: "SPIN_800",
    6: "SPIN_1000", 8: "SPIN_1200",
}
_WASHER_TEMP_RAW_TO_WIDEQ = {
    0: "NO_TEMP", 2: "TEMP_30", 3: "TEMP_40", 5: "TEMP_60",
    6: "TEMP_95", 8: "TEMP_COLD",
}
_DRYER_STATE_RAW_TO_WIDEQ = {
    0: "POWEROFF", 1: "INITIAL", 2: "RUNNING", 3: "PAUSE", 4: "END",
    5: "ERROR", 6: "AUDIBLE_DIAGNOSIS", 7: "DRYING", 8: "COOLING",
    9: "WRINKLECARE", 10: "RESERVED", 11: "DELAYLOAD", 12: "SPINREERVE",
    13: "AUTOTEST", 14: "DETECTING", 15: "STEAM",
    16: "CLOTHING_RECOGNITION", 17: "CONDENSER_CLEAN",
    18: "BEDDINGBRUSHING", 19: "DRY_REFRESHING", 20: "ALLERGYCARE",
    21: "CONDENSERCARE", 22: "END_REMOTE_MAINTAIN_ON", 23: "DRYREADY",
    24: "LAUNDRYCARE", 25: "DEHUMIDIFICATION",
    26: "DEHUMIDIFICATION_END", 27: "END_WAITING",
}


def _legacy_read_value(key: str, value: object) -> int | float | str | None:
    # Historical ThinQ accumulatedEnergyData follows the same resettable
    # course-Wh counter at roughly 15-minute boundaries. Prefer the exact
    # Local counter rather than reproducing the delayed cloud readback; this
    # is not lifetime energy.
    if key in {"washer_energy", "dryer_energy", "styler_energy"}:
        return value if type(value) is int and 0 <= value <= 0xffff else None
    if key == "dryer_state" and type(value) is int:
        return _DRYER_STATE_RAW_TO_WIDEQ.get(value)
    if key == "washer_course" and type(value) is int:
        return _WASHER_COURSE_RAW_TO_WIDEQ.get(value)
    if key == "washer_spin" and type(value) is int:
        return _WASHER_SPIN_RAW_TO_WIDEQ.get(value)
    if key == "washer_water_temp" and type(value) is int:
        return _WASHER_TEMP_RAW_TO_WIDEQ.get(value)
    if key == "washer_water_level" and type(value) is int and value == 0:
        return "WATERLEVEL_1"
    if key in {"washer_error", "dryer_error"} and type(value) is int and value == 0:
        return "ERROR_NO"
    if key == "washer_door_lock" and type(value) is int:
        return "DOORLOCK_ON" if value & 0x01 else "DOORLOCK_OFF"
    if key == "dryer_dry_level" and type(value) is int and value == 0:
        return "NO_DRYLEVEL"
    if key == "dryer_duct_clogging" and type(value) is int and value == 0:
        return "DUCT_CLOGGING_LEVEL_0"
    if key == "styler_remain" and type(value) in (int, float) and value >= 0:
        return value
    if key == "styler_door_lock" and type(value) is bool:
        return "DOOR_LOCK_ON" if value else "DOOR_LOCK_OFF"
    if key == "styler_night_dry" and type(value) is bool:
        return "NIGHTDRY_ON" if value else "NIGHTDRY_OFF"
    return None

# These are the only retained-current W measurements in the complete audited
# 14-profile catalogue.  Both are instantaneous and therefore physically
# integrable.  Wh interval events and washer/dryer cycle Wh values are already
# energy quantities and must never be integrated a second time.
AC_POWER_SEMANTICS = frozenset(
    {
        "power.indoor_compressor_share_w",
        "power.outdoor_unit_total_w",
    }
)
AC_REPORTED_ENERGY_REFERENCE_MODELS = frozenset({"CST_170004_WW", "CST_570004_WW"})

# The bridge refreshes an active AC at roughly 28 seconds and falls back to a
# 15-minute status query while quiescent.  Twenty minutes admits that documented
# idle cadence plus jitter, while refusing to estimate over a lost publication
# epoch or an extended bridge/device outage.
MAX_TLV_POWER_INTEGRATION_GAP = timedelta(minutes=20)

_POWER_SCOPE_ATTRIBUTES: dict[str, dict[str, object]] = {
    "power.indoor_compressor_share_w": {
        "power_scope": "indoor_compressor_share",
        "measurement_scope": "one_indoor_binding",
        "may_duplicate_across_indoor_bindings": False,
        "scope_warning": (
            "indoor compressor share only; this is not whole-system energy"
        ),
    },
    "power.outdoor_unit_total_w": {
        "power_scope": "shared_outdoor_unit_total",
        "measurement_scope": "shared_outdoor_unit",
        "may_duplicate_across_indoor_bindings": True,
        "scope_warning": (
            "shared outdoor total may duplicate across indoor bindings; "
            "do not sum those bindings"
        ),
    },
}

_PM_DEVICE_CLASSES = {
    "air_quality.pm1_ug_m3": SensorDeviceClass.PM1,
    "air_quality.pm2_5_ug_m3": SensorDeviceClass.PM25,
    "air_quality.pm10_ug_m3": SensorDeviceClass.PM10,
}

def _same_unit(
    description: SensorEntityDescription, contract: TlvReadFieldContract | None
) -> bool:
    description_unit = description.native_unit_of_measurement
    exact_or_known_equivalent = contract is not None and _units_match(
        contract.unit, description_unit
    )
    return (
        contract is not None
        and contract.domain == "sensor"
        and exact_or_known_equivalent
    )


def _ac_pat_sensor_replaced_by_local_leaf(
    coordinator: PatDeviceCoordinator,
    description: MyLgSensorDescription,
    semantic_id: str | None,
    contract: TlvReadFieldContract | None,
) -> bool:
    """Require exact domain/type/unit equivalence before removing a PAT leaf."""
    return (
        coordinator.device_type == DEVICE_TYPE_AIR_CONDITIONER
        and semantic_id
        in {"temperature.current_c", "humidity.current_pct"}
        and contract is not None
        and contract.semantic_id == semantic_id
        and contract.domain == "sensor"
        and contract.value_types == ("number",)
        and _units_match(contract.unit, description.native_unit_of_measurement)
    )


def _integrable_ac_power_contract(
    coordinator: PatDeviceCoordinator,
    semantic_id: str,
    contract: TlvReadFieldContract,
) -> bool:
    """Return whether a field is one exact instantaneous AC W source."""
    return (
        coordinator.device_type == DEVICE_TYPE_AIR_CONDITIONER
        and semantic_id in AC_POWER_SEMANTICS
        and contract.semantic_id == semantic_id
        and contract.domain == "sensor"
        and contract.value_types == ("number",)
        and contract.unit == UnitOfPower.WATT
        and contract.publication_mode == "retained-current"
    )


def _tlv_sensor_metadata(
    contract: TlvReadFieldContract,
) -> tuple[SensorDeviceClass | None, SensorStateClass | None]:
    """Map only exact unit/semantic combinations to HA statistics metadata."""
    if contract.value_types != ("number",):
        return None, None
    semantic_id = contract.semantic_id
    unit = contract.unit
    if semantic_id in AC_POWER_SEMANTICS and unit == UnitOfPower.WATT:
        return SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT
    if semantic_id.endswith(".energy_wh") and unit == UnitOfEnergy.WATT_HOUR:
        # These retained values reset per appliance cycle. They are useful
        # energy readings, but are deliberately not monotonic long-term meters.
        return SensorDeviceClass.ENERGY, None
    if unit == UnitOfTemperature.CELSIUS:
        return SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT
    if semantic_id.startswith("humidity.") and unit == PERCENTAGE:
        return SensorDeviceClass.HUMIDITY, SensorStateClass.MEASUREMENT
    if semantic_id in _PM_DEVICE_CLASSES and _units_match(
        unit, UnitOfDensity.MICROGRAMS_PER_CUBIC_METER
    ):
        return _PM_DEVICE_CLASSES[semantic_id], SensorStateClass.MEASUREMENT
    if unit in {UnitOfTime.SECONDS, UnitOfTime.MINUTES, UnitOfTime.HOURS}:
        return SensorDeviceClass.DURATION, SensorStateClass.MEASUREMENT
    return None, None


def _units_match(left: str | None, right: object) -> bool:
    return left == right or {left, str(right)} == {"µg/m³", "μg/m³"}


def _same_pilot_contract(
    description: SensorEntityDescription,
    contract: LocalSemanticFieldContract | None,
) -> bool:
    return (
        contract is not None
        and contract.value_type in ("number", "string")
        and _units_match(contract.unit, description.native_unit_of_measurement)
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    from .feature_runtime import setup_feature_entities

    setup_feature_entities(entry, "sensor", lambda: _build_entities(entry), async_add_entities)
    data = entry.runtime_data
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


def _build_entities(entry: MyLgConfigEntry) -> list[SensorEntity]:
    data = entry.runtime_data
    overlay_duplicates = (
        getattr(entry, "options", {}).get(OPT_LOCAL_READ_DUPLICATE_OVERLAY) is True
    )
    entities: list[SensorEntity] = []
    for coordinator in data.coordinators.values():
        established_semantics: set[str] = set()
        legacy_read_alias_semantics: set[str] = set()
        local_provider = data.local_providers.get(coordinator.device_id)
        read_provider = getattr(data, "local_read_providers", {}).get(
            coordinator.device_id
        )
        energy_provider = getattr(data, "local_energy_providers", {}).get(
            coordinator.device_id
        )
        read_contracts = (
            read_provider.profile.fields_by_semantic_id
            if read_provider is not None
            else {}
        )
        descs = PAT_SENSORS_BY_TYPE.get(coordinator.device_type, ())
        builder = DYNAMIC_PAT_SENSORS.get(coordinator.device_type)
        if builder is not None:
            descs = descs + builder(coordinator)
        # Create a sensor when the device reports the field now, or when its
        # profile advertises the capability (so offline-at-startup devices keep
        # their entities instead of losing them until the next reload).
        for desc in descs:
            semantic_id = _PAT_SENSOR_SEMANTICS.get(
                coordinator.device_type, {}
            ).get(desc.key)
            replaced_by_local_leaf = _ac_pat_sensor_replaced_by_local_leaf(
                coordinator,
                desc,
                semantic_id,
                read_contracts.get(semantic_id) if semantic_id is not None else None,
            )
            if replaced_by_local_leaf:
                # The canonical exact Local leaf is materialized by the TLV
                # iterator below. Do not create an overlapping PAT entity or
                # mark the semantic established, even when comparison-overlay
                # mode is enabled.
                continue
            if (
                desc.value_fn(coordinator) is not None
                or (
                desc.profile_group is not None
                and coordinator.supports(desc.profile_group)
                )
            ):
                entities.append(MyLgSensor(coordinator, desc))
                if semantic_id is not None and (
                    read_provider is None
                    or (
                        semantic_id not in read_contracts
                        and _same_pilot_contract(
                            desc,
                            local_provider.profile.fields.get(semantic_id)
                            if local_provider is not None
                            else None,
                        )
                    )
                    or _same_unit(desc, read_contracts.get(semantic_id))
                ):
                    established_semantics.add(semantic_id)
        # Exact Local replacements keep the existing WideQ sensor unique ID.
        # They must also exist when WideQ is no longer configured. Anything
        # without a proven Local source retains its original cloud owner.
        wideq_descriptions = WIDEQ_SENSORS_BY_TYPE.get(
            coordinator.device_type, ()
        ) + WIDEQ_SENSORS_BY_MODEL.get(coordinator.model, ())
        for wdesc in wideq_descriptions:
            local_semantic = _LOCAL_LEGACY_SENSOR_SEMANTICS.get(
                (coordinator.model, wdesc.key)
            )
            local_contract = (
                local_provider.profile.fields.get(local_semantic)
                if local_provider is not None and local_semantic is not None
                else None
            )
            if (
                local_provider is not None
                and local_provider.model_id == coordinator.model
                and local_contract is not None
                and local_contract.exposure == "state"
                and local_contract.value_type
                == _LOCAL_LEGACY_SENSOR_TYPES[wdesc.key]
                and local_contract.unit == wdesc.native_unit_of_measurement
            ):
                entities.append(
                    LocalLegacySensor(
                        local_provider,
                        coordinator,
                        local_semantic,
                        local_contract,
                        wdesc,
                    )
                )
                established_semantics.add(local_semantic)
                continue
            read_semantic = _LOCAL_LEGACY_READ_SEMANTICS.get(
                (coordinator.model, wdesc.key)
            )
            read_contract = (
                read_contracts.get(read_semantic)
                if read_provider is not None and read_semantic is not None
                else None
            )
            if (
                read_provider is not None
                and read_contract is not None
                and getattr(read_provider.profile, "model_id", None) == coordinator.model
                and read_contract.value_types == _LOCAL_LEGACY_READ_TYPES[wdesc.key]
                and read_contract.unit == wdesc.native_unit_of_measurement
            ):
                entities.append(
                    LocalLegacyReadSensor(
                        read_provider, coordinator, read_semantic, read_contract, wdesc
                    )
                )
                established_semantics.add(read_semantic)
                legacy_read_alias_semantics.add(read_semantic)
                continue
            if data.wideq_coordinator is not None:
                entities.append(
                    WideqDeviceSensor(data.wideq_coordinator, coordinator, wdesc)
                )
                semantic_id = _WIDEQ_SENSOR_SEMANTICS.get(wdesc.key)
                if semantic_id is not None and (
                    read_provider is None
                    or (
                        semantic_id not in read_contracts
                        and _same_pilot_contract(
                            wdesc,
                            local_provider.profile.fields.get(semantic_id)
                            if local_provider is not None
                            else None,
                        )
                    )
                    or _same_unit(wdesc, read_contracts.get(semantic_id))
                ):
                    established_semantics.add(semantic_id)
        if read_provider is not None:
            for semantic_id, contract in iter_tlv_read_contracts(
                read_provider,
                "sensor",
                established_semantics=established_semantics,
                excluded_semantics=legacy_read_alias_semantics,
                overlay_duplicates=overlay_duplicates,
            ):
                entities.append(
                    TlvReadSensor(
                        read_provider, coordinator, semantic_id, contract
                    )
                )
                if _integrable_ac_power_contract(
                    coordinator, semantic_id, contract
                ):
                    entities.append(
                        TlvIntegratedEnergySensor(
                            read_provider, coordinator, semantic_id, contract
                        )
                    )
            entities.extend(
                TlvReadDiagnosticSensor(read_provider, coordinator, diagnostic_key)
                for diagnostic_key in TLV_READ_DIAGNOSTIC_KEYS
            )
        if energy_provider is not None:
            entities.extend(
                LocalCumulativeEnergySensor(
                    energy_provider, coordinator, semantic_id
                )
                for semantic_id in energy_provider.semantic_ids
            )
        if local_provider is not None:
            full_semantics = (
                set(read_contracts) if read_provider is not None else set()
            )
            promoted_semantics = set(
                local_climate_promoted_semantics(
                    local_provider,
                    coordinator.model,
                    getattr(data, "local_control_composite_domain_contract", None),
                )
            )
            for value_type in ("number", "string"):
                for semantic_id, contract in iter_local_semantic_contracts(
                    local_provider,
                    value_type,
                    wideq_configured=data.wideq_coordinator is not None,
                    established_semantics=established_semantics,
                    excluded_semantics=full_semantics | promoted_semantics,
                    overlay_duplicates=overlay_duplicates,
                ):
                    entities.append(
                        LocalSemanticSensor(
                            local_provider, coordinator, semantic_id, contract
                        )
                    )
    from .app_setting_entity import app_setting_entities
    entities.extend(app_setting_entities(entry, 'sensor'))
    return entities


class MyLgSensor(MyLgEntity, SensorEntity):
    """A single value read from the device status dict."""

    entity_description: MyLgSensorDescription

    def __init__(
        self,
        coordinator: PatDeviceCoordinator,
        description: MyLgSensorDescription,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        return self.entity_description.value_fn(self.coordinator)


class LocalLegacySensor(LocalSemanticEntityMixin, SensorEntity):
    """An exact Local read retaining an established WideQ sensor identity."""

    entity_description: WideqSensorDescription

    def __init__(
        self,
        provider: LocalSemanticShadowProvider,
        pat_coordinator: PatDeviceCoordinator,
        semantic_id: str,
        contract: LocalSemanticFieldContract,
        description: WideqSensorDescription,
    ) -> None:
        expected_semantic = _LOCAL_LEGACY_SENSOR_SEMANTICS.get(
            (pat_coordinator.model, description.key)
        )
        if (
            expected_semantic != semantic_id
            or contract.value_type != _LOCAL_LEGACY_SENSOR_TYPES[description.key]
            or contract.exposure != "state"
            or contract.unit != description.native_unit_of_measurement
        ):
            raise ValueError("Local legacy sensor requires an exact source")
        super().__init__(provider, pat_coordinator, semantic_id, contract)
        self.entity_description = description
        self._attr_unique_id = f"{pat_coordinator.device_id}_{description.key}"
        self._attr_name = description.name
        self._attr_entity_registry_enabled_default = True

    def _display_value(self, value: object) -> int | float | str | None:
        key = self.entity_description.key
        if key == "washer_state":
            return _WASHER_LOCAL_TO_WIDEQ_STATE.get(value) if isinstance(value, str) else None
        if key == "styler_state":
            return _STYLER_LOCAL_TO_WIDEQ_STATE.get(value) if isinstance(value, str) else None
        if key == "styler_course":
            # The exact-model course code table is shared with the producer;
            # never synthesize a label for an out-of-domain raw value.
            return value if value == "NONE" or (
                isinstance(value, str)
                and re.fullmatch(r"(?:STYLING|SANITARY|DRY)_[A-Z0-9_]+_[0-9]+", value)
            ) else None
        if key == "washer_child_lock":
            return ("CHILDLOCK_ON" if value else "CHILDLOCK_OFF") if type(value) is bool else None
        if key in {"washer_remain", "dryer_remain"}:
            return value if type(value) in (int, float) and value >= 0 else None
        return None

    @property
    def native_value(self) -> int | float | str | None:
        field = self._shadow_field
        return None if field is None else self._display_value(field.value)

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class LocalLegacyReadSensor(TlvReadEntityMixin, SensorEntity):
    """One exact complete-feed field under its existing WideQ sensor identity."""

    entity_description: WideqSensorDescription

    def __init__(
        self,
        provider: TlvReadShadowProvider,
        pat_coordinator: PatDeviceCoordinator,
        semantic_id: str,
        contract: TlvReadFieldContract,
        description: WideqSensorDescription,
    ) -> None:
        if (
            _LOCAL_LEGACY_READ_SEMANTICS.get((pat_coordinator.model, description.key))
            != semantic_id
            or contract.value_types != _LOCAL_LEGACY_READ_TYPES[description.key]
            or contract.unit != description.native_unit_of_measurement
        ):
            raise ValueError("Local legacy read requires its exact model field")
        super().__init__(provider, pat_coordinator, semantic_id, contract)
        self.entity_description = description
        self._attr_unique_id = f"{pat_coordinator.device_id}_{description.key}"
        self._attr_name = description.name
        self._attr_entity_category = None
        self._attr_entity_registry_enabled_default = True
        self._attr_native_unit_of_measurement = description.native_unit_of_measurement

    @property
    def native_value(self) -> int | float | str | None:
        field = self._read_field
        return None if field is None else _legacy_read_value(self.entity_description.key, field.value)

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class LocalSemanticSensor(LocalSemanticEntityMixin, SensorEntity):
    """One exact number or string from the read-only Local profile."""

    def __init__(
        self,
        provider: LocalSemanticShadowProvider,
        pat_coordinator: PatDeviceCoordinator,
        semantic_id: str,
        contract: LocalSemanticFieldContract,
    ) -> None:
        if contract.value_type not in ("number", "string"):
            raise ValueError("Local sensor requires a number or string contract")
        super().__init__(provider, pat_coordinator, semantic_id, contract)
        self._attr_native_unit_of_measurement = contract.unit
        if contract.value_type == "string" and contract.allowed_values is not None:
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = list(contract.allowed_values)

    @property
    def native_value(self) -> int | float | str | None:
        field = self._shadow_field
        if field is None:
            return None
        value = field.value
        if self._contract.value_type == "number":
            return (
                value
                if not isinstance(value, bool) and isinstance(value, (int, float))
                else None
            )
        return value if isinstance(value, str) else None


class TlvReadSensor(TlvReadEntityMixin, SensorEntity):
    """One exact number/string/union value from the complete TLV read feed."""

    def __init__(
        self,
        provider: TlvReadShadowProvider,
        pat_coordinator: PatDeviceCoordinator,
        semantic_id: str,
        contract: TlvReadFieldContract,
    ) -> None:
        if contract.domain != "sensor":
            raise ValueError("Complete TLV sensor requires a sensor contract")
        super().__init__(provider, pat_coordinator, semantic_id, contract)
        self._attr_native_unit_of_measurement = contract.unit
        device_class, state_class = _tlv_sensor_metadata(contract)
        self._attr_device_class = device_class
        self._attr_state_class = state_class

    @property
    def native_value(self) -> int | float | str | None:
        field = self._read_field
        if field is None:
            return None
        value = field.value
        if field.value_type == "number":
            return (
                value
                if not isinstance(value, bool) and isinstance(value, (int, float))
                else None
            )
        return value if field.value_type == "string" and isinstance(value, str) else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        attributes = dict(super().extra_state_attributes)
        if self._semantic_id in _POWER_SCOPE_ATTRIBUTES:
            attributes.update(_POWER_SCOPE_ATTRIBUTES[self._semantic_id])
        if (
            self._contract.value_types == ("number",)
            and self._contract.unit == UnitOfEnergy.WATT_HOUR
            and self._semantic_id.endswith(".energy_wh")
        ):
            attributes.update(
                {
                    "energy_scope": "current_or_last_appliance_cycle",
                    "monotonic_meter": False,
                    "statistics_exclusion_reason": "value resets per appliance cycle",
                }
            )
        return attributes


class LocalCumulativeEnergySensor(SensorEntity):
    """One producer-owned durable monotonic energy total."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_should_poll = False
    _attr_suggested_display_precision = 3

    def __init__(
        self,
        provider: CumulativeEnergyShadowProvider,
        pat_coordinator: PatDeviceCoordinator,
        semantic_id: str,
    ) -> None:
        super().__init__()
        if semantic_id not in provider.semantic_ids:
            raise ValueError("Cumulative-energy semantic is not authorized")
        self._provider = provider
        self._semantic_id = semantic_id
        self._reference_only = (
            pat_coordinator.model in AC_REPORTED_ENERGY_REFERENCE_MODELS
        )
        label = {
            "energy.total_wh": "Local cumulative energy",
            "washer.energy.total_wh": "Washer local cumulative energy",
            "dryer.energy.total_wh": "Dryer local cumulative energy",
        }[semantic_id]
        if self._reference_only:
            label = "Local · 기기 보고 누적 전력량 (참고)"
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_entity_registry_enabled_default = False
            # Keep the coarse appliance report separate from official W integrals.
            # No energy statistics: this reference must not be summed a second time.
            self._attr_state_class = None
        self._attr_name = label
        self._attr_unique_id = local_semantic_unique_id(
            pat_coordinator.device_id, f"cumulative.{semantic_id}"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, pat_coordinator.device_id)},
            name=pat_coordinator.alias,
            manufacturer="LG",
            model=pat_coordinator.model or pat_coordinator.device_type,
        )
        self._remove_provider_listener = None

    @property
    def available(self) -> bool:
        return self._provider.field_available(self._semantic_id)

    @property
    def native_value(self) -> float | None:
        total_wh = self._provider.total_wh(self._semantic_id)
        return None if total_wh is None else total_wh / 1000

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "source_unit": "Wh",
            "source_semantic_id": self._semantic_id,
            "baseline_generation": self._provider.baseline_generation,
            "last_counted_generation": self._provider.last_counted_generation,
            "published_at": self._provider.published_at,
            "durability": "producer_fsynced_monotonic_ledger",
            **(
                {"usage_role": "reference_only", "excluded_from_official_energy": True}
                if self._reference_only
                else {}
            ),
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._remove_provider_listener = self._provider.async_add_listener(
            self._handle_provider_update
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_provider_listener is not None:
            self._remove_provider_listener()
            self._remove_provider_listener = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_provider_update(self) -> None:
        self.async_write_ha_state()


class TlvReadDiagnosticSensor(SensorEntity):
    """One non-sensitive counter from the latest accepted full-read current."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_should_poll = False

    def __init__(
        self,
        provider: TlvReadShadowProvider,
        pat_coordinator: PatDeviceCoordinator,
        diagnostic_key: str,
    ) -> None:
        super().__init__()
        if diagnostic_key not in TLV_READ_DIAGNOSTIC_KEYS:
            raise ValueError("TLV read diagnostic key is not authorized")
        self._provider = provider
        self._diagnostic_key = diagnostic_key
        self._attr_name = f"Local · Feed {diagnostic_key.replace('_', ' ')}"
        self._attr_unique_id = local_semantic_unique_id(
            pat_coordinator.device_id, f"feed_diagnostic.{diagnostic_key}"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, pat_coordinator.device_id)},
            name=pat_coordinator.alias,
            manufacturer="LG",
            model=pat_coordinator.model or pat_coordinator.device_type,
        )
        self._remove_provider_listener = None

    @property
    def diagnostic_key(self) -> str:
        return self._diagnostic_key

    @property
    def available(self) -> bool:
        return (
            self._provider.diagnostics_available
            and self._diagnostic_key in self._provider.current_diagnostics
        )

    @property
    def native_value(self) -> int | None:
        value = self._provider.current_diagnostics.get(self._diagnostic_key)
        return value if type(value) is int and value >= 0 else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        # Deliberately expose only the counter identity and public profile pin;
        # binding/device proofs, frames, payloads and private diagnostics stay
        # inside the provider.
        return {
            "diagnostic_counter": self._diagnostic_key,
            "profile_id": self._provider.profile.profile_id,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._remove_provider_listener = self._provider.async_add_listener(
            self._handle_provider_update
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_provider_listener is not None:
            self._remove_provider_listener()
            self._remove_provider_listener = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_provider_update(self) -> None:
        self.async_write_ha_state()


class TlvIntegratedEnergySensor(RestoreSensor):
    """A fail-closed kWh integral of one exact retained-current AC W source."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_should_poll = False
    _attr_suggested_display_precision = 3

    def __init__(
        self,
        provider: TlvReadShadowProvider,
        pat_coordinator: PatDeviceCoordinator,
        source_semantic_id: str,
        contract: TlvReadFieldContract,
    ) -> None:
        super().__init__()
        if (
            provider.profile.fields_by_semantic_id.get(source_semantic_id)
            is not contract
            or not _integrable_ac_power_contract(
                pat_coordinator, source_semantic_id, contract
            )
        ):
            raise ValueError("Integrated energy source contract is not exact")
        self._provider = provider
        self._source_semantic_id = source_semantic_id
        self._contract = contract
        self._derived_semantic_id = (
            "energy.integrated."
            f"{source_semantic_id.removeprefix('power.').removesuffix('_w')}_kwh"
        )
        duplicate_prone = bool(
            _POWER_SCOPE_ATTRIBUTES[source_semantic_id][
                "may_duplicate_across_indoor_bindings"
            ]
        )
        warning_suffix = " · 공유값 중복 합산 금지" if duplicate_prone else ""
        self._attr_name = (
            f"Local · {contract.label_ko} 누적 에너지{warning_suffix}"
        )
        self._attr_unique_id = local_semantic_unique_id(
            pat_coordinator.device_id, self._derived_semantic_id
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, pat_coordinator.device_id)},
            name=pat_coordinator.alias,
            manufacturer="LG",
            model=pat_coordinator.model or pat_coordinator.device_type,
        )
        # The source-observed-time W integral is the official AC energy source.
        # Shared outdoor readings may be duplicated across indoor bindings and
        # must not be added to the indoor reading, which already includes its share.
        self._attr_entity_registry_enabled_default = (
            contract.enabled_by_default and not duplicate_prone
        )
        self._energy_kwh = 0.0
        self._total_valid = False
        self._last_boundary: datetime | None = None
        self._last_power_w: float | None = None
        self._observation_high_water: datetime | None = None
        self._blocked_through: datetime | None = None
        self._resume_at: datetime | None = None
        self._integration_status = "awaiting_first_boundary"
        self._skipped_intervals = 0
        self._remove_provider_listener = None

    @property
    def source_semantic_id(self) -> str:
        return self._source_semantic_id

    @property
    def native_value(self) -> float:
        return self._energy_kwh

    def _current_sample(self) -> tuple[datetime, float] | None:
        if not self._provider.field_available(self._source_semantic_id):
            return None
        published_at = self._provider.current_published_at
        field = self._provider.fields.get(self._source_semantic_id)
        if published_at is None or field is None or field.value_type != "number":
            return None
        field_age = published_at - field.observed_at
        if field_age < timedelta(0) or field_age > MAX_TLV_POWER_INTEGRATION_GAP:
            # A current envelope can legitimately carry an older retained
            # field. It remains displayable, but is not fresh enough to extend
            # an energy interval.
            return None
        value = field.value
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            return None
        return field.observed_at, float(value)

    @property
    def available(self) -> bool:
        # Source freshness controls whether another interval may be integrated,
        # not whether an already restored monotonic total remains valid.
        return self._total_valid

    def _break_continuity(self, status: str) -> None:
        # Keep the processed boundary even when the interpolation anchor is
        # discarded. A carried pre-disconnect sample must not reopen a gap or
        # an interval already included in the restored cumulative total.
        boundaries = [dt_util.utcnow()]
        for value in (
            self._observation_high_water,
            self._blocked_through,
            self._provider.current_published_at,
        ):
            if value is not None:
                boundaries.append(value)
        self._blocked_through = max(boundaries)
        self._last_boundary = None
        self._last_power_w = None
        self._integration_status = status

    def _consume_current_publication(self) -> None:
        sample = self._current_sample()
        if sample is None:
            self._break_continuity(
                "source_unavailable_or_invalid"
                if self._total_valid
                else "restore_rejected"
            )
            return
        published_at, power_w = sample
        if (
            (self._resume_at is not None and published_at < self._resume_at)
            or (self._blocked_through is not None and published_at <= self._blocked_through)
        ):
            self._integration_status = "awaiting_fresh_observation"
            return
        if self._observation_high_water is not None and published_at < self._observation_high_water:
            self._skipped_intervals += 1
            self._break_continuity("non_monotonic_boundary")
            return
        self._observation_high_water = published_at
        if self._last_boundary is None or self._last_power_w is None:
            self._last_boundary = published_at
            self._last_power_w = power_w
            self._integration_status = "anchored"
            self._total_valid = True
            return
        elapsed = published_at - self._last_boundary
        if elapsed < timedelta(0):
            # Do not let a clock-regressed boundary become the next anchor: a
            # later sample could otherwise overlap an already counted interval.
            self._skipped_intervals += 1
            self._break_continuity("non_monotonic_boundary")
            return
        if elapsed == timedelta(0):
            # Same authenticated boundary can be replayed after an availability
            # change. A changed value at the same observation time is a source
            # collision, not another instantaneous sample.
            if power_w != self._last_power_w:
                self._skipped_intervals += 1
                self._break_continuity("observation_boundary_collision")
                return
            self._integration_status = "duplicate_boundary"
            return
        if elapsed > MAX_TLV_POWER_INTEGRATION_GAP:
            self._skipped_intervals += 1
            self._last_boundary = published_at
            self._last_power_w = power_w
            self._integration_status = "excessive_gap_skipped"
            return
        mean_power_w = (self._last_power_w + power_w) / 2
        self._energy_kwh += mean_power_w * elapsed.total_seconds() / 3_600_000
        self._last_boundary = published_at
        self._last_power_w = power_w
        self._integration_status = "integrated"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._resume_at = dt_util.utcnow()
        restored = await self.async_get_last_sensor_data()
        if restored is None:
            # A newly created meter deliberately adopts a zero baseline. This
            # differs from a corrupt restore, which must not expose a false
            # reset until a fresh authenticated source sample is observed.
            self._total_valid = True
        elif (
            restored is not None
            and restored.native_unit_of_measurement
            == UnitOfEnergy.KILO_WATT_HOUR
            and isinstance(restored.native_value, (int, float))
            and not isinstance(restored.native_value, bool)
            and math.isfinite(restored.native_value)
            and restored.native_value >= 0
        ):
            self._energy_kwh = float(restored.native_value)
            self._total_valid = True
        else:
            self._integration_status = "restore_rejected"
        self._remove_provider_listener = self._provider.async_add_listener(
            self._handle_provider_update
        )
        # Restoring the total never restores or estimates a power/time anchor.
        self._consume_current_publication()

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_provider_listener is not None:
            self._remove_provider_listener()
            self._remove_provider_listener = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_provider_update(self) -> None:
        self._consume_current_publication()
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        attributes: dict[str, object] = {
            "derived_semantic_id": self._derived_semantic_id,
            "source_semantic_id": self._source_semantic_id,
            "source_descriptor_key": self._contract.descriptor_key,
            "source_profile_id": self._provider.profile.profile_id,
            "integration_method": "trapezoidal",
            "integration_clock": "source_field_observed_at",
            "integration_max_gap_s": int(
                MAX_TLV_POWER_INTEGRATION_GAP.total_seconds()
            ),
            "source_freshness_max_age_s": int(
                MAX_TLV_POWER_INTEGRATION_GAP.total_seconds()
            ),
            "integration_status": self._integration_status,
            "skipped_intervals": self._skipped_intervals,
        }
        attributes.update(_POWER_SCOPE_ATTRIBUTES[self._source_semantic_id])
        return attributes


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
        self._device_id = pat_coordinator.device_id
        self._pat_coordinator = pat_coordinator
        self.entity_description = description
        self._attr_unique_id = f"{pat_coordinator.device_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, pat_coordinator.device_id)},
            name=pat_coordinator.alias,
            manufacturer="LG",
            model=pat_coordinator.model or pat_coordinator.device_type,
        )
        # Snapshot-backed energy display values may disappear while an
        # appliance is offline.  Keep the last verified cycle reading for UI
        # continuity; period totals use the separate persisted history cache.
        self._is_energy = description.device_class == SensorDeviceClass.ENERGY
        self._last_value: Any = None

    def _ac_power_is_off(self) -> bool:
        """Return whether the authoritative PAT status confirms AC power off."""
        return (
            self._pat_coordinator.device_type == DEVICE_TYPE_AIR_CONDITIONER
            and self._pat_coordinator.get("operation", "airConOperationMode")
            == "POWER_OFF"
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to PAT too so an AC power-off zero is published at once."""
        await super().async_added_to_hass()
        if self.entity_description.key == "energy_current":
            self.async_on_remove(
                self._pat_coordinator.async_add_listener(self.async_write_ha_state)
            )

    @property
    def available(self) -> bool:
        if self.entity_description.history_key is not None:
            return self.coordinator.energy_history_available(
                self._device_id, self.entity_description.history_key
            )
        if self.entity_description.key == "power_save_mode":
            return self.coordinator.power_save_available(self._device_id)
        if (
            self.entity_description.key == "energy_current"
            and self._ac_power_is_off()
        ):
            return True
        if self._device_id in (self.coordinator.data or {}):
            return True
        # energy: stay available on cached value even while device is absent
        return self._is_energy and self._last_value is not None

    @property
    def native_value(self) -> Any:
        if self.entity_description.history_key is not None:
            return self.coordinator.energy_history_value(
                self._device_id, self.entity_description.history_key
            )
        if (
            self.entity_description.key == "energy_current"
            and self._ac_power_is_off()
        ):
            return 0.0

        snapshot = (
            self.coordinator.power_save_snapshot_for(self._device_id)
            if self.entity_description.key == "power_save_mode"
            else self.coordinator.snapshot_for(self._device_id)
        )
        value = self.entity_description.value_fn(snapshot)
        if value is not None:
            if self._is_energy:
                self._last_value = value
            return value
        # Device absent/None: hold the last energy display reading, else None.
        return self._last_value if self._is_energy else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(self.coordinator.diagnostic_attributes)
        if self.entity_description.attribute_fn is not None:
            snapshot = (
                self.coordinator.power_save_snapshot_for(self._device_id)
                if self.entity_description.key == "power_save_mode"
                else self.coordinator.snapshot_for(self._device_id)
            )
            attrs.update(
                self.entity_description.attribute_fn(snapshot)
            )
        if self.entity_description.key == "power_save_mode":
            attrs.update(
                self.coordinator.power_save_diagnostic_attributes(self._device_id)
            )
        if self.entity_description.history_key is not None:
            attrs.update(
                self.coordinator.energy_history_attributes(
                    self._device_id, self.entity_description.history_key
                )
            )
        elif self.entity_description.key == "energy_current":
            attrs.update(
                {
                    "power_source": (
                        "pat_confirmed_power_off"
                        if self._ac_power_is_off()
                        else "wideq_snapshot"
                    )
                }
            )
        elif self.entity_description.key in {
            "washer_energy",
            "dryer_energy",
            "styler_energy",
        }:
            attrs.update(
                {
                    "energy_source": "wideq_snapshot",
                    "energy_scope": "current_or_last_cycle",
                    "long_term_statistics_eligible": False,
                }
            )
        return attrs
