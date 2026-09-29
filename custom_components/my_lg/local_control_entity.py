"""Disabled-by-default Home Assistant owners for exact Local controls."""

from __future__ import annotations

import asyncio
import aiohttp
import hashlib
import logging
import math
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, cast

from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import NumberEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.components.text import TextEntity
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory

from .entity import MyLgEntity
from .local_command import (
    LocalCommandBusy,
    LocalCommandFailed,
    LocalCommandPending,
    LocalCommandRetryable,
)
from .local_control_contract import (
    LocalControlEntityDescriptor,
    eligible_factory_descriptors,
)
from .local_control_router import LocalControlRouter
from .local_control_confirmed_features import APPLIANCE_SETTING_MODELS, APPLIANCE_VALUE_MODELS
from .local_washer_options import CAPABILITY as WASHER_PROGRAM, canonical_program
from .local_dryer_options import CAPABILITY as DRYER_PROGRAM, canonical_program as canonical_dryer_program
from .local_styler_options import CAPABILITY as STYLER_PROGRAM, canonical_program as canonical_styler_program
from .local_styler_dnd import RESERVATION as STYLER_DND_RESERVATION, canonical_reservation
from .local_water_dnd import WINDOW as WATER_DND_WINDOW, canonical_window
from .local_water_parameters import (
    CUSTOM_RECIPES as WATER_CUSTOM_RECIPES,
    HOT_TEMPERATURE_PRESETS as WATER_HOT_TEMPERATURE_PRESETS,
    PRESETS as WATER_AMOUNT_PRESETS,
    SCHEMAS as WATER_PARAMETER_SCHEMAS,
    STERILIZATION as WATER_STERILIZATION_CALENDAR,
    canonical_parameter,
)
from .local_vacuum_reservation import ENABLED as RESERVATION_ENABLED, SCHEDULE as RESERVATION_SCHEDULE, canonical_schedule, display_schedule
from .local_provider import LocalSemanticShadowProvider
from .local_read_provider import TlvReadShadowProvider

_LOGGER = logging.getLogger(__name__)

LocalControlDomain = Literal["switch", "select", "number", "button", "text"]


def _same_primitive(left: object, right: object) -> bool:
    """Compare contract values without treating bool as the integers 0/1."""
    return type(left) is type(right) and left == right


def local_control_unique_id(pat_device_id: str, entity_key: str) -> str:
    """Return a stable Local-control unique id bounded to 128 characters."""
    candidate = f"{pat_device_id}_{entity_key}"
    if len(candidate) <= 128:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:16]
    suffix = f"_{digest}"
    return f"{candidate[: 128 - len(suffix)]}{suffix}"


class _LocalContractEntity(MyLgEntity):
    """Common exact-state listener and one-command/no-cloud write policy."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator,
        descriptor: LocalControlEntityDescriptor,
        router: LocalControlRouter,
        primary_provider: LocalSemanticShadowProvider,
        read_provider: TlvReadShadowProvider | None,
    ) -> None:
        super().__init__(coordinator, descriptor.home_assistant_entity_key)
        self._attr_unique_id = local_control_unique_id(
            coordinator.device_id, descriptor.home_assistant_entity_key
        )
        self._descriptor = descriptor
        self._router = router
        self._primary_provider = primary_provider
        self._read_provider = read_provider
        self._attr_name = descriptor.label_ko
        self._remove_local_listeners: list[Callable[[], None]] = []
        self._reported_state_diagnostics: set[str] = set()
        self._prewire_refused = False
        self._command_lock = asyncio.Lock()

    def _report_state_diagnostic(self, reason: str) -> None:
        semantic_id = self._descriptor.exact_state_semantic
        key = f"{semantic_id}:{reason}"
        if key in self._reported_state_diagnostics:
            return
        self._reported_state_diagnostics.add(key)
        # Semantic ids are public contract vocabulary.  Never log the raw
        # value, binding/device identity, alias, or source locator here.
        _LOGGER.warning(
            "Rethink Local control state semantic %s is %s; no state was guessed",
            semantic_id,
            reason,
        )

    def _state_value(self) -> object | None:
        semantic_id = self._descriptor.exact_state_semantic
        if semantic_id is None:
            return None
        candidates: list[object] = []
        read = self._read_provider
        if read is not None and read.field_available(semantic_id):
            value = read.field_value(semantic_id)
            if value is not None:
                candidates.append(value)
        primary = self._primary_provider
        if primary.semantic_field_available(semantic_id):
            value = primary.field_value(semantic_id)
            if value is not None:
                candidates.append(value)
        if not candidates:
            return None
        first = candidates[0]
        if any(not _same_primitive(first, candidate) for candidate in candidates[1:]):
            self._report_state_diagnostic("inconsistent across exact Local sources")
            return None
        return first

    @property
    def available(self) -> bool:
        return (
            not self._prewire_refused
            and self.coordinator.model == self._descriptor.model_id
            and self._primary_provider.model_id == self._descriptor.model_id
            and self._primary_provider.control_alive
            and self._router.control_target_available(self.coordinator.device_id)
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._descriptor.capability_id in (STYLER_PROGRAM, 'styler.operation.start_or_resume'):
            self._remove_local_listeners.append(
                self._router.subscribe_styler_choice(self.coordinator.device_id, self._handle_local_update)
            )
        self._remove_local_listeners.append(
            self._primary_provider.async_add_listener(self._handle_local_update)
        )
        if self._read_provider is not None:
            self._remove_local_listeners.append(
                self._read_provider.async_add_listener(self._handle_local_update)
            )

    async def async_will_remove_from_hass(self) -> None:
        for remove in self._remove_local_listeners:
            remove()
        self._remove_local_listeners.clear()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_local_update(self) -> None:
        self.async_write_ha_state()

    async def _async_send(self, local_request_value: str) -> None:
        if not self.available:
            raise HomeAssistantError(
                "Rethink Local 제어 연결 또는 정확한 모델 계약을 확인할 수 없어요."
            )
        async with self._command_lock:
            try:
                if self._descriptor.one_shot:
                    outcome = await self._router.async_execute_strict(
                        self.coordinator.device_id,
                        self._descriptor.capability_id,
                        local_request_value,
                    )
                else:
                    outcome = await self._router.async_set_value_strict(
                        self.coordinator.device_id,
                        self._descriptor.capability_id,
                        local_request_value,
                    )
            except LocalCommandRetryable as err:
                message = (
                    "다른 명령을 확인하고 있어요. 잠시 후 다시 시도해 주세요."
                    if isinstance(err, LocalCommandBusy)
                    else "현재 기기 상태에서는 실행할 수 없어요. 상태가 갱신되거나 조건이 맞은 뒤 다시 시도해 주세요."
                )
                raise HomeAssistantError(message) from err
            except LocalCommandPending:
                # A frame may already be applied. Keep the service call
                # non-optimistic and let Local readback reconcile it; surfacing
                # a definite failure would invite a duplicate user retry.
                return
            except LocalCommandFailed as err:
                raise HomeAssistantError(
                    "Rethink Local 명령 응답이 유효하지 않아 상태를 바꾸지 않았어요."
                ) from err
            if outcome is None:
                # None is a guaranteed pre-wire refusal.  Unlike existing
                # owners, this generic surface has no cloud fallback.
                self._prewire_refused = True
                self.async_write_ha_state()
                raise HomeAssistantError(
                    "Rethink Local 명령이 전송 전에 거부됐어요. 이 제어는 다시 불러올 때까지 사용할 수 없어요."
                )
            if not outcome.confirmed:
                # An unverifiable verdict still means a frame left the bridge.
                # Never issue a second command or reflect an optimistic state;
                # the next Local readback is the only reconciliation source.
                return


class MyLgLocalContractSwitch(_LocalContractEntity, SwitchEntity):
    """Exact two-polarity boolean control."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        mappings = {item.home_assistant_value: item for item in self._descriptor.value_mappings}
        if set(mappings) != {"off", "on"}:
            raise ValueError("Local contract switch requires exact off/on mappings")
        self._off_value = mappings["off"]
        self._on_value = mappings["on"]

    @property
    def is_on(self) -> bool | None:
        value = self._state_value()
        if value is None:
            return None
        for supported, mapping in zip(
            self._descriptor.supported_values, self._descriptor.value_mappings
        ):
            if _same_primitive(value, supported):
                return mapping.home_assistant_value == "on"
        self._report_state_diagnostic("outside the exact boolean domain")
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_send(self._on_value.local_request_value)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_send(self._off_value.local_request_value)


class MyLgBridgeCachedSwitch(MyLgLocalContractSwitch):
    """Local-only setting and display; poll only the bridge's in-memory cache.

    Use HA's normal switch polling lifecycle, not a custom timer or LG poll.
    No command value is reflected optimistically.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._reported_enabled: bool | None = None

    @property
    def should_poll(self) -> bool:
        return True

    def _state_value(self) -> object | None:
        return self._reported_enabled

    async def async_update(self) -> None:
        try:
            value = await self._async_reported_value()
            self._reported_enabled = value if type(value) is bool else None
        except (TimeoutError, OSError, ValueError, aiohttp.ClientError):
            self._reported_enabled = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self.async_update()
        self.async_write_ha_state()

    async def _async_send(self, local_request_value: str) -> None:
        await super()._async_send(local_request_value)
        await self.async_update()
        self.async_write_ha_state()


class MyLgVacuumAutoEmptyingSwitch(MyLgBridgeCachedSwitch):
    async def _async_reported_value(self) -> bool | None:
        return await self._router.async_vacuum_auto_emptying_state(self.coordinator.device_id)


class MyLgVacuumReservationSwitch(MyLgBridgeCachedSwitch):
    async def _async_reported_value(self) -> bool | None:
        state = await self._router.async_vacuum_reservation_state(self.coordinator.device_id)
        return state.get(RESERVATION_ENABLED) if state else None


class MyLgVacuumReservationText(_LocalContractEntity, TextEntity):
    """Atomic HH:MM|weekdays edit. No local timer, no optimistic state, no cloud retry."""
    _attr_native_min = 0
    _attr_native_max = 64
    _attr_icon = 'mdi:calendar-clock'

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._reported_schedule: str | None = None

    @property
    def should_poll(self) -> bool:
        return True

    @property
    def native_value(self) -> str | None:
        return display_schedule(self._reported_schedule) if self._reported_schedule is not None else None

    async def async_update(self) -> None:
        try:
            state = await self._router.async_vacuum_reservation_state(self.coordinator.device_id)
            self._reported_schedule = state.get(RESERVATION_SCHEDULE) if state else None
        except (TimeoutError, OSError, ValueError, aiohttp.ClientError):
            self._reported_schedule = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self.async_update()
        self.async_write_ha_state()

    async def async_set_value(self, value: str) -> None:
        try:
            request = canonical_schedule(value)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        await self._async_send(request)
        await self.async_update()
        self.async_write_ha_state()


class MyLgApplianceSettingSwitch(MyLgBridgeCachedSwitch):
    async def _async_reported_value(self) -> bool | None:
        return await self._router.async_appliance_setting_state(self.coordinator.device_id, self._descriptor.capability_id)


class MyLgAirExtraSwitch(MyLgBridgeCachedSwitch):
    async def _async_reported_value(self) -> bool | None:
        return await self._router.async_air_extra_state(self.coordinator.device_id, self._descriptor.capability_id)


class MyLgAirExtraLegacyJetSwitch(MyLgAirExtraSwitch):
    """Use the reviewed local rapid operation under the established jet ID."""

    def __init__(self, coordinator, descriptor, router, primary_provider, read_provider) -> None:
        if descriptor.model_id != 'AIR_910604_WW' or descriptor.capability_id != 'rapid_operation.enabled':
            raise ValueError('Legacy jet identity is limited to the exact AIR_910604_WW rapid setting')
        super().__init__(coordinator, descriptor, router, primary_provider, read_provider)
        self._attr_unique_id = f'{coordinator.device_id}_jet_mode'
        self._attr_name = 'Jet mode'


class MyLgAirExtraLegacyUvSwitch(MyLgAirExtraSwitch):
    """Keep the old UV switch identity for Web's confirmed hygienic-dry toggle."""

    def __init__(self, coordinator, descriptor, router, primary_provider, read_provider) -> None:
        if descriptor.model_id != 'AIR_910604_WW' or descriptor.capability_id != 'clean_dry.enabled':
            raise ValueError('Legacy UV identity is limited to the exact AIR_910604_WW hygienic-dry setting')
        super().__init__(coordinator, descriptor, router, primary_provider, read_provider)
        self._attr_unique_id = f'{coordinator.device_id}_uv_disinfection'
        self._attr_name = '위생 건조'


class MyLgTowerLegacyUvSwitch(MyLgLocalContractSwitch):
    """Expose the exact tower UVnano control under its pre-existing UV ID."""

    def __init__(self, coordinator, descriptor, router, primary_provider, read_provider) -> None:
        if descriptor.model_id != 'AIR_2C0001_WW' or descriptor.capability_id != 'sterilization.uvnano_enabled':
            raise ValueError('Legacy UV identity is limited to the exact AIR_2C0001_WW UVnano setting')
        super().__init__(coordinator, descriptor, router, primary_provider, read_provider)
        self._attr_unique_id = f'{coordinator.device_id}_uv_disinfection'
        self._attr_name = 'UVnano 공기살균'


class MyLgLocalContractSelect(_LocalContractEntity, SelectEntity):
    """Exact enum or sparse numeric control."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._mapping_by_option = {
            cast(str, item.home_assistant_value): item
            for item in self._descriptor.value_mappings
        }
        self._attr_options = list(self._mapping_by_option)

    @property
    def current_option(self) -> str | None:
        if self._descriptor.capability_id == "styler.operation.start_or_resume":
            desired = self._router.selected_styler_course(self.coordinator.device_id)
            return next((option for option, mapping in self._mapping_by_option.items() if mapping.local_request_value == desired), None)
        value = self._state_value()
        if value is None:
            return None
        for supported, mapping in zip(
            self._descriptor.supported_values, self._descriptor.value_mappings
        ):
            if _same_primitive(value, supported):
                return cast(str, mapping.home_assistant_value)
        self._report_state_diagnostic("outside the exact select options")
        return None

    async def async_select_option(self, option: str) -> None:
        mapping = self._mapping_by_option.get(option)
        if mapping is None:
            raise HomeAssistantError(
                "검증된 Rethink Local 선택지에 없는 값은 보낼 수 없어요."
            )
        if self._descriptor.capability_id == "styler.operation.start_or_resume":
            # A course bundle can start the appliance. Selection is UI-only;
            # the existing Start button dispatches the complete producer transaction.
            self._router.select_styler_course(self.coordinator.device_id, mapping.local_request_value)
            self.async_write_ha_state()
            return
        await self._async_send(mapping.local_request_value)


class MyLgApplianceSettingSelect(MyLgLocalContractSelect):
    """A real own-connection value, not a one-shot or optimistic UI selection."""
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._reported_value: str | None = None

    @property
    def should_poll(self) -> bool:
        return True

    @property
    def current_option(self) -> str | None:
        return next((option for option,mapping in self._mapping_by_option.items()
                     if mapping.local_request_value == self._reported_value),None)

    async def async_update(self) -> None:
        try:
            value=await self._router.async_appliance_setting_state(self.coordinator.device_id,self._descriptor.capability_id)
            self._reported_value=value if isinstance(value,str) else None
        except (TimeoutError,OSError,ValueError,aiohttp.ClientError):
            self._reported_value=None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self.async_update()
        self.async_write_ha_state()

    async def _async_send(self, local_request_value: str) -> None:
        await super()._async_send(local_request_value)
        await self.async_update()
        self.async_write_ha_state()


class MyLgWaterDndText(_LocalContractEntity, TextEntity):
    """Korean wall-clock input; saving times never enables DND or dispenses water."""
    _attr_native_min = 11
    _attr_native_max = 11
    _attr_icon = 'mdi:clock-outline'

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._reported_value: str | None = None

    @property
    def should_poll(self) -> bool:
        return True

    @property
    def native_value(self) -> str | None:
        return self._reported_value

    async def async_update(self) -> None:
        try:
            value=await self._router.async_appliance_setting_state(self.coordinator.device_id,self._descriptor.capability_id)
            self._reported_value=self._canonical(value) if isinstance(value,str) else None
        except (TimeoutError,OSError,ValueError,aiohttp.ClientError):
            self._reported_value=None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self.async_update()
        self.async_write_ha_state()

    async def async_set_value(self, value: str) -> None:
        try:
            request=self._canonical(value)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        await self._async_send(request)
        await self.async_update()
        self.async_write_ha_state()

    def _canonical(self, value: str) -> str:
        return canonical_window(value)


class MyLgWaterParameterText(MyLgWaterDndText):
    """Whole preset transaction or date-preserving calendar; never dispense/start."""

    _LENGTH_LIMITS = {
        WATER_AMOUNT_PRESETS: (15, 19),
        WATER_STERILIZATION_CALENDAR: (11, 11),
        WATER_HOT_TEMPERATURE_PRESETS: (8, 8),
        **{capability: (3, 64) for capability in WATER_CUSTOM_RECIPES},
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._attr_native_min, self._attr_native_max = self._LENGTH_LIMITS[
            self._descriptor.capability_id
        ]

    def _canonical(self, value: str) -> str:
        return canonical_parameter(self._descriptor.capability_id, value)


class MyLgStylerDndText(MyLgWaterDndText):
    """Atomic Styler DND time/mute tuple; partial writes are never sent."""

    _attr_native_min = 17
    _attr_native_max = 19
    _attr_icon = 'mdi:bell-sleep-outline'

    def _canonical(self, value: str) -> str:
        return canonical_reservation(value)


class MyLgWasherOptionProgramText(MyLgWaterDndText):
    """Complete current-course option program; starting remains a separate action."""
    _attr_native_min = 19
    _attr_native_max = 255
    _attr_icon = 'mdi:washing-machine'

    def _canonical(self, value: str) -> str:
        return canonical_program(value)


class MyLgDryerOptionProgramText(MyLgWaterDndText):
    """Explicit whole replacement: care options may reset, never starts drying."""
    _attr_native_min = 19
    _attr_native_max = 255
    _attr_icon = 'mdi:tumble-dryer'

    def _canonical(self, value: str) -> str:
        return canonical_dryer_program(value)


class MyLgLocalContractNumber(_LocalContractEntity, NumberEntity):
    """A complete exact numeric grid; every off-grid write is rejected."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        descriptor = self._descriptor
        if (
            descriptor.number_min is None
            or descriptor.number_max is None
            or descriptor.number_step is None
        ):
            raise ValueError("Local contract number has no complete grid")
        self._attr_native_min_value = descriptor.number_min
        self._attr_native_max_value = descriptor.number_max
        self._attr_native_step = descriptor.number_step
        self._attr_native_unit_of_measurement = descriptor.unit

    @staticmethod
    def _decimal(value: object) -> Decimal | None:
        if type(value) not in (int, float) or (
            type(value) is float and not math.isfinite(value)
        ):
            return None
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None

    def _mapping_for_number(self, value: object):
        requested = self._decimal(value)
        if requested is None:
            return None
        for mapping in self._descriptor.value_mappings:
            if self._decimal(mapping.home_assistant_value) == requested:
                return mapping
        return None

    @property
    def native_value(self) -> float | None:
        value = self._state_value()
        mapping = self._mapping_for_number(value)
        if mapping is None:
            if value is not None:
                self._report_state_diagnostic("outside the exact number grid")
            return None
        return float(mapping.home_assistant_value)

    async def async_set_native_value(self, value: float) -> None:
        mapping = self._mapping_for_number(value)
        if mapping is None:
            raise HomeAssistantError(
                "검증된 Rethink Local 숫자 단계에 없는 값은 보낼 수 없어요."
            )
        await self._async_send(mapping.local_request_value)


class MyLgStylerOptionDraftText(_LocalContractEntity, TextEntity):
    """Volatile desired start parameters, never an appliance state or write."""
    _attr_native_min = 0
    _attr_native_max = 160
    _attr_icon = 'mdi:wardrobe-outline'

    @property
    def native_value(self):
        return self._router.selected_styler_options(self.coordinator.device_id)

    async def async_set_value(self, value):
        try:
            self._router.select_styler_options(self.coordinator.device_id, canonical_styler_program(value))
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        self.async_write_ha_state()


class MyLgLocalContractButton(_LocalContractEntity, ButtonEntity):
    """One reviewed, parameterless one-shot command."""

    async def async_press(self) -> None:
        mappings = self._descriptor.value_mappings
        if len(mappings) != 1 or mappings[0].home_assistant_value != "press":
            raise HomeAssistantError("검증된 Rethink Local 단발 명령이 아니에요.")
        await self._async_send(mappings[0].local_request_value)


_ENTITY_CLASS_BY_DOMAIN = {
    "switch": MyLgLocalContractSwitch,
    "select": MyLgLocalContractSelect,
    "number": MyLgLocalContractNumber,
    "button": MyLgLocalContractButton,
    "text": MyLgVacuumReservationText,
}


def local_control_entities_for_domain(entry, domain: LocalControlDomain) -> list[Any]:
    """Materialize only exact private/public intersections for this platform."""
    data = entry.runtime_data
    # Older/migrating config entries may not yet carry the optional generic
    # control fields.  Their existing PAT/WideQ surfaces must remain intact.
    contract = getattr(data, "local_control_entity_contract", None)
    router = getattr(data, "local_control", None)
    eligibility = getattr(data, "local_control_binding_eligibility", {})
    if contract is None or router is None or not eligibility:
        return []
    entity_class = _ENTITY_CLASS_BY_DOMAIN[domain]
    entities: list[Any] = []
    seen_surfaces: set[tuple[str, str]] = set()
    for coordinator in data.coordinators.values():
        primary = data.local_providers.get(coordinator.device_id)
        if (
            primary is None
            or primary.model_id != coordinator.model
            or primary.binding_id not in eligibility
        ):
            continue
        read = data.local_read_providers.get(coordinator.device_id)
        for descriptor in eligible_factory_descriptors(
            contract,
            eligibility,
            binding_id=primary.binding_id,
            model_id=coordinator.model,
            domain=domain,
        ):
            surface = (coordinator.device_id, descriptor.home_assistant_entity_key)
            if surface in seen_surfaces:
                # A pinned contract should make this impossible.  Fail closed
                # per surface instead of creating duplicate registry owners.
                continue
            seen_surfaces.add(surface)
            if descriptor.capability_id == STYLER_PROGRAM and domain == 'text':
                entities.append(MyLgStylerOptionDraftText(coordinator, descriptor, router, primary, read))
                continue
            setting_key = (descriptor.model_id, descriptor.capability_id)
            if APPLIANCE_SETTING_MODELS.get(setting_key) == descriptor.model_id and domain == 'switch':
                entities.append(MyLgApplianceSettingSwitch(coordinator, descriptor, router, primary, read))
                continue
            if APPLIANCE_VALUE_MODELS.get(setting_key) == descriptor.model_id:
                if domain == 'text' and descriptor.capability_id == DRYER_PROGRAM:
                    entities.append(MyLgDryerOptionProgramText(coordinator, descriptor, router, primary, read))
                    continue
                if domain == 'text' and descriptor.capability_id == WASHER_PROGRAM:
                    entities.append(MyLgWasherOptionProgramText(coordinator, descriptor, router, primary, read))
                    continue
                if domain == 'select':
                    entities.append(MyLgApplianceSettingSelect(coordinator, descriptor, router, primary, read))
                    continue
                if domain == 'text' and descriptor.capability_id == WATER_DND_WINDOW:
                    entities.append(MyLgWaterDndText(coordinator, descriptor, router, primary, read))
                    continue
                if domain == 'text' and descriptor.capability_id == STYLER_DND_RESERVATION:
                    entities.append(MyLgStylerDndText(coordinator, descriptor, router, primary, read))
                    continue
                if domain == 'text' and descriptor.capability_id in WATER_PARAMETER_SCHEMAS:
                    entities.append(MyLgWaterParameterText(coordinator, descriptor, router, primary, read))
                    continue
            if descriptor.model_id == 'AIR_910604_WW' and descriptor.capability_id == 'rapid_operation.enabled' and domain == 'switch':
                entities.append(MyLgAirExtraLegacyJetSwitch(coordinator, descriptor, router, primary, read))
                continue
            if descriptor.model_id == 'AIR_910604_WW' and descriptor.capability_id == 'clean_dry.enabled' and domain == 'switch':
                entities.append(MyLgAirExtraLegacyUvSwitch(coordinator, descriptor, router, primary, read))
                continue
            if descriptor.model_id == 'AIR_2C0001_WW' and descriptor.capability_id == 'sterilization.uvnano_enabled' and domain == 'switch':
                entities.append(MyLgTowerLegacyUvSwitch(coordinator, descriptor, router, primary, read))
                continue
            if descriptor.capability_id == "vacuum.auto_dust_emptying_enabled" and domain == "switch":
                entities.append(MyLgVacuumAutoEmptyingSwitch(coordinator, descriptor, router, primary, read))
                continue
            if descriptor.capability_id == RESERVATION_ENABLED and domain == 'switch':
                entities.append(MyLgVacuumReservationSwitch(coordinator, descriptor, router, primary, read))
                continue
            entities.append(
                entity_class(coordinator, descriptor, router, primary, read)
            )
    return entities
