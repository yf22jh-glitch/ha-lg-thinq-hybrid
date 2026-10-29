"""Reproduce SDK Future.result blocking using the actual manager entry point."""
import asyncio
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from custom_components.my_lg.mqtt import MyLgMqtt
from tests.test_mqtt import _Api, _MqttClient

class EventLoopContinuity(unittest.IsolatedAsyncioTestCase):
    async def test_pat_connect_does_not_delay_local_event_loop(self):
        class BlockingSdk(_MqttClient):
            async def async_connect_mqtt(self):time.sleep(.15)
        manager=MyLgMqtt(SimpleNamespace(loop=asyncio.get_running_loop()),_Api(),'fixture',{})
        pulses=[]
        async def local_updates():
            for _ in range(35):
                pulses.append(time.monotonic());await asyncio.sleep(.005)
        with patch('thinqconnect.ThinQMQTTClient',BlockingSdk):
            await asyncio.gather(local_updates(),manager.async_start())
        largest=max(b-a for a,b in zip(pulses,pulses[1:]))
        print(f'local_event_loop_max_gap_seconds={largest:.4f}',flush=True)
        self.assertLess(largest,.08)
        await manager.async_shutdown()
if __name__=='__main__':unittest.main()
