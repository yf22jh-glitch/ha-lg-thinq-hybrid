"""Local HA startup must survive an unavailable cloud account."""
import asyncio
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant import loader
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

import custom_components.my_lg as integration
from custom_components.my_lg.const import DEVICE_TYPE_AIR_PURIFIER


class CloudIsolationSetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.hass = HomeAssistant(self.tmp.name)
        loader.async_setup(self.hass)
        self.hass.config_entries = SimpleNamespace(async_forward_entry_setups=AsyncMock())
        self.entry = ConfigEntry(version=1, minor_version=1, domain='my_lg',
            title='Isolated LG', data={'access_token':'fixture', 'client_id':'fixture'},
            source='user', options={}, unique_id='fixture', discovery_keys={}, subentries_data=())
        self.device = {'deviceId':'fixture-purifier', 'deviceInfo':{
            'alias':'Purifier', 'modelName':'AIR_910604_WW',
            'deviceType':DEVICE_TYPE_AIR_PURIFIER}}
        await Store(self.hass, 1, f'my_lg.device_inventory.{self.entry.entry_id}').async_save({
            'devices':[self.device], 'profiles':{'fixture-purifier':{'property':{}}}})

    async def asyncTearDown(self):
        await self.hass.async_stop(force=True)
        self.tmp.cleanup()

    async def test_cloud_list_failure_still_starts_saved_local_runtime(self):
        api = AsyncMock()
        api.async_get_device_list.side_effect = OSError('isolated WAN unavailable')
        local = AsyncMock()
        with ExitStack() as stack:
            stack.enter_context(patch('thinqconnect.ThinQApi',return_value=api))
            stack.enter_context(patch.object(integration,'async_get_clientsession',return_value=object()))
            stack.enter_context(patch.object(integration,'_setup_local_shadows',local))
            stack.enter_context(patch.object(integration,'_start_local_control'))
            stack.enter_context(patch.object(integration.MyLgMqtt,'async_start',AsyncMock(return_value=False)))
            stack.enter_context(patch.object(integration.MyLgMqtt,'async_shutdown',AsyncMock()))
            stack.enter_context(patch.object(integration,'default_database_path',return_value=Path(self.tmp.name)/'absent.db'))
            stack.enter_context(patch.object(self.hass.config_entries,'async_forward_entry_setups',AsyncMock()))
            stack.enter_context(patch.object(integration,'async_recover_startup',AsyncMock()))
            try:
                ok = await integration.async_setup_entry(self.hass,self.entry)
            except Exception as exc:
                self.fail(f'Cloud failure prevented Local startup: {type(exc).__name__}')
            self.assertTrue(ok)
            local.assert_awaited_once()
            coordinator=self.entry.runtime_data.coordinators['fixture-purifier']
            self.assertEqual(coordinator.model,'AIR_910604_WW')
            self.assertFalse(coordinator.startup_status_ready)
            self.assertFalse(coordinator.data, 'Cached metadata must not invent live cloud values')

from custom_components.my_lg.cloud_runtime import CloudGate, CloudPausedError, CloudRuntime, GuardedCloudApi
from custom_components.my_lg.coordinator import PatDeviceCoordinator
from custom_components.my_lg import MyLgData
from custom_components.my_lg.services import async_register_services
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError


class CloudGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_fences_source_immediately_and_other_source_still_runs(self):
        changed=[]
        pat=CloudGate('pat',changed.append); wideq=CloudGate('wideq',changed.append)
        class InvalidCredentialError(Exception): pass
        failure=AsyncMock(side_effect=InvalidCredentialError('fixture'))
        with self.assertRaises(InvalidCredentialError):
            await wideq.call(failure)
        with self.assertRaises(CloudPausedError):
            await wideq.call(failure)
        self.assertEqual(failure.await_count,1)
        self.assertEqual(changed,['wideq'])
        self.assertEqual(await pat.call(AsyncMock(return_value=42)),42)

    async def test_delayed_response_ignoring_cancellation_cannot_escape_pause_or_resume(self):
        gate=CloudGate('pat',lambda _:None); started=asyncio.Event()
        async def delayed():
            started.set()
            try: await asyncio.Event().wait()
            except asyncio.CancelledError: return {'old':True}
        pending=asyncio.create_task(gate.call(delayed)); await started.wait()
        gate.block('authentication')
        await gate.cancel_pending()
        gate.reason=None; gate.generation+=1
        with self.assertRaises(CloudPausedError): await pending
        self.assertEqual(await gate.call(AsyncMock(return_value={'new':True})),{'new':True})
        self.assertFalse(gate.tasks)

    async def test_timeout_and_appliance_error_do_not_lock_account(self):
        gate=CloudGate('pat',lambda _:self.fail('non-auth must not protect account'))
        for error in (TimeoutError(),OSError('offline'),ValueError('device is not connected')):
            with self.assertRaises(type(error)): await gate.call(AsyncMock(side_effect=error))
            self.assertIsNone(gate.reason)


class CloudRuntimeTests(CloudIsolationSetupTests):
    # Setup-only reproduction is inherited intentionally and exercises both fixtures.
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.data=MyLgData(api=None)
        self.runtime=CloudRuntime(self.hass,self.entry,self.data)
        self.data.cloud=self.runtime; self.entry.runtime_data=self.data
        self.sdk=AsyncMock()
        self.data.api=GuardedCloudApi(self.sdk,self.runtime.gates['pat'])
        self.coordinator=PatDeviceCoordinator(self.hass,self.entry,self.data.api,self.device)
        self.data.coordinators[self.coordinator.device_id]=self.coordinator
        self.data.mqtt=SimpleNamespace(async_shutdown=AsyncMock())
        self.data.wideq_client=SimpleNamespace(async_close=AsyncMock())
        self.data.wideq_coordinator=SimpleNamespace(cloud_paused=False,async_update_listeners=lambda:None)

    async def asyncTearDown(self):
        await self.runtime.async_close()
        await super().asyncTearDown()

    async def test_pause_resume_source_lifecycle_and_durable_reload(self):
        for source in ('pat','wideq'):
            await self.runtime.async_pause((source,), 'authentication')
            other='wideq' if source=='pat' else 'pat'
            self.assertTrue(self.runtime.status()['sources'][source]['paused'])
            self.assertFalse(self.runtime.status()['sources'][other]['paused'])
            restored=CloudRuntime(self.hass,self.entry,self.data)
            await restored.async_restore()
            self.assertTrue(restored.gates[source].reason)
            self.assertFalse(restored.gates[other].reason)
            callback=AsyncMock(); self.runtime.start_source=callback
            await self.runtime.async_resume((source,)); await asyncio.sleep(0)
            await self.runtime.async_resume((source,)); await asyncio.sleep(0)
            callback.assert_awaited_once_with(source)
            self.assertGreater(self.runtime.resumed_at[source],0)
            await restored.async_close()
        self.data.mqtt.async_shutdown.assert_awaited_once()
        self.data.wideq_client.async_close.assert_awaited_once()

    async def test_real_pat_coordinator_fences_late_push_and_command_after_pause(self):
        self.coordinator.handle_mqtt_status({'operation':{'power':'ON'}})
        await self.runtime.async_pause(('pat',))
        self.coordinator.handle_mqtt_status({'operation':{'power':'OFF'}})
        self.assertEqual(self.coordinator.data['operation']['power'],'ON')
        with self.assertRaises(CloudPausedError):
            await self.data.api.async_control_device('fixture',{})
        self.sdk.async_control_device.assert_not_awaited()
        self.assertTrue(self.runtime.status()['local_preserved'])

    async def test_actual_service_dispatch_requires_admin_and_targets_one_source(self):
        self.hass.config_entries.async_get_entry=lambda _:self.entry
        self.hass.auth=SimpleNamespace(async_get_user=AsyncMock(side_effect=lambda uid:SimpleNamespace(is_admin=uid=='admin')))
        async_register_services(self.hass)
        request={'entry_id':self.entry.entry_id,'operation':'pause','source':'wideq'}
        with self.assertRaises(HomeAssistantError):
            await self.hass.services.async_call('my_lg','cloud_connection',request,blocking=True,
                                              context=Context(user_id='ordinary'),return_response=True)
        self.assertFalse(self.runtime.gates['wideq'].reason)
        result=await self.hass.services.async_call('my_lg','cloud_connection',request,blocking=True,
                                              context=Context(user_id='admin'),return_response=True)
        self.assertTrue(result['local_preserved'])
        self.assertTrue(result['sources']['wideq']['paused'])
        self.assertFalse(result['sources']['pat']['paused'])

    async def test_corrupt_inventory_does_not_create_an_unverified_local_identity(self):
        await self.runtime.inventory_store.async_save({'devices':[None,{},self.device,self.device,
            {'deviceId':'bad','deviceInfo':{'modelName':4,'deviceType':'AIR_PURIFIER'}}],
            'profiles':{'bad':{},'fixture-purifier':{'property':{}}}})
        await self.runtime.async_restore()
        self.assertEqual(self.runtime.inventory['devices'],[self.device])
        self.assertEqual(set(self.runtime.inventory['profiles']),{'fixture-purifier'})

    async def test_inventory_preserves_capabilities_but_never_cloud_measurements(self):
        self.coordinator.profile={'property':{'fixture':{}}}
        self.coordinator.data={'humidity':99}
        await self.runtime.async_save_inventory()
        saved=await self.runtime.inventory_store.async_load()
        self.assertNotIn('humidity',repr(saved))
        self.assertEqual(saved['devices'],[self.device])
        self.sdk.async_get_device_profile.side_effect=OSError('offline')
        self.assertFalse(await self.coordinator.async_load_profile())
        self.assertEqual(self.coordinator.profile,{'property':{'fixture':{}}})
