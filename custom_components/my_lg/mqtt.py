"""MQTT push manager: subscribe device events and route DEVICE_STATUS reports."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    MQTT_SETUP_CALL_TIMEOUT,
    MQTT_SUBSCRIBE_CALL_TIMEOUT,
    MQTT_SUBSCRIBE_CONCURRENCY,
    PUSH_TYPE_DEVICE_PUSH,
    PUSH_TYPE_DEVICE_STATUS,
)
from .coordinator import PatDeviceCoordinator

_LOGGER = logging.getLogger(__name__)

# Client registration body used by thinqconnect (verified against the SDK).
CLIENT_BODY = {
    "type": "MQTT",
    "service-code": "SVC202",
    "device-type": "607",
    "allowExist": True,
}


def _extract_payload(args: tuple, kwargs: dict) -> bytes | None:
    """Find the JSON payload among the callback args (thinqconnect passes bytes)."""
    for value in (*args, *kwargs.values()):
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        if isinstance(value, str) and value.strip().startswith("{"):
            return value.encode()
    return None


class MyLgMqtt:
    """Own the ThinQMQTTClient and dispatch pushes to coordinators."""

    def __init__(
        self,
        hass: HomeAssistant,
        api,
        client_id: str,
        coordinators: dict[str, PatDeviceCoordinator],
        on_push: Callable[[str, str], None] | None = None,
    ) -> None:
        self.hass = hass
        self.api = api
        self.client_id = client_id
        self.coordinators = coordinators
        self._on_push = on_push
        self._client = None
        self._event_subscribed: list[str] = []
        self._subscription_failures = 0
        self._subscription_timeouts = 0

    async def _async_subscribe_device(
        self, device_id: str, semaphore: asyncio.Semaphore
    ) -> None:
        """Subscribe one device without allowing it to stall the whole entry."""
        async with semaphore:
            # DEVICE_STATUS (realtime state) requires an *event* subscription.
            try:
                await asyncio.wait_for(
                    self.api.async_post_event_subscribe(device_id),
                    timeout=MQTT_SUBSCRIBE_CALL_TIMEOUT,
                )
                self._event_subscribed.append(device_id)
            except asyncio.TimeoutError:
                self._subscription_timeouts += 1
                _LOGGER.warning("event subscribe %s timed out", device_id[:8])
            except Exception as err:  # noqa: BLE001
                self._subscription_failures += 1
                _LOGGER.debug("event subscribe %s: %s", device_id[:8], err)

            # DEVICE_PUSH notifications; account-shared, may already be subscribed.
            try:
                await asyncio.wait_for(
                    self.api.async_post_push_subscribe(device_id),
                    timeout=MQTT_SUBSCRIBE_CALL_TIMEOUT,
                )
            except asyncio.TimeoutError:
                self._subscription_timeouts += 1
                _LOGGER.warning("push subscribe %s timed out", device_id[:8])
            except Exception as err:  # noqa: BLE001
                self._subscription_failures += 1
                _LOGGER.debug("push subscribe %s: %s", device_id[:8], err)

    async def async_start(self) -> bool:
        """Register, subscribe whitelisted devices, and connect. Best-effort."""
        from thinqconnect import ThinQMQTTClient

        try:
            self._client = ThinQMQTTClient(self.api, self.client_id, self._on_message)
            await asyncio.wait_for(
                self._client.async_init(), timeout=MQTT_SETUP_CALL_TIMEOUT
            )
            await asyncio.wait_for(
                self._client.async_prepare_mqtt(), timeout=MQTT_SETUP_CALL_TIMEOUT
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("my_lg MQTT prepare failed (falling back to REST): %s", err)
            self._client = None
            return False

        semaphore = asyncio.Semaphore(MQTT_SUBSCRIBE_CONCURRENCY)
        await asyncio.gather(
            *(
                self._async_subscribe_device(device_id, semaphore)
                for device_id in self.coordinators
            )
        )

        try:
            await asyncio.wait_for(
                self._client.async_connect_mqtt(), timeout=MQTT_SETUP_CALL_TIMEOUT
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("my_lg MQTT connect failed (falling back to REST): %s", err)
            return False

        _LOGGER.info(
            "my_lg MQTT connected (event subscriptions=%d, failures=%d, timeouts=%d)",
            len(self._event_subscribed),
            self._subscription_failures,
            self._subscription_timeouts,
        )
        return True

    def _on_message(self, *args: Any, **kwargs: Any) -> None:
        """Runs in the MQTT client thread — hop to the event loop."""
        payload = _extract_payload(args, kwargs)
        if payload is None:
            return
        try:
            message = json.loads(payload.decode())
        except (ValueError, UnicodeDecodeError):
            return

        push_type = message.get("pushType")
        device_id = message.get("deviceId")

        if push_type == PUSH_TYPE_DEVICE_STATUS:
            coordinator = self.coordinators.get(device_id)
            if coordinator is None:
                return
            report = message.get("report") or {}
            self.hass.loop.call_soon_threadsafe(coordinator.handle_mqtt_status, report)
        elif push_type == PUSH_TYPE_DEVICE_PUSH and self._on_push and device_id:
            code = message.get("pushCode") or ""
            self.hass.loop.call_soon_threadsafe(self._on_push, device_id, code)

    async def async_stop(self) -> None:
        """Clean up: delete our event subscriptions and deregister the client."""
        async def _delete_event(device_id: str) -> None:
            try:
                await asyncio.wait_for(
                    self.api.async_delete_event_subscribe(device_id),
                    timeout=MQTT_SUBSCRIBE_CALL_TIMEOUT,
                )
            except Exception:  # noqa: BLE001
                pass

        semaphore = asyncio.Semaphore(MQTT_SUBSCRIBE_CONCURRENCY)

        async def _bounded_delete(device_id: str) -> None:
            async with semaphore:
                await _delete_event(device_id)

        await asyncio.gather(
            *(_bounded_delete(device_id) for device_id in self._event_subscribed)
        )
        self._event_subscribed.clear()
        try:
            await asyncio.wait_for(
                self.api.async_delete_client_register(payload=CLIENT_BODY),
                timeout=MQTT_SETUP_CALL_TIMEOUT,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("deregister client: %s", err)
        self._client = None
