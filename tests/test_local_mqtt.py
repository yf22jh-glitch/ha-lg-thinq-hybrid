"""Pure lifecycle tests for the dedicated read-only pilot MQTT subscriber."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.test_local_provider import (
    BINDING_ID,
    availability_payload,
    runtime_payload,
    state_payload,
)

COMPONENT_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "my_lg"
PACKAGE_NAME = "my_lg_local_mqtt_test"
PACKAGE = ModuleType(PACKAGE_NAME)
PACKAGE.__path__ = [str(COMPONENT_PATH)]
sys.modules[PACKAGE_NAME] = PACKAGE


def _load(name: str):
    path = COMPONENT_PATH / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{PACKAGE_NAME}.{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


local = _load("local_provider")
local_mqtt = _load("local_mqtt")
NOW = datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self, *args, **kwargs) -> None:
        self.constructor_args = args
        self.constructor_kwargs = kwargs
        self.on_connect = None
        self.on_connect_fail = None
        self.on_disconnect = None
        self.on_message = None
        self.on_subscribe = None
        self.username = None
        self.password = None
        self.reconnect_delays = None
        self.connect_args = None
        self.loop_started = 0
        self.loop_stopped = 0
        self.disconnect_calls = 0
        self.subscriptions = []
        self.subscribe_result = (0, 41)
        self.subscribe_error = None

    def username_pw_set(self, username, password=None):
        self.username = username
        self.password = password

    def reconnect_delay_set(self, min_delay=1, max_delay=120):
        self.reconnect_delays = (min_delay, max_delay)

    def connect_async(self, host, port=1883, keepalive=60):
        self.connect_args = (host, port, keepalive)
        return 0

    def loop_start(self):
        self.loop_started += 1
        return 0

    def subscribe(self, topics):
        if self.subscribe_error is not None:
            raise self.subscribe_error
        self.subscriptions.append(topics)
        return self.subscribe_result

    def disconnect(self):
        self.disconnect_calls += 1
        return 0

    def loop_stop(self):
        self.loop_stopped += 1
        return 0


class FakeMqttV1:
    MQTTv311 = 4
    MQTT_ERR_SUCCESS = 0

    def __init__(self) -> None:
        self.clients = []

    def Client(self, *args, **kwargs):
        client = FakeClient(*args, **kwargs)
        self.clients.append(client)
        return client


class FakeMqttV2(FakeMqttV1):
    class CallbackAPIVersion:
        VERSION2 = object()


class FakeReasonCode:
    def __init__(self, value) -> None:
        self.value = value


class FourTopicProvider:
    """Minimal synthetic provider proving transport treats presence as bootstrap input."""

    def __init__(self) -> None:
        self.binding_id = BINDING_ID
        prefix = f"{local.LOCAL_PILOT_PREFIX}"
        self.topics = (
            f"{prefix}/state/{BINDING_ID}",
            f"{prefix}/availability/{BINDING_ID}",
            f"{prefix}/runtime/{BINDING_ID}/availability",
            f"{prefix}/presence/{BINDING_ID}",
        )
        self.state_topic = self.topics[0]
        self.availability_topic = self.topics[1]
        self.runtime_availability_topic = self.topics[2]
        self.presence_topic = self.topics[3]
        self.control_presence_enabled = True
        self.transport_ready = False
        self.control_bootstrap_calls = []
        self.semantic_bootstrap_calls = []
        self.ingest_calls = []

    def set_transport_ready(self, ready):
        self.transport_ready = ready

    def ingest_control_bootstrap_final_current(self, publications):
        self.control_bootstrap_calls.append(dict(publications))
        return True

    def ingest_semantic_bootstrap_final_current(self, publications):
        self.semantic_bootstrap_calls.append(dict(publications))
        return True

    def ingest(self, topic, payload, *, qos, retained):
        self.ingest_calls.append((topic, payload, qos, retained))
        return True


class RecoveringFourTopicProvider(FourTopicProvider):
    """Reject one cross-service batch and accept its later coherent repair."""

    def ingest_control_bootstrap_final_current(self, publications):
        self.control_bootstrap_calls.append(dict(publications))
        runtime = publications[self.runtime_availability_topic][0]
        presence = publications[self.presence_topic][0]
        if runtime == b"retained-runtime-b" and presence == b"live-presence-a":
            raise local.LocalProviderContractError("synthetic service mismatch")
        return True


class FakeReadProvider:
    """Auxiliary feed proving read topics never join retained primary bootstrap."""

    def __init__(self) -> None:
        self.binding_id = BINDING_ID
        self.current_topic = f"{local.LOCAL_PILOT_PREFIX}/read/current/{BINDING_ID}"
        self.event_topic = f"{local.LOCAL_PILOT_PREFIX}/read/event/{BINDING_ID}"
        self.topics = (self.current_topic, self.event_topic)
        self.transport_ready = False
        self.ingest_calls = []

    def set_transport_ready(self, ready):
        self.transport_ready = ready

    def ingest(self, topic, payload, *, qos, retained):
        self.ingest_calls.append((topic, payload, qos, retained))
        return True


class ExpiringFourTopicProvider(FourTopicProvider):
    """Minimal authority timer seam for stale-callback/cancellation tests."""

    def __init__(self) -> None:
        super().__init__()
        self.read_publication_authority_expiry = (
            (1, "1" * 32),
            NOW,
        )
        self.expired = []

    def read_publication_authority_expiry_delay(self, expected):
        return 60.0 if expected == self.read_publication_authority_expiry else None

    def expire_read_publication_authority(self, expected):
        self.expired.append(expected)
        return expected == self.read_publication_authority_expiry


class LocalMqttSubscriberTests(unittest.IsolatedAsyncioTestCase):
    def provider(self):
        return local.LocalWaterTankShadowProvider(BINDING_ID, now=lambda: NOW)

    def subscriber(self, mqtt_module, provider=None):
        return local_mqtt.LocalPilotMqttSubscriber(
            asyncio.get_running_loop(),
            provider or self.provider(),
            host="127.0.0.1",
            port=18883,
            username=f"shadow-{BINDING_ID}",
            password="private-test-password",
            mqtt_module=mqtt_module,
        )

    async def test_supports_paho_v1_and_v2_with_one_stable_distinct_client_id(
        self,
    ) -> None:
        identifiers = []
        for mqtt_module in (FakeMqttV1(), FakeMqttV2()):
            subscriber = self.subscriber(mqtt_module)
            await subscriber.async_start()
            client = mqtt_module.clients[0]
            identifiers.append(client.constructor_kwargs["client_id"])
            if hasattr(mqtt_module, "CallbackAPIVersion"):
                self.assertIs(
                    client.constructor_args[0],
                    mqtt_module.CallbackAPIVersion.VERSION2,
                )
            else:
                self.assertEqual(client.constructor_args, ())
            self.assertEqual(client.connect_args, ("127.0.0.1", 18883, 60))
            self.assertEqual(client.username, f"shadow-{BINDING_ID}")
            self.assertEqual(client.password, "private-test-password")
            self.assertEqual(client.reconnect_delays, (300, 1800))
            await subscriber.async_stop()
            self.assertEqual(client.disconnect_calls, 1)
            self.assertEqual(client.loop_stopped, 1)

        self.assertEqual(identifiers[0], identifiers[1])
        self.assertEqual(len(identifiers[0]), 23)
        self.assertTrue(identifiers[0].startswith("mlg-"))
        self.assertNotEqual(identifiers[0][:4], "lrp-")

    async def test_lazy_paho_import_is_offloaded_from_the_ha_event_loop(self) -> None:
        mqtt_module = FakeMqttV1()
        subscriber = local_mqtt.LocalPilotMqttSubscriber(
            asyncio.get_running_loop(),
            self.provider(),
            host="127.0.0.1",
            port=18883,
            username=f"shadow-{BINDING_ID}",
            password="private-test-password",
        )
        offload = AsyncMock(return_value=mqtt_module)
        with patch.object(local_mqtt.asyncio, "to_thread", offload):
            await subscriber.async_start()

        offload.assert_awaited_once_with(
            local_mqtt.importlib.import_module, "paho.mqtt.client"
        )
        await subscriber.async_stop()

    async def test_constructs_the_installed_paho_version(self) -> None:
        try:
            import paho.mqtt.client as installed_mqtt
        except ModuleNotFoundError:
            self.skipTest(
                "Paho is installed by Home Assistant, not the host test Python"
            )
        subscriber = self.subscriber(installed_mqtt)
        client = subscriber._new_client()
        raw_client_id = getattr(client, "_client_id", b"")
        self.assertEqual(raw_client_id.decode(), subscriber.client_id)

    async def test_presence_expiry_timer_reschedules_and_unload_cancels_stale_callbacks(
        self,
    ) -> None:
        provider = ExpiringFourTopicProvider()
        subscriber = local_mqtt.LocalPilotMqttSubscriber(
            asyncio.get_running_loop(),
            provider,
            host="127.0.0.1",
            port=18883,
            username=f"shadow-{BINDING_ID}",
            password="private-test-password",
            read_provider=FakeReadProvider(),
            mqtt_module=FakeMqttV1(),
        )
        subscriber._subscriptions_ready = True
        subscriber._reschedule_presence_expiry()
        first_handle = subscriber._presence_expiry_handle
        first_generation = subscriber._presence_expiry_generation
        first_expiry = provider.read_publication_authority_expiry
        self.assertIsNotNone(first_handle)

        provider.read_publication_authority_expiry = (
            (1, "1" * 32),
            NOW.replace(minute=1),
        )
        subscriber._reschedule_presence_expiry()
        second_handle = subscriber._presence_expiry_handle
        second_generation = subscriber._presence_expiry_generation
        second_expiry = provider.read_publication_authority_expiry
        self.assertTrue(first_handle.cancelled())
        self.assertIsNot(first_handle, second_handle)

        subscriber._presence_expired(first_generation, first_expiry)
        self.assertIs(subscriber._presence_expiry_handle, second_handle)
        self.assertEqual(provider.expired, [])

        await subscriber.async_stop()
        self.assertTrue(second_handle.cancelled())
        subscriber._presence_expired(second_generation, second_expiry)
        self.assertEqual(provider.expired, [])

    async def test_subscribes_only_exact_qos_one_topics_and_waits_for_suback(
        self,
    ) -> None:
        mqtt_module = FakeMqttV1()
        provider = self.provider()
        subscriber = self.subscriber(mqtt_module, provider)
        await subscriber.async_start()
        client = mqtt_module.clients[0]

        client.on_connect(client, None, {}, 0)
        await asyncio.sleep(0)
        self.assertEqual(
            client.subscriptions,
            [[(topic, 1) for topic in provider.topics]],
        )
        self.assertFalse(provider.transport_ready)
        client.on_subscribe(client, None, 41, [1, 1, 1])
        await asyncio.sleep(0)
        self.assertFalse(
            provider.transport_ready,
            "SUBACK alone must not trust an old provider generation",
        )
        await subscriber.async_stop()

    async def test_auxiliary_read_topics_share_transport_but_not_primary_bootstrap(
        self,
    ) -> None:
        mqtt_module = FakeMqttV1()
        provider = self.provider()
        read_provider = FakeReadProvider()
        subscriber = local_mqtt.LocalPilotMqttSubscriber(
            asyncio.get_running_loop(),
            provider,
            host="127.0.0.1",
            port=18883,
            username=f"shadow-{BINDING_ID}",
            password="private-test-password",
            read_provider=read_provider,
            mqtt_module=mqtt_module,
        )
        await subscriber.async_start()
        client = mqtt_module.clients[0]

        client.on_connect(client, None, {}, 0)
        await asyncio.sleep(0)
        self.assertEqual(
            client.subscriptions,
            [[(topic, 1) for topic in provider.topics + read_provider.topics]],
        )
        client.on_subscribe(client, None, 41, [1, 1, 1, 1, 1])
        await asyncio.sleep(0)
        self.assertTrue(read_provider.transport_ready)

        client.on_message(
            client,
            None,
            SimpleNamespace(
                topic=read_provider.current_topic,
                payload=b"read-current",
                qos=1,
                retain=True,
            ),
        )
        await asyncio.sleep(0)
        self.assertEqual(
            read_provider.ingest_calls,
            [(read_provider.current_topic, b"read-current", 1, True)],
        )
        self.assertNotIn(read_provider.current_topic, subscriber._retained_bootstrap)
        await subscriber.async_stop()
        self.assertFalse(read_provider.transport_ready)

    async def test_retained_bootstrap_is_applied_in_state_availability_runtime_order(
        self,
    ) -> None:
        mqtt_module = FakeMqttV1()
        provider = self.provider()
        subscriber = self.subscriber(mqtt_module, provider)
        await subscriber.async_start()
        client = mqtt_module.clients[0]
        client.on_connect(client, None, {}, 0)
        client.on_subscribe(client, None, 41, [1, 1, 1])

        for topic, payload in (
            (provider.availability_topic, availability_payload("online")),
            (provider.runtime_availability_topic, runtime_payload("online")),
            (provider.state_topic, state_payload(value=True)),
        ):
            client.on_message(
                client,
                None,
                SimpleNamespace(topic=topic, payload=payload, qos=1, retain=True),
            )
        await asyncio.sleep(0)
        self.assertTrue(provider.shadow_value)
        self.assertTrue(provider.shadow_healthy)
        self.assertEqual(provider.rejected_messages, 0)
        await subscriber.async_stop()

    async def test_presence_and_runtime_open_no_state_bootstrap_then_semantics_pair(
        self,
    ) -> None:
        mqtt_module = FakeMqttV1()
        provider = FourTopicProvider()
        subscriber = self.subscriber(mqtt_module, provider)
        await subscriber.async_start()
        client = mqtt_module.clients[0]
        client.on_connect(client, None, {}, 0)
        client.on_subscribe(client, None, 41, [1, 1, 1, 1])

        for topic in (provider.presence_topic,):
            client.on_message(
                client,
                None,
                SimpleNamespace(
                    topic=topic, payload=b"synthetic", qos=1, retain=True
                ),
            )
        await asyncio.sleep(0)
        self.assertFalse(provider.transport_ready)
        self.assertEqual(provider.control_bootstrap_calls, [])

        client.on_message(
            client,
            None,
            SimpleNamespace(
                topic=provider.runtime_availability_topic,
                payload=b"synthetic-runtime",
                qos=1,
                retain=True,
            ),
        )
        await asyncio.sleep(0)
        self.assertTrue(provider.transport_ready)
        self.assertEqual(
            set(provider.control_bootstrap_calls[0]),
            {provider.presence_topic, provider.runtime_availability_topic},
        )
        self.assertEqual(provider.semantic_bootstrap_calls, [])

        client.on_message(
            client,
            None,
            SimpleNamespace(
                topic=provider.availability_topic,
                payload=b"synthetic-availability",
                qos=1,
                retain=True,
            ),
        )
        await asyncio.sleep(0)
        self.assertEqual(provider.semantic_bootstrap_calls, [])
        client.on_message(
            client,
            None,
            SimpleNamespace(
                topic=provider.state_topic,
                payload=b"synthetic-state",
                qos=1,
                retain=True,
            ),
        )
        await asyncio.sleep(0)
        self.assertEqual(
            set(provider.semantic_bootstrap_calls[0]),
            {provider.state_topic, provider.availability_topic},
        )
        self.assertEqual(
            client.subscriptions,
            [[(topic, 1) for topic in provider.topics]],
        )
        await subscriber.async_stop()

    async def test_mismatched_bootstrap_is_retained_until_runtime_repairs_it(
        self,
    ) -> None:
        mqtt_module = FakeMqttV1()
        provider = RecoveringFourTopicProvider()
        subscriber = self.subscriber(mqtt_module, provider)
        await subscriber.async_start()
        client = mqtt_module.clients[0]
        client.on_connect(client, None, {}, 0)
        client.on_subscribe(client, None, 41, [1, 1, 1, 1])

        client.on_message(
            client,
            None,
            SimpleNamespace(
                topic=provider.runtime_availability_topic,
                payload=b"retained-runtime-b",
                qos=1,
                retain=True,
            ),
        )
        client.on_message(
            client,
            None,
            SimpleNamespace(
                topic=provider.presence_topic,
                payload=b"live-presence-a",
                qos=1,
                retain=False,
            ),
        )
        await asyncio.sleep(0)
        self.assertFalse(provider.transport_ready)
        self.assertEqual(subscriber.rejected_messages, 1)
        self.assertEqual(
            set(subscriber._retained_bootstrap),
            {provider.runtime_availability_topic, provider.presence_topic},
        )

        client.on_message(
            client,
            None,
            SimpleNamespace(
                topic=provider.runtime_availability_topic,
                payload=b"live-runtime-a",
                qos=1,
                retain=False,
            ),
        )
        await asyncio.sleep(0)
        self.assertTrue(provider.transport_ready)
        self.assertEqual(len(provider.control_bootstrap_calls), 2)
        repaired = provider.control_bootstrap_calls[-1]
        self.assertEqual(
            repaired[provider.runtime_availability_topic],
            (b"live-runtime-a", 1, False),
        )
        self.assertEqual(
            repaired[provider.presence_topic],
            (b"live-presence-a", 1, False),
        )
        await subscriber.async_stop()

    async def test_an_old_connection_message_cannot_enter_the_new_bootstrap_buffer(
        self,
    ) -> None:
        mqtt_module = FakeMqttV1()
        provider = FourTopicProvider()
        subscriber = self.subscriber(mqtt_module, provider)
        await subscriber.async_start()
        client = mqtt_module.clients[0]

        client.on_connect(client, None, {}, 0)
        await asyncio.sleep(0)
        old_generation = subscriber._active_connection_generation
        client.on_disconnect(client, None, 0)
        await asyncio.sleep(0)
        client.on_connect(client, None, {}, 0)
        await asyncio.sleep(0)
        self.assertNotEqual(
            subscriber._active_connection_generation, old_generation
        )

        subscriber._dispatch_message(
            client,
            provider.presence_topic,
            b"old-live-presence",
            1,
            False,
            old_generation,
        )

        self.assertEqual(subscriber._retained_bootstrap, {})
        self.assertEqual(provider.ingest_calls, [])
        await subscriber.async_stop()

    async def test_denied_suback_retries_with_fresh_mid_and_ignores_stale_ack(
        self,
    ) -> None:
        mqtt_module = FakeMqttV1()
        provider = self.provider()
        subscriber = self.subscriber(mqtt_module, provider)
        await subscriber.async_start()
        client = mqtt_module.clients[0]
        client.on_connect(client, None, {}, 0)
        await asyncio.sleep(0)
        client.subscribe_result = (0, 42)
        client.on_subscribe(client, None, 41, [1, 128, 1])
        await asyncio.sleep(0)

        self.assertFalse(provider.transport_ready)
        self.assertIsNotNone(subscriber._subscription_retry_handle)
        subscriber._cancel_subscription_retry()
        subscriber._retry_subscription(client)
        self.assertEqual(len(client.subscriptions), 2)
        self.assertEqual(subscriber._subscription_mid, 42)

        client.on_subscribe(client, None, 41, [1, 1, 1])
        await asyncio.sleep(0)
        self.assertFalse(provider.transport_ready, "stale SUBACK must be ignored")

        client.on_subscribe(client, None, 42, [1, 1, 1])
        for topic, payload in (
            (provider.state_topic, state_payload()),
            (provider.availability_topic, availability_payload("online")),
            (provider.runtime_availability_topic, runtime_payload("online")),
        ):
            client.on_message(
                client,
                None,
                SimpleNamespace(topic=topic, payload=payload, qos=1, retain=True),
            )
        await asyncio.sleep(0)
        self.assertTrue(provider.transport_ready)
        client.on_disconnect(client, None, 7)
        await asyncio.sleep(0)
        self.assertFalse(provider.transport_ready)
        await subscriber.async_stop()

    async def test_missing_suback_watchdog_retries_and_disconnect_cancels_it(
        self,
    ) -> None:
        mqtt_module = FakeMqttV1()
        provider = self.provider()
        subscriber = self.subscriber(mqtt_module, provider)
        await subscriber.async_start()
        client = mqtt_module.clients[0]
        client.on_connect(client, None, {}, 0)
        await asyncio.sleep(0)

        self.assertIsNotNone(subscriber._subscription_retry_handle)
        client.subscribe_result = (0, 42)
        subscriber._cancel_subscription_retry()
        subscriber._retry_subscription(client)
        self.assertEqual(len(client.subscriptions), 2)
        self.assertEqual(subscriber._subscription_mid, 42)

        client.on_disconnect(client, None, 7)
        await asyncio.sleep(0)
        self.assertIsNone(subscriber._subscription_retry_handle)
        self.assertIsNone(subscriber._subscription_mid)
        self.assertFalse(provider.transport_ready)
        await subscriber.async_stop()

    async def test_subscribe_exception_uses_the_same_bounded_retry_path(self) -> None:
        mqtt_module = FakeMqttV1()
        provider = self.provider()
        subscriber = self.subscriber(mqtt_module, provider)
        await subscriber.async_start()
        client = mqtt_module.clients[0]
        client.subscribe_error = RuntimeError("synthetic subscribe failure")

        client.on_connect(client, None, {}, 0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertIsNotNone(subscriber._subscription_retry_handle)
        client.subscribe_error = None
        client.subscribe_result = (0, 42)
        subscriber._cancel_subscription_retry()
        subscriber._retry_subscription(client)

        self.assertEqual(len(client.subscriptions), 1)
        self.assertEqual(subscriber._subscription_mid, 42)
        self.assertFalse(provider.transport_ready)
        await subscriber.async_stop()

    async def test_subscription_retry_backoff_is_capped(self) -> None:
        mqtt_module = FakeMqttV1()
        subscriber = self.subscriber(mqtt_module)
        await subscriber.async_start()
        client = mqtt_module.clients[0]
        subscriber._connected = True

        for expected in (600, 1200, 1800, 1800):
            subscriber._cancel_subscription_retry()
            subscriber._subscription_failed(client)
            self.assertEqual(subscriber._subscription_retry_seconds, expected)

        await subscriber.async_stop()

    async def test_late_subscribe_failure_after_disconnect_cannot_schedule_retry(
        self,
    ) -> None:
        mqtt_module = FakeMqttV1()
        subscriber = self.subscriber(mqtt_module)
        await subscriber.async_start()
        client = mqtt_module.clients[0]
        client.on_connect(client, None, {}, 0)
        await asyncio.sleep(0)

        subscriber._connection_lost(
            client, subscriber._callback_connection_generation
        )
        subscriber._subscription_failed(client)

        self.assertIsNone(subscriber._subscription_retry_handle)
        self.assertIsNone(subscriber._subscription_mid)
        await subscriber.async_stop()

    async def test_paho_v2_callback_shapes_are_accepted(self) -> None:
        mqtt_module = FakeMqttV2()
        provider = self.provider()
        subscriber = self.subscriber(mqtt_module, provider)
        await subscriber.async_start()
        client = mqtt_module.clients[0]

        client.on_connect(client, None, {}, FakeReasonCode(0), object())
        client.on_subscribe(
            client,
            None,
            41,
            [FakeReasonCode(1), FakeReasonCode(1), FakeReasonCode(1)],
            object(),
        )
        await asyncio.sleep(0)
        self.assertFalse(provider.transport_ready)

        client.on_disconnect(client, None, object(), FakeReasonCode(7), object())
        await asyncio.sleep(0)
        self.assertFalse(provider.transport_ready)
        await subscriber.async_stop()

    async def test_late_suback_after_stop_cannot_reenable_transport(self) -> None:
        mqtt_module = FakeMqttV1()
        provider = self.provider()
        subscriber = self.subscriber(mqtt_module, provider)
        await subscriber.async_start()
        client = mqtt_module.clients[0]
        client.on_connect(client, None, {}, 0)

        await subscriber.async_stop()
        client.on_subscribe(client, None, 41, [1, 1, 1])
        await asyncio.sleep(0)

        self.assertFalse(provider.transport_ready)

    async def test_previous_client_message_after_restart_is_ignored(self) -> None:
        mqtt_module = FakeMqttV1()
        provider = self.provider()
        subscriber = self.subscriber(mqtt_module, provider)
        await subscriber.async_start()
        previous_client = mqtt_module.clients[0]
        await subscriber.async_stop()
        await subscriber.async_start()

        previous_client.on_message(
            previous_client,
            None,
            SimpleNamespace(
                topic=provider.state_topic,
                payload=state_payload(),
                qos=1,
                retain=True,
            ),
        )
        await asyncio.sleep(0)

        self.assertIsNone(provider.shadow_value)
        self.assertEqual(subscriber.rejected_messages, 0)
        await subscriber.async_stop()

    async def test_reconnect_adopts_one_complete_new_final_current_generation(
        self,
    ) -> None:
        mqtt_module = FakeMqttV1()
        provider = self.provider()
        subscriber = self.subscriber(mqtt_module, provider)
        await subscriber.async_start()
        client = mqtt_module.clients[0]

        client.on_connect(client, None, {}, 0)
        client.on_subscribe(client, None, 41, [1, 1, 1])
        for topic, payload in (
            (provider.state_topic, state_payload(value=True)),
            (provider.availability_topic, availability_payload("online")),
            (provider.runtime_availability_topic, runtime_payload("online")),
        ):
            client.on_message(
                client,
                None,
                SimpleNamespace(topic=topic, payload=payload, qos=1, retain=True),
            )
        await asyncio.sleep(0)
        self.assertTrue(provider.shadow_healthy)

        client.on_disconnect(client, None, 7)
        client.on_connect(client, None, {}, 0)
        client.on_subscribe(client, None, 41, [1, 1, 1])
        for topic, payload in (
            (
                provider.runtime_availability_topic,
                runtime_payload("online", service_instance_id="2" * 32),
            ),
            (
                provider.availability_topic,
                availability_payload("online", session_id="session_dhum_provider_002"),
            ),
            (
                provider.state_topic,
                state_payload(
                    value=False,
                    session_id="session_dhum_provider_002",
                    sequence=1,
                ),
            ),
        ):
            client.on_message(
                client,
                None,
                SimpleNamespace(topic=topic, payload=payload, qos=1, retain=True),
            )
        await asyncio.sleep(0)

        self.assertTrue(provider.transport_ready)
        self.assertTrue(provider.shadow_healthy)
        self.assertEqual(provider.session_id, "session_dhum_provider_002")
        self.assertFalse(provider.shadow_value)
        self.assertEqual(subscriber.rejected_messages, 0)

        client.on_message(
            client,
            None,
            SimpleNamespace(
                topic=provider.state_topic,
                payload=state_payload(value=True, sequence=2),
                qos=1,
                retain=False,
            ),
        )
        await asyncio.sleep(0)
        self.assertEqual(subscriber.rejected_messages, 1)
        self.assertFalse(provider.shadow_value)
        await subscriber.async_stop()

    async def test_partial_or_failed_subscription_never_enables_transport(self) -> None:
        mqtt_module = FakeMqttV1()
        provider = self.provider()
        subscriber = self.subscriber(mqtt_module, provider)
        await subscriber.async_start()
        client = mqtt_module.clients[0]
        client.on_connect(client, None, {}, 0)
        client.on_subscribe(client, None, 41, [1, 1, 1])
        for topic, payload in (
            (provider.state_topic, state_payload()),
            (provider.availability_topic, availability_payload("online")),
        ):
            client.on_message(
                client,
                None,
                SimpleNamespace(topic=topic, payload=payload, qos=1, retain=True),
            )
        await asyncio.sleep(0)
        self.assertFalse(provider.transport_ready)
        self.assertIsNone(provider.shadow_value)

        client.subscribe_error = RuntimeError("synthetic subscribe failure")
        client.on_connect(client, None, {}, 0)
        await asyncio.sleep(0)
        self.assertFalse(provider.transport_ready)
        self.assertIsNone(provider.shadow_value)
        await subscriber.async_stop()

    async def test_live_qos_one_repairs_inconsistent_retained_bootstrap(self) -> None:
        mqtt_module = FakeMqttV1()
        provider = self.provider()
        subscriber = self.subscriber(mqtt_module, provider)
        await subscriber.async_start()
        client = mqtt_module.clients[0]
        client.on_connect(client, None, {}, 0)
        client.on_subscribe(client, None, 41, [1, 1, 1])

        for topic, payload in (
            (
                provider.state_topic,
                state_payload(session_id="session_dhum_provider_002"),
            ),
            (provider.availability_topic, availability_payload("online")),
            (provider.runtime_availability_topic, runtime_payload("online")),
        ):
            client.on_message(
                client,
                None,
                SimpleNamespace(topic=topic, payload=payload, qos=1, retain=True),
            )
        await asyncio.sleep(0)
        self.assertFalse(provider.transport_ready)
        self.assertIsNone(provider.shadow_value)
        self.assertEqual(subscriber.rejected_messages, 1)

        client.on_message(
            client,
            None,
            SimpleNamespace(
                topic=provider.availability_topic,
                payload=availability_payload(
                    "online", session_id="session_dhum_provider_002"
                ),
                qos=1,
                retain=False,
            ),
        )
        await asyncio.sleep(0)
        self.assertTrue(provider.transport_ready)
        self.assertTrue(provider.shadow_healthy)
        self.assertEqual(provider.session_id, "session_dhum_provider_002")
        await subscriber.async_stop()

    async def test_live_v3_transport_commits_the_pair_without_an_unavailable_edge(self) -> None:
        from tests.test_local_provider import IdentityBoundPublicationTests

        fixture = IdentityBoundPublicationTests()
        provider = local.LocalSemanticShadowProvider(
            BINDING_ID,
            local.LocalSemanticProfile(
                profile_id='synthetic-identity-v1', model_id='SYNTHETIC_MODEL', platform='thinq2',
                semantics_revision=31,
                fields={'door.open': local.LocalSemanticFieldContract(value_type='boolean', exposure='state', confidence=('confirmed-synthetic',))},
            ),
            pat_device_id=fixture.PAT_DEVICE_ID, require_identity=True, now=lambda: NOW,
        )
        mqtt_module = FakeMqttV1()
        subscriber = self.subscriber(mqtt_module, provider)
        await subscriber.async_start()
        client = mqtt_module.clients[0]
        client.on_connect(client, None, {}, 0)
        client.on_subscribe(client, None, 41, [1, 1, 1])
        def message(topic, payload):
            client.on_message(client, None, SimpleNamespace(topic=topic, payload=payload, qos=1, retain=False))
        for topic, payload in (
            (provider.state_topic, fixture.payload(3)),
            (provider.availability_topic, fixture.availability(3)),
            (provider.runtime_availability_topic, runtime_payload('online')),
        ):
            message(topic, payload)
        await asyncio.sleep(0)
        self.assertTrue(provider.shadow_healthy)
        changes = []
        provider.async_add_listener(lambda: changes.append((provider.sequence, provider.shadow_healthy)))
        message(provider.state_topic, fixture.payload(3, sequence=2))
        await asyncio.sleep(0)
        self.assertEqual(provider.sequence, 1)
        self.assertEqual(changes, [])
        message(provider.availability_topic, fixture.availability(3, state_sequence=2))
        await asyncio.sleep(0)
        self.assertEqual(changes, [(2, True)])
        self.assertEqual(subscriber.rejected_messages, 0)
        await subscriber.async_stop()
        self.assertFalse(provider.shadow_healthy)

    async def test_invalid_messages_are_isolated_and_never_escape_callback_thread(
        self,
    ) -> None:
        mqtt_module = FakeMqttV1()
        provider = self.provider()
        subscriber = self.subscriber(mqtt_module, provider)
        await subscriber.async_start()
        client = mqtt_module.clients[0]
        client.on_connect(client, None, {}, 0)
        await asyncio.sleep(0)
        client.on_message(
            client,
            None,
            SimpleNamespace(
                topic=f"{provider.state_topic}/foreign",
                payload=state_payload(),
                qos=1,
                retain=False,
            ),
        )
        await asyncio.sleep(0)
        self.assertEqual(provider.rejected_messages, 1)
        self.assertEqual(subscriber.rejected_messages, 1)
        self.assertIsNone(provider.shadow_value)
        await subscriber.async_stop()

    async def test_transport_does_not_coerce_malformed_message_metadata(self) -> None:
        mqtt_module = FakeMqttV1()
        provider = self.provider()
        subscriber = self.subscriber(mqtt_module, provider)
        await subscriber.async_start()
        client = mqtt_module.clients[0]
        client.on_connect(client, None, {}, 0)
        await asyncio.sleep(0)
        for message in (
            SimpleNamespace(
                topic=provider.state_topic,
                payload=state_payload(),
                qos=True,
                retain=True,
            ),
            SimpleNamespace(
                topic=provider.state_topic,
                payload=state_payload(),
                qos=1,
                retain="false",
            ),
            SimpleNamespace(
                topic=provider.state_topic,
                payload=bytearray(state_payload()),
                qos=1,
                retain=True,
            ),
        ):
            client.on_message(client, None, message)
        await asyncio.sleep(0)
        self.assertEqual(subscriber.rejected_messages, 3)
        self.assertIsNone(provider.shadow_value)
        await subscriber.async_stop()

    def test_rejects_non_loopback_broker_bad_acl_identity_and_bad_secret(self) -> None:
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        provider = self.provider()
        cases = [
            {"host": "192.0.2.1", "username": f"shadow-{BINDING_ID}", "password": "x"},
            {"host": "127.0.0.1", "username": BINDING_ID, "password": "x"},
            {"host": "127.0.0.1", "username": f"shadow-{BINDING_ID}", "password": ""},
        ]
        for values in cases:
            with (
                self.subTest(values=values),
                self.assertRaises(local_mqtt.LocalMqttConfigurationError),
            ):
                local_mqtt.LocalPilotMqttSubscriber(
                    loop,
                    provider,
                    port=18883,
                    mqtt_module=FakeMqttV1(),
                    **values,
                )

    def test_module_has_no_outbound_or_control_surface(self) -> None:
        source = (COMPONENT_PATH / "local_mqtt.py").read_text()
        self.assertNotIn(".publish(", source)
        self.assertNotIn("removeDevice", source)
        self.assertNotIn("initDevice", source)
        self.assertNotIn("command_topic", source)


if __name__ == "__main__":
    unittest.main()
