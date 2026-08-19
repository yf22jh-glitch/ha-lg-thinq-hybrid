"""Setup isolation tests for the optional Rethink Local shadow sidecar."""

from __future__ import annotations

import asyncio
import threading
import unittest
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, patch

import custom_components.my_lg as integration
from custom_components.my_lg.const import DEVICE_TYPE_DEHUMIDIFIER
from custom_components.my_lg.local_provider import (
    LOCAL_PROVIDER_MODE_SHADOW,
    OPT_LOCAL_BINDING_ID,
    OPT_LOCAL_BINDINGS,
    OPT_LOCAL_MQTT_PASSWORD,
    OPT_LOCAL_PAT_DEVICE_ID,
    OPT_LOCAL_PROVIDER_MODE,
)
from tests.test_local_provider import BINDING_ID

PAT_DEVICE_ID = "pat-dehumidifier-001"
PAT_DEVICE_ID_TWO = "pat-dehumidifier-002"


def options(**overrides):
    result = {
        OPT_LOCAL_PROVIDER_MODE: LOCAL_PROVIDER_MODE_SHADOW,
        OPT_LOCAL_PAT_DEVICE_ID: PAT_DEVICE_ID,
        OPT_LOCAL_BINDING_ID: BINDING_ID,
        OPT_LOCAL_MQTT_PASSWORD: "private-test-password",
    }
    result.update(overrides)
    return result


def binding_options(*pat_device_ids):
    return {
        OPT_LOCAL_BINDINGS: [
            {
                "schema_version": 1,
                "mode": "shadow",
                "profile_id": "dhum-water-tank-v1",
                "model_id": "DHUM_056905_WW",
                "platform": "thinq2",
                "pat_device_id": pat_device_id,
                "binding_id": f"pilot_dhum_provider_{index:03d}",
                "mqtt_password": f"private-test-password-{index}",
            }
            for index, pat_device_id in enumerate(pat_device_ids, start=1)
        ]
    }


def data(*, model="DHUM_056905_WW", wideq=True, multiple=False):
    device_ids = [PAT_DEVICE_ID]
    if multiple:
        device_ids.append(PAT_DEVICE_ID_TWO)
    coordinators = {
        device_id: SimpleNamespace(
            device_id=device_id,
            device_type=DEVICE_TYPE_DEHUMIDIFIER,
            model=model,
        )
        for device_id in device_ids
    }
    return integration.MyLgData(
        api=object(),
        coordinators=coordinators,
        wideq_coordinator=object() if wideq else None,
    )


class FakeSubscriber:
    instances: ClassVar[list[FakeSubscriber]] = []
    start_error_bindings: ClassVar[set[str]] = set()
    stop_error_bindings: ClassVar[set[str]] = set()

    def __init__(self, loop, provider, **kwargs) -> None:
        self.loop = loop
        self.provider = provider
        self.kwargs = kwargs
        self.started = 0
        self.stopped = 0
        type(self).instances.append(self)

    async def async_start(self):
        self.started += 1
        if self.provider.binding_id in self.start_error_bindings:
            raise RuntimeError("synthetic transport failure")

    async def async_stop(self):
        self.stopped += 1
        if self.provider.binding_id in self.stop_error_bindings:
            raise RuntimeError("synthetic stop failure")


class LocalShadowSetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        FakeSubscriber.instances.clear()
        FakeSubscriber.start_error_bindings.clear()
        FakeSubscriber.stop_error_bindings.clear()

        async def async_add_executor_job(target, *args):
            return await asyncio.to_thread(target, *args)

        self.hass = SimpleNamespace(
            loop=asyncio.get_running_loop(),
            async_add_executor_job=async_add_executor_job,
        )

    async def test_configuration_and_catalogue_io_run_off_event_loop(self) -> None:
        loop_thread = threading.get_ident()
        worker_threads: list[int] = []
        original = integration.local_shadow_configurations

        def observed(options_value):
            worker_threads.append(threading.get_ident())
            return original(options_value)

        with (
            patch.object(integration, "local_shadow_configurations", observed),
            patch.object(integration, "LocalPilotMqttSubscriber", FakeSubscriber),
        ):
            await integration._setup_local_shadows(
                self.hass, SimpleNamespace(options=options()), data()
            )

        self.assertEqual(len(worker_threads), 1)
        self.assertNotEqual(worker_threads[0], loop_thread)

    async def test_exact_target_starts_one_sidecar_and_keeps_pat_anchor(self) -> None:
        runtime = data()
        entry = SimpleNamespace(options=options())
        with patch.object(integration, "LocalPilotMqttSubscriber", FakeSubscriber):
            await integration._setup_local_shadows(self.hass, entry, runtime)

        self.assertEqual(len(FakeSubscriber.instances), 1)
        subscriber = FakeSubscriber.instances[0]
        self.assertEqual(subscriber.started, 1)
        self.assertEqual(subscriber.kwargs["host"], "127.0.0.1")
        self.assertEqual(subscriber.kwargs["port"], 18883)
        self.assertEqual(subscriber.kwargs["username"], f"shadow-{BINDING_ID}")
        self.assertIs(runtime.local_mqtt_subscribers[PAT_DEVICE_ID], subscriber)
        self.assertIs(runtime.local_providers[PAT_DEVICE_ID], subscriber.provider)

    async def test_missing_wideq_or_wrong_model_is_nonfatal_and_starts_nothing(
        self,
    ) -> None:
        for runtime in (data(wideq=False), data(model="OTHER_MODEL")):
            with self.subTest(runtime=runtime):
                with patch.object(
                    integration, "LocalPilotMqttSubscriber", FakeSubscriber
                ):
                    await integration._setup_local_shadows(
                        self.hass,
                        SimpleNamespace(options=options()),
                        runtime,
                    )
                self.assertEqual(runtime.local_providers, {})
                self.assertEqual(runtime.local_mqtt_subscribers, {})
        self.assertEqual(FakeSubscriber.instances, [])

    async def test_transport_start_failure_never_blocks_wideq_runtime(self) -> None:
        runtime = data()
        FakeSubscriber.start_error_bindings.add(BINDING_ID)
        with patch.object(integration, "LocalPilotMqttSubscriber", FakeSubscriber):
            await integration._setup_local_shadows(
                self.hass,
                SimpleNamespace(options=options()),
                runtime,
            )
        self.assertIsNotNone(runtime.wideq_coordinator)
        self.assertEqual(runtime.local_providers, {})
        self.assertEqual(runtime.local_mqtt_subscribers, {})
        self.assertEqual(FakeSubscriber.instances[0].stopped, 1)

    async def test_stop_detaches_identity_and_transport_before_returning(self) -> None:
        runtime = data()
        entry = SimpleNamespace(options=options())
        with patch.object(integration, "LocalPilotMqttSubscriber", FakeSubscriber):
            await integration._setup_local_shadows(self.hass, entry, runtime)
        subscriber = FakeSubscriber.instances[0]

        await integration._stop_local_shadows(runtime)

        self.assertEqual(subscriber.stopped, 1)
        self.assertEqual(runtime.local_providers, {})
        self.assertEqual(runtime.local_mqtt_subscribers, {})

    async def test_disabled_mode_never_constructs_a_subscriber(self) -> None:
        runtime = data()
        entry = SimpleNamespace(options={OPT_LOCAL_PROVIDER_MODE: "disabled"})
        with patch.object(integration, "LocalPilotMqttSubscriber", FakeSubscriber):
            await integration._setup_local_shadows(self.hass, entry, runtime)

        self.assertEqual(FakeSubscriber.instances, [])
        self.assertEqual(runtime.local_providers, {})
        self.assertEqual(runtime.local_mqtt_subscribers, {})

    async def test_multiple_bindings_start_independently_by_pat_identity(self) -> None:
        runtime = data(multiple=True)
        entry = SimpleNamespace(
            options=binding_options(PAT_DEVICE_ID, PAT_DEVICE_ID_TWO)
        )
        with patch.object(integration, "LocalPilotMqttSubscriber", FakeSubscriber):
            await integration._setup_local_shadows(self.hass, entry, runtime)

        self.assertEqual(
            set(runtime.local_providers), {PAT_DEVICE_ID, PAT_DEVICE_ID_TWO}
        )
        self.assertEqual(
            set(runtime.local_mqtt_subscribers),
            {PAT_DEVICE_ID, PAT_DEVICE_ID_TWO},
        )
        self.assertEqual(len(FakeSubscriber.instances), 2)
        self.assertTrue(all(item.started == 1 for item in FakeSubscriber.instances))

    async def test_non_dhum_profile_starts_without_wideq_and_stays_shadow_only(
        self,
    ) -> None:
        pat_id = "pat-styler-001"
        runtime = integration.MyLgData(
            api=object(),
            coordinators={
                pat_id: SimpleNamespace(
                    device_id=pat_id,
                    device_type="STYLER",
                    model="ST_R_ETH01Y_",
                )
            },
            wideq_coordinator=None,
        )
        entry = SimpleNamespace(
            options={
                OPT_LOCAL_BINDINGS: [
                    {
                        "schema_version": 1,
                        "mode": "shadow",
                        "profile_id": "styler-core-state-v1",
                        "model_id": "ST_R_ETH01Y_",
                        "platform": "thinq2",
                        "pat_device_id": pat_id,
                        "binding_id": "pilot_styler_provider_001",
                        "mqtt_password": "private-test-password",
                    }
                ]
            }
        )

        with patch.object(integration, "LocalPilotMqttSubscriber", FakeSubscriber):
            await integration._setup_local_shadows(self.hass, entry, runtime)

        provider = runtime.local_providers[pat_id]
        self.assertEqual(provider.profile_id, "styler-core-state-v1")
        self.assertEqual(provider.model_id, "ST_R_ETH01Y_")
        self.assertEqual(
            set(provider.profile.fields),
            {
                "cycle.course",
                "cycle.state",
                "option.no_interrupt_enabled",
            },
        )
        self.assertIsNone(runtime.wideq_coordinator)

    async def test_one_binding_start_failure_does_not_remove_a_healthy_binding(
        self,
    ) -> None:
        runtime = data(multiple=True)
        entry = SimpleNamespace(
            options=binding_options(PAT_DEVICE_ID, PAT_DEVICE_ID_TWO)
        )
        FakeSubscriber.start_error_bindings.add("pilot_dhum_provider_002")
        with patch.object(integration, "LocalPilotMqttSubscriber", FakeSubscriber):
            await integration._setup_local_shadows(self.hass, entry, runtime)

        self.assertEqual(set(runtime.local_providers), {PAT_DEVICE_ID})
        self.assertEqual(set(runtime.local_mqtt_subscribers), {PAT_DEVICE_ID})
        failed = next(
            item
            for item in FakeSubscriber.instances
            if item.provider.binding_id == "pilot_dhum_provider_002"
        )
        self.assertEqual(failed.stopped, 1)

    async def test_stop_failure_is_isolated_and_all_bindings_are_detached(self) -> None:
        runtime = data(multiple=True)
        entry = SimpleNamespace(
            options=binding_options(PAT_DEVICE_ID, PAT_DEVICE_ID_TWO)
        )
        with patch.object(integration, "LocalPilotMqttSubscriber", FakeSubscriber):
            await integration._setup_local_shadows(self.hass, entry, runtime)
        FakeSubscriber.stop_error_bindings.add("pilot_dhum_provider_001")

        await integration._stop_local_shadows(runtime)

        self.assertEqual(runtime.local_providers, {})
        self.assertEqual(runtime.local_mqtt_subscribers, {})
        self.assertTrue(all(item.stopped == 1 for item in FakeSubscriber.instances))

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
        runtime.local_providers[PAT_DEVICE_ID] = object()
        runtime.local_mqtt_subscribers[PAT_DEVICE_ID] = local_subscriber
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
        self.assertIs(runtime.local_mqtt_subscribers[PAT_DEVICE_ID], local_subscriber)

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
        runtime.local_providers[PAT_DEVICE_ID] = object()
        runtime.local_mqtt_subscribers[PAT_DEVICE_ID] = local_subscriber
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
        self.assertEqual(runtime.local_providers, {})
        self.assertEqual(runtime.local_mqtt_subscribers, {})


if __name__ == "__main__":
    unittest.main()
