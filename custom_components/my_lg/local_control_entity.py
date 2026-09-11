"""Disabled-by-default Home Assistant owners for exact Local controls."""

from __future__ import annotations

import asyncio
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
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory

from .entity import MyLgEntity
from .local_command import LocalCommandFailed, LocalCommandPending
from .local_control_contract import (
    LocalControlEntityDescriptor,
    eligible_factory_descriptors,
)
from .local_control_router import LocalControlRouter
from .local_provider import LocalSemanticShadowProvider
from .local_read_provider import TlvReadShadowProvider

_LOGGER = logging.getLogger(__name__)

LocalControlDomain = Literal["switch", "select", "number", "button"]


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
                    outcome = await self._router.async_execute(
                        self.coordinator.device_id,
                        self._descriptor.capability_id,
                        local_request_value,
                    )
                else:
                    outcome = await self._router.async_set_value(
                        self.coordinator.device_id,
                        self._descriptor.capability_id,
                        local_request_value,
                    )
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
        await self._async_send(mapping.local_request_value)


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
            entities.append(
                entity_class(coordinator, descriptor, router, primary, read)
            )
    return entities
