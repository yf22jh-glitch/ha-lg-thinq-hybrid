"""Refrigerator target-temperature Number entities (PAT write).

Only compartments that report a numeric ``targetTemperature`` are settable —
kimchi fridges expose storage-mode enums (FREEZER/KIMCHI/…) and are read-only.

Write payload mirrors the thinqconnect SDK: the value goes to the
``temperatureInUnits`` resource with the compartment's ``locationName``:
    {"temperatureInUnits": {"locationName": "FRIDGE", "targetTemperatureC": 3}}
"""

from __future__ import annotations

from typing import Any

from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
)
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory

from . import MyLgConfigEntry
from .compat import AddConfigEntryEntitiesCallback
from .const import (
    DEVICE_TYPE_AIR_PURIFIER,
    DEVICE_TYPE_COOKTOP,
    DEVICE_TYPE_REFRIGERATOR,
    DEVICE_TYPE_STYLER,
    DEVICE_TYPE_WASHTOWER,
    OPT_ALLOW_HAZARDOUS_CONTROLS,
    OPT_ALLOW_EXPERIMENTAL_CONTROLS,
)
from .coordinator import PatDeviceCoordinator
from .coordinator_wideq import WideqCoordinator
from .entity import MyLgEntity, MyLgWideqEntity
from .feature import FeatureAccess
from .feature_catalog import discover_pat_features
from .value_access import is_meaningful
from .wideq_control import iter_wideq_field_controls
from .wideq_control import control_risk_allowed

# Fallback °C ranges when the profile doesn't pin them per compartment.
_FALLBACK_RANGE: dict[str, tuple[int, int]] = {
    "FRIDGE": (1, 7),
    "FREEZER": (-23, -15),
    "CONVERTIBLE": (-23, 7),
}


@dataclass(frozen=True, kw_only=True)
class MyLgWideqNumberDescription(NumberEntityDescription):
    """A wideq numeric (range) field with its thinq2 control shape."""

    ctrl_key: str
    data_key: str


WIDEQ_NUMBERS_BY_TYPE: dict[str, tuple[MyLgWideqNumberDescription, ...]] = {
    DEVICE_TYPE_AIR_PURIFIER: (
        # 희망습도 (가습 겸용 공청기). PAT는 습도를 센서로만 노출.
        MyLgWideqNumberDescription(
            key="target_humidity", translation_key="target_humidity",
            ctrl_key="basicCtrl", data_key="airState.humidity.desired",
            native_min_value=30, native_max_value=70, native_step=5,
            native_unit_of_measurement=PERCENTAGE,
        ),
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up refrigerator target-temperature numbers + wideq-only numbers."""
    entities: list[NumberEntity] = []
    for coord in entry.runtime_data.coordinators.values():
        if coord.device_type != DEVICE_TYPE_REFRIGERATOR:
            continue
        for item in coord.get("temperature") or []:
            if not isinstance(item, dict):
                continue
            loc = item.get("locationName")
            if loc and isinstance(item.get("targetTemperature"), (int, float)):
                entities.append(MyLgFridgeTargetTemp(coord, loc))

    # Remaining official PAT ranges that are not already represented by a
    # climate/humidifier/number entity. All are disabled by default.
    for coord in entry.runtime_data.coordinators.values():
        writable = {
            feature.path: feature
            for feature in discover_pat_features(coord.profile)
            if feature.access in {FeatureAccess.WRITE, FeatureAccess.READ_WRITE}
        }
        if coord.device_type == DEVICE_TYPE_COOKTOP:
            for feature in writable.values():
                if feature.path[-2:] == ("power", "powerLevel") and feature.location:
                    entities.append(
                        MyLgCooktopPower(
                            coord,
                            feature.location,
                            feature,
                            bool(
                                entry.options.get(
                                    OPT_ALLOW_HAZARDOUS_CONTROLS, False
                                )
                            ),
                        )
                    )
        elif coord.device_type in {DEVICE_TYPE_WASHTOWER, DEVICE_TYPE_STYLER}:
            for feature in writable.values():
                if feature.path[-2:] == ("timer", "relativeHourToStop"):
                    entities.append(MyLgPatRangeNumber(coord, feature))

    wideq: WideqCoordinator | None = entry.runtime_data.wideq_coordinator
    if wideq is not None:
        allow_hazardous = bool(
            entry.options.get(OPT_ALLOW_HAZARDOUS_CONTROLS, False)
        )
        allow_experimental = bool(
            entry.options.get(OPT_ALLOW_EXPERIMENTAL_CONTROLS, False)
        )
        for coord in entry.runtime_data.coordinators.values():
            for wdesc in WIDEQ_NUMBERS_BY_TYPE.get(coord.device_type, ()):
                entities.append(MyLgWideqNumber(wideq, coord, wdesc))
            for control in iter_wideq_field_controls(coord.model):
                if control.value_type == "range":
                    entities.append(
                        MyLgWideqCatalogNumber(
                            wideq,
                            coord,
                            control,
                            allow_hazardous,
                            allow_experimental,
                        )
                    )

    async_add_entities(entities)


class MyLgFridgeTargetTemp(MyLgEntity, NumberEntity):
    """A refrigerator compartment target temperature (settable)."""

    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_step = 1

    def __init__(self, coordinator: PatDeviceCoordinator, location: str) -> None:
        super().__init__(coordinator, f"{location.lower()}_target_temp")
        self._location = location
        self._attr_translation_key = f"{location.lower()}_target_temp"
        lo, hi = _FALLBACK_RANGE.get(location, (-23, 7))
        self._attr_native_min_value = lo
        self._attr_native_max_value = hi

    @property
    def native_value(self) -> float | None:
        value = self.coordinator.get_location(
            "temperature", self._location, "targetTemperature"
        )
        return value if isinstance(value, (int, float)) else None

    async def async_set_native_value(self, value: float) -> None:
        payload: dict[str, Any] = {
            "temperatureInUnits": {
                "locationName": self._location,
                "targetTemperatureC": int(value),
            }
        }
        await self.coordinator.async_control(payload)
        # Status temperature is a location-keyed list; a genuine value arrives
        # via the next DEVICE_STATUS push, so no optimistic merge here.


class MyLgWideqNumber(MyLgWideqEntity, NumberEntity):
    """A wideq-only numeric range field (air-purifier target humidity…)."""

    entity_description: MyLgWideqNumberDescription

    def __init__(
        self,
        wideq_coordinator: WideqCoordinator,
        pat_coordinator: PatDeviceCoordinator,
        description: MyLgWideqNumberDescription,
    ) -> None:
        super().__init__(wideq_coordinator, pat_coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        raw = self._snapshot.get(self.entity_description.data_key)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        # 0 = humidification off / no target set; show nothing rather than a
        # value below the valid range.
        return value if value >= self.native_min_value else None

    async def async_set_native_value(self, value: float) -> None:
        d = self.entity_description
        await self._wideq_set(d.ctrl_key, d.data_key, int(value), use_dataset=False)


class MyLgPatRangeNumber(MyLgEntity, NumberEntity):
    """A profile-advertised PAT range not covered by a primary entity."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: PatDeviceCoordinator, feature) -> None:
        super().__init__(coordinator, feature.key)
        self._feature = feature
        self._attr_name = ".".join(feature.path).replace("_", " ").title()
        self._attr_native_min_value = feature.minimum if feature.minimum is not None else 0
        self._attr_native_max_value = feature.maximum if feature.maximum is not None else 24
        self._attr_native_step = feature.step or 1

    @property
    def native_value(self) -> float | None:
        node: Any = self.coordinator.data
        for token in self._feature.path:
            if not isinstance(node, dict):
                return None
            node = node.get(token)
        return node if isinstance(node, (int, float)) else None

    async def async_set_native_value(self, value: float) -> None:
        result: Any = int(value) if float(value).is_integer() else value
        for token in reversed(self._feature.path):
            result = {token: result}
        await self.coordinator.async_control(result)
        self.coordinator.handle_mqtt_status(result)


class MyLgCooktopPower(MyLgEntity, NumberEntity):
    """Hazardous cooktop zone power exposed disabled and remote-gated."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: PatDeviceCoordinator,
        location: str,
        feature,
        hazardous_controls_allowed: bool,
    ) -> None:
        super().__init__(coordinator, f"{location.lower()}_power_control")
        self._location = location
        self._hazardous_controls_allowed = hazardous_controls_allowed
        self._attr_name = f"{location.replace('_', ' ').title()} power control"
        self._attr_native_min_value = feature.minimum or 0
        self._attr_native_max_value = feature.maximum or 11
        self._attr_native_step = feature.step or 1

    @property
    def available(self) -> bool:
        remote = self.coordinator.get_zone(
            self._location, "remoteControlEnable", "remoteControlEnabled"
        )
        control = self.coordinator.get_zone(
            self._location, "control", "controlEnabled"
        )
        return (
            self._hazardous_controls_allowed
            and bool(self.coordinator.data)
            # Older PAT cooktop profiles expose only controlEnabled. Treat an
            # explicit remote-control denial as blocking, but do not require a
            # field that the model does not advertise.
            and remote is not False
            and control is True
        )

    @property
    def native_value(self) -> float | None:
        value = self.coordinator.get_zone(self._location, "power", "powerLevel")
        return value if isinstance(value, (int, float)) else None

    async def async_set_native_value(self, value: float) -> None:
        payload = {
            "power": {"powerLevel": int(value)},
            "timer": {
                "remainHour": self.coordinator.get_zone(
                    self._location, "timer", "remainHour", default=0
                ),
                "remainMinute": self.coordinator.get_zone(
                    self._location, "timer", "remainMinute", default=0
                ),
            },
            "location": {"locationName": self._location},
        }
        await self.coordinator.async_control(payload)


class MyLgWideqCatalogNumber(MyLgWideqEntity, NumberEntity):
    """A model-advertised WideQ range not duplicated by PAT."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        wideq_coordinator: WideqCoordinator,
        pat_coordinator: PatDeviceCoordinator,
        control,
        hazardous_controls_allowed: bool,
        experimental_controls_allowed: bool,
    ) -> None:
        super().__init__(wideq_coordinator, pat_coordinator, control.key)
        self._control = control
        self._hazardous_controls_allowed = hazardous_controls_allowed
        self._experimental_controls_allowed = experimental_controls_allowed
        self._attr_name = f"WideQ · {control.field}"
        self._attr_native_min_value = control.minimum if control.minimum is not None else 0
        self._attr_native_max_value = control.maximum if control.maximum is not None else 100000
        self._attr_native_step = control.step

    @property
    def available(self) -> bool:
        if not control_risk_allowed(
            self._control,
            allow_hazardous=self._hazardous_controls_allowed,
            allow_experimental=self._experimental_controls_allowed,
            pat_data=self._pat_coordinator.data,
            snapshot=self._snapshot,
        ):
            return False
        return (
            not self.coordinator.circuit_open
            and is_meaningful(self._snapshot.get(self._control.field))
        )

    @property
    def native_value(self) -> float | None:
        raw = self._snapshot.get(self._control.field)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        send_value: Any = int(value) if float(value).is_integer() else value
        await self._wideq_set(
            self._control.ctrl_key,
            self._control.field,
            send_value,
            self._control.use_dataset,
            optimistic=self._control.risk == "low",
        )
