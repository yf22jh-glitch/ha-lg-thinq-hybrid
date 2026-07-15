"""PAT reservation and cooktop timer entities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory

from . import MyLgConfigEntry
from .compat import AddConfigEntryEntitiesCallback
from .const import (
    DEVICE_TYPE_AIR_CONDITIONER,
    DEVICE_TYPE_AIR_PURIFIER,
    DEVICE_TYPE_COOKTOP,
    DEVICE_TYPE_DEHUMIDIFIER,
    DEVICE_TYPE_HUMIDIFIER,
    OPT_ALLOW_HAZARDOUS_CONTROLS,
)
from .coordinator import PatDeviceCoordinator
from .entity import MyLgEntity
from .feature import FeatureAccess
from .feature_catalog import discover_pat_features


@dataclass(frozen=True)
class TimerSpec:
    key: str
    group: str
    hour_field: str
    minute_field: str | None
    name: str


_TIMERS = (
    TimerSpec(
        "absolute_start_time",
        "timer",
        "absoluteHourToStart",
        "absoluteMinuteToStart",
        "Absolute start time",
    ),
    TimerSpec(
        "absolute_stop_time",
        "timer",
        "absoluteHourToStop",
        "absoluteMinuteToStop",
        "Absolute stop time",
    ),
    TimerSpec(
        "sleep_duration",
        "sleepTimer",
        "relativeHourToStop",
        "relativeMinuteToStop",
        "Sleep duration",
    ),
)

_TIMER_DEVICE_TYPES = {
    DEVICE_TYPE_AIR_CONDITIONER,
    DEVICE_TYPE_AIR_PURIFIER,
    DEVICE_TYPE_DEHUMIDIFIER,
    DEVICE_TYPE_HUMIDIFIER,
}


def _writable_paths(coordinator: PatDeviceCoordinator) -> set[tuple[str, ...]]:
    return {
        feature.path
        for feature in discover_pat_features(coordinator.profile)
        if feature.access in {FeatureAccess.WRITE, FeatureAccess.READ_WRITE}
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    entities: list[TimeEntity] = []
    for coordinator in entry.runtime_data.coordinators.values():
        writable = _writable_paths(coordinator)
        if coordinator.device_type in _TIMER_DEVICE_TYPES:
            for spec in _TIMERS:
                if (spec.group, spec.hour_field) in writable:
                    entities.append(PatTimerEntity(coordinator, spec, writable))
        if coordinator.device_type == DEVICE_TYPE_COOKTOP:
            locations = {
                feature.location
                for feature in discover_pat_features(coordinator.profile)
                if feature.location and feature.path[-2:] in {
                    ("timer", "remainHour"),
                    ("timer", "remainMinute"),
                }
            }
            entities.extend(
                CooktopTimerEntity(
                    coordinator,
                    location,
                    bool(entry.options.get(OPT_ALLOW_HAZARDOUS_CONTROLS, False)),
                )
                for location in sorted(locations)
            )
    async_add_entities(entities)


class PatTimerEntity(MyLgEntity, TimeEntity):
    """Absolute reservation or relative sleep duration sent through PAT."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: PatDeviceCoordinator,
        spec: TimerSpec,
        writable_paths: set[tuple[str, ...]],
    ) -> None:
        super().__init__(coordinator, spec.key)
        self._spec = spec
        self._write_minute = (
            spec.minute_field is not None
            and (spec.group, spec.minute_field) in writable_paths
        )
        self._attr_name = spec.name

    @property
    def native_value(self) -> time | None:
        hour = self._get(self._spec.group, self._spec.hour_field)
        minute = (
            self._get(self._spec.group, self._spec.minute_field)
            if self._spec.minute_field
            else 0
        )
        if hour is None and minute is None:
            return None
        try:
            return time(int(hour or 0) % 24, int(minute or 0) % 60)
        except (TypeError, ValueError):
            return None

    async def async_set_value(self, value: time) -> None:
        fields = {self._spec.hour_field: value.hour}
        if self._write_minute and self._spec.minute_field:
            fields[self._spec.minute_field] = value.minute
        payload = {self._spec.group: fields}
        await self.coordinator.async_control(payload)
        self.coordinator.handle_mqtt_status(payload)


class CooktopTimerEntity(MyLgEntity, TimeEntity):
    """Cooktop zone timer; PAT requires current power and both timer fields."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: PatDeviceCoordinator,
        location: str,
        hazardous_controls_allowed: bool,
    ) -> None:
        super().__init__(coordinator, f"{location.lower()}_timer")
        self._location = location
        self._hazardous_controls_allowed = hazardous_controls_allowed
        self._attr_name = f"{location.replace('_', ' ').title()} timer"

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
    def native_value(self) -> time | None:
        hour = self.coordinator.get_zone(self._location, "timer", "remainHour")
        minute = self.coordinator.get_zone(self._location, "timer", "remainMinute")
        if hour is None and minute is None:
            return None
        try:
            return time(int(hour or 0), int(minute or 0))
        except (TypeError, ValueError):
            return None

    async def async_set_value(self, value: time) -> None:
        payload = {
            "power": {
                "powerLevel": self.coordinator.get_zone(
                    self._location, "power", "powerLevel", default=0
                )
            },
            "timer": {"remainHour": value.hour, "remainMinute": value.minute},
            "location": {"locationName": self._location},
        }
        await self.coordinator.async_control(payload)
