"""Regression tests for bounded MQTT subscription setup."""

from __future__ import annotations

import asyncio
from unittest.mock import patch
import unittest

from custom_components.my_lg.mqtt import MyLgMqtt


class _Api:
    def __init__(self, delay: float = 0) -> None:
        self.delay = delay
        self.active = 0
        self.maximum = 0
        self.event_calls = 0
        self.push_calls = 0

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


class MqttStartupTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
