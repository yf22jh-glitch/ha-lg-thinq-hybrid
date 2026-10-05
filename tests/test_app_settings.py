"""DB-driven settings and display preferences do not send appliance commands."""
import json
from datetime import time
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import unittest

from homeassistant.core import HomeAssistant
from custom_components.my_lg.app_settings import AppSettingsCoordinator, load_app_settings
from custom_components.my_lg.app_setting_entity import app_setting_entities
from custom_components.my_lg.feature_database import feature_database_token
from custom_components.my_lg.temperature_presentation import TemperaturePresentationMixin
from custom_components.my_lg.local_control_entity import _exact_select_readback
from custom_components.my_lg.wideq_client import WideqClient
from scripts.install_app_settings import definitions, install


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hass=HomeAssistant(self.temp.name)
        self.metadata=SimpleNamespace(device_id='test-ac',model='CST_570004_WW',alias='Test',device_type='AIR_CONDITIONER')
        self.entry=SimpleNamespace(entry_id='test', async_on_unload=Mock(), runtime_data=SimpleNamespace(
            coordinators={'test-ac':self.metadata},wideq_coordinator=None,local_providers={}))
        defs=tuple(dict(model_id=m,feature_id=f,**d) for m,f,d in definitions())
        self.coordinator=AppSettingsCoordinator(self.hass,self.entry,defs)
        self.coordinator.store=SimpleNamespace(async_load=AsyncMock(return_value=None),async_save=AsyncMock())
        self.coordinator.data={}
        self.entry.runtime_data.app_settings=self.coordinator

    async def test_preference_persists_without_cloud_or_appliance_io(self):
        await self.coordinator.async_restore()
        await self.coordinator.async_set(self.metadata,'display.temperature_format','1℃')
        self.assertEqual(self.coordinator.temperature_format('test-ac'),'1℃')
        self.coordinator.store.async_save.assert_awaited_once_with({'test-ac':{'display.temperature_format':'1℃'}})
        self.assertEqual(self.coordinator.data,{})
        self.coordinator.store.async_save.side_effect=OSError('storage unavailable')
        with self.assertRaises(OSError):
            await self.coordinator.async_set(self.metadata,'display.temperature_format','1℉')
        self.assertEqual(self.coordinator.temperature_format('test-ac'),'1℃')

    async def test_display_sensor_uses_local_values_and_never_renders_auto_carrier_as_degrees(self):
        values={'temperature.current_c':25,'temperature.target_c':23.5,'operation.mode':'cool','fan.mode':'low'}
        self.entry.runtime_data.local_providers['test-ac']=SimpleNamespace(
            field_value=values.get,semantic_field_available=lambda key:key in values)
        entity=app_setting_entities(self.entry,'sensor')[0]
        self.assertEqual(entity.native_value,'현재 25℃ / 설정 23.5℃')
        await self.coordinator.async_set(self.metadata,'display.temperature_format','1℉')
        self.assertEqual(entity.native_value,'현재 77℉ / 설정 74℉')
        values['operation.mode']='auto'
        values['temperature.target_c']=2
        self.assertEqual(entity.native_value,'현재 77℉ / 설정 —')
        self.assertEqual(values['temperature.target_c'],2)

    async def test_climate_preference_changes_only_step_not_native_units_or_writes(self):
        class Base:
            target_temperature_step=0.5
            temperature_unit='°C'
            current_temperature=25
        class Climate(TemperaturePresentationMixin,Base): pass
        climate=Climate()
        climate.configure_temperature_presentation(self.coordinator,'test-ac')
        self.assertEqual(climate.target_temperature_step,0.5)
        await self.coordinator.async_set(self.metadata,'display.temperature_format','1℃')
        self.assertEqual(climate.target_temperature_step,1)
        await self.coordinator.async_set(self.metadata,'display.temperature_format','1℉')
        self.assertEqual((climate.temperature_unit,climate.current_temperature,climate.target_temperature_step),('°C',25,0.5))

    async def test_disabled_definition_cannot_be_executed(self):
        await self.coordinator.async_set(self.metadata,'display.temperature_format','1℃')
        self.coordinator.definitions=()
        self.assertIsNone(self.coordinator.temperature_format('test-ac'))
        self.assertEqual(app_setting_entities(self.entry,'select'),[])
        with self.assertRaisesRegex(Exception,'비활성화'):
            await self.coordinator.async_set(self.metadata,'display.temperature_format','1℃')

    async def test_night_mode_entities_share_confirmed_cache_and_db_availability(self):
        from custom_components.my_lg.night_mode import NightModeSaved
        metadata = SimpleNamespace(device_id='fridge', model='2REFO1DBN3K_U', alias='Fridge')
        state = NightModeSaved('CUSTOM', 40, '21:00', '06:00')
        wideq = SimpleNamespace(config_entry=None, night_mode_for=Mock(return_value=state),
                                async_set_night_mode_setting=AsyncMock(), circuit_open=False)
        self.entry.runtime_data.coordinators = {'fridge':metadata}
        self.entry.runtime_data.wideq_coordinator = wideq
        times = app_setting_entities(self.entry, 'time')
        self.assertEqual(len(times), 2)
        start = next(x for x in times if x._spec['feature_id']=='night_mode.start_time')
        self.assertTrue(start.available)
        self.assertEqual(start.native_value, time(21))
        await start.async_set_value(time(21, 1))
        wideq.async_set_night_mode_setting.assert_awaited_once_with('fridge',expected=state,
                                                                 feature='start_time',value='21:01')
        self.assertEqual(start.native_value, time(21))  # Never optimistic.
        with self.assertRaises(Exception):
            await start.async_set_value(time(21, 1, 1))
        wideq.night_mode_for.return_value = NightModeSaved('OFF',40,'21:00','06:00')
        self.assertFalse(start.available)
        self.assertIsNone(start.native_value)
        with self.assertRaises(Exception):
            await start.async_set_value(time(21, 1))
        mode = next(x for x in app_setting_entities(self.entry,'select') if x._spec['feature_id']=='night_mode.mode')
        self.assertEqual(mode.current_option,'꺼짐')
        self.assertEqual(mode.extra_state_attributes['value_source'],'thinq-server')
        self.coordinator.definitions=()
        with self.assertRaisesRegex(Exception,'비활성화'):
            await mode.async_select_option('사용자 지정')

    async def test_db_disable_enable_and_label_edit_reconcile_live_without_restart(self):
        from custom_components.my_lg.feature_database import create_database
        from custom_components.my_lg.feature_runtime import FeatureEntityRuntime
        from scripts.manage_local_features import current_definitions
        from tests.test_feature_runtime import MemoryPlatform
        path=Path(self.temp.name)/'features.sqlite3'
        create_database(path,current_definitions())
        install(path,True)
        self.coordinator.definitions=load_app_settings(path)
        self.entry.options={}
        data=self.entry.runtime_data
        data.local_read_providers={}
        data.local_control_entity_contract=None
        data.local_control_binding_eligibility={}
        data.local_control_composite_domain_contract=None
        data.local_disabled_controls=frozenset()
        data.local_disabled_reads=frozenset()
        self.coordinator.async_request_refresh=AsyncMock()
        runtime=FeatureEntityRuntime(self.hass,self.entry,path)
        platform=MemoryPlatform()
        build=lambda:app_setting_entities(self.entry,'select')
        initial=build()
        runtime.register('select',platform,build,initial)
        await platform.async_add_entities(initial)
        ids=dict(platform.registry_ids)
        with sqlite3.connect(path) as c:
            c.execute("UPDATE app_settings SET enabled=0 WHERE feature_id='display.temperature_format'")
        await runtime.async_refresh()
        self.assertFalse(platform.entities)
        with sqlite3.connect(path) as c:
            c.execute("UPDATE app_settings SET enabled=1,definition_json=json_set(definition_json,'$.label_ko','My temperature') WHERE feature_id='display.temperature_format'")
        await runtime.async_refresh()
        self.assertEqual(platform.registry_ids,ids)
        self.assertEqual(next(iter(platform.entities.values())).name,'My temperature')
        await self.hass.async_block_till_done()

    async def test_pairing_rechecks_conditions_before_any_write(self):
        metadata=SimpleNamespace(device_id='test-st',model='ST_R_ETH01Y_')
        wideq=SimpleNamespace(circuit_open=False,wideq_device_id=lambda _: 'styler-one',
            async_pairing_context=AsyncMock(return_value={}),async_web_setting_request=AsyncMock())
        self.entry.runtime_data.wideq_coordinator=wideq
        self.coordinator.data={'test-st':{'smart_pairing':'washer-one'}}
        with self.assertRaisesRegex(Exception,'잠금 해제'):
            await self.coordinator.async_set(metadata,'smart_pairing','none')
        wideq.async_pairing_context.assert_awaited_once()
        wideq.async_web_setting_request.assert_not_awaited()

    async def test_relationship_transport_does_not_retry_error_ack(self):
        from custom_components.my_lg.wideq.core_async import Session
        response=SimpleNamespace(status=200)
        transport=Mock()
        transport.request.return_value.__aenter__=AsyncMock(return_value=response)
        transport.request.return_value.__aexit__=AsyncMock(return_value=False)
        core=SimpleNamespace(_get_session=lambda:transport,_thinq2_headers=Mock(return_value={}),
            _get_client_id=Mock(return_value='test'),_country='KR',_language='ko-KR',_timeout=None,
            _get_json_resp=AsyncMock(return_value={'resultCode':'9006'}))
        session=Session(SimpleNamespace(gateway=SimpleNamespace(core=core,thinq2_uri='https://example.test/'),
            access_token='synthetic',user_number='synthetic'))
        with self.assertRaises(Exception):
            await session.account_write2('POST','service/devices/styler-one/pairing-devices/washer-one',{})
        transport.request.assert_called_once()

    async def test_account_client_does_not_offer_device_removal_or_generic_control(self):
        client=WideqClient.__new__(WideqClient)
        for method,path in [('DELETE','service/devices/test-device'),('POST','service/devices/test-device/control-sync'),
                            ('POST','service/users/push/config'),('GET','https://example.test/steal')]:
            with self.assertRaises(ValueError): await client.async_web_setting_request(method,path,{})

    async def test_dehumidifier_cached_read_label_matches_existing_control(self):
        self.assertEqual(_exact_select_readback('running only',('always','operation_only'),
                         'DHUM_056905_WW','air_quality.monitor_mode'),'operation_only')
        self.assertEqual(_exact_select_readback('running only',('always','operation_only'),
                         'OTHER','air_quality.monitor_mode'),'running only')


class DatabaseTests(unittest.TestCase):
    def test_registration_is_idempotent_and_disable_delete_label_edits_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'db.sqlite3'
            with sqlite3.connect(path) as c:
                c.executescript('PRAGMA application_id=1279739462; PRAGMA user_version=2;'
                    'CREATE TABLE features(channel,model_id,profile_id,feature_id,platform,definition_json,enabled);'
                    'CREATE TABLE model_rollout(model_id,enabled);')
            initial=feature_database_token(path)
            self.assertEqual(install(path)['status'],'ready')
            self.assertEqual(feature_database_token(path),initial)
            self.assertEqual(install(path,True)['new_definitions'],13)
            self.assertEqual(len(load_app_settings(path)),13)
            registered=feature_database_token(path)
            self.assertNotEqual(initial,registered)
            with sqlite3.connect(path) as c:
                c.execute("UPDATE app_settings SET enabled=0 WHERE feature_id='smart_pairing'")
                c.execute("UPDATE app_settings SET delete_requested=1 WHERE feature_id='display.temperature_summary'")
            self.assertNotEqual(feature_database_token(path),registered)
            self.assertEqual(install(path,True)['status'],'already-installed')
            self.assertEqual(len(load_app_settings(path)),10)


if __name__=='__main__': unittest.main()
