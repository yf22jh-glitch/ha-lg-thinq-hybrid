"""Select entities (enum resource fields: modes, water settings, etc.)."""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MyLgConfigEntry
from .const import (
    DEVICE_TYPE_AIR_PURIFIER,
    DEVICE_TYPE_HUMIDIFIER,
    DEVICE_TYPE_WATER_PURIFIER,
)
from .coordinator import PatDeviceCoordinator
from .entity import MyLgEntity


@dataclass(frozen=True, kw_only=True)
class MyLgSelectDescription(SelectEntityDescription):
    """An enum resource field exposed as a select."""

    group: str
    field: str
    choices: list[str] = dc_field(default_factory=list)


SELECTS_BY_TYPE: dict[str, tuple[MyLgSelectDescription, ...]] = {
    DEVICE_TYPE_AIR_PURIFIER: (
        MyLgSelectDescription(
            key="job_mode", translation_key="job_mode",
            group="airPurifierJobMode", field="currentJobMode",
            choices=["CLEAN", "SILENT", "HUMIDITY"],
        ),
    ),
    DEVICE_TYPE_WATER_PURIFIER: (
        MyLgSelectDescription(
            key="water_type", translation_key="water_type",
            group="waterSetting", field="waterType",
            choices=["RECENT", "NORMAL", "COLD"],
        ),
        MyLgSelectDescription(
            key="default_water", translation_key="default_water",
            group="waterSetting", field="defaultWaterAmount",
            choices=["DEFAULT_WATER_1", "DEFAULT_WATER_2", "DEFAULT_WATER_3", "DEFAULT_WATER_4"],
        ),
    ),
    DEVICE_TYPE_HUMIDIFIER: (
        MyLgSelectDescription(
            key="display_light", translation_key="display_light",
            group="display", field="light",
            choices=["OFF", "LEVEL_1", "LEVEL_2", "LEVEL_3"],
        ),
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    entities: list[MyLgSelect] = []
    for coordinator in entry.runtime_data.coordinators.values():
        for desc in SELECTS_BY_TYPE.get(coordinator.device_type, ()):
            if coordinator.get(desc.group, desc.field) is not None:
                entities.append(MyLgSelect(coordinator, desc))
    async_add_entities(entities)


class MyLgSelect(MyLgEntity, SelectEntity):
    entity_description: MyLgSelectDescription

    def __init__(
        self, coordinator: PatDeviceCoordinator, description: MyLgSelectDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_options = description.choices

    @property
    def current_option(self) -> str | None:
        d = self.entity_description
        return self._get(d.group, d.field)

    async def async_select_option(self, option: str) -> None:
        d = self.entity_description
        payload = {d.group: {d.field: option}}
        await self.coordinator.async_control(payload)
        self.coordinator.handle_mqtt_status(payload)
