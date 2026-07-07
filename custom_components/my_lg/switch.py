"""Switch entities (boolean/enum toggles: express mode, sterilization, etc.)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MyLgConfigEntry
from .const import (
    DEVICE_TYPE_AIR_CONDITIONER,
    DEVICE_TYPE_AIR_PURIFIER,
    DEVICE_TYPE_DEHUMIDIFIER,
    DEVICE_TYPE_HUMIDIFIER,
    DEVICE_TYPE_REFRIGERATOR,
    DEVICE_TYPE_WATER_PURIFIER,
)
from .coordinator import PatDeviceCoordinator
from .coordinator_wideq import WideqCoordinator
from .entity import MyLgEntity, MyLgWideqEntity


@dataclass(frozen=True, kw_only=True)
class MyLgSwitchDescription(SwitchEntityDescription):
    """A toggle mapped to one resource field with explicit on/off values."""

    group: str
    field: str
    on_value: Any
    off_value: Any
    # Reflect the commanded value immediately. Turn off for toggles the device
    # may silently ignore (e.g. warm mist needs heated water) so the UI follows
    # the real reported state instead of showing a fake "on".
    optimistic: bool = True


SWITCHES_BY_TYPE: dict[str, tuple[MyLgSwitchDescription, ...]] = {
    DEVICE_TYPE_AIR_CONDITIONER: (
        # Airflow "wind modes" (windDirection booleans). ThinQ app exposes these;
        # the device may treat some as mutually exclusive, so follow real state.
        MyLgSwitchDescription(
            key="wind_forest", translation_key="wind_forest",
            group="windDirection", field="forestWind",
            on_value=True, off_value=False, optimistic=False,
        ),
        MyLgSwitchDescription(
            key="wind_long_power", translation_key="wind_long_power",
            group="windDirection", field="longPowerWind",
            on_value=True, off_value=False, optimistic=False,
        ),
        MyLgSwitchDescription(
            key="wind_concentration", translation_key="wind_concentration",
            group="windDirection", field="concentrationWind",
            on_value=True, off_value=False, optimistic=False,
        ),
        MyLgSwitchDescription(
            key="wind_manner", translation_key="wind_manner",
            group="windDirection", field="mannerWind",
            on_value=True, off_value=False, optimistic=False,
        ),
        MyLgSwitchDescription(
            key="wind_auto_fit", translation_key="wind_auto_fit",
            group="windDirection", field="autoFitWind",
            on_value=True, off_value=False, optimistic=False,
        ),
        MyLgSwitchDescription(
            key="power_save", translation_key="power_save",
            group="powerSave", field="powerSaveEnabled",
            on_value=True, off_value=False,
        ),
    ),
    DEVICE_TYPE_REFRIGERATOR: (
        MyLgSwitchDescription(
            key="express_mode", translation_key="express_mode",
            group="refrigeration", field="expressMode",
            on_value=True, off_value=False,
        ),
    ),
    DEVICE_TYPE_WATER_PURIFIER: (
        MyLgSwitchDescription(
            key="sterilization", translation_key="sterilization",
            group="sterilization", field="reservation",
            on_value="ON", off_value="OFF",
        ),
    ),
    DEVICE_TYPE_HUMIDIFIER: (
        MyLgSwitchDescription(
            key="auto_mode", translation_key="auto_mode",
            group="operation", field="autoMode",
            on_value="AUTO_ON", off_value="AUTO_OFF",
        ),
        MyLgSwitchDescription(
            key="sleep_mode", translation_key="sleep_mode",
            group="operation", field="sleepMode",
            on_value="SLEEP_ON", off_value="SLEEP_OFF",
        ),
        MyLgSwitchDescription(
            key="warm_mode", translation_key="warm_mode",
            group="humidity", field="warmMode",
            on_value="WARM_ON", off_value="WARM_OFF",
            optimistic=False,  # only engages with heated water; follow real state
        ),
        MyLgSwitchDescription(
            key="mood_lamp", translation_key="mood_lamp",
            group="moodLamp", field="moodLampState",
            on_value="ON", off_value="OFF",
        ),
    ),
}


# --- wideq-only toggles (fields the PAT API does not expose) ---


@dataclass(frozen=True, kw_only=True)
class MyLgWideqSwitchDescription(SwitchEntityDescription):
    """A wideq boolean field with its thinq2 control shape."""

    ctrl_key: str
    data_key: str
    use_dataset: bool = False  # wModeCtrl needs the dataSetList payload form
    on_value: int = 1
    off_value: int = 0


WIDEQ_SWITCHES_BY_TYPE: dict[str, tuple[MyLgWideqSwitchDescription, ...]] = {
    DEVICE_TYPE_AIR_CONDITIONER: (
        # wMode toggles use wModeCtrl (single key in a dataSetList).
        MyLgWideqSwitchDescription(
            key="air_clean", translation_key="air_clean",
            ctrl_key="wModeCtrl", data_key="airState.wMode.airClean", use_dataset=True,
        ),
        MyLgWideqSwitchDescription(
            key="smart_care", translation_key="smart_care",
            ctrl_key="wModeCtrl", data_key="airState.wMode.smartCare", use_dataset=True,
        ),
    ),
    DEVICE_TYPE_AIR_PURIFIER: (
        MyLgWideqSwitchDescription(
            key="jet_mode", translation_key="jet_mode",
            ctrl_key="basicCtrl", data_key="airState.miscFuncState.airFast",
        ),
        MyLgWideqSwitchDescription(
            key="uv_disinfection", translation_key="uv_disinfection",
            ctrl_key="basicCtrl", data_key="airState.miscFuncState.airUVDisinfection",
        ),
    ),
    DEVICE_TYPE_DEHUMIDIFIER: (
        MyLgWideqSwitchDescription(
            key="uvnano", translation_key="uvnano",
            ctrl_key="basicCtrl", data_key="airState.miscFuncState.Uvnano",
        ),
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    entities: list[SwitchEntity] = []
    for coordinator in entry.runtime_data.coordinators.values():
        for desc in SWITCHES_BY_TYPE.get(coordinator.device_type, ()):
            # Create if the profile advertises the field (write-capable) even when
            # the current status doesn't report it yet (e.g. AC wind modes only
            # appear in status while active); fall back to a status probe.
            if (
                coordinator.supports_field(desc.group, desc.field)
                or coordinator.get(desc.group, desc.field) is not None
            ):
                entities.append(MyLgSwitch(coordinator, desc))

    # wideq-only toggles (created by device type; unavailable until wideq polls).
    wideq: WideqCoordinator | None = entry.runtime_data.wideq_coordinator
    if wideq is not None:
        for coordinator in entry.runtime_data.coordinators.values():
            for wdesc in WIDEQ_SWITCHES_BY_TYPE.get(coordinator.device_type, ()):
                entities.append(MyLgWideqSwitch(wideq, coordinator, wdesc))

    async_add_entities(entities)


class MyLgSwitch(MyLgEntity, SwitchEntity):
    entity_description: MyLgSwitchDescription

    def __init__(
        self, coordinator: PatDeviceCoordinator, description: MyLgSwitchDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        d = self.entity_description
        return self._get(d.group, d.field) == d.on_value

    async def _set(self, value: Any) -> None:
        d = self.entity_description
        payload = {d.group: {d.field: value}}
        await self.coordinator.async_control(payload)
        if d.optimistic:
            self.coordinator.handle_mqtt_status(payload)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(self.entity_description.on_value)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(self.entity_description.off_value)


class MyLgWideqSwitch(MyLgWideqEntity, SwitchEntity):
    """A wideq-only boolean toggle (AC air-clean/smart-care, purifier jet/UV…)."""

    entity_description: MyLgWideqSwitchDescription

    def __init__(
        self,
        wideq_coordinator: WideqCoordinator,
        pat_coordinator: PatDeviceCoordinator,
        description: MyLgWideqSwitchDescription,
    ) -> None:
        super().__init__(wideq_coordinator, pat_coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        raw = self._snapshot.get(self.entity_description.data_key)
        try:
            return raw is not None and int(raw) == self.entity_description.on_value
        except (TypeError, ValueError):
            return False

    async def async_turn_on(self, **kwargs: Any) -> None:
        d = self.entity_description
        await self._wideq_set(d.ctrl_key, d.data_key, d.on_value, d.use_dataset)

    async def async_turn_off(self, **kwargs: Any) -> None:
        d = self.entity_description
        await self._wideq_set(d.ctrl_key, d.data_key, d.off_value, d.use_dataset)
