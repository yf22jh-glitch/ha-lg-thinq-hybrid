"""Select entities (enum resource fields: modes, water settings, etc.)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
try:
    from homeassistant.helpers.device_registry import DeviceInfo
except ImportError:  # pragma: no cover - compatibility with older HA layouts
    from homeassistant.helpers.entity import DeviceInfo

from . import MyLgConfigEntry
from .compat import AddConfigEntryEntitiesCallback
from .const import (
    DEVICE_TYPE_AIR_CONDITIONER,
    DEVICE_TYPE_AIR_PURIFIER,
    DEVICE_TYPE_DEHUMIDIFIER,
    DEVICE_TYPE_HUMIDIFIER,
    DEVICE_TYPE_WATER_PURIFIER,
    DOMAIN,
    OPT_ALLOW_EXPERIMENTAL_CONTROLS,
    OPT_ALLOW_HAZARDOUS_CONTROLS,
)
from .coordinator import PatDeviceCoordinator
from .coordinator_wideq import WideqCoordinator
from .entity import MyLgEntity, MyLgWideqEntity
from .local_command import (
    CLIMATE_POWER_ON_CAPABILITY,
    CLIMATE_TUPLE_CAPABILITY,
    LocalCommandFailed,
    LocalCommandPending,
)
from .local_control_composite_domain import LocalControlCompositeInputDomain
from .local_control_entity import local_control_entities_for_domain
from .local_control_router import LocalControlRouter
from .local_control_native import LocalConditionEntityMixin, async_native_local_control, native_local_available
from .local_provider import LocalSemanticShadowProvider
from .local_read_owner import (
    local_auto_comfort_owner_configured,
    local_climate_control_domain,
    resolve_tlv_read_owned_field,
    tlv_read_owner_configured,
)
from .local_read_provider import TlvReadShadowProvider
from .value_access import is_meaningful
from .wideq_control import (
    control_risk_allowed,
    iter_wideq_field_controls,
    normalize_option,
)

_LOGGER = logging.getLogger(__name__)

_AUTO_COMFORT_OPTIONS = (
    "warmer",
    "slightly_warmer",
    "comfortable",
    "slightly_cooler",
    "cooler",
)
_AUTO_COMFORT_VALUE = {
    "warmer": -2,
    "slightly_warmer": -1,
    "comfortable": 0,
    "slightly_cooler": 1,
    "cooler": 2,
}
_AUTO_COMFORT_OPTION = {value: option for option, value in _AUTO_COMFORT_VALUE.items()}


@dataclass(frozen=True, kw_only=True)
class MyLgSelectDescription(SelectEntityDescription):
    """An enum resource field exposed as a select."""

    group: str
    field: str
    choices: list[str] = dc_field(default_factory=list)
    local_semantic: str | None = None
    # Existing HA option -> reviewed local semantic value.
    local_value_map: dict[str, str] = dc_field(default_factory=dict)
    local_scalar_semantic: str | None = None
    local_scalar_values: dict[str, str] = dc_field(default_factory=dict)


SELECTS_BY_TYPE: dict[str, tuple[MyLgSelectDescription, ...]] = {
    DEVICE_TYPE_AIR_CONDITIONER: (
        # Detailed fan speed incl. 미풍(SLOW_LOW) that the climate fan_mode
        # (windStrength) doesn't expose.
        MyLgSelectDescription(
            key="wind_strength_detail", translation_key="wind_strength_detail",
            group="airFlow", field="windStrengthDetail",
            choices=["SLOW_LOW", "LOW", "MID", "HIGH", "POWER", "AUTO"],
            local_semantic="fan.mode",
            local_value_map={
                "SLOW_LOW": "very low",
                "LOW": "low",
                "MID": "medium",
                "HIGH": "high",
                "POWER": "power",
                "AUTO": "auto",
            },
        ),
    ),
    DEVICE_TYPE_AIR_PURIFIER: (
        MyLgSelectDescription(
            key="job_mode", translation_key="job_mode",
            group="airPurifierJobMode", field="currentJobMode",
            choices=["CLEAN", "SILENT", "HUMIDITY"],
            local_scalar_semantic="operation.mode",
            local_scalar_values={"CLEAN": "clean", "SILENT": "silent", "HUMIDITY": "humidify"},
        ),
        MyLgSelectDescription(
            key="wind_strength_detail", translation_key="wind_strength_detail",
            group="airFlow", field="windStrengthDetail",
            choices=["OFF", "LOW", "MID", "HIGH", "AUTO"],
            local_scalar_semantic="fan.mode",
            local_scalar_values={"LOW": "low", "MID": "mid", "HIGH": "high", "AUTO": "auto"},
            entity_registry_enabled_default=False,
        ),
    ),
    DEVICE_TYPE_WATER_PURIFIER: (
        MyLgSelectDescription(
            key="water_type", translation_key="water_type",
            group="waterSetting", field="waterType",
            choices=["RECENT", "NORMAL", "COLD"],
            local_scalar_semantic="water.default_selection",
            local_scalar_values={"RECENT": "RECENT_WATER", "NORMAL": "NORMAL_WATER", "COLD": "COLD_WATER"},
        ),
        MyLgSelectDescription(
            key="default_water", translation_key="default_water",
            group="waterSetting", field="defaultWaterAmount",
            choices=["DEFAULT_WATER_1", "DEFAULT_WATER_2", "DEFAULT_WATER_3", "DEFAULT_WATER_4"],
        ),
    ),
    DEVICE_TYPE_HUMIDIFIER: (
        MyLgSelectDescription(
            key="wind_strength", translation_key="wind_strength",
            group="airFlow", field="windStrength",
            choices=["LOW", "MID", "HIGH", "POWER"],
            local_scalar_semantic="fan.mode",
            local_scalar_values={"LOW": "low", "MID": "mid", "HIGH": "high", "POWER": "turbo"},
            entity_registry_enabled_default=False,
        ),
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
            local_scalar_semantic="hygienic_dry.mode",
            local_scalar_values={"OFF": "off", "SILENT": "quiet", "NORMAL": "gentle", "FAST": "quick"},
        ),
    ),
    DEVICE_TYPE_DEHUMIDIFIER: (
        # 제습 풍량(약/강). windStrengthLevel이 정식 write 필드(windStrength는 alias).
        MyLgSelectDescription(
            key="wind_strength", translation_key="wind_strength",
            group="airFlow", field="windStrengthLevel",
            choices=["LOW", "HIGH"],
            local_scalar_semantic="fan.mode",
            local_scalar_values={"LOW": "low", "HIGH": "high"},
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
    local_semantic: str | None = None
    # Existing HA option -> reviewed local semantic value.
    local_value_map: dict[str, str] = dc_field(default_factory=dict)


WIDEQ_SELECTS_BY_TYPE: dict[str, tuple[MyLgWideqSelectDescription, ...]] = {
    DEVICE_TYPE_AIR_CONDITIONER: (
        # 자동건조: 냉방/제습 종료 후 내부를 말려 곰팡이 예방.
        MyLgWideqSelectDescription(
            key="auto_dry", translation_key="auto_dry",
            ctrl_key="settingInfo", data_key="airState.miscFuncState.autoDry",
            value_map={"off": 0, "on": 1, "30min": 2, "60min": 3, "ai_auto": 255},
            local_semantic="auto_dry.mode",
            local_value_map={
                "off": "off",
                "on": "10 min or firmware ON",
                "30min": "30 min",
                "60min": "60 min",
                "ai_auto": "smart",
            },
        ),
        # LED 디스플레이 밝기 (이 모델은 100=끄기 ~ 200=100% 스케일).
        MyLgWideqSelectDescription(
            key="display_brightness", translation_key="display_brightness",
            ctrl_key="settingInfo", data_key="airState.lightingState.displayControl",
            value_map={"off": 100, "20": 120, "40": 140, "50": 150,
                       "60": 160, "80": 180, "100": 200},
            local_semantic="display.brightness_level",
            local_value_map={"off": "off", "50": "50%", "100": "100%"},
        ),
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    from .feature_runtime import setup_feature_entities

    setup_feature_entities(entry, "select", lambda: _build_entities(entry), async_add_entities)


def _build_entities(entry: MyLgConfigEntry) -> list[SelectEntity]:
    entities: list[SelectEntity] = []
    local_control = entry.runtime_data.local_control
    primary_providers = getattr(entry.runtime_data, "local_providers", {})
    for coordinator in entry.runtime_data.coordinators.values():
        local_read_provider = entry.runtime_data.local_read_providers.get(
            coordinator.device_id
        )
        for desc in SELECTS_BY_TYPE.get(coordinator.device_type, ()):
            local_owned = tlv_read_owner_configured(
                local_read_provider, desc.local_semantic
            )
            if (
                coordinator.supports_field(desc.group, desc.field)
                or coordinator.get(desc.group, desc.field) is not None
                or local_owned
            ):
                entity = MyLgSelect(
                    coordinator,
                    desc,
                    local_control,
                    local_read_provider,
                )
                if not local_owned or entity.options:
                    entities.append(entity)

        primary_provider = primary_providers.get(coordinator.device_id)
        composite_contract = getattr(
            entry.runtime_data, "local_control_composite_domain_contract", None
        )
        climate_domain = local_climate_control_domain(
            primary_provider,
            coordinator.model,
            composite_contract,
        )
        if (
            climate_domain is not None
            and local_auto_comfort_owner_configured(
                primary_provider,
                coordinator.model,
                composite_contract,
            )
        ):
            assert primary_provider is not None
            entities.append(
                MyLgLocalAutoComfortSelect(
                    coordinator,
                    primary_provider,
                    local_control,
                    climate_domain,
                )
            )

    wideq: WideqCoordinator | None = entry.runtime_data.wideq_coordinator
    if wideq is not None:
        allow_hazardous = bool(
            entry.options.get(OPT_ALLOW_HAZARDOUS_CONTROLS, False)
        )
        allow_experimental = bool(
            entry.options.get(OPT_ALLOW_EXPERIMENTAL_CONTROLS, False)
        )
        for coordinator in entry.runtime_data.coordinators.values():
            local_read_provider = entry.runtime_data.local_read_providers.get(
                coordinator.device_id
            )
            for wdesc in WIDEQ_SELECTS_BY_TYPE.get(coordinator.device_type, ()):
                entity = MyLgWideqSelect(
                    wideq,
                    coordinator,
                    wdesc,
                    local_control,
                    local_read_provider,
                )
                if (
                    not tlv_read_owner_configured(
                        local_read_provider, wdesc.local_semantic
                    )
                    or entity.options
                ):
                    entities.append(entity)
            for control in iter_wideq_field_controls(coordinator.model):
                if control.value_type == "enum" and control.options:
                    entities.append(
                        MyLgWideqCatalogSelect(
                            wideq,
                            coordinator,
                            control,
                            allow_hazardous,
                            allow_experimental,
                        )
                    )

    entities.extend(local_control_entities_for_domain(entry, "select"))
    return entities


class _LocalReadSelectMixin:
    """Overlay an exact reviewed local semantic onto an existing select."""

    _local_read_provider: TlvReadShadowProvider | None
    _local_read_semantic: str | None
    _local_option_by_value: dict[str, str]
    _remove_local_read_listener: Callable[[], None] | None

    def _configure_local_read(
        self,
        provider: TlvReadShadowProvider | None,
        semantic_id: str | None,
        local_value_map: dict[str, str],
    ) -> None:
        self._local_read_provider = provider
        self._local_read_semantic = semantic_id
        self._local_option_by_value = {
            local_value: option for option, local_value in local_value_map.items()
        }
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

    def _local_current_option(self) -> tuple[bool, str | None]:
        semantic_id = self._local_read_semantic
        state = resolve_tlv_read_owned_field(
            self._local_read_provider, semantic_id
        )
        if not state.owner_configured or semantic_id is None:
            return False, None
        raw = state.value
        if not isinstance(raw, str):
            if state.available:
                self._report_invalid_local_read(semantic_id)
            return False, None
        option = self._local_option_by_value.get(raw)
        if option is None or option not in self.options:
            if state.available:
                self._report_invalid_local_read(semantic_id)
            return False, None
        return state.available, option

    def _local_field_available(self) -> bool:
        return self._local_current_option()[0]

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


class MyLgLocalAutoComfortSelect(SelectEntity):
    """Five app-labelled AUTO comfort steps, owned entirely by Local state/control."""

    _attr_has_entity_name = True
    _attr_translation_key = "auto_comfort_preference"
    _attr_options = list(_AUTO_COMFORT_OPTIONS)
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: PatDeviceCoordinator,
        provider: LocalSemanticShadowProvider,
        router: LocalControlRouter | None,
        domain: LocalControlCompositeInputDomain,
    ) -> None:
        self._metadata = coordinator
        self._provider = provider
        self._router = router
        self._domain = domain
        self._remove_listener: Callable[[], None] | None = None
        self._attr_unique_id = f"{coordinator.device_id}_auto_comfort_preference"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.device_id)},
            name=coordinator.alias,
            manufacturer="LG",
            model=coordinator.model or coordinator.device_type,
        )

    def _power_requested(self) -> bool | None:
        """Return the exact Local power state, never a PAT/WideQ fallback."""
        if not self._provider.semantic_field_available(
            "operation.power_requested"
        ):
            return None
        value = self._provider.field_value("operation.power_requested")
        return value if type(value) is bool else None

    @property
    def available(self) -> bool:
        router = self._router
        power = self._power_requested()
        capability = (
            CLIMATE_TUPLE_CAPABILITY
            if power is True
            else CLIMATE_POWER_ON_CAPABILITY
        )
        return (
            self._provider.shadow_healthy
            and power is not None
            and router is not None
            and router.control_target_available(self._metadata.device_id)
            and router.capability_authorized(
                self._metadata.device_id, capability
            )
            and "auto" in self._domain.comfort_preference.applies_to_modes
        )

    @property
    def current_option(self) -> str | None:
        if (
            self._provider.field_value("operation.mode") != "auto"
            or not self._provider.semantic_field_available(
                "comfort.preference_step"
            )
        ):
            return None
        value = self._provider.field_value("comfort.preference_step")
        return _AUTO_COMFORT_OPTION.get(value) if type(value) is int else None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._remove_listener = self._provider.async_add_listener(
            self._handle_local_update
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_local_update(self) -> None:
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        preference = _AUTO_COMFORT_VALUE.get(option)
        router = self._router
        power = self._power_requested()
        if preference is None or router is None or power is None or not self.available:
            raise HomeAssistantError(
                f"{self._metadata.alias}: 이 자동 쾌적 설정은 Local로 전송할 수 없어요."
            )
        try:
            operation = (
                router.async_set_climate(
                    self._metadata.device_id,
                    mode="auto",
                    comfort_preference=preference,
                    cloud_fallback=False,
                )
                if power
                else router.async_turn_on(
                    self._metadata.device_id,
                    mode="auto",
                    comfort_preference=preference,
                    cloud_fallback=False,
                )
            )
            outcome = await operation
        except LocalCommandPending:
            # It may already be applied. The Local provider is the only state source.
            return
        except LocalCommandFailed as err:
            raise HomeAssistantError(
                f"{self._metadata.alias}: Local 명령 응답을 확인할 수 없어요."
            ) from err
        if outcome is None:
            raise HomeAssistantError(
                f"{self._metadata.alias}: Local 명령이 전송 전에 거부됐어요."
            )


class MyLgSelect(LocalConditionEntityMixin, _LocalReadSelectMixin, MyLgEntity, SelectEntity):
    entity_description: MyLgSelectDescription

    def __init__(
        self,
        coordinator: PatDeviceCoordinator,
        description: MyLgSelectDescription,
        local_control: LocalControlRouter | None = None,
        local_read_provider: TlvReadShadowProvider | None = None,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_options = description.choices
        self._local_control = local_control
        self._configure_local_read(
            local_read_provider,
            description.local_semantic,
            description.local_value_map,
        )
        if self._local_read_owns_state():
            authorized = (
                ()
                if local_control is None
                else local_control.authorized_values(
                    coordinator.device_id, CLIMATE_TUPLE_CAPABILITY
                )
            )
            authorized_fans = {
                parts[1]
                for value in authorized
                if len(parts := value.split("|")) == 3
            }
            self._attr_options = [
                option
                for option in description.choices
                if description.local_value_map.get(option) in authorized_fans
            ]

    @property
    def current_option(self) -> str | None:
        d = self.entity_description
        local_available, option = self._local_current_option()
        if self._local_read_owns_state():
            return option
        return self._get(d.group, d.field)

    @property
    def options(self) -> list[str]:
        capability = self.entity_description.local_scalar_semantic
        if self._local_control is None or capability is None:
            return self._attr_options
        return [option for option in self._attr_options
                if self._local_control.feature_condition_available(
                    self.coordinator.device_id, capability, self.entity_description.local_scalar_values.get(option))]

    @property
    def available(self) -> bool:
        capability = self.entity_description.local_scalar_semantic
        if self._local_control is not None and capability is not None and not self._local_control.feature_condition_available(self.coordinator.device_id, capability):
            return False
        if self._local_read_owns_state():
            return self._local_field_available()
        return native_local_available(self._local_control, self.coordinator.device_id,
                                      self.entity_description.local_scalar_semantic) or super().available

    async def async_select_option(self, option: str) -> None:
        d = self.entity_description
        payload = {d.group: {d.field: option}}
        local_value = d.local_value_map.get(option)
        if (
            self._local_read_owns_state()
            and d.local_semantic == "fan.mode"
        ):
            if self._local_control is None or local_value is None:
                raise HomeAssistantError(
                    f"{self.coordinator.alias}: 검증된 Local 풍량 선택지가 아니어서 전송하지 않았어요."
                )
            try:
                outcome = await self._local_control.async_set_climate(
                    self.coordinator.device_id,
                    fan=local_value,
                    cloud_fallback=False,
                )
            except LocalCommandPending:
                return
            except LocalCommandFailed as err:
                raise HomeAssistantError(
                    f"{self.coordinator.alias}: {err}"
                ) from err
            if outcome is None:
                raise HomeAssistantError(
                    f"{self.coordinator.alias}: 이 풍량 조합은 검증된 Local 명령 범위에 없어서 전송하지 않았어요."
                )
            # Confirmed and post-wire-unverifiable results both reconcile from
            # the Local readback. Never write a PAT optimistic shadow.
            return
        if await async_native_local_control(self._local_control, self.coordinator.device_id,
                                            d.local_scalar_semantic, d.local_scalar_values.get(option)):
            return
        await self.coordinator.async_control(payload)
        self.coordinator.handle_mqtt_status(payload)


class MyLgWideqSelect(_LocalReadSelectMixin, MyLgWideqEntity, SelectEntity):
    """A wideq-only enum (AC auto-dry, LED display brightness…)."""

    entity_description: MyLgWideqSelectDescription

    def __init__(
        self,
        wideq_coordinator: WideqCoordinator,
        pat_coordinator: PatDeviceCoordinator,
        description: MyLgWideqSelectDescription,
        local_control: LocalControlRouter | None = None,
        local_read_provider: TlvReadShadowProvider | None = None,
    ) -> None:
        super().__init__(wideq_coordinator, pat_coordinator, description.key)
        self.entity_description = description
        self._attr_options = list(description.value_map)
        self._reverse = {v: k for k, v in description.value_map.items()}
        self._local_control = local_control
        self._configure_local_read(
            local_read_provider,
            description.local_semantic,
            description.local_value_map,
        )
        if self._local_read_owns_state():
            authorized = (
                ()
                if local_control is None or description.local_semantic is None
                else local_control.authorized_values(
                    pat_coordinator.device_id, description.local_semantic
                )
            )
            self._attr_options = [
                option
                for option in description.value_map
                if description.local_value_map.get(option) in authorized
            ]

    @property
    def current_option(self) -> str | None:
        local_available, option = self._local_current_option()
        if self._local_read_owns_state():
            return option
        raw = self._snapshot.get(self.entity_description.data_key)
        if raw is None:
            return None
        try:
            return self._reverse.get(int(raw))
        except (TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        if self._local_read_owns_state():
            return self._local_field_available()
        return super().available

    async def async_select_option(self, option: str) -> None:
        d = self.entity_description
        value = d.value_map.get(option)
        if value is None:
            return
        local_value = d.local_value_map.get(option)
        if self._local_read_owns_state():
            if (
                self._local_control is None
                or d.local_semantic is None
                or local_value is None
            ):
                raise HomeAssistantError(
                    f"{self._pat_coordinator.alias}: 검증된 Local 선택지가 아니어서 전송하지 않았어요."
                )
            try:
                outcome = await self._local_control.async_set_value(
                    self._device_id,
                    d.local_semantic,
                    local_value,
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
        await self._wideq_set(d.ctrl_key, d.data_key, value, d.use_dataset)


class MyLgWideqCatalogSelect(MyLgWideqEntity, SelectEntity):
    """A model-advertised enum that is not duplicated by PAT."""

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
        self._attr_options = list(control.options)

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
    def current_option(self) -> str | None:
        option = normalize_option(self._snapshot.get(self._control.field))
        return option if option in self.options else None

    async def async_select_option(self, option: str) -> None:
        value: Any = option
        if option.lstrip("-").isdigit():
            value = int(option)
        await self._wideq_set(
            self._control.ctrl_key,
            self._control.field,
            value,
            self._control.use_dataset,
            optimistic=self._control.risk == "low",
        )
