"""Notification event entities (device completion / alerts via DEVICE_PUSH)."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MyLgConfigEntry
from .compat import AddConfigEntryEntitiesCallback
from .const import DOMAIN
from .coordinator import PatDeviceCoordinator
from .local_entity import iter_tlv_read_contracts, local_semantic_unique_id
from .local_read_provider import (
    TlvReadEvent,
    TlvReadFieldContract,
    TlvReadShadowProvider,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    from .feature_runtime import setup_feature_entities

    setup_feature_entities(entry, "event", lambda: _build_entities(entry), async_add_entities)


def _build_entities(entry: MyLgConfigEntry) -> list[EventEntity]:
    entities = [
        MyLgNotificationEvent(coordinator)
        for coordinator in entry.runtime_data.coordinators.values()
        if coordinator.push_codes()
    ]
    for coordinator in entry.runtime_data.coordinators.values():
        provider = getattr(entry.runtime_data, "local_read_providers", {}).get(
            coordinator.device_id
        )
        if provider is None:
            continue
        entities.extend(
            TlvReadEventEntity(provider, coordinator, semantic_id, contract)
            for semantic_id, contract in iter_tlv_read_contracts(provider, "event")
        )
    return entities


class MyLgNotificationEvent(CoordinatorEntity[PatDeviceCoordinator], EventEntity):
    """Fires when the device emits a DEVICE_PUSH notification (e.g. completion)."""

    _attr_has_entity_name = True
    _attr_translation_key = "notification"

    def __init__(self, coordinator: PatDeviceCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device_id}_notification"
        self._attr_event_types = coordinator.push_codes()
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.device_id)},
            name=coordinator.alias,
            manufacturer="LG",
            model=coordinator.model or coordinator.device_type,
        )

    @property
    def available(self) -> bool:
        return True  # push-driven; independent of coordinator polling

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_push_{self.coordinator.device_id}",
                self._handle_push,
            )
        )

    @callback
    def _handle_push(self, code: str) -> None:
        if code in self.event_types:
            self._trigger_event(code)
            self.async_write_ha_state()


class TlvReadEventEntity(EventEntity):
    """One closed transient event from the complete TLV read feed."""

    _attr_has_entity_name = True

    def __init__(
        self,
        provider: TlvReadShadowProvider,
        pat_coordinator: PatDeviceCoordinator,
        semantic_id: str,
        contract: TlvReadFieldContract,
    ) -> None:
        super().__init__()
        if (
            provider.profile.fields_by_semantic_id.get(semantic_id) is not contract
            or contract.domain != "event"
            or contract.value_types not in (("number",), ("string",))
            or contract.event_type is None
        ):
            raise ValueError("Complete read event requires a profile-owned event")
        self._provider = provider
        self._semantic_id = semantic_id
        self._contract = contract
        self._attr_name = f"Local · {contract.label_ko}"
        self._attr_unique_id = local_semantic_unique_id(
            pat_coordinator.device_id, semantic_id
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, pat_coordinator.device_id)},
            name=pat_coordinator.alias,
            manufacturer="LG",
            model=pat_coordinator.model or pat_coordinator.device_type,
        )
        self._attr_entity_category = (
            EntityCategory.DIAGNOSTIC
            if contract.entity_category == "diagnostic"
            else None
        )
        self._attr_entity_registry_enabled_default = contract.enabled_by_default
        # HA event types are artifact-owned closed literals. Numeric observed
        # values belong in event_data and never become dynamic event types.
        self._attr_event_types = [contract.event_type]
        self._remove_state_listener = None
        self._remove_event_listener = None

    @property
    def available(self) -> bool:
        return self._provider.event_available

    @property
    def semantic_id(self) -> str:
        return self._semantic_id

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._remove_state_listener = self._provider.async_add_listener(
            self._handle_provider_update
        )
        self._remove_event_listener = self._provider.async_add_event_listener(
            self._handle_event
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_state_listener is not None:
            self._remove_state_listener()
            self._remove_state_listener = None
        if self._remove_event_listener is not None:
            self._remove_event_listener()
            self._remove_event_listener = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_provider_update(self) -> None:
        self.async_write_ha_state()

    @callback
    def _handle_event(self, event: TlvReadEvent) -> None:
        if (
            event.semantic_id != self._semantic_id
            or event.descriptor_key != self._contract.descriptor_key
            or event.event_type != self._contract.event_type
            or event.value_type not in self._contract.value_types
            or event.unit != self._contract.unit
            or (
                event.value_type == "number"
                and (isinstance(event.value, bool) or not isinstance(event.value, (int, float)))
            )
            or (
                event.value_type == "string"
                and (
                    not isinstance(event.value, str)
                    or event.value != event.event_type
                )
            )
        ):
            return
        self._trigger_event(
            event.event_type,
            {
                "semantic_id": event.semantic_id,
                "descriptor_key": event.descriptor_key,
                "value": event.value,
                "value_type": event.value_type,
                "unit": event.unit,
                "observed_at": event.observed_at.isoformat(),
                "confidence": event.confidence,
                "sequence": event.sequence,
            },
        )
        self.async_write_ha_state()
