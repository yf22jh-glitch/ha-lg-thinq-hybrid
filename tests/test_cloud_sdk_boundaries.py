"""Additional regression tests using the installed SDK's blocking boundary."""
import asyncio
from concurrent.futures import Future
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock
from custom_components.my_lg.mqtt import MyLgMqtt
from tests.test_cloud_isolation import CloudRuntimeTests

class PatResumeTests(CloudRuntimeTests):
    async def test_fresh_mqtt_recovers_pat_even_when_rest_has_not_recovered(self):
        self.coordinator.handle_mqtt_status({'power':'old'})
        await self.runtime.async_pause(('pat',))
        self.coordinator.handle_mqtt_status({'power':'late-old'})
        self.assertEqual(self.coordinator.data['power'],'old')
        await self.runtime.async_resume(('pat',))
        self.coordinator.handle_mqtt_status({'power':'new'})
        self.assertEqual(self.coordinator.data['power'],'new')
        self.assertFalse(self.coordinator.cloud_paused)

class SdkLoopIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocking_sdk_does_not_stop_ha_loop_and_cancelled_late_socket_closes(self):
        started=threading.Event(); release=threading.Event(); closed=threading.Event()
        done=Future();done.set_result(None)
        connection=SimpleNamespace(disconnect=Mock(side_effect=lambda:(closed.set(),done)[1]))
        client=SimpleNamespace(_mqtt_connection=None)
        async def sdk():
            started.set()
            release.wait(2)  # actual blocking SDK behavior, not asyncio.sleep
            client._mqtt_connection=connection
        client.async_connect_mqtt=sdk
        manager=MyLgMqtt(None,AsyncMock(),'fixture',{})
        manager._client=client
        task=asyncio.create_task(manager._async_connect_sdk())
        begin=time.monotonic()
        while not started.is_set(): await asyncio.sleep(.005)
        await asyncio.sleep(.02)
        self.assertLess(time.monotonic()-begin,.5,'SDK blocked the HA event loop')
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        release.set()
        async with asyncio.timeout(1):
            while not closed.is_set(): await asyncio.sleep(.005)
        connection.disconnect.assert_called_once()

class InstalledPatSdkErrors(unittest.TestCase):
    def test_real_sdk_token_and_terms_codes_are_separate_from_device_permissions(self):
        from thinqconnect.thinq_api import ThinQAPIException
        from custom_components.my_lg.cloud_runtime import authentication_failure
        for code in ('1103','1218','1302','1304'):
            self.assertTrue(authentication_failure(ThinQAPIException(code,'fixture',{})),code)
        class NotLoggedInError(Exception): pass
        self.assertFalse(authentication_failure(NotLoggedInError('renewable session expiry')))
        for code in ('1308','1222','2301','1306','2000'):
            self.assertFalse(authentication_failure(ThinQAPIException(code,'fixture',{})),code)

if __name__=='__main__': unittest.main()
