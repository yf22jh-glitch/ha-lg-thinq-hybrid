"""Switch entities (boolean/enum toggles: express mode, sterilization, etc.)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from . import MyLgConfigEntry
from .compat import AddConfigEntryEntitiesCallback
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
from .local_provider import LocalSemanticShadowProvider
from .pat_control import PatStateRequirement, build_pat_control_request
from .power_save import (
    ac_power_save_snapshot_with_local,
    local_comfort_power_save_configured,
    local_comfort_power_save_value,
)
from .wideq_control import exact_wideq_field_spec


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
    allowed_job_modes: tuple[str, ...] = ()


# The installed cassette models advertise generic WideQ air-clean/smart-care
# fields in their status schema, but their model support flags contain neither
# AIRCLEAN nor SMARTCARE.  Creating those controls produces ghost switches that
# stay off/unavailable and are rejected when commanded.
UNSUPPORTED_WIDEQ_AC_FEATURES_BY_MODEL: dict[str, frozenset[str]] = {
    "CST_170004_WW": frozenset({"air_clean", "smart_care"}),
    "CST_570004_WW": frozenset({"air_clean", "smart_care"}),
}


SWITCHES_BY_TYPE: dict[str, tuple[MyLgSwitchDescription, ...]] = {
    DEVICE_TYPE_AIR_CONDITIONER: (
        # Airflow "wind modes" (windDirection booleans). ThinQ app exposes these;
        # the device may treat some as mutually exclusive, so follow real state.
        MyLgSwitchDescription(
            key="wind_forest", translation_key="wind_forest",
            group="windDirection", field="forestWind",
            on_value=True, off_value=False, optimistic=False,
            allowed_job_modes=("COOL", "AIR_DRY"),
        ),
        MyLgSwitchDescription(
            key="wind_long_power", translation_key="wind_long_power",
            group="windDirection", field="longPowerWind",
            on_value=True, off_value=False, optimistic=False,
            allowed_job_modes=("COOL", "AIR_DRY"),
        ),
        MyLgSwitchDescription(
            key="wind_concentration", translation_key="wind_concentration",
            group="windDirection", field="concentrationWind",
            on_value=True, off_value=False, optimistic=False,
            allowed_job_modes=("COOL", "AIR_DRY"),
        ),
        MyLgSwitchDescription(
            key="wind_manner", translation_key="wind_manner",
            group="windDirection", field="mannerWind",
            on_value=True, off_value=False, optimistic=False,
            allowed_job_modes=("COOL", "AIR_DRY"),
        ),
        MyLgSwitchDescription(
            key="wind_auto_fit", translation_key="wind_auto_fit",
            group="windDirection", field="autoFitWind",
            on_value=True, off_value=False, optimistic=False,
            allowed_job_modes=("COOL", "AIR_DRY"),
        ),
        MyLgSwitchDescription(
            key="power_save", translation_key="power_save",
            group="powerSave", field="powerSaveEnabled",
            on_value=True, off_value=False,
            allowed_job_modes=("COOL",),
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
    supported_models: frozenset[str] = frozenset()
    allowed_job_modes: tuple[str, ...] = ()


WIDEQ_SWITCHES_BY_TYPE: dict[str, tuple[MyLgWideqSwitchDescription, ...]] = {
    DEVICE_TYPE_AIR_CONDITIONER: (
        # The installed cassette models expose this as the LG app's separate
        # "comfort power save" toggle.  The official app writes
        # settingInfo/Set airState.powerSave.hum with 0/1.
        MyLgWideqSwitchDescription(
            key="comfortable_power_save",
            translation_key="comfortable_power_save",
            ctrl_key="settingInfo",
            data_key="airState.powerSave.hum",
            supported_models=frozenset({"CST_170004_WW", "CST_570004_WW"}),
            allowed_job_modes=("COOL",),
        ),
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
                if (
                    wdesc.supported_models
                    and coordinator.model not in wdesc.supported_models
                ):
                    continue
                if (
                    exact_wideq_field_spec(
                        coordinator.model,
                        wdesc.ctrl_key,
                        wdesc.data_key,
                        wdesc.use_dataset,
                    )
                    is None
                ):
                    continue
                if wdesc.key in UNSUPPORTED_WIDEQ_AC_FEATURES_BY_MODEL.get(
                    coordinator.model, frozenset()
                ):
                    continue
                entities.append(
                    MyLgWideqSwitch(
                        wideq,
                        coordinator,
                        wdesc,
                        local_provider=getattr(
                            entry.runtime_data, "local_providers", {}
                        ).get(coordinator.device_id),
                    )
                )

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

    def _job_mode_error(self) -> str:
        if self.entity_description.key == "power_save":
            return "일반 절전은 냉방 모드에서만 사용할 수 있어요."
        return "특수 바람 기능은 냉방 또는 제습 모드에서만 사용할 수 있어요."

    async def _set(self, value: Any) -> None:
        d = self.entity_description
        payload = {d.group: {d.field: value}}
        requirements = (
            (
                PatStateRequirement(
                    ("airConJobMode", "currentJobMode"),
                    d.allowed_job_modes,
                    self._job_mode_error(),
                ),
            )
            if d.allowed_job_modes
            else ()
        )
        await self.coordinator.async_control(
            build_pat_control_request(payload, requirements=requirements)
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        d = self.entity_description
        if d.allowed_job_modes:
            job_mode = self._get("airConJobMode", "currentJobMode")
            if job_mode not in d.allowed_job_modes:
                raise HomeAssistantError(
                    f"{self.coordinator.alias}: {self._job_mode_error()}"
                )
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
        *,
        local_provider: LocalSemanticShadowProvider | None = None,
    ) -> None:
        super().__init__(wideq_coordinator, pat_coordinator, description.key)
        self.entity_description = description
        self._local_provider = local_provider

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (
            self.entity_description.key == "comfortable_power_save"
            and local_comfort_power_save_configured(
                self._local_provider, self._pat_coordinator.model
            )
        ):
            self.async_on_remove(
                self._local_provider.async_add_listener(
                    self.async_write_ha_state
                )
            )

    @property
    def is_on(self) -> bool:
        d = self.entity_description
        snapshot = (
            ac_power_save_snapshot_with_local(
                self.coordinator.power_save_snapshot_for(self._device_id),
                self._local_provider,
                self._pat_coordinator.model,
            )
            if d.key == "comfortable_power_save"
            else self._snapshot
        )
        raw = snapshot.get(d.data_key)
        try:
            return raw is not None and int(raw) == d.on_value
        except (TypeError, ValueError):
            return False

    @property
    def available(self) -> bool:
        d = self.entity_description
        if not self._wideq_field_write_available(
            d.ctrl_key, d.data_key, d.use_dataset
        ):
            return False
        if d.key == "comfortable_power_save":
            return (
                local_comfort_power_save_value(
                    self._local_provider, self._pat_coordinator.model
                )
                is not None
                or self.coordinator.power_save_field_available(
                    self._device_id, d.data_key
                )
            )
        return d.data_key in self._snapshot

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(super().extra_state_attributes)
        if self.entity_description.key == "comfortable_power_save":
            attrs.update(
                self.coordinator.power_save_diagnostic_attributes(self._device_id)
            )
            attrs["comfortable_power_save_provider"] = (
                "local"
                if local_comfort_power_save_value(
                    self._local_provider, self._pat_coordinator.model
                )
                is not None
                else "wideq"
            )
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        d = self.entity_description
        if d.allowed_job_modes:
            job_mode = self._pat_coordinator.get(
                "airConJobMode", "currentJobMode"
            )
            if job_mode not in d.allowed_job_modes:
                raise HomeAssistantError(
                    f"{self._pat_coordinator.alias}: "
                    "쾌적 절전은 냉방 모드에서만 사용할 수 있어요."
                )
        await self._wideq_set(
            d.ctrl_key,
            d.data_key,
            d.on_value,
            d.use_dataset,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        d = self.entity_description
        await self._wideq_set(
            d.ctrl_key,
            d.data_key,
            d.off_value,
            d.use_dataset,
        )
