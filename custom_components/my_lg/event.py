"""Notification event entities (device completion / alerts via DEVICE_PUSH)."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MyLgConfigEntry
from .compat import AddConfigEntryEntitiesCallback
from .const import DOMAIN
from .coordinator import PatDeviceCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    entities = [
        MyLgNotificationEvent(coordinator)
        for coordinator in entry.runtime_data.coordinators.values()
        if coordinator.push_codes()
    ]
    async_add_entities(entities)


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
