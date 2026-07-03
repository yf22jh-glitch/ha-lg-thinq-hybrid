"""Data update coordinator for a single LG ThinQ device (PAT + MQTT push)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, PAT_FALLBACK_INTERVAL

_LOGGER = logging.getLogger(__name__)


def deep_merge(base: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``delta`` into ``base`` and return ``base``.

    MQTT DEVICE_STATUS reports carry only *changed* fields (a delta), so the
    coordinator must merge onto existing state rather than replace it, or
    unrelated fields would be lost.
    """
    for key, value in delta.items():
        if (
            isinstance(value, dict)
            and isinstance(base.get(key), dict)
        ):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


class PatDeviceCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Hold one device's status; primary source is MQTT push, REST is fallback."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api,
        device: dict[str, Any],
    ) -> None:
        self.api = api
        self.device_info_raw = device
        self.device_id: str = device["deviceId"]
        info = device.get("deviceInfo", {})
        self.device_type: str = info.get("deviceType", "")
        self.alias: str = info.get("alias") or self.device_id
        self.model: str = info.get("modelName", "")

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {self.alias}",
            update_interval=timedelta(seconds=PAT_FALLBACK_INTERVAL),
            config_entry=entry,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Low-frequency REST fallback (MQTT push handles realtime)."""
        try:
            status = await self.api.async_get_device_status(self.device_id)
        except Exception as err:  # noqa: BLE001 - surface as UpdateFailed
            raise UpdateFailed(f"{self.alias}: {err}") from err
        if not isinstance(status, dict):
            raise UpdateFailed(f"{self.alias}: unexpected status {status!r}")
        # Merge onto any state we already have (keeps push-only fields).
        merged = deep_merge(dict(self.data or {}), status)
        return merged

    @property
    def value(self):
        """Return the current merged status dict (never None once set up)."""
        return self.data or {}

    def handle_mqtt_status(self, report: dict[str, Any]) -> None:
        """Apply an MQTT DEVICE_STATUS report (delta) onto current state."""
        if not isinstance(report, dict) or not report:
            return
        merged = deep_merge(dict(self.data or {}), report)
        self.async_set_updated_data(merged)

    def get(self, *path: str, default=None):
        """Read a nested value by resource-group path, e.g. get('temperature', 'targetTemperature')."""
        node: Any = self.data or {}
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node
