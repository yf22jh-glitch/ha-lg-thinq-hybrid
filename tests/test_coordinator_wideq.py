"""Regression tests for the WideQ outage circuit and request serialization."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
import unittest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

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
        self.energy_calls = 0
        self.poll_error: Exception | None = None
        self.energy_error: Exception | None = None
        self.energy_values = {"today": 1.3, "month": 98.6}
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

    async def async_get_energy_usage(
        self, alias, appliance, *, target_date, before_request
    ):
        self.energy_calls += 1
        await before_request()
        if self.energy_error is not None:
            raise self.energy_error
        return self.energy_values


class FakeStore:
    def __init__(self, data=None) -> None:
        self.data = data
        self.save_calls = 0

    async def async_load(self):
        return self.data

    def async_delay_save(self, data_func, delay=0) -> None:
        self.save_calls += 1
        self.data = data_func()

    async def async_save(self, data) -> None:
        self.save_calls += 1
        self.data = data


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

    async def test_energy_history_uses_separate_cache(self) -> None:
        coordinator = WideqCoordinator(
            self.hass,
            None,
            self.client,
            self.limiter,
            lambda: 600,
            {"device": "aircon"},
        )

        result = await coordinator._async_update_data()

        self.assertEqual(result, self.client.snapshots)
        self.assertEqual(coordinator.energy_history_value("device", "today"), 1.3)
        self.assertEqual(coordinator.energy_history_value("device", "month"), 98.6)
        self.assertEqual(self.client.energy_calls, 1)
        self.assertEqual(self.limiter.calls, 2)

    async def test_energy_history_restores_current_period_without_polling(self) -> None:
        today = dt_util.now().date().isoformat()
        store = FakeStore(
            {
                "items": {
                    "device": {
                        "today": 1.3,
                        "month": 98.6,
                        "period_date": today,
                        "fetched_at": "persisted",
                    }
                }
            }
        )
        coordinator = WideqCoordinator(
            self.hass,
            None,
            self.client,
            self.limiter,
            lambda: 600,
            {"device": "aircon"},
            store,
        )

        await coordinator.async_restore_energy_history()

        self.assertEqual(self.client.poll_calls, 0)
        self.assertEqual(coordinator.energy_history_value("device", "today"), 1.3)
        self.assertEqual(coordinator.energy_history_value("device", "month"), 98.6)
        self.assertTrue(
            coordinator.energy_history_attributes("device")[
                "energy_history_restored"
            ]
        )
        self.assertTrue(
            coordinator.energy_history_attributes("device")[
                "energy_history_stale"
            ]
        )

    async def test_energy_history_rejects_previous_month_cache(self) -> None:
        today = dt_util.now().date()
        previous_month = today - timedelta(days=today.day)
        store = FakeStore(
            {
                "items": {
                    "device": {
                        "today": 9.9,
                        "month": 123.4,
                        "period_date": previous_month.isoformat(),
                        "fetched_at": "stale",
                    }
                }
            }
        )
        coordinator = WideqCoordinator(
            self.hass,
            None,
            self.client,
            self.limiter,
            lambda: 600,
            {"device": "aircon"},
            store,
        )

        await coordinator.async_restore_energy_history()

        self.assertIsNone(coordinator.energy_history_value("device", "today"))
        self.assertIsNone(coordinator.energy_history_value("device", "month"))

    async def test_successful_energy_history_is_persisted(self) -> None:
        store = FakeStore()
        coordinator = WideqCoordinator(
            self.hass,
            None,
            self.client,
            self.limiter,
            lambda: 600,
            {"device": "aircon"},
            store,
        )

        await coordinator._async_update_data()

        self.assertEqual(store.save_calls, 1)
        self.assertEqual(store.data["items"]["device"]["today"], 1.3)
        self.assertEqual(store.data["items"]["device"]["month"], 98.6)

    async def test_energy_history_failure_does_not_fail_snapshot_poll(self) -> None:
        class HttpError(Exception):
            status = 504

        self.client.energy_error = HttpError("maintenance")
        coordinator = WideqCoordinator(
            self.hass,
            None,
            self.client,
            self.limiter,
            lambda: 600,
            {"device": "aircon"},
        )
        coordinator._energy_history["device"] = {
            "today": 1.3,
            "month": 98.6,
            "period_date": dt_util.now().date().isoformat(),
            "fetched_at": "cached",
        }

        result = await coordinator._async_update_data()

        self.assertEqual(result, self.client.snapshots)
        self.assertFalse(coordinator.circuit_open)
        self.assertEqual(coordinator.energy_history_value("device", "today"), 1.3)
        self.assertTrue(
            coordinator.energy_history_attributes("device")["energy_history_stale"]
        )

    async def test_unsupported_energy_history_is_probed_once(self) -> None:
        class UnsupportedError(Exception):
            code = "0005"

        self.client.energy_error = UnsupportedError("unsupported")
        coordinator = WideqCoordinator(
            self.hass,
            None,
            self.client,
            self.limiter,
            lambda: 600,
            {"device": "fridge"},
        )

        await coordinator._async_update_data()
        coordinator._energy_history_next_attempt = None
        await coordinator._async_update_data()

        self.assertEqual(self.client.energy_calls, 1)
        self.assertFalse(
            coordinator.energy_history_attributes("device")[
                "energy_history_supported"
            ]
        )

    async def test_successful_recovery_probe_skips_optional_energy_batch(self) -> None:
        coordinator = WideqCoordinator(
            self.hass,
            None,
            self.client,
            self.limiter,
            lambda: 600,
            {"device": "aircon"},
        )
        coordinator._fail_count = 3

        result = await coordinator._async_update_data()

        self.assertEqual(result, self.client.snapshots)
        self.assertEqual(self.client.energy_calls, 0)
        self.assertFalse(coordinator.circuit_open)


if __name__ == "__main__":
    unittest.main()
