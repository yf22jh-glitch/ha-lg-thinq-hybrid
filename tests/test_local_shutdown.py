"""Local MQTT connections are drained on HA stop, not only entry unload."""

from __future__ import annotations

import asyncio
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant

import custom_components.my_lg as integration


class LocalShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.config_dir = TemporaryDirectory()
        self.addCleanup(self.config_dir.cleanup)
        self.hass = HomeAssistant(self.config_dir.name)
        self.entry = SimpleNamespace(async_on_unload=Mock())

    async def test_ha_stop_drains_connections_once_without_entry_unload(self) -> None:
        runtime = integration.MyLgData(api=object())
        stop = AsyncMock()
        runtime.local_mqtt_subscribers["synthetic-binding"] = SimpleNamespace(
            async_stop=stop,
        )
        integration._register_local_shutdown(self.hass, self.entry, runtime)

        self.hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await self.hass.async_block_till_done()
        self.hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await self.hass.async_block_till_done()

        stop.assert_awaited_once_with()
        self.assertEqual(runtime.local_mqtt_subscribers, {})

    async def test_entry_unload_removes_its_shutdown_listener(self) -> None:
        runtime = integration.MyLgData(api=object())
        stop = AsyncMock()
        runtime.local_mqtt_subscribers["synthetic-binding"] = SimpleNamespace(
            async_stop=stop,
        )
        integration._register_local_shutdown(self.hass, self.entry, runtime)
        cancel = self.entry.async_on_unload.call_args.args[0]
        cancel()

        self.hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await self.hass.async_block_till_done()

        stop.assert_not_awaited()

    async def test_connections_stop_concurrently_within_ha_shutdown(self) -> None:
        runtime = integration.MyLgData(api=object())
        second_started = asyncio.Event()

        async def first_stop():
            await second_started.wait()

        async def second_stop():
            second_started.set()

        first = AsyncMock(side_effect=first_stop)
        second = AsyncMock(side_effect=second_stop)
        runtime.local_mqtt_subscribers.update({
            "synthetic-binding-one": SimpleNamespace(async_stop=first),
            "synthetic-binding-two": SimpleNamespace(async_stop=second),
        })

        await asyncio.wait_for(integration._stop_local_shadows(runtime), 0.5)

        first.assert_awaited_once_with()
        second.assert_awaited_once_with()
        self.assertEqual(runtime.local_mqtt_subscribers, {})


if __name__ == "__main__":
    unittest.main()
