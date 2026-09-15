"""Switch entities (boolean/enum toggles: express mode, sterilization, etc.)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant, callback
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
from .local_command import LocalCommandFailed, LocalCommandPending
from .local_control_entity import local_control_entities_for_domain
from .local_control_router import LocalControlRouter
from .local_control_native import async_native_local_control, native_local_available
from .local_read_owner import (
    resolve_tlv_read_owned_field,
    tlv_read_owner_configured,
)
from .local_read_provider import TlvReadShadowProvider

_LOGGER = logging.getLogger(__name__)


_LOCAL_OPERATION_MODE_TO_PAT = {
    "cool": "COOL",
    "dry": "AIR_DRY",
    "fan_only": "FAN",
    "auto": "AUTO",
}


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
    # Read and control authority are separate.  A sparse local field is safe to
    # command when its frame is reviewed, but it must not replace cloud state
    # unless its snapshot/coherence contract is independently established.
    local_read_semantic: str | None = None
    local_control_semantic: str | None = None
    # Some reviewed codecs have appliance-confirmed ON frames but no explicit
    # OFF golden on the wire. OFF requires an explicit per-value extension
    # grant; the old ON grant must never implicitly authorize it.
    local_control_on_only: bool = False


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
        # Retained 2026-08-19 evidence on both cassette models maps each PAT/
        # WideQ field below to a single TLV ON write, the same-tag readback and
        # a protocol completion ACK. Declared OFF frames use the separately
        # authorized official WindFlow extension, not these retained ON grants.
        MyLgSwitchDescription(
            key="wind_forest", translation_key="wind_forest",
            group="windDirection", field="forestWind",
            on_value=True, off_value=False, optimistic=False,
            allowed_job_modes=("COOL", "AIR_DRY"),
            local_read_semantic="airflow.forest_enabled",
            local_control_semantic="airflow.forest_enabled",  # 0x03D5
            local_control_on_only=True,
        ),
        MyLgSwitchDescription(
            key="wind_long_power", translation_key="wind_long_power",
            group="windDirection", field="longPowerWind",
            on_value=True, off_value=False, optimistic=False,
            allowed_job_modes=("COOL", "AIR_DRY"),
            local_read_semantic="airflow.long_distance_enabled",
            local_control_semantic="airflow.long_distance_enabled",  # 0x03D7
            local_control_on_only=True,
        ),
        MyLgSwitchDescription(
            key="wind_concentration", translation_key="wind_concentration",
            group="windDirection", field="concentrationWind",
            on_value=True, off_value=False, optimistic=False,
            allowed_job_modes=("COOL", "AIR_DRY"),
            local_read_semantic="airflow.study_enabled",
            local_control_semantic="airflow.study_enabled",  # 0x0291
            local_control_on_only=True,
        ),
        MyLgSwitchDescription(
            key="wind_manner", translation_key="wind_manner",
            group="windDirection", field="mannerWind",
            on_value=True, off_value=False, optimistic=False,
            allowed_job_modes=("COOL", "AIR_DRY"),
            local_read_semantic="airflow.quiet_enabled",
            local_control_semantic="airflow.quiet_enabled",  # 0x03D6
            local_control_on_only=True,
        ),
        MyLgSwitchDescription(
            key="wind_auto_fit", translation_key="wind_auto_fit",
            group="windDirection", field="autoFitWind",
            on_value=True, off_value=False, optimistic=False,
            allowed_job_modes=("COOL", "AIR_DRY"),
            local_read_semantic="airflow.auto_temperature_enabled",
            local_control_semantic="airflow.auto_temperature_enabled",  # 0x0290
            local_control_on_only=True,
        ),
        MyLgSwitchDescription(
            key="power_save", translation_key="power_save",
            group="powerSave", field="powerSaveEnabled",
            on_value=True, off_value=False,
            allowed_job_modes=("COOL",),
            local_read_semantic="energy_saving.enabled",
            local_control_semantic="energy_saving.enabled",
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
            local_control_semantic="auto_operation.enabled",
        ),
        MyLgSwitchDescription(
            key="sleep_mode", translation_key="sleep_mode",
            group="operation", field="sleepMode",
            on_value="SLEEP_ON", off_value="SLEEP_OFF",
            local_control_semantic="sleep_mode.enabled",
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
            local_control_semantic="mood_light.enabled",
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
    local_read_semantic: str | None = None
    local_control_semantic: str | None = None


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
            local_read_semantic="comfort_energy_saving.enabled",
            local_control_semantic="comfort_energy_saving.enabled",
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


def _wideq_switch_for_model(
    description: MyLgWideqSwitchDescription, model: str
) -> MyLgWideqSwitchDescription:
    # Keep the existing UVnano entity/unique_id. This exact model already has
    # an independently authorized boolean reader and ON/OFF local commands;
    # an empty legacy cloud snapshot must not strand its original switch.
    # Other dehumidifiers retain their existing cloud ownership.
    if (
        model == "DHUM_056905_WW"
        and description.key == "uvnano"
        and description.data_key == "airState.miscFuncState.Uvnano"
    ):
        return replace(
            description,
            local_read_semantic="sterilization.uvnano_enabled",
            local_control_semantic="sterilization.uvnano_enabled",
        )
    return description


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    entities: list[SwitchEntity] = []
    local_control = entry.runtime_data.local_control
    for coordinator in entry.runtime_data.coordinators.values():
        local_read_provider = entry.runtime_data.local_read_providers.get(
            coordinator.device_id
        )
        for desc in SWITCHES_BY_TYPE.get(coordinator.device_type, ()):
            local_owned = tlv_read_owner_configured(
                local_read_provider, desc.local_read_semantic
            )
            local_complete = (
                local_owned
                and local_control is not None
                and desc.local_control_semantic is not None
                and {"false", "true"}.issubset(
                    local_control.authorized_values(
                        coordinator.device_id, desc.local_control_semantic
                    )
                )
            )
            if local_owned and not local_complete:
                # A one-sided Local toggle is not a safe switch. Its exact
                # state remains visible through the Local binary-sensor leaf.
                continue
            # Create if the profile advertises the field (write-capable) even when
            # the current status doesn't report it yet (e.g. AC wind modes only
            # appear in status while active); fall back to a status probe.
            if (
                coordinator.supports_field(desc.group, desc.field)
                or coordinator.get(desc.group, desc.field) is not None
                or local_owned
            ):
                entities.append(
                    MyLgSwitch(
                        coordinator,
                        desc,
                        local_control,
                        local_read_provider,
                    )
                )

    # wideq-only toggles (created by device type; unavailable until wideq polls).
    wideq: WideqCoordinator | None = entry.runtime_data.wideq_coordinator
    if wideq is not None:
        for coordinator in entry.runtime_data.coordinators.values():
            local_read_provider = entry.runtime_data.local_read_providers.get(
                coordinator.device_id
            )
            for wdesc in WIDEQ_SWITCHES_BY_TYPE.get(coordinator.device_type, ()):
                wdesc = _wideq_switch_for_model(wdesc, coordinator.model)
                if (
                    wdesc.supported_models
                    and coordinator.model not in wdesc.supported_models
                ):
                    continue
                if wdesc.key in UNSUPPORTED_WIDEQ_AC_FEATURES_BY_MODEL.get(
                    coordinator.model, frozenset()
                ):
                    continue
                local_owned = tlv_read_owner_configured(
                    local_read_provider, wdesc.local_read_semantic
                )
                local_complete = (
                    local_owned
                    and local_control is not None
                    and wdesc.local_control_semantic is not None
                    and {"false", "true"}.issubset(
                        local_control.authorized_values(
                            coordinator.device_id, wdesc.local_control_semantic
                        )
                    )
                )
                if local_owned and not local_complete:
                    continue
                entities.append(
                    MyLgWideqSwitch(
                        wideq,
                        coordinator,
                        wdesc,
                        local_control,
                        local_read_provider,
                    )
                )

    entities.extend(local_control_entities_for_domain(entry, "switch"))
    async_add_entities(entities)


class _LocalReadSwitchMixin:
    """Overlay an exact reviewed local boolean onto an existing switch."""

    _local_read_provider: TlvReadShadowProvider | None
    _local_read_semantic: str | None
    _remove_local_read_listener: Callable[[], None] | None

    def _configure_local_read(
        self,
        provider: TlvReadShadowProvider | None,
        semantic_id: str | None,
    ) -> None:
        self._local_read_provider = provider
        self._local_read_semantic = semantic_id
        self._remove_local_read_listener = None
        self._reported_invalid_local_reads: set[str] = set()

    def _report_invalid_local_read(self, semantic_id: str) -> None:
        if semantic_id in self._reported_invalid_local_reads:
            return
        self._reported_invalid_local_reads.add(semantic_id)
        _LOGGER.warning(
            "Unsupported Rethink Local value for semantic %s; "
            "cloud fallback disabled",
            semantic_id,
        )

    def _local_read_owns_state(self) -> bool:
        return resolve_tlv_read_owned_field(
            self._local_read_provider, self._local_read_semantic
        ).owner_configured

    def _local_is_on(self) -> tuple[bool, bool | None]:
        semantic_id = self._local_read_semantic
        state = resolve_tlv_read_owned_field(
            self._local_read_provider, semantic_id
        )
        if not state.owner_configured or semantic_id is None:
            return False, None
        raw = state.value
        if type(raw) is not bool:
            if state.available:
                self._report_invalid_local_read(semantic_id)
            return False, None
        return state.available, raw

    def _local_field_available(self) -> bool:
        return self._local_is_on()[0]

    def _local_first_job_mode(self, pat_job_mode: Any) -> Any:
        semantic_id = "operation.mode"
        state = resolve_tlv_read_owned_field(
            self._local_read_provider, semantic_id
        )
        if not state.owner_configured:
            return pat_job_mode
        raw = state.value
        if not isinstance(raw, str):
            if state.available:
                self._report_invalid_local_read(semantic_id)
            return None
        job_mode = _LOCAL_OPERATION_MODE_TO_PAT.get(raw)
        if job_mode is None:
            self._report_invalid_local_read(semantic_id)
            return None
        return job_mode

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        provider = self._local_read_provider
        if provider is not None and self._local_read_semantic is not None:
            self._remove_local_read_listener = provider.async_add_listener(
                self._handle_local_read_update
            )

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_local_read_listener is not None:
            self._remove_local_read_listener()
            self._remove_local_read_listener = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_local_read_update(self) -> None:
        self.async_write_ha_state()


class MyLgSwitch(_LocalReadSwitchMixin, MyLgEntity, SwitchEntity):
    entity_description: MyLgSwitchDescription

    def __init__(
        self,
        coordinator: PatDeviceCoordinator,
        description: MyLgSwitchDescription,
        local_control: LocalControlRouter | None = None,
        local_read_provider: TlvReadShadowProvider | None = None,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._local_control = local_control
        self._configure_local_read(
            local_read_provider, description.local_read_semantic
        )

    @property
    def is_on(self) -> bool | None:
        d = self.entity_description
        local_available, is_on = self._local_is_on()
        if self._local_read_owns_state():
            return is_on
        return self._get(d.group, d.field) == d.on_value

    @property
    def available(self) -> bool:
        if self._local_read_owns_state():
            return self._local_field_available()
        return native_local_available(self._local_control, self.coordinator.device_id,
                                      self.entity_description.local_control_semantic) or super().available

    async def _set(self, value: Any) -> None:
        d = self.entity_description
        payload = {d.group: {d.field: value}}
        local_write_allowed = (
            not d.local_control_on_only or value == d.on_value
            or (self._local_control is not None
                and d.local_control_semantic is not None
                and "false" in self._local_control.authorized_values(
                    self.coordinator.device_id, d.local_control_semantic))
        )
        if self._local_read_owns_state():
            if (
                not local_write_allowed
                or self._local_control is None
                or d.local_control_semantic is None
            ):
                raise HomeAssistantError(
                    f"{self.coordinator.alias}: 양방향으로 검증된 Local 스위치가 아니어서 전송하지 않았어요."
                )
            try:
                outcome = await self._local_control.async_set_flag(
                    self.coordinator.device_id,
                    d.local_control_semantic,
                    value == d.on_value,
                )
            except LocalCommandPending:
                return
            except LocalCommandFailed as err:
                raise HomeAssistantError(
                    f"{self.coordinator.alias}: {err}"
                ) from err
            if outcome is None:
                if d.local_control_on_only and value == d.off_value and (d.local_control_semantic or '').startswith('airflow.'):
                    raise HomeAssistantError(
                        f"{self.coordinator.alias}: 현재 Local 전송 조건을 충족하지 않아 보내지 않았어요. "
                        "전원 ON·냉방/제습·특수 바람 하나만 켜진 상태인지 확인해 주세요. "
                        "계속 거부되면 기기나 LG 앱에서 꺼 주세요."
                    )
                raise HomeAssistantError(
                    f"{self.coordinator.alias}: 이 값은 검증된 Local 명령 범위에 없어서 전송하지 않았어요."
                )
            return
        if local_write_allowed and await async_native_local_control(
            self._local_control, self.coordinator.device_id, d.local_control_semantic,
            "true" if value == d.on_value else "false",
        ):
            return
        await self.coordinator.async_control(payload)
        if d.optimistic:
            self.coordinator.handle_mqtt_status(payload)

    async def async_turn_on(self, **kwargs: Any) -> None:
        d = self.entity_description
        if d.allowed_job_modes:
            job_mode = self._local_first_job_mode(
                self._get("airConJobMode", "currentJobMode")
            )
            if job_mode not in d.allowed_job_modes:
                if d.key == "power_save":
                    detail = "일반 절전은 냉방 모드에서만 사용할 수 있어요."
                else:
                    detail = "특수 바람 기능은 냉방 또는 제습 모드에서만 사용할 수 있어요."
                raise HomeAssistantError(
                    f"{self.coordinator.alias}: {detail}"
                )
        await self._set(self.entity_description.on_value)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(self.entity_description.off_value)


class MyLgWideqSwitch(_LocalReadSwitchMixin, MyLgWideqEntity, SwitchEntity):
    """A wideq-only boolean toggle (AC air-clean/smart-care, purifier jet/UV…)."""

    entity_description: MyLgWideqSwitchDescription

    def __init__(
        self,
        wideq_coordinator: WideqCoordinator,
        pat_coordinator: PatDeviceCoordinator,
        description: MyLgWideqSwitchDescription,
        local_control: LocalControlRouter | None = None,
        local_read_provider: TlvReadShadowProvider | None = None,
    ) -> None:
        super().__init__(wideq_coordinator, pat_coordinator, description.key)
        self.entity_description = description = _wideq_switch_for_model(
            description, pat_coordinator.model
        )
        self._local_control = local_control
        self._configure_local_read(
            local_read_provider, description.local_read_semantic
        )

    @property
    def is_on(self) -> bool | None:
        d = self.entity_description
        local_available, is_on = self._local_is_on()
        if self._local_read_owns_state():
            return is_on
        snapshot = (
            self.coordinator.power_save_snapshot_for(self._device_id)
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
        if self._local_read_owns_state():
            return self._local_field_available()
        if d.key == "comfortable_power_save":
            return self.coordinator.power_save_field_available(
                self._device_id, d.data_key
            )
        return super().available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(super().extra_state_attributes)
        if self.entity_description.key == "comfortable_power_save":
            attrs.update(
                self.coordinator.power_save_diagnostic_attributes(self._device_id)
            )
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        d = self.entity_description
        if d.allowed_job_modes:
            job_mode = self._local_first_job_mode(
                self._pat_coordinator.get(
                    "airConJobMode", "currentJobMode"
                )
            )
            if job_mode not in d.allowed_job_modes:
                raise HomeAssistantError(
                    f"{self._pat_coordinator.alias}: "
                    "쾌적 절전은 냉방 모드에서만 사용할 수 있어요."
                )
        await self._set(d.on_value)

    async def async_turn_off(self, **kwargs: Any) -> None:
        d = self.entity_description
        await self._set(d.off_value)

    async def _set(self, value: int) -> None:
        d = self.entity_description
        if self._local_read_owns_state():
            if self._local_control is None or d.local_control_semantic is None:
                raise HomeAssistantError(
                    f"{self._pat_coordinator.alias}: 양방향으로 검증된 Local 스위치가 아니어서 전송하지 않았어요."
                )
            try:
                outcome = await self._local_control.async_set_flag(
                    self._device_id,
                    d.local_control_semantic,
                    value == d.on_value,
                )
            except LocalCommandPending:
                return
            except LocalCommandFailed as err:
                raise HomeAssistantError(
                    f"{self._pat_coordinator.alias}: {err}"
                ) from err
            if outcome is None:
                raise HomeAssistantError(
                    f"{self._pat_coordinator.alias}: 이 값은 검증된 Local 명령 범위에 없어서 전송하지 않았어요."
                )
            return
        await self._wideq_set(
            d.ctrl_key,
            d.data_key,
            value,
            d.use_dataset,
            power_save_only=d.key == "comfortable_power_save",
        )
