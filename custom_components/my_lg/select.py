"""Select entities (enum resource fields: modes, water settings, etc.)."""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MyLgConfigEntry
from .const import (
    DEVICE_TYPE_AIR_CONDITIONER,
    DEVICE_TYPE_AIR_PURIFIER,
    DEVICE_TYPE_DEHUMIDIFIER,
    DEVICE_TYPE_HUMIDIFIER,
    DEVICE_TYPE_WATER_PURIFIER,
)
from .coordinator import PatDeviceCoordinator
from .coordinator_wideq import WideqCoordinator
from .entity import MyLgEntity, MyLgWideqEntity


@dataclass(frozen=True, kw_only=True)
class MyLgSelectDescription(SelectEntityDescription):
    """An enum resource field exposed as a select."""

    group: str
    field: str
    choices: list[str] = dc_field(default_factory=list)


SELECTS_BY_TYPE: dict[str, tuple[MyLgSelectDescription, ...]] = {
    DEVICE_TYPE_AIR_CONDITIONER: (
        # Detailed fan speed incl. 미풍(SLOW_LOW) that the climate fan_mode
        # (windStrength) doesn't expose.
        MyLgSelectDescription(
            key="wind_strength_detail", translation_key="wind_strength_detail",
            group="airFlow", field="windStrengthDetail",
            choices=["SLOW_LOW", "LOW", "MID", "HIGH", "POWER", "AUTO"],
        ),
    ),
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
        # 위생건조(살균건조): 가습 종료 후 내부를 말려 곰팡이/물때 예방
        MyLgSelectDescription(
            key="hygiene_dry", translation_key="hygiene_dry",
            group="operation", field="hygieneDryMode",
            choices=["OFF", "SILENT", "NORMAL", "FAST"],
        ),
    ),
    DEVICE_TYPE_DEHUMIDIFIER: (
        # 제습 풍량(약/강). windStrengthLevel이 정식 write 필드(windStrength는 alias).
        MyLgSelectDescription(
            key="wind_strength", translation_key="wind_strength",
            group="airFlow", field="windStrengthLevel",
            choices=["LOW", "HIGH"],
        ),
    ),
}


# --- wideq-only enums (fields the PAT API does not expose) ---


@dataclass(frozen=True, kw_only=True)
class MyLgWideqSelectDescription(SelectEntityDescription):
    """A wideq enum field with its thinq2 control shape and value map."""

    ctrl_key: str
    data_key: str
    use_dataset: bool = False
    # HA option name -> wideq numeric value (order defines the option list).
    value_map: dict[str, int] = dc_field(default_factory=dict)


WIDEQ_SELECTS_BY_TYPE: dict[str, tuple[MyLgWideqSelectDescription, ...]] = {
    DEVICE_TYPE_AIR_CONDITIONER: (
        # 자동건조: 냉방/제습 종료 후 내부를 말려 곰팡이 예방.
        MyLgWideqSelectDescription(
            key="auto_dry", translation_key="auto_dry",
            ctrl_key="settingInfo", data_key="airState.miscFuncState.autoDry",
            value_map={"OFF": 0, "ON": 1, "30MIN": 2, "60MIN": 3, "AI_AUTO": 255},
        ),
        # LED 디스플레이 밝기 (이 모델은 100=끄기 ~ 200=100% 스케일).
        MyLgWideqSelectDescription(
            key="display_brightness", translation_key="display_brightness",
            ctrl_key="settingInfo", data_key="airState.lightingState.displayControl",
            value_map={"OFF": 100, "20": 120, "40": 140, "50": 150,
                       "60": 160, "80": 180, "100": 200},
        ),
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    entities: list[SelectEntity] = []
    for coordinator in entry.runtime_data.coordinators.values():
        for desc in SELECTS_BY_TYPE.get(coordinator.device_type, ()):
            if (
                coordinator.supports_field(desc.group, desc.field)
                or coordinator.get(desc.group, desc.field) is not None
            ):
                entities.append(MyLgSelect(coordinator, desc))

    wideq: WideqCoordinator | None = entry.runtime_data.wideq_coordinator
    if wideq is not None:
        for coordinator in entry.runtime_data.coordinators.values():
            for wdesc in WIDEQ_SELECTS_BY_TYPE.get(coordinator.device_type, ()):
                entities.append(MyLgWideqSelect(wideq, coordinator, wdesc))

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


class MyLgWideqSelect(MyLgWideqEntity, SelectEntity):
    """A wideq-only enum (AC auto-dry, LED display brightness…)."""

    entity_description: MyLgWideqSelectDescription

    def __init__(
        self,
        wideq_coordinator: WideqCoordinator,
        pat_coordinator: PatDeviceCoordinator,
        description: MyLgWideqSelectDescription,
    ) -> None:
        super().__init__(wideq_coordinator, pat_coordinator, description.key)
        self.entity_description = description
        self._attr_options = list(description.value_map)
        self._reverse = {v: k for k, v in description.value_map.items()}

    @property
    def current_option(self) -> str | None:
        raw = self._snapshot.get(self.entity_description.data_key)
        if raw is None:
            return None
        try:
            return self._reverse.get(int(raw))
        except (TypeError, ValueError):
            return None

    async def async_select_option(self, option: str) -> None:
        d = self.entity_description
        value = d.value_map.get(option)
        if value is None:
            return
        await self._wideq_set(d.ctrl_key, d.data_key, value, d.use_dataset)
