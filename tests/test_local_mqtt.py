"""Pure lifecycle tests for the dedicated read-only pilot MQTT subscriber."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.test_local_provider import (
    BINDING_ID,
    IDENTITY_BINDING,
    IDENTITY_GENERATION,
    PAT_ID_A,
    PUBLICATION_SESSION_ONE,
    PUBLICATION_SESSION_TWO,
    SESSION_ONE,
    SESSION_TWO,
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


def cohort_profile():
    return local.LocalSemanticProfile(
        profile_id="synthetic-cohort-v1",
        model_id="AIR_910604_WW",
        platform="thinq2",
        semantics_revision=30,
        fields={
            "feature.enabled": local.LocalSemanticFieldContract(
                value_type="boolean",
                exposure="state",
                confidence=("confirmed-synthetic",),
            )
        },
    )


def cohort_expectation():
    profile = cohort_profile()
    return local.LocalPilotIdentityExpectation(
        binding_id=IDENTITY_BINDING,
        binding_generation=IDENTITY_GENERATION,
        model_id=profile.model_id,
        platform=profile.platform,
        pat_device_id_proof_sha256=local.create_local_pat_device_identity_proof(
            binding_id=IDENTITY_BINDING,
            model_id=profile.model_id,
            platform=profile.platform,
            pat_device_id=PAT_ID_A,
        ),
    )


def cohort_provider():
    return local.LocalSemanticShadowProvider(
        IDENTITY_BINDING,
        cohort_profile(),
        identity_expectation=cohort_expectation(),
        snapshot_schema_version=3,
        now=lambda: NOW,
    )


def cohort_state(
    cohort_generation: int,
    *,
    session_id: str = PUBLICATION_SESSION_ONE,
    sequence: int = 1,
    value: bool = True,
) -> bytes:
    expectation = cohort_expectation()
    return json.dumps(
        {
            "schema_version": 3,
            "semantics_revision": 30,
            "binding_id": IDENTITY_BINDING,
            "binding_generation": IDENTITY_GENERATION,
            "pat_device_id_proof_sha256": expectation.pat_device_id_proof_sha256,
            "cohort_generation": cohort_generation,
            "model_id": expectation.model_id,
            "platform": expectation.platform,
            "session_id": session_id,
            "sequence": sequence,
            "published_at": "2026-08-13T00:59:59.000Z",
            "fields": {
                "feature.enabled": {
                    "value": value,
                    "value_type": "boolean",
                    "observed_at": "2026-08-13T00:59:58.000Z",
                    "confidence": "confirmed-synthetic",
                    "exposure": "state",
                }
            },
            "diagnostics": {
                "rejected_frames": 0,
                "unresolved_fields": 0,
                "invalid_values": 0,
                "unsupported_frames": 0,
            },
        },
        separators=(",", ":"),
    ).encode()


def cohort_availability(
    cohort_generation: int,
    *,
    session_id: str = PUBLICATION_SESSION_ONE,
    state_sequence: int = 1,
    status: str = "online",
) -> bytes:
    expectation = cohort_expectation()
    return json.dumps(
        {
            "schema_version": 3,
            "status": status,
            "session_id": session_id,
            "observed_at": "2026-08-13T01:00:00.000Z",
            "binding_generation": IDENTITY_GENERATION,
            "pat_device_id_proof_sha256": expectation.pat_device_id_proof_sha256,
            "cohort_generation": cohort_generation,
            "state_sequence": state_sequence,
        },
        separators=(",", ":"),
    ).encode()


def cohort_identity() -> bytes:
    expectation = cohort_expectation()
    return json.dumps(
        {
            "schema_version": 1,
            "binding_id": IDENTITY_BINDING,
            "binding_generation": IDENTITY_GENERATION,
            "model_id": expectation.model_id,
            "platform": expectation.platform,
            "pat_device_id_proof_sha256": expectation.pat_device_id_proof_sha256,
        },
        separators=(",", ":"),
    ).encode()


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

        subscriber._connection_lost(client)
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

    async def test_v3_bootstrap_waits_for_one_exact_cohort_then_live_supersedes(
        self,
    ) -> None:
        mqtt_module = FakeMqttV1()
        provider = cohort_provider()
        subscriber = local_mqtt.LocalPilotMqttSubscriber(
            asyncio.get_running_loop(),
            provider,
            host="127.0.0.1",
            port=18883,
            username=f"shadow-{IDENTITY_BINDING}",
            password="private-test-password",
            mqtt_module=mqtt_module,
        )
        await subscriber.async_start()
        client = mqtt_module.clients[0]
        client.on_connect(client, None, {}, 0)
        client.on_subscribe(client, None, 41, [1, 1, 1, 1])

        # The broker can expose a mixed retained window. It must not become
        # transport-ready until a live retained repair completes one cohort.
        for topic, payload in (
            (
                provider.state_topic,
                cohort_state(2, session_id=PUBLICATION_SESSION_TWO),
            ),
            (provider.availability_topic, cohort_availability(1)),
            (
                provider.runtime_availability_topic,
                runtime_payload(
                    "online",
                    service_instance_id=PUBLICATION_SESSION_TWO,
                ),
            ),
            (provider.identity_topic, cohort_identity()),
        ):
            client.on_message(
                client,
                None,
                SimpleNamespace(topic=topic, payload=payload, qos=1, retain=True),
            )
        await asyncio.sleep(0)
        self.assertFalse(provider.transport_ready)
        self.assertFalse(provider.shadow_healthy)
        self.assertEqual(subscriber.rejected_messages, 1)

        client.on_message(
            client,
            None,
            SimpleNamespace(
                topic=provider.availability_topic,
                payload=cohort_availability(
                    2,
                    session_id=PUBLICATION_SESSION_TWO,
                ),
                qos=1,
                retain=False,
            ),
        )
        await asyncio.sleep(0)
        self.assertTrue(provider.transport_ready)
        self.assertTrue(provider.shadow_healthy)
        self.assertEqual(provider.cohort_generation, 2)

        # A higher live cohort may supersede without an intermediate device
        # offline, but state-first must immediately make every entity unavailable.
        client.on_message(
            client,
            None,
            SimpleNamespace(
                topic=provider.state_topic,
                payload=cohort_state(
                    3,
                    session_id=PUBLICATION_SESSION_TWO,
                    value=False,
                ),
                qos=1,
                retain=False,
            ),
        )
        await asyncio.sleep(0)
        self.assertFalse(provider.shadow_healthy)
        self.assertIs(provider.field_value("feature.enabled"), False)

        client.on_message(
            client,
            None,
            SimpleNamespace(
                topic=provider.availability_topic,
                payload=cohort_availability(
                    3,
                    session_id=PUBLICATION_SESSION_TWO,
                ),
                qos=1,
                retain=False,
            ),
        )
        await asyncio.sleep(0)
        self.assertTrue(provider.shadow_healthy)
        self.assertEqual(provider.cohort_generation, 3)
        await subscriber.async_stop()

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

    async def test_tombstone_discards_every_partial_bootstrap_candidate(self) -> None:
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
        self.assertEqual(set(subscriber._retained_bootstrap), {
            provider.state_topic,
            provider.availability_topic,
        })

        # A live delivery of a retained delete normally has retain=False.
        client.on_message(
            client,
            None,
            SimpleNamespace(
                topic=provider.state_topic,
                payload=b"",
                qos=1,
                retain=False,
            ),
        )
        await asyncio.sleep(0)
        self.assertEqual(subscriber._retained_bootstrap, {})
        self.assertFalse(provider.transport_ready)
        self.assertIsNone(provider.shadow_value)
        self.assertEqual(subscriber.rejected_messages, 0)
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
