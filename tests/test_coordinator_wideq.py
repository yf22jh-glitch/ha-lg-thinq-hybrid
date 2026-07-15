"""Regression tests for the WideQ outage circuit and request serialization."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
import unittest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.my_lg.const import WIDEQ_PROBE_INTERVAL
from custom_components.my_lg.coordinator_wideq import WideqCoordinator


class FakeLimiter:
    def __init__(self) -> None:
        self.calls = 0

    async def acquire(self) -> None:
        self.calls += 1


class FakeClient:
    def __init__(self) -> None:
        self.poll_calls = 0
        self.control_calls = 0
        self.poll_error: Exception | None = None
        self.snapshots = {"device": {"value": 1}}
        self.active = 0
        self.max_active = 0

    async def async_get_snapshots(self):
        self.poll_calls += 1
        if self.poll_error is not None:
            raise self.poll_error
        return self.snapshots

    async def async_control(self, alias, ctrl_key, **kwargs):
        self.control_calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)
        self.active -= 1


class WideqCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant(str(Path("/tmp/lg-ha-coordinator-test")))
        self.client = FakeClient()
        self.limiter = FakeLimiter()
        self.coordinator = WideqCoordinator(
            self.hass,
            None,
            self.client,
            self.limiter,
            lambda: 600,
        )

    async def test_constructor_does_not_eager_poll(self) -> None:
        self.assertEqual(self.client.poll_calls, 0)
        self.assertEqual(self.limiter.calls, 0)

    async def test_three_failures_open_circuit_and_keep_cached_data(self) -> None:
        cached = {"device": {"value": 7}}
        self.coordinator.data = cached
        self.client.poll_error = RuntimeError("maintenance")
        for expected in range(1, 4):
            with self.assertRaisesRegex(UpdateFailed, f"x{expected}"):
                await self.coordinator._async_update_data()
        self.assertTrue(self.coordinator.circuit_open)
        self.assertIs(self.coordinator.data, cached)
        self.assertEqual(
            self.coordinator.update_interval,
            timedelta(seconds=WIDEQ_PROBE_INTERVAL),
        )
        with self.assertRaises(HomeAssistantError):
            await self.coordinator.async_control("device", "basicCtrl")
        self.assertEqual(self.client.control_calls, 0)

    async def test_successful_probe_closes_circuit(self) -> None:
        self.coordinator._fail_count = 3
        result = await self.coordinator._async_update_data()
        self.assertEqual(result, self.client.snapshots)
        self.assertFalse(self.coordinator.circuit_open)
        self.assertEqual(self.coordinator.update_interval, timedelta(seconds=600))

    async def test_controls_are_serialized_across_devices(self) -> None:
        await asyncio.gather(
            self.coordinator.async_control("one", "basicCtrl"),
            self.coordinator.async_control("two", "basicCtrl"),
        )
        self.assertEqual(self.client.control_calls, 2)
        self.assertEqual(self.client.max_active, 1)


if __name__ == "__main__":
    unittest.main()
