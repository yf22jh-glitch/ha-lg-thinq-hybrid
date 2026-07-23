"""Data update coordinator for a single LG ThinQ device (PAT + MQTT push)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from thinqconnect.thinq_api import ThinQAPIException

from .const import DOMAIN, PAT_FALLBACK_INTERVAL

# LG control errors that have a clear user action; anything else surfaces the
# raw error name/code.
_CONTROL_HINTS: dict[str, str] = {
    "2301": "기기에서 '원격 시작'을 먼저 켜 주세요.",  # COMMAND_NOT_SUPPORTED_IN_REMOTE_OFF
    "2302": "지금 기기 상태에서는 이 명령을 쓸 수 없어요.",  # COMMAND_NOT_SUPPORTED_IN_STATE
    "2304": "전원이 꺼져 있어요. 먼저 전원을 켠 뒤 다시 시도해 주세요.",  # ...IN_POWER_OFF
}

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
        self.profile: dict | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {self.alias}",
            update_interval=timedelta(seconds=PAT_FALLBACK_INTERVAL),
            config_entry=entry,
        )

    async def _async_update_data(self):
        """Low-frequency REST fallback (MQTT push handles realtime)."""
        try:
            status = await self.api.async_get_device_status(self.device_id)
        except Exception as err:  # noqa: BLE001 - surface as UpdateFailed
            raise UpdateFailed(f"{self.alias}: {err}") from err
        # Oven/cooktop status is a top-level list of cavities/zones — replace.
        if isinstance(status, list):
            return status
        if not isinstance(status, dict):
            raise UpdateFailed(f"{self.alias}: unexpected status {status!r}")
        # Merge onto any state we already have (keeps push-only fields).
        base = dict(self.data) if isinstance(self.data, dict) else {}
        return deep_merge(base, status)

    @property
    def value(self):
        """Return the current merged status dict (never None once set up)."""
        return self.data or {}

    def handle_mqtt_status(self, report) -> None:
        """Apply an MQTT DEVICE_STATUS report (delta) onto current state."""
        if not report:
            return
        if isinstance(report, list):  # oven/cooktop cavity/zone list
            self.async_set_updated_data(report)
            return
        if not isinstance(report, dict):
            return
        base = dict(self.data) if isinstance(self.data, dict) else {}
        self.async_set_updated_data(deep_merge(base, report))

    def get(self, *path: str, default=None):
        """Read a nested value by resource-group path, e.g. get('temperature', 'targetTemperature')."""
        node: Any = self.data or {}
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def get_zone(self, location: str, *path: str, default=None):
        """Read from oven/cooktop status: a top-level list where each item has
        item['location']['locationName']. e.g. get_zone('UPPER','runState','currentState').
        """
        items = self.data
        if isinstance(items, list):
            for item in items:
                if (
                    isinstance(item, dict)
                    and item.get("location", {}).get("locationName") == location
                ):
                    node: Any = item
                    for key in path:
                        if not isinstance(node, dict) or key not in node:
                            return default
                        node = node[key]
                    return node
        return default

    def get_location(self, group: str, location: str, field: str, default=None):
        """Read a field from a location-keyed list group (fridge/oven/cooktop).

        e.g. status['temperature'] = [{'locationName':'FRIDGE','targetTemperature':3}, ...]
        """
        items = self.get(group)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("locationName") == location:
                    return item.get(field, default)
        return default

    async def async_load_profile(self) -> bool:
        """Fetch the device profile once (defines push codes & capabilities)."""
        try:
            self.profile = await self.api.async_get_device_profile(self.device_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("%s: profile load failed: %s", self.alias, err)
            self.profile = None
            return False
        return isinstance(self.profile, dict)

    def supports(self, group: str) -> bool:
        """True if the device profile advertises a property group.

        Used to create optional sensors (air quality, filter) even when the
        device is offline at startup and reports no live value yet.
        """
        prop = (self.profile or {}).get("property")
        if isinstance(prop, dict):
            return group in prop
        if isinstance(prop, list):
            return any(isinstance(p, dict) and group in p for p in prop)
        return False

    async def async_control(self, payload: dict[str, Any]) -> None:
        """Post a control payload, translating LG API errors to friendly ones."""
        try:
            await self.api.async_post_device_control(self.device_id, payload)
        except ThinQAPIException as err:
            hint = _CONTROL_HINTS.get(str(err.code))
            detail = hint or f"{err.error_name} ({err.code})"
            raise HomeAssistantError(f"{self.alias}: {detail}") from err

    def supports_field(self, group: str, field: str) -> bool:
        """True if the profile advertises a specific field within a property group."""
        prop = (self.profile or {}).get("property")
        if isinstance(prop, dict):
            grp = prop.get(group)
            return isinstance(grp, dict) and field in grp
        if isinstance(prop, list):
            return any(
                isinstance(p, dict)
                and isinstance(p.get(group), dict)
                and field in p[group]
                for p in prop
            )
        return False

    def push_codes(self) -> list[str]:
        """All DEVICE_PUSH notification codes this device can emit (recursive)."""
        codes: list[str] = []

        def _walk(node: Any) -> None:
            if isinstance(node, dict):
                notif = node.get("notification")
                if isinstance(notif, dict) and isinstance(notif.get("push"), list):
                    codes.extend(notif["push"])
                for key, value in node.items():
                    if key != "notification":
                        _walk(value)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)

        _walk(self.profile)
        return sorted(set(codes))
