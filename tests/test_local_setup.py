"""Setup isolation tests for the optional Rethink Local shadow sidecar."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, patch

import custom_components.my_lg as integration
from custom_components.my_lg.const import DEVICE_TYPE_DEHUMIDIFIER
from custom_components.my_lg.local_provider import (
    LOCAL_PROVIDER_MODE_SHADOW,
    OPT_LOCAL_BINDING_ID,
    OPT_LOCAL_MQTT_PASSWORD,
    OPT_LOCAL_PAT_DEVICE_ID,
    OPT_LOCAL_PROVIDER_MODE,
)
from tests.test_local_provider import BINDING_ID

PAT_DEVICE_ID = "pat-dehumidifier-001"


def options(**overrides):
    result = {
        OPT_LOCAL_PROVIDER_MODE: LOCAL_PROVIDER_MODE_SHADOW,
        OPT_LOCAL_PAT_DEVICE_ID: PAT_DEVICE_ID,
        OPT_LOCAL_BINDING_ID: BINDING_ID,
        OPT_LOCAL_MQTT_PASSWORD: "private-test-password",
    }
    result.update(overrides)
    return result


def data(*, model="DHUM_056905_WW", wideq=True):
    coordinator = SimpleNamespace(
        device_id=PAT_DEVICE_ID,
        device_type=DEVICE_TYPE_DEHUMIDIFIER,
        model=model,
    )
    return integration.MyLgData(
        api=object(),
        coordinators={PAT_DEVICE_ID: coordinator},
        wideq_coordinator=object() if wideq else None,
    )


class FakeSubscriber:
    instances: ClassVar[list[FakeSubscriber]] = []
    start_error: ClassVar[Exception | None] = None

    def __init__(self, loop, provider, **kwargs) -> None:
        self.loop = loop
        self.provider = provider
        self.kwargs = kwargs
        self.started = 0
        self.stopped = 0
        type(self).instances.append(self)

    async def async_start(self):
        self.started += 1
        if self.start_error is not None:
            raise self.start_error

    async def async_stop(self):
        self.stopped += 1


class LocalShadowSetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        FakeSubscriber.instances.clear()
        FakeSubscriber.start_error = None
        self.hass = SimpleNamespace(loop=asyncio.get_running_loop())

    async def test_exact_target_starts_one_sidecar_and_keeps_pat_anchor(self) -> None:
        runtime = data()
        entry = SimpleNamespace(options=options())
        with patch.object(integration, "LocalPilotMqttSubscriber", FakeSubscriber):
            await integration._setup_local_shadow(self.hass, entry, runtime)

        self.assertEqual(len(FakeSubscriber.instances), 1)
        subscriber = FakeSubscriber.instances[0]
        self.assertEqual(subscriber.started, 1)
        self.assertEqual(subscriber.kwargs["host"], "127.0.0.1")
        self.assertEqual(subscriber.kwargs["port"], 18883)
        self.assertEqual(subscriber.kwargs["username"], f"shadow-{BINDING_ID}")
        self.assertIs(runtime.local_mqtt, subscriber)
        self.assertIs(runtime.local_provider, subscriber.provider)
        self.assertEqual(runtime.local_pat_device_id, PAT_DEVICE_ID)

    async def test_missing_wideq_or_wrong_model_is_nonfatal_and_starts_nothing(
        self,
    ) -> None:
        for runtime in (data(wideq=False), data(model="OTHER_MODEL")):
            with self.subTest(runtime=runtime):
                with patch.object(
                    integration, "LocalPilotMqttSubscriber", FakeSubscriber
                ):
                    await integration._setup_local_shadow(
                        self.hass,
                        SimpleNamespace(options=options()),
                        runtime,
                    )
                self.assertIsNone(runtime.local_provider)
                self.assertIsNone(runtime.local_mqtt)
        self.assertEqual(FakeSubscriber.instances, [])

    async def test_transport_start_failure_never_blocks_wideq_runtime(self) -> None:
        runtime = data()
        FakeSubscriber.start_error = RuntimeError("synthetic transport failure")
        with patch.object(integration, "LocalPilotMqttSubscriber", FakeSubscriber):
            await integration._setup_local_shadow(
                self.hass,
                SimpleNamespace(options=options()),
                runtime,
            )
        self.assertIsNotNone(runtime.wideq_coordinator)
        self.assertIsNone(runtime.local_provider)
        self.assertIsNone(runtime.local_mqtt)
        self.assertEqual(FakeSubscriber.instances[0].stopped, 1)

    async def test_stop_detaches_identity_and_transport_before_returning(self) -> None:
        runtime = data()
        entry = SimpleNamespace(options=options())
        with patch.object(integration, "LocalPilotMqttSubscriber", FakeSubscriber):
            await integration._setup_local_shadow(self.hass, entry, runtime)
        subscriber = FakeSubscriber.instances[0]

        await integration._stop_local_shadow(runtime)

        self.assertEqual(subscriber.stopped, 1)
        self.assertIsNone(runtime.local_provider)
        self.assertIsNone(runtime.local_mqtt)
        self.assertIsNone(runtime.local_pat_device_id)

    async def test_disabled_mode_never_constructs_a_subscriber(self) -> None:
        runtime = data()
        entry = SimpleNamespace(options={OPT_LOCAL_PROVIDER_MODE: "disabled"})
        with patch.object(integration, "LocalPilotMqttSubscriber", FakeSubscriber):
            await integration._setup_local_shadow(self.hass, entry, runtime)

        self.assertEqual(FakeSubscriber.instances, [])
        self.assertIsNone(runtime.local_provider)
        self.assertIsNone(runtime.local_mqtt)

    async def test_failed_platform_unload_leaves_every_runtime_alive(self) -> None:
        local_subscriber = SimpleNamespace(async_stop=AsyncMock())
        pat_mqtt = SimpleNamespace(async_stop=AsyncMock())
        wideq = SimpleNamespace(
            async_persist_power_save=AsyncMock(),
            async_persist_energy_history=AsyncMock(),
            async_persist_device_map=AsyncMock(),
        )
        wideq_client = SimpleNamespace(async_close=AsyncMock())
        runtime = data()
        runtime.local_provider = object()
        runtime.local_mqtt = local_subscriber
        runtime.local_pat_device_id = PAT_DEVICE_ID
        runtime.mqtt = pat_mqtt
        runtime.wideq_coordinator = wideq
        runtime.wideq_client = wideq_client
        config_entries = SimpleNamespace(
            async_unload_platforms=AsyncMock(return_value=False)
        )
        hass = SimpleNamespace(config_entries=config_entries)
        entry = SimpleNamespace(runtime_data=runtime)

        self.assertFalse(await integration.async_unload_entry(hass, entry))

        local_subscriber.async_stop.assert_not_awaited()
        pat_mqtt.async_stop.assert_not_awaited()
        wideq.async_persist_power_save.assert_not_awaited()
        wideq.async_persist_energy_history.assert_not_awaited()
        wideq.async_persist_device_map.assert_not_awaited()
        wideq_client.async_close.assert_not_awaited()
        self.assertIs(runtime.local_mqtt, local_subscriber)
        self.assertEqual(runtime.local_pat_device_id, PAT_DEVICE_ID)

    async def test_successful_platform_unload_stops_each_runtime_once(self) -> None:
        local_subscriber = SimpleNamespace(async_stop=AsyncMock())
        pat_mqtt = SimpleNamespace(async_stop=AsyncMock())
        wideq = SimpleNamespace(
            async_persist_power_save=AsyncMock(),
            async_persist_energy_history=AsyncMock(),
            async_persist_device_map=AsyncMock(),
        )
        wideq_client = SimpleNamespace(async_close=AsyncMock())
        runtime = data()
        runtime.local_provider = object()
        runtime.local_mqtt = local_subscriber
        runtime.local_pat_device_id = PAT_DEVICE_ID
        runtime.mqtt = pat_mqtt
        runtime.wideq_coordinator = wideq
        runtime.wideq_client = wideq_client
        config_entries = SimpleNamespace(
            async_unload_platforms=AsyncMock(return_value=True)
        )
        hass = SimpleNamespace(config_entries=config_entries)
        entry = SimpleNamespace(runtime_data=runtime)

        self.assertTrue(await integration.async_unload_entry(hass, entry))

        local_subscriber.async_stop.assert_awaited_once_with()
        pat_mqtt.async_stop.assert_awaited_once_with()
        wideq.async_persist_power_save.assert_awaited_once_with()
        wideq.async_persist_energy_history.assert_awaited_once_with()
        wideq.async_persist_device_map.assert_awaited_once_with()
        wideq_client.async_close.assert_awaited_once_with()
        self.assertIsNone(runtime.local_provider)
        self.assertIsNone(runtime.local_mqtt)
        self.assertIsNone(runtime.local_pat_device_id)


if __name__ == "__main__":
    unittest.main()
