"""Real Paho → local provider → HA entity → recorder, in loopback-only Docker.

Run with LG_ISOLATED_MQTT=1 and an isolated Mosquitto on 127.0.0.1:18883.
No production mounts, credentials or appliance command paths are used.
"""
import asyncio
import logging
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from homeassistant import bootstrap, loader
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.config_entries import ConfigEntry
from custom_components.my_lg import MyLgData
from custom_components.my_lg.binary_sensor import LocalSemanticBinarySensor
from custom_components.my_lg.cloud_runtime import CloudRuntime, GuardedCloudApi
from custom_components.my_lg.coordinator import PatDeviceCoordinator
from custom_components.my_lg.local_mqtt import LocalPilotMqttSubscriber
from custom_components.my_lg.local_provider import LocalWaterTankShadowProvider
from tests.test_local_provider import NOW, BINDING_ID, state_payload, availability_payload, runtime_payload


@unittest.skipUnless(os.environ.get('LG_ISOLATED_MQTT')=='1','requires isolated loopback Mosquitto')
class RealLocalChainTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_updates_are_recorded_while_both_cloud_sources_are_paused(self):
        from paho.mqtt import publish
        with TemporaryDirectory() as directory:
            hass=HomeAssistant(directory); hass.config.skip_pip=True
            loader.async_setup(hass)
            Path(directory,'configuration.yaml').write_text('{}\n')
            db=Path(directory,'history.db')
            config={'homeassistant':{'name':'Isolated LG','latitude':0,'longitude':0,'elevation':0,
                    'time_zone':'UTC','unit_system':'metric','country':'US'},
                    'recorder':{'db_url':f'sqlite:///{db}','commit_interval':1,'auto_purge':False}}
            await bootstrap.async_from_config_dict(config,hass)
            await hass.async_start()
            entry=ConfigEntry(version=1,minor_version=1,domain='my_lg',title='fixture',
                data={},source='user',options={},unique_id='fixture',discovery_keys={},subentries_data=())
            data=MyLgData(api=None); entry.runtime_data=data
            cloud=CloudRuntime(hass,entry,data); data.cloud=cloud
            data.api=GuardedCloudApi(AsyncMock(),cloud.gates['pat'])
            coord=PatDeviceCoordinator(hass,None,data.api,{'deviceId':'fixture-local',
                'deviceInfo':{'alias':'fixture','modelName':'DHUM_056905_WW','deviceType':'DEVICE_DEHUMIDIFIER'}})
            data.coordinators[coord.device_id]=coord
            data.mqtt=SimpleNamespace(async_shutdown=AsyncMock())
            data.wideq_client=SimpleNamespace(async_close=AsyncMock())
            provider=LocalWaterTankShadowProvider(BINDING_ID,now=lambda:NOW)
            subscriber=LocalPilotMqttSubscriber(hass.loop,provider,host='127.0.0.1',port=18883,
                username=f'shadow-{BINDING_ID}',password='isolated-fixture')
            entity=LocalSemanticBinarySensor(provider,coord,'water_tank.full',provider.profile.fields['water_tank.full'])
            entity.entity_id='binary_sensor.isolated_lg_local'
            entity._attr_device_info=None
            entity._attr_entity_registry_enabled_default=True
            component=EntityComponent(logging.getLogger(__name__),'binary_sensor',hass)
            await component.async_add_entities([entity])
            events=[]
            unsub=hass.bus.async_listen('state_changed',lambda event:events.append(event.data)
                if event.data['entity_id']==entity.entity_id else None)
            async def send(topic,payload):
                await hass.async_add_executor_job(lambda:publish.single(topic,payload,hostname='127.0.0.1',port=18883,qos=1,retain=False))
            async def wait_state(expected):
                async with asyncio.timeout(10):
                    while hass.states.get(entity.entity_id).state!=expected:
                        await asyncio.sleep(.05)
            try:
                await subscriber.async_start()
                async with asyncio.timeout(10):
                    while not subscriber._subscriptions_ready: await asyncio.sleep(.05)
                await send(provider.runtime_availability_topic,runtime_payload('online'))
                await send(provider.availability_topic,availability_payload('online'))
                await send(provider.state_topic,state_payload(value=True,sequence=1))
                await wait_state('on'); events.clear()
                await cloud.async_pause(('wideq',),'authentication')
                await send(provider.state_topic,state_payload(value=False,sequence=2))
                await wait_state('off')
                await cloud.async_pause(('pat',),'authentication')
                await send(provider.state_topic,state_payload(value=True,sequence=3))
                await wait_state('on')
                self.assertTrue(subscriber._subscriptions_ready)
                self.assertTrue(entity.available)
                self.assertEqual([e['new_state'].state for e in events],['off','on'])
                self.assertEqual(entity.unique_id,'fixture-local_local_semantic_water_tank.full')
                await asyncio.sleep(2)
            finally:
                unsub(); await cloud.async_close(); await subscriber.async_stop()
                await entity.async_remove(); await hass.async_stop()
            with sqlite3.connect(db) as connection:
                rows=connection.execute("SELECT state FROM states JOIN states_meta USING(metadata_id) WHERE states_meta.entity_id=? ORDER BY state_id",(entity.entity_id,)).fetchall()
            values=[row[0] for row in rows]
            start=values.index('on')
            self.assertEqual(values[start:start+3],['on','off','on'])
