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
from .coordinator_wideq import WideqCoordinator


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


class MyLgWideqEntity(CoordinatorEntity[WideqCoordinator]):
    """Entity backed by the wideq coordinator for a wideq-only field.

    State and control go through wideq (keyed by device alias), but the entity
    attaches to the *same* device as the PAT entities via the shared PAT device
    id — the user sees one device, with both PAT and wideq controls on it.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        wideq_coordinator: WideqCoordinator,
        pat_coordinator: PatDeviceCoordinator,
        key: str,
    ) -> None:
        super().__init__(wideq_coordinator)
        self._alias = pat_coordinator.alias
        self._attr_unique_id = f"{pat_coordinator.device_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, pat_coordinator.device_id)},
            name=pat_coordinator.alias,
            manufacturer="LG",
            model=pat_coordinator.model or pat_coordinator.device_type,
        )

    @property
    def _snapshot(self) -> dict[str, Any]:
        return self.coordinator.snapshot_for(self._alias)

    @property
    def available(self) -> bool:
        # Unavailable until wideq has actually polled this device (the first
        # poll is deliberately delayed after startup).
        return self.coordinator.last_update_success and bool(self._snapshot)

    async def _wideq_set(
        self, ctrl_key: str, data_key: str, value: Any, use_dataset: bool
    ) -> None:
        """Send one control and optimistically reflect the new value."""
        if use_dataset:
            await self.coordinator.async_control(
                self._alias, ctrl_key, data_set_list={data_key: value}
            )
        else:
            await self.coordinator.async_control(
                self._alias, ctrl_key, data_key=data_key, value=value
            )
        self.coordinator.apply_optimistic(self._alias, data_key, value)
