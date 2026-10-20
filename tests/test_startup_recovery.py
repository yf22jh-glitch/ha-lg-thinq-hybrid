"""Exercise failed boot reads through real coordinators and HA entity platforms."""
import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant import loader
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry, entity_registry, frame
from homeassistant.helpers.entity_component import EntityComponent

from custom_components.my_lg import MyLgData, number, sensor, startup
from custom_components.my_lg.coordinator import PatDeviceCoordinator
from custom_components.my_lg.feature_runtime import FeatureEntityRuntime, setup_feature_entities
from custom_components.my_lg.coordinator_wideq import WideqCoordinator
from custom_components.my_lg.device_identity import PatDeviceIdentity
from tests.test_coordinator_wideq import FakeClient, FakeLimiter


class API:
    def __init__(self):
        self.reply = None
        self.calls = 0
        self.started = asyncio.Event()
        self.release = None

    async def async_get_device_status(self, device_id):
        self.calls += 1
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.reply is None:
            raise RuntimeError('NOT_CONNECTED_DEVICE during boot')
        return self.reply

    async def async_get_device_profile(self, device_id):
        return {'property': {}}

    async def async_post_device_control(self, *args):
        raise AssertionError('Startup recovery must never operate a device')


class StartupRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='lg-startup-recovery-')
        self.hass = HomeAssistant(self.tmp.name)
        loader.async_setup(self.hass)
        frame.async_setup(self.hass)
        device_registry.async_setup(self.hass)
        await device_registry.async_load(self.hass)
        await entity_registry.async_load(self.hass)
        self.api = API()
        self.data = MyLgData(api=self.api)
        self.entry = SimpleNamespace(runtime_data=self.data, options={})
        self.runtime = FeatureEntityRuntime(self.hass, self.entry, Path(self.tmp.name)/'absent.sqlite3')
        self.data.feature_runtime = self.runtime
        self.components = {}
        self.tasks = []

    async def asyncTearDown(self):
        self.runtime.close()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        for component in self.components.values():
            for entity in list(component.entities):
                await entity.async_remove()
        await self.hass.async_stop(force=True)
        self.tmp.cleanup()

    async def device(self, device_type, model='TEST_MODEL'):
        coord = PatDeviceCoordinator(self.hass, None, self.api, {
            'deviceId': '1'*64,
            'deviceInfo': {'alias': 'Startup test', 'modelName': model, 'deviceType': device_type},
        })
        coord.profile = {'property': {}}
        self.data.coordinators[coord.device_id] = coord
        await coord.async_refresh()
        return coord

    async def platform(self, domain):
        component = EntityComponent(logging.getLogger(__name__), domain, self.hass)
        self.components[domain] = component
        module = {'number': number, 'sensor': sensor}[domain]
        def add(entities):
            self.tasks.append(self.hass.async_create_task(component.async_add_entities(entities)))
        with patch('custom_components.my_lg.feature_runtime.async_get_current_platform', return_value=component):
            setup_feature_entities(self.entry, domain, lambda: module._build_entities(self.entry), add)
        await self.hass.async_block_till_done()
        return component

    async def test_fridge_compartments_appear_after_failed_boot_and_late_reply(self):
        coord = await self.device('DEVICE_REFRIGERATOR')
        component = await self.platform('number')
        self.assertEqual(len(component.entities), 0)
        self.api.reply = {'temperature': [
            {'locationName': 'FRIDGE', 'targetTemperature': 3},
            {'locationName': 'FREEZER', 'targetTemperature': -17},
        ]}
        await coord.async_refresh()
        await self.hass.async_block_till_done()
        self.assertEqual(len(component.entities), 2)
        self.assertEqual({e.native_value for e in component.entities}, {3, -17})

    async def test_styler_total_appears_without_reload_after_late_reply(self):
        coord = await self.device('DEVICE_STYLER')
        component = await self.platform('sensor')
        self.api.reply = {'runState': {'currentState': 'POWER_OFF'},
                          'timer': {'totalHour': 0, 'totalMinute': 18}}
        await coord.async_refresh()
        await self.hass.async_block_till_done()
        found = [e for e in component.entities if e.unique_id.endswith('_styler_total')]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].native_value, 18)

    async def test_water_state_appears_without_reload_after_late_reply(self):
        coord = await self.device('DEVICE_WATER_PURIFIER')
        component = await self.platform('sensor')
        self.api.reply = {'runState': {'cockState': 'NORMAL', 'sterilizingState': 'OFF'}}
        await coord.async_refresh()
        await self.hass.async_block_till_done()
        self.assertTrue(any(e.unique_id.endswith('_sterilizing_state') for e in component.entities))

    async def test_partial_push_does_not_finish_initial_snapshot_recovery(self):
        coord = await self.device('DEVICE_REFRIGERATOR')
        component = await self.platform('number')
        coord.handle_mqtt_status({'doorStatus': [{'doorState': 'CLOSE'}]})
        self.api.reply = {'temperature': [{'locationName': 'FRIDGE', 'targetTemperature': 3}]}
        await coord.async_refresh()
        await self.hass.async_block_till_done()
        self.assertEqual(len(component.entities), 1)
        self.assertEqual(coord.get('doorStatus'), [{'doorState': 'CLOSE'}])

    async def test_unload_prevents_late_entity_registration(self):
        coord = await self.device('DEVICE_REFRIGERATOR')
        component = await self.platform('number')
        self.runtime.close()
        self.api.reply = {'temperature': [{'locationName': 'FRIDGE', 'targetTemperature': 3}]}
        await coord.async_refresh()
        await self.hass.async_block_till_done()
        self.assertEqual(len(component.entities), 0)

    async def test_repeated_updates_preserve_entities_and_user_disables(self):
        from tests.test_feature_runtime import MemoryPlatform
        coord = await self.device('DEVICE_REFRIGERATOR')
        platform = MemoryPlatform()
        self.runtime.register('number', platform, lambda: number._build_entities(self.entry), [])
        self.api.reply = {'temperature': [{'locationName': 'FRIDGE', 'targetTemperature': 3}]}
        # HA's registry applies disables when async_add_entities is called.
        coord.data = self.api.reply
        unique_id = number._build_entities(self.entry)[0].unique_id
        coord.data = None
        platform.user_disabled.add(unique_id)
        await coord.async_refresh()
        await self.hass.async_block_till_done()
        self.assertEqual(platform.added, [unique_id])
        self.assertEqual(platform.entities, {})
        await coord.async_refresh()
        await self.hass.async_block_till_done()
        self.assertEqual(platform.added, [unique_id])
        self.assertEqual(platform.removed, [])

    @unittest.skipUnless(hasattr(startup, 'async_recover_startup'), 'Recovery runner is absent in baseline')
    async def test_profile_timeout_does_not_prevent_status_recovery(self):
        coord = await self.device('DEVICE_REFRIGERATOR')
        coord.profile = None
        component = await self.platform('number')
        async def blocked_profile(_device_id):
            await asyncio.Event().wait()
        self.api.async_get_device_profile = blocked_profile
        self.api.reply = {'temperature': [{'locationName': 'FRIDGE', 'targetTemperature': 3}]}
        await startup.async_recover_startup(self.data, delays=(0,), call_timeout=0.01)
        await self.hass.async_block_till_done()
        self.assertEqual(len(component.entities), 1)
        self.assertTrue(coord.startup_status_ready)

    @unittest.skipUnless(hasattr(startup, 'async_recover_startup'), 'Recovery runner is absent in baseline')
    async def test_shared_wideq_warmup_uses_limiter_once_without_changing_cadence(self):
        client, limiter = FakeClient(), FakeLimiter()
        self.data.wideq_coordinator = WideqCoordinator(
            self.hass, None, client, limiter, lambda: 1800,
            pat_devices={'device': PatDeviceIdentity('device', 'Device', 'MODEL')},
        )
        self.assertEqual(client.poll_calls, 0)
        await startup.async_recover_startup(self.data, delays=(0, 0, 0))
        self.assertEqual(client.poll_calls, 1)
        self.assertEqual(limiter.calls, 1)
        self.assertEqual(client.control_calls, 0)
        self.assertEqual(self.data.wideq_coordinator.snapshot_for('device'), {'value': 1})
        self.assertEqual(self.data.wideq_coordinator.update_interval.total_seconds(), 1800)

    @unittest.skipUnless(hasattr(startup, 'async_recover_startup'), 'Recovery runner is absent in baseline')
    async def test_missing_reply_has_bounded_timeout(self):
        await self.device('DEVICE_REFRIGERATOR')
        self.api.release = asyncio.Event()
        await asyncio.wait_for(
            startup.async_recover_startup(self.data, delays=(0, 0), call_timeout=0.01), 1
        )
        self.assertEqual(self.api.calls, 3)

    @unittest.skipUnless(hasattr(startup, 'async_recover_startup'), 'Recovery runner is absent in baseline')
    async def test_bounded_recovery_refreshes_failed_device_but_keeps_normal_interval(self):
        coord = await self.device('DEVICE_REFRIGERATOR')
        component = await self.platform('number')
        self.api.reply = {'temperature': [{'locationName': 'FRIDGE', 'targetTemperature': 3}]}
        await startup.async_recover_startup(self.data, delays=(0, 0, 0), call_timeout=1)
        await self.hass.async_block_till_done()
        self.assertEqual(len(component.entities), 1)
        self.assertEqual(self.api.calls, 2)
        self.assertEqual(coord.update_interval.total_seconds(), 3600)

    @unittest.skipUnless(hasattr(startup, 'async_recover_startup'), 'Recovery runner is absent in baseline')
    async def test_failed_reads_retry_only_bounded_times(self):
        await self.device('DEVICE_REFRIGERATOR')
        await startup.async_recover_startup(self.data, delays=(0, 0, 0), call_timeout=1)
        self.assertEqual(self.api.calls, 4)

    @unittest.skipUnless(hasattr(startup, 'async_recover_startup'), 'Recovery runner is absent in baseline')
    async def test_cancelled_startup_stops_a_delayed_read(self):
        await self.device('DEVICE_REFRIGERATOR')
        self.api.started.clear()
        self.api.release = asyncio.Event()
        task = asyncio.create_task(startup.async_recover_startup(self.data, delays=(0, 0), call_timeout=1))
        await asyncio.wait_for(self.api.started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.api.calls, 2)
