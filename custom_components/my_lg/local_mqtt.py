"""Dedicated read-only MQTT transport for one Rethink Local pilot binding."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import logging
from datetime import datetime
from typing import Any

from .local_energy_provider import (
    CumulativeEnergyProviderContractError,
    CumulativeEnergyShadowProvider,
)
from .local_provider import (
    LocalProviderContractError,
    LocalSemanticShadowProvider,
    validate_binding_id,
)
from .local_read_provider import (
    TlvReadProviderContractError,
    TlvReadShadowProvider,
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
    """Receive one profile's exact QoS 1 topics for a read-only Local binding."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        provider: LocalSemanticShadowProvider,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        read_provider: TlvReadShadowProvider | None = None,
        energy_provider: CumulativeEnergyShadowProvider | None = None,
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
        if read_provider is not None and read_provider.binding_id != provider.binding_id:
            raise LocalMqttConfigurationError(
                "TLV read provider does not match the pilot binding"
            )
        self.read_provider = read_provider
        if energy_provider is not None and energy_provider.binding_id != provider.binding_id:
            raise LocalMqttConfigurationError(
                "Cumulative-energy provider does not match the pilot binding"
            )
        self.energy_provider = energy_provider
        self._mqtt_module = mqtt_module
        self._client: Any | None = None
        self._callback_connection_generation = 0
        self._active_connection_generation = 0
        self._connected = False
        self._subscription_mid: int | None = None
        self._subscription_retry_handle: asyncio.TimerHandle | None = None
        self._presence_expiry_handle: asyncio.TimerHandle | None = None
        self._presence_expiry_generation = 0
        self._subscription_retry_seconds = LOCAL_PILOT_RECONNECT_MIN_SECONDS
        self._subscriptions_ready = False
        self._stopping = False
        self._retained_bootstrap: dict[str, tuple[bytes, int, bool]] = {}
        self._semantic_bootstrap_pending = False
        self._rejected_messages = 0

    @property
    def subscription_topics(self) -> tuple[str, ...]:
        """Return primary bootstrap topics plus independent read-feed topics."""
        return (
            self.provider.topics
            + (() if self.read_provider is None else self.read_provider.topics)
            + (() if self.energy_provider is None else self.energy_provider.topics)
        )

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
            self._cancel_presence_expiry()
            self.provider.set_transport_ready(False)
            if self.read_provider is not None:
                self.read_provider.set_transport_ready(False)
            if self.energy_provider is not None:
                self.energy_provider.set_transport_ready(False)
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
        self._cancel_presence_expiry()
        self._subscriptions_ready = False
        self._retained_bootstrap.clear()
        self._semantic_bootstrap_pending = False
        self.provider.set_transport_ready(False)
        if self.read_provider is not None:
            self.read_provider.set_transport_ready(False)
        if self.energy_provider is not None:
            self.energy_provider.set_transport_ready(False)
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
        self._callback_connection_generation += 1
        generation = self._callback_connection_generation
        mqtt = self._mqtt()
        if _result_code(result_code) != mqtt.MQTT_ERR_SUCCESS:
            self._loop.call_soon_threadsafe(
                self._connection_lost, client, generation
            )
            return
        self._loop.call_soon_threadsafe(
            self._begin_connection, client, generation
        )

    def _request_subscription(self, client: Any) -> None:
        """Queue the exact subscription; Paho's subscribe call is non-blocking."""
        if self._stopping or not self._connected or client is not self._client:
            return
        mqtt = self._mqtt()
        try:
            result, mid = client.subscribe(
                [(topic, 1) for topic in self.subscription_topics]
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
            self._callback_connection_generation += 1
            self._loop.call_soon_threadsafe(
                self._connection_lost,
                client,
                self._callback_connection_generation,
            )

    def _on_disconnect(
        self,
        client: Any,
        _userdata: object,
        *_callback_values: object,
    ) -> None:
        if not self._stopping:
            self._callback_connection_generation += 1
            self._loop.call_soon_threadsafe(
                self._connection_lost,
                client,
                self._callback_connection_generation,
            )

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
        ready = len(grants) == len(self.subscription_topics) and all(
            _result_code(value) == 1 for value in grants
        )
        self._loop.call_soon_threadsafe(
            self._handle_suback,
            client,
            mid,
            ready,
            self._callback_connection_generation,
        )

    def _on_message(self, client: Any, _userdata: object, message: object) -> None:
        if self._stopping:
            return
        try:
            topic = message.topic  # type: ignore[attr-defined]
            payload = message.payload  # type: ignore[attr-defined]
            qos = message.qos  # type: ignore[attr-defined]
            retained = message.retain  # type: ignore[attr-defined]
        except AttributeError:
            self._loop.call_soon_threadsafe(
                self._note_transport_rejection,
                client,
                self._callback_connection_generation,
            )
            return
        if (
            not isinstance(topic, str)
            or not isinstance(payload, bytes)
            or type(qos) is not int
            or type(retained) is not bool
        ):
            self._loop.call_soon_threadsafe(
                self._note_transport_rejection,
                client,
                self._callback_connection_generation,
            )
            return
        self._loop.call_soon_threadsafe(
            self._dispatch_message,
            client,
            topic,
            payload,
            qos,
            retained,
            self._callback_connection_generation,
        )

    def _note_transport_rejection(self, client: Any, generation: int) -> None:
        if (
            self._stopping
            or not self._connected
            or client is not self._client
            or generation != self._active_connection_generation
            or generation != self._callback_connection_generation
        ):
            return
        self._rejected_messages += 1

    def _cancel_subscription_retry(self) -> None:
        handle = self._subscription_retry_handle
        self._subscription_retry_handle = None
        if handle is not None:
            handle.cancel()

    def _cancel_presence_expiry(self) -> None:
        self._presence_expiry_generation += 1
        handle = self._presence_expiry_handle
        self._presence_expiry_handle = None
        if handle is not None:
            handle.cancel()

    def _reschedule_presence_expiry(self) -> None:
        """Keep one binding-level timer for the live presence authority."""
        self._cancel_presence_expiry()
        if self.read_provider is None or self._stopping or not self._subscriptions_ready:
            return
        expiry = getattr(self.provider, "read_publication_authority_expiry", None)
        delay_for = getattr(
            self.provider, "read_publication_authority_expiry_delay", None
        )
        if expiry is None or not callable(delay_for):
            return
        delay = delay_for(expiry)
        if not isinstance(delay, (int, float)) or isinstance(delay, bool) or delay < 0:
            return
        generation = self._presence_expiry_generation
        self._presence_expiry_handle = self._loop.call_later(
            delay,
            self._presence_expired,
            generation,
            expiry,
        )

    def _presence_expired(
        self,
        generation: int,
        expiry: tuple[tuple[int, str], datetime],
    ) -> None:
        if (
            generation != self._presence_expiry_generation
            or self._stopping
            or not self._subscriptions_ready
        ):
            return
        self._presence_expiry_handle = None
        expire = getattr(self.provider, "expire_read_publication_authority", None)
        if callable(expire) and expire(expiry):
            return
        # Event-loop clock rounding can fire at the inclusive boundary.  Only
        # the still-current epoch is rescheduled; a heartbeat makes this
        # callback stale through its generation token.
        self._reschedule_presence_expiry()

    def _begin_connection(self, client: Any, generation: int) -> None:
        if (
            self._stopping
            or client is not self._client
            or generation != self._callback_connection_generation
        ):
            return
        self._active_connection_generation = generation
        self._cancel_subscription_retry()
        self._cancel_presence_expiry()
        self._subscription_retry_seconds = LOCAL_PILOT_RECONNECT_MIN_SECONDS
        self._connected = True
        self._subscription_mid = None
        self._subscriptions_ready = False
        self._retained_bootstrap.clear()
        self._semantic_bootstrap_pending = False
        self.provider.set_transport_ready(False)
        if self.read_provider is not None:
            self.read_provider.set_transport_ready(False)
        if self.energy_provider is not None:
            self.energy_provider.set_transport_ready(False)
        self._request_subscription(client)

    def _connection_lost(self, client: Any, generation: int) -> None:
        if (
            client is not self._client
            or generation != self._callback_connection_generation
        ):
            return
        self._connected = False
        self._cancel_subscription_retry()
        self._cancel_presence_expiry()
        self._subscription_mid = None
        self._subscriptions_ready = False
        self._retained_bootstrap.clear()
        self._semantic_bootstrap_pending = False
        self.provider.set_transport_ready(False)
        if self.read_provider is not None:
            self.read_provider.set_transport_ready(False)
        if self.energy_provider is not None:
            self.energy_provider.set_transport_ready(False)

    def _handle_suback(
        self, client: Any, mid: object, ready: bool, generation: int
    ) -> None:
        if (
            self._stopping
            or not self._connected
            or client is not self._client
            or generation != self._active_connection_generation
            or generation != self._callback_connection_generation
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
        self._cancel_presence_expiry()
        self.provider.set_transport_ready(False)
        if self.read_provider is not None:
            self.read_provider.set_transport_ready(ready)
        if self.energy_provider is not None:
            self.energy_provider.set_transport_ready(ready)
        if not ready:
            self._retained_bootstrap.clear()
            self._semantic_bootstrap_pending = False
            return
        self._drain_retained_bootstrap()

    def _dispatch_message(
        self,
        client: Any,
        topic: str,
        payload: bytes,
        qos: int,
        retained: bool,
        generation: int,
    ) -> None:
        if (
            self._stopping
            or not self._connected
            or client is not self._client
            or generation != self._active_connection_generation
            or generation != self._callback_connection_generation
        ):
            return
        if self.energy_provider is not None and topic in self.energy_provider.topics:
            self._apply_energy_message(topic, payload, qos, retained)
            return
        if self.read_provider is not None and topic in self.read_provider.topics:
            # The retained current route has its own lifecycle and never joins
            # the primary final-current bootstrap set.  Transient events are
            # accepted only after SUBACK by the read provider itself.
            self._apply_read_message(topic, payload, qos, retained)
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
        if (
            self._semantic_bootstrap_pending
            and topic in (self.provider.state_topic, self.provider.availability_topic)
        ):
            if qos != 1:
                self._apply_message(topic, payload, qos, retained)
                return
            self._retained_bootstrap[topic] = (payload, qos, retained)
            self._drain_semantic_bootstrap()
            return
        self._apply_message(topic, payload, qos, retained)

    def _drain_retained_bootstrap(self) -> None:
        if not self._subscriptions_ready:
            return
        if self.provider.control_presence_enabled:
            required = {
                self.provider.runtime_availability_topic,
                self.provider.presence_topic,
            }
            if not required.issubset(self._retained_bootstrap):
                return
            publications = {
                topic: self._retained_bootstrap[topic] for topic in required
            }
            try:
                self.provider.ingest_control_bootstrap_final_current(publications)
            except LocalProviderContractError:
                self._record_provider_rejection()
                return
            for topic in required:
                self._retained_bootstrap.pop(topic, None)
            self._semantic_bootstrap_pending = True
            self.provider.set_transport_ready(True)
            self._reschedule_presence_expiry()
            self._drain_semantic_bootstrap()
            return
        if set(self._retained_bootstrap) != set(self.provider.topics):
            return
        try:
            self.provider.ingest_bootstrap_final_current(self._retained_bootstrap)
        except LocalProviderContractError:
            self._record_provider_rejection()
            return
        self._retained_bootstrap.clear()
        self.provider.set_transport_ready(True)

    def _drain_semantic_bootstrap(self) -> None:
        if not self._semantic_bootstrap_pending:
            return
        required = {self.provider.state_topic, self.provider.availability_topic}
        if not required.issubset(self._retained_bootstrap):
            return
        publications = {
            topic: self._retained_bootstrap[topic] for topic in required
        }
        try:
            self.provider.ingest_semantic_bootstrap_final_current(publications)
        except LocalProviderContractError:
            self._record_provider_rejection()
            return
        for topic in required:
            self._retained_bootstrap.pop(topic, None)
        self._semantic_bootstrap_pending = False

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
        finally:
            self._reschedule_presence_expiry()

    def _apply_read_message(
        self, topic: str, payload: bytes, qos: int, retained: bool
    ) -> None:
        if self.read_provider is None:
            self._record_provider_rejection()
            return
        try:
            self.read_provider.ingest(topic, payload, qos=qos, retained=retained)
        except TlvReadProviderContractError:
            self._record_provider_rejection()

    def _apply_energy_message(
        self, topic: str, payload: bytes, qos: int, retained: bool
    ) -> None:
        if self.energy_provider is None:
            self._record_provider_rejection()
            return
        try:
            self.energy_provider.ingest(topic, payload, qos=qos, retained=retained)
        except CumulativeEnergyProviderContractError:
            self.energy_provider.reject_current()
            self._record_provider_rejection()
