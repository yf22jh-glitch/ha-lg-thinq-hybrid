"""Operation-control buttons for washtower (washer/dryer) and styler.

These issue a one-shot ``*OperationMode`` command (START/STOP). Payloads mirror
the thinqconnect SDK — washtower sub-units are location-keyed, the styler is flat:
    washer: {"washer": {"operation": {"washerOperationMode": "START"}}}
    dryer:  {"dryer":  {"operation": {"dryerOperationMode":  "START"}}}
    styler: {"operation": {"stylerOperationMode": "START"}}

STOP typically requires the appliance to be running; START a loaded/ready one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MyLgConfigEntry
from .const import DEVICE_TYPE_STYLER, DEVICE_TYPE_WASHTOWER
from .coordinator import PatDeviceCoordinator
from .entity import MyLgEntity


@dataclass(frozen=True, kw_only=True)
class MyLgButtonDescription(ButtonEntityDescription):
    """Button that posts a fixed control payload on press."""

    payload: dict[str, Any]


def _op(key: str, payload: dict[str, Any]) -> MyLgButtonDescription:
    return MyLgButtonDescription(key=key, translation_key=key, payload=payload)


def _washer(mode: str) -> dict[str, Any]:
    return {"washer": {"operation": {"washerOperationMode": mode}}}


def _dryer(mode: str) -> dict[str, Any]:
    return {"dryer": {"operation": {"dryerOperationMode": mode}}}


WASHTOWER_BUTTONS: tuple[MyLgButtonDescription, ...] = (
    _op("washer_start", _washer("START")),
    _op("washer_stop", _washer("STOP")),
    _op("dryer_start", _dryer("START")),
    _op("dryer_stop", _dryer("STOP")),
)

STYLER_BUTTONS: tuple[MyLgButtonDescription, ...] = (
    _op("styler_start", {"operation": {"stylerOperationMode": "START"}}),
    _op("styler_stop", {"operation": {"stylerOperationMode": "STOP"}}),
)

BUTTONS_BY_TYPE: dict[str, tuple[MyLgButtonDescription, ...]] = {
    DEVICE_TYPE_WASHTOWER: WASHTOWER_BUTTONS,
    DEVICE_TYPE_STYLER: STYLER_BUTTONS,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up operation-control buttons."""
    entities: list[ButtonEntity] = []
    for coordinator in entry.runtime_data.coordinators.values():
        for desc in BUTTONS_BY_TYPE.get(coordinator.device_type, ()):
            entities.append(MyLgButton(coordinator, desc))
    async_add_entities(entities)


class MyLgButton(MyLgEntity, ButtonEntity):
    """One-shot operation command."""

    entity_description: MyLgButtonDescription

    def __init__(
        self, coordinator: PatDeviceCoordinator, description: MyLgButtonDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        await self.coordinator.api.async_post_device_control(
            self.coordinator.device_id, self.entity_description.payload
        )
