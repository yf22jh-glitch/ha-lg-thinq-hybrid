"""Base entity for my_lg."""

from __future__ import annotations

from typing import Any

try:
    # HA 2023.8+ location; used by official integrations.
    from homeassistant.helpers.device_registry import DeviceInfo
except ImportError:  # pragma: no cover - fallback for older/newer reorgs
    from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PatDeviceCoordinator


class MyLgEntity(CoordinatorEntity[PatDeviceCoordinator]):
    """Common base tying an entity to one device coordinator."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PatDeviceCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{coordinator.device_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.device_id)},
            name=coordinator.alias,
            manufacturer="LG",
            model=coordinator.model or coordinator.device_type,
        )

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and bool(self.coordinator.data)

    def _get(self, *path: str, default: Any = None) -> Any:
        return self.coordinator.get(*path, default=default)
