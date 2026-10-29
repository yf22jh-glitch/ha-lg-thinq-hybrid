"""Regression tests for bounded MQTT subscription setup."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import unittest

from custom_components.my_lg.mqtt import MyLgMqtt


class _Api:
    def __init__(self, delay: float = 0) -> None:
        self.delay = delay
        self.active = 0
        self.maximum = 0
        self.event_calls = 0
        self.push_calls = 0
        self.lifecycle_calls = 0
        self.lifecycle_delete_calls = 0
        self.client_delete_calls = 0

    async def _work(self) -> None:
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.active -= 1

    async def async_post_event_subscribe(self, device_id: str) -> None:
        self.event_calls += 1
        await self._work()

    async def async_post_push_subscribe(self, device_id: str) -> None:
        self.push_calls += 1
        await self._work()

    async def async_post_push_devices_subscribe(self) -> None:
        self.lifecycle_calls += 1

    async def async_delete_push_devices_subscribe(self) -> None:
        self.lifecycle_delete_calls += 1

    async def async_delete_client_register(self, payload) -> None:
        self.client_delete_calls += 1


class _MqttClient:
    def __init__(self, api, client_id, callback) -> None:
        self.api = api
        self.callback = callback
        self.disconnect_calls = 0

    async def async_init(self) -> None:
        return None

    async def async_prepare_mqtt(self) -> None:
        return None

    async def async_connect_mqtt(self) -> None:
        return None

    async def async_disconnect(self) -> None:
        self.disconnect_calls += 1
        await self.api.async_delete_client_register(payload={})


class MqttStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_disconnects_once_and_drops_already_queued_messages(self):
        hass = SimpleNamespace(loop=asyncio.get_running_loop())
        callback = Mock()
        manager = MyLgMqtt(hass, _Api(), 'client', {'test': SimpleNamespace(handle_mqtt_status=callback)})
        client = _MqttClient(manager.api, 'client', manager._on_message)
        manager._client = client
        payload = json.dumps({'pushType': 'DEVICE_STATUS', 'deviceId': 'test', 'report': {'x': 1}}).encode()
        manager._on_message(payload)
        await manager.async_shutdown()
        await asyncio.gather(manager.async_shutdown(), manager.async_shutdown())
        manager._on_message(payload)
        await asyncio.sleep(0)
        self.assertEqual(client.disconnect_calls, 1)
        callback.assert_not_called()

    async def test_callback_racing_with_loop_close_is_ignored_but_other_runtime_errors_are_not(self):
        loop = Mock()
        loop.is_closed.side_effect = [False, True]
        loop.call_soon_threadsafe.side_effect = RuntimeError('Event loop is closed')
        manager = MyLgMqtt(SimpleNamespace(loop=loop), _Api(), 'client', {}, on_lifecycle=Mock())
        payload = json.dumps({'pushType': 'DEVICE_REGISTERED', 'deviceId': 'test'}).encode()
        manager._on_message(payload)
        loop.is_closed.side_effect = None
        loop.is_closed.return_value = False
        loop.call_soon_threadsafe.side_effect = RuntimeError('unexpected scheduler failure')
        with self.assertRaisesRegex(RuntimeError, 'unexpected scheduler'):
            manager._on_message(payload)

    async def test_slow_sdk_disconnect_is_bounded_and_late_callbacks_stay_fenced(self):
        async def slow():
            await asyncio.sleep(60)
        manager = MyLgMqtt(SimpleNamespace(loop=asyncio.get_running_loop()), _Api(), 'client', {})
        manager._client = SimpleNamespace(async_disconnect=AsyncMock(side_effect=slow))
        with patch('custom_components.my_lg.mqtt.MQTT_SHUTDOWN_CALL_TIMEOUT', .01):
            await asyncio.wait_for(manager.async_shutdown(), .5)
        self.assertTrue(manager._stopping)
        self.assertIsNone(manager._client)

    async def test_paused_rest_registration_still_disconnects_actual_sdk_socket(self):
        from concurrent.futures import Future
        from custom_components.my_lg.cloud_runtime import CloudGate, GuardedCloudApi
        gate=CloudGate('pat',lambda _:None)
        api=GuardedCloudApi(_Api(),gate)
        manager=MyLgMqtt(SimpleNamespace(loop=asyncio.get_running_loop()),api,'client',{})
        client=_MqttClient(api,'client',manager._on_message)
        done=Future(); done.set_result(None)
        client._mqtt_connection=SimpleNamespace(disconnect=Mock(return_value=done))
        manager._client=client; gate.block('authentication')
        await manager.async_shutdown()
        client._mqtt_connection.disconnect.assert_called_once()
        self.assertEqual(api.wrapped.client_delete_calls,0)

    async def test_device_subscriptions_are_bounded(self) -> None:
        api = _Api(delay=0.005)
        manager = MyLgMqtt(
            None,
            api,
            "client",
            {str(index): object() for index in range(9)},
        )
        semaphore = asyncio.Semaphore(3)

        await asyncio.gather(
            *(
                manager._async_subscribe_device(device_id, semaphore)
                for device_id in manager.coordinators
            )
        )

        self.assertEqual(api.maximum, 3)
        self.assertEqual(api.event_calls, 9)
        self.assertEqual(api.push_calls, 9)
        self.assertEqual(len(manager._event_subscribed), 9)

    async def test_subscription_timeout_is_isolated_to_one_call(self) -> None:
        api = _Api(delay=0.02)
        manager = MyLgMqtt(None, api, "client", {"device": object()})

        with patch("custom_components.my_lg.mqtt.MQTT_SUBSCRIBE_CALL_TIMEOUT", 0.001):
            await manager._async_subscribe_device("device", asyncio.Semaphore(1))

        self.assertEqual(manager._subscription_timeouts, 2)
        self.assertEqual(manager._event_subscribed, [])

    async def test_account_lifecycle_subscription_and_cleanup_are_bounded(self) -> None:
        api = _Api()
        events: list[dict] = []
        manager = MyLgMqtt(None, api, "client", {}, on_lifecycle=events.append)

        with patch("thinqconnect.ThinQMQTTClient", _MqttClient):
            self.assertTrue(await manager.async_start())

        self.assertEqual(api.lifecycle_calls, 1)
        self.assertEqual(events, [{"kind": "subscription_ready"}])

        await manager.async_stop()
        self.assertEqual(api.lifecycle_delete_calls, 1)
        self.assertEqual(api.client_delete_calls, 1)

    async def test_account_lifecycle_message_is_sanitized_on_event_loop(self) -> None:
        api = _Api()
        events: list[dict] = []
        hass = SimpleNamespace(loop=asyncio.get_running_loop())
        manager = MyLgMqtt(hass, api, "client", {}, on_lifecycle=events.append)

        manager._on_message(
            json.dumps(
                {
                    "pushType": "DEVICE_REGISTERED",
                    "deviceId": "device-1",
                    "deviceInfo": {"alias": "Study AC", "macAddress": "private"},
                }
            ).encode()
        )
        await asyncio.sleep(0)

        self.assertEqual(events[0]["kind"], "registered")
        self.assertEqual(events[0]["deviceId"], "device-1")
        self.assertNotIn("Study AC", repr(events[0]))
        self.assertNotIn("private", repr(events[0]))


if __name__ == "__main__":
    unittest.main()
