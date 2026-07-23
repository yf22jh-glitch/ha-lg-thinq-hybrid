"""Regression tests for bounded PAT startup preparation."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "my_lg"
    / "startup.py"
)
SPEC = importlib.util.spec_from_file_location("my_lg_startup_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
startup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = startup
SPEC.loader.exec_module(startup)


class _Tracker:
    def __init__(self) -> None:
        self.active = 0
        self.maximum = 0

    async def work(self, delay: float) -> None:
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            await asyncio.sleep(delay)
        finally:
            self.active -= 1


class _Coordinator:
    def __init__(
        self,
        device_id: str,
        tracker: _Tracker,
        *,
        delay: float = 0,
        profile_ready: bool = True,
    ) -> None:
        self.device_id = device_id
        self.alias = device_id
        self.last_update_success = False
        self._tracker = tracker
        self._delay = delay
        self._profile_ready = profile_ready

    async def async_refresh(self) -> None:
        await self._tracker.work(self._delay)
        self.last_update_success = True

    async def async_load_profile(self) -> bool:
        await self._tracker.work(self._delay)
        return self._profile_ready


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_preparation_has_bounded_device_concurrency(self) -> None:
        tracker = _Tracker()
        coordinators = [
            _Coordinator(str(index), tracker, delay=0.005) for index in range(6)
        ]

        results, metrics = await startup.async_prepare_coordinators(
            coordinators,
            concurrency=2,
            call_timeout=1,
        )

        self.assertEqual(set(results), {"0", "1", "2", "3", "4", "5"})
        self.assertEqual(tracker.maximum, 2)
        self.assertEqual(metrics.status_ready, 6)
        self.assertEqual(metrics.profile_ready, 6)

    async def test_one_timeout_does_not_abort_other_devices(self) -> None:
        tracker = _Tracker()
        coordinators = [
            _Coordinator("slow", tracker, delay=0.03),
            _Coordinator("ready", tracker),
        ]

        results, metrics = await startup.async_prepare_coordinators(
            coordinators,
            concurrency=2,
            call_timeout=0.005,
        )

        self.assertTrue(results["slow"].status_timed_out)
        self.assertTrue(results["slow"].profile_timed_out)
        self.assertTrue(results["ready"].status_ready)
        self.assertTrue(results["ready"].profile_ready)
        self.assertEqual(metrics.status_timeouts, 1)
        self.assertEqual(metrics.profile_timeouts, 1)

    async def test_invalid_policy_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await startup.async_prepare_coordinators(
                [], concurrency=0, call_timeout=1
            )
        with self.assertRaises(ValueError):
            await startup.async_prepare_coordinators(
                [], concurrency=1, call_timeout=0
            )


if __name__ == "__main__":
    unittest.main()
