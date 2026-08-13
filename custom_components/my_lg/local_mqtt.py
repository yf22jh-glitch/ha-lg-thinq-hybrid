"""Dedicated read-only MQTT transport for one Rethink Local pilot binding."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import logging
from typing import Any

from .local_provider import (
    LocalProviderContractError,
    LocalWaterTankShadowProvider,
    validate_binding_id,
)

_LOGGER = logging.getLogger(__name__)

LOCAL_PILOT_MQTT_PORT = 18883
LOCAL_PILOT_MQTT_KEEPALIVE = 60
LOCAL_PILOT_RECONNECT_MIN_SECONDS = 300
LOCAL_PILOT_RECONNECT_MAX_SECONDS = 1800
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class LocalMqttConfigurationError(ValueError):
    """The dedicated Local MQTT subscriber is not exactly scoped."""


def stable_local_subscriber_client_id(binding_id: str) -> str:
    """Return a stable 23-byte id distinct from the publisher/shadow runtime."""
    binding_id = validate_binding_id(binding_id)
    digest = hashlib.sha256(
        b"my-lg-local-shadow-v1\0" + binding_id.encode("ascii")
    ).hexdigest()
    return f"mlg-{digest[:19]}"


def _result_code(value: object) -> int | None:
    if value is None:
        return None
    candidate = getattr(value, "value", value)
    try:
        return int(candidate)
    except (TypeError, ValueError):
        return None


class LocalPilotMqttSubscriber:
    """Receive three exact QoS 1 topics from the loopback pilot broker."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        provider: LocalWaterTankShadowProvider,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        mqtt_module: Any | None = None,
    ) -> None:
        if host not in _LOOPBACK_HOSTS:
            raise LocalMqttConfigurationError("Local pilot MQTT host must be loopback")
        if type(port) is not int or port != LOCAL_PILOT_MQTT_PORT:
            raise LocalMqttConfigurationError(
                "Local pilot MQTT port must be the isolated pilot port"
            )
        expected_username = f"shadow-{provider.binding_id}"
        if username != expected_username:
            raise LocalMqttConfigurationError(
                "Local pilot MQTT username does not match the read-only binding ACL"
            )
        if (
            not isinstance(password, str)
            or not password
            or len(password.encode("utf-8")) > 1024
        ):
            raise LocalMqttConfigurationError("Local pilot MQTT password is invalid")

        self._loop = loop
        self.provider = provider
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._mqtt_module = mqtt_module
        self._client: Any | None = None
        self._connected = False
        self._subscription_mid: int | None = None
        self._subscription_retry_handle: asyncio.TimerHandle | None = None
        self._subscription_retry_seconds = LOCAL_PILOT_RECONNECT_MIN_SECONDS
        self._subscriptions_ready = False
        self._stopping = False
        self._retained_bootstrap: dict[str, tuple[bytes, int, bool]] = {}
        self._rejected_messages = 0

    @property
    def rejected_messages(self) -> int:
        return self._rejected_messages

    @property
    def client_id(self) -> str:
        return stable_local_subscriber_client_id(self.provider.binding_id)

    def _mqtt(self) -> Any:
        if self._mqtt_module is None:
            raise RuntimeError("Local pilot MQTT module is not loaded")
        return self._mqtt_module

    def _new_client(self) -> Any:
        mqtt = self._mqtt()
        kwargs = {
            "client_id": self.client_id,
            "clean_session": True,
            "protocol": mqtt.MQTTv311,
            "transport": "tcp",
        }
        callback_versions = getattr(mqtt, "CallbackAPIVersion", None)
        if callback_versions is None:
            return mqtt.Client(**kwargs)
        # Paho 2.x receives its native callback API; the handlers below accept
        # its trailing properties/reason fields while also matching Paho 1.6.1.
        return mqtt.Client(callback_versions.VERSION2, **kwargs)

    async def async_start(self) -> None:
        if self._client is not None:
            return
        self._stopping = False
        self._connected = False
        if self._mqtt_module is None:
            self._mqtt_module = await asyncio.to_thread(
                importlib.import_module, "paho.mqtt.client"
            )
        mqtt = self._mqtt()
        client = self._new_client()
        client.username_pw_set(self._username, self._password)
        client.reconnect_delay_set(
            min_delay=LOCAL_PILOT_RECONNECT_MIN_SECONDS,
            max_delay=LOCAL_PILOT_RECONNECT_MAX_SECONDS,
        )
        client.on_connect = self._on_connect
        client.on_connect_fail = self._on_connect_fail
        client.on_disconnect = self._on_disconnect
        client.on_subscribe = self._on_subscribe
        client.on_message = self._on_message
        self._client = client

        try:
            result = client.connect_async(
                self._host,
                self._port,
                keepalive=LOCAL_PILOT_MQTT_KEEPALIVE,
            )
            if _result_code(result) not in (None, mqtt.MQTT_ERR_SUCCESS):
                raise RuntimeError("Local pilot MQTT connect setup failed")
            result = client.loop_start()
            if _result_code(result) not in (None, mqtt.MQTT_ERR_SUCCESS):
                raise RuntimeError("Local pilot MQTT network loop failed")
        except Exception:
            self._stopping = True
            self._connected = False
            self._client = None
            self._cancel_subscription_retry()
            self.provider.set_transport_ready(False)
            try:
                await asyncio.to_thread(client.loop_stop)
            except Exception:  # noqa: BLE001 - best-effort partial-start cleanup
                _LOGGER.warning(
                    "Rethink Local shadow MQTT partial-start cleanup failed"
                )
            raise

    async def async_stop(self) -> None:
        self._stopping = True
        self._connected = False
        client = self._client
        self._client = None
        self._subscription_mid = None
        self._cancel_subscription_retry()
        self._subscriptions_ready = False
        self._retained_bootstrap.clear()
        self.provider.set_transport_ready(False)
        if client is None:
            return
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001 - optional sidecar teardown is best-effort
            _LOGGER.warning("Rethink Local shadow MQTT disconnect failed")
        try:
            await asyncio.to_thread(client.loop_stop)
        except Exception:  # noqa: BLE001 - never block config-entry unload
            _LOGGER.warning("Rethink Local shadow MQTT loop shutdown failed")

    def _on_connect(
        self,
        client: Any,
        _userdata: object,
        _flags: object,
        result_code: object,
        _properties: object | None = None,
    ) -> None:
        if self._stopping:
            return
        mqtt = self._mqtt()
        if _result_code(result_code) != mqtt.MQTT_ERR_SUCCESS:
            self._loop.call_soon_threadsafe(self._connection_lost, client)
            return
        self._loop.call_soon_threadsafe(self._begin_connection, client)

    def _request_subscription(self, client: Any) -> None:
        """Queue the exact subscription; Paho's subscribe call is non-blocking."""
        if self._stopping or not self._connected or client is not self._client:
            return
        mqtt = self._mqtt()
        try:
            result, mid = client.subscribe(
                [(topic, 1) for topic in self.provider.topics]
            )
        except Exception:  # noqa: BLE001 - isolate third-party callback failures
            self._subscription_mid = None
            self._loop.call_soon_threadsafe(self._subscription_failed, client)
            return
        if _result_code(result) != mqtt.MQTT_ERR_SUCCESS or type(mid) is not int:
            self._subscription_mid = None
            self._loop.call_soon_threadsafe(self._subscription_failed, client)
            return
        self._subscription_mid = mid
        self._schedule_subscription_retry(client)

    def _on_connect_fail(self, client: Any, _userdata: object) -> None:
        if not self._stopping:
            self._loop.call_soon_threadsafe(self._connection_lost, client)

    def _on_disconnect(
        self,
        client: Any,
        _userdata: object,
        *_callback_values: object,
    ) -> None:
        if not self._stopping:
            self._loop.call_soon_threadsafe(self._connection_lost, client)

    def _on_subscribe(
        self,
        client: Any,
        _userdata: object,
        mid: object,
        granted_qos: object,
        _properties: object | None = None,
    ) -> None:
        if self._stopping:
            return
        try:
            grants = list(granted_qos)  # type: ignore[arg-type]
        except TypeError:
            grants = []
        ready = len(grants) == len(self.provider.topics) and all(
            _result_code(value) == 1 for value in grants
        )
        self._loop.call_soon_threadsafe(self._handle_suback, client, mid, ready)

    def _on_message(self, client: Any, _userdata: object, message: object) -> None:
        if self._stopping:
            return
        try:
            topic = message.topic  # type: ignore[attr-defined]
            payload = message.payload  # type: ignore[attr-defined]
            qos = message.qos  # type: ignore[attr-defined]
            retained = message.retain  # type: ignore[attr-defined]
        except AttributeError:
            self._loop.call_soon_threadsafe(self._note_transport_rejection, client)
            return
        if (
            not isinstance(topic, str)
            or not isinstance(payload, bytes)
            or type(qos) is not int
            or type(retained) is not bool
        ):
            self._loop.call_soon_threadsafe(self._note_transport_rejection, client)
            return
        self._loop.call_soon_threadsafe(
            self._dispatch_message, client, topic, payload, qos, retained
        )

    def _note_transport_rejection(self, client: Any) -> None:
        if self._stopping or not self._connected or client is not self._client:
            return
        self._rejected_messages += 1

    def _cancel_subscription_retry(self) -> None:
        handle = self._subscription_retry_handle
        self._subscription_retry_handle = None
        if handle is not None:
            handle.cancel()

    def _begin_connection(self, client: Any) -> None:
        if self._stopping or client is not self._client:
            return
        self._cancel_subscription_retry()
        self._subscription_retry_seconds = LOCAL_PILOT_RECONNECT_MIN_SECONDS
        self._connected = True
        self._subscription_mid = None
        self._subscriptions_ready = False
        self._retained_bootstrap.clear()
        self.provider.set_transport_ready(False)
        self._request_subscription(client)

    def _connection_lost(self, client: Any) -> None:
        if client is not self._client:
            return
        self._connected = False
        self._cancel_subscription_retry()
        self._subscription_mid = None
        self._subscriptions_ready = False
        self._retained_bootstrap.clear()
        self.provider.set_transport_ready(False)

    def _handle_suback(self, client: Any, mid: object, ready: bool) -> None:
        if (
            self._stopping
            or not self._connected
            or client is not self._client
            or type(mid) is not int
            or mid != self._subscription_mid
        ):
            return
        if ready:
            self._subscription_mid = None
            self._cancel_subscription_retry()
            self._subscription_retry_seconds = LOCAL_PILOT_RECONNECT_MIN_SECONDS
            self._set_subscriptions_ready(True)
            return
        self._subscription_mid = None
        self._set_subscriptions_ready(False)

    def _subscription_failed(self, client: Any) -> None:
        if self._stopping or not self._connected or client is not self._client:
            return
        self._set_subscriptions_ready(False)
        self._schedule_subscription_retry(client)

    def _schedule_subscription_retry(self, client: Any) -> None:
        if self._stopping or not self._connected or client is not self._client:
            return
        if self._subscription_retry_handle is not None:
            return
        delay = self._subscription_retry_seconds
        self._subscription_retry_seconds = min(
            delay * 2, LOCAL_PILOT_RECONNECT_MAX_SECONDS
        )
        self._subscription_retry_handle = self._loop.call_later(
            delay, self._retry_subscription, client
        )

    def _retry_subscription(self, client: Any) -> None:
        self._subscription_retry_handle = None
        if self._stopping or not self._connected or client is not self._client:
            return
        self._subscription_mid = None
        self._request_subscription(client)

    def _set_subscriptions_ready(self, ready: bool) -> None:
        self._subscriptions_ready = ready
        self.provider.set_transport_ready(False)
        if not ready:
            self._retained_bootstrap.clear()
            return
        self._drain_retained_bootstrap()

    def _dispatch_message(
        self, client: Any, topic: str, payload: bytes, qos: int, retained: bool
    ) -> None:
        if self._stopping or not self._connected or client is not self._client:
            return
        if topic not in self.provider.topics:
            self._apply_message(topic, payload, qos, retained)
            return
        if not self.provider.transport_ready:
            if qos != 1:
                self._apply_message(topic, payload, qos, retained)
                return
            self._retained_bootstrap[topic] = (payload, qos, retained)
            self._drain_retained_bootstrap()
            return
        self._apply_message(topic, payload, qos, retained)

    def _drain_retained_bootstrap(self) -> None:
        if not self._subscriptions_ready or set(self._retained_bootstrap) != set(
            self.provider.topics
        ):
            return
        try:
            self.provider.ingest_bootstrap_final_current(self._retained_bootstrap)
        except LocalProviderContractError:
            self._record_provider_rejection()
            return
        self._retained_bootstrap.clear()
        self.provider.set_transport_ready(True)

    def _record_provider_rejection(self) -> None:
        self._rejected_messages += 1
        count = self._rejected_messages
        if count == 1 or count % 100 == 0:
            _LOGGER.warning(
                "Rethink Local shadow rejected an MQTT publication (count=%d)",
                count,
            )

    def _apply_message(
        self, topic: str, payload: bytes, qos: int, retained: bool
    ) -> None:
        try:
            self.provider.ingest(topic, payload, qos=qos, retained=retained)
        except LocalProviderContractError:
            self._record_provider_rejection()
