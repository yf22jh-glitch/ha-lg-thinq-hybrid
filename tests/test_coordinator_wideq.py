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
from custom_components.my_lg.device_identity import PatDeviceIdentity, WideqDeviceData


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
        self.energy_values_by_id: dict[str, dict[str, float] | None] = {}
        self.energy_errors_by_id: dict[str, Exception] = {}
        self.snapshots = [
            WideqDeviceData("wideq-device", "Device", "MODEL", {"value": 1}),
            WideqDeviceData("wideq-one", "One", "MODEL", {"value": 2}),
            WideqDeviceData("wideq-two", "Two", "MODEL", {"value": 3}),
        ]
        self.control_device_ids: list[str] = []
        self.energy_device_ids: list[str] = []
        self.active = 0
        self.max_active = 0

    async def async_get_snapshots(self):
        self.poll_calls += 1
        if self.poll_error is not None:
            raise self.poll_error
        return self.snapshots

    async def async_control(self, device_id, ctrl_key, **kwargs):
        self.control_calls += 1
        self.control_device_ids.append(device_id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)
        self.active -= 1

    async def async_get_energy_usage(
        self, device_id, appliance, *, target_date, before_request
    ):
        self.energy_calls += 1
        self.energy_device_ids.append(device_id)
        await before_request()
        error = self.energy_errors_by_id.get(device_id, self.energy_error)
        if error is not None:
            raise error
        return self.energy_values_by_id.get(device_id, self.energy_values)


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
        self.pat_devices = {
            "device": PatDeviceIdentity("device", "Device", "MODEL"),
            "one": PatDeviceIdentity("one", "One", "MODEL"),
            "two": PatDeviceIdentity("two", "Two", "MODEL"),
        }
        self.coordinator = WideqCoordinator(
            self.hass,
            None,
            self.client,
            self.limiter,
            lambda: 600,
            pat_devices=self.pat_devices,
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
        self.assertEqual(
            result,
            {"device": {"value": 1}, "one": {"value": 2}, "two": {"value": 3}},
        )
        self.assertFalse(self.coordinator.circuit_open)
        self.assertEqual(self.coordinator.update_interval, timedelta(seconds=600))

    async def test_controls_are_serialized_across_devices(self) -> None:
        await asyncio.gather(
            self.coordinator.async_control("one", "basicCtrl"),
            self.coordinator.async_control("two", "basicCtrl"),
        )
        self.assertEqual(self.client.control_calls, 2)
        self.assertEqual(self.client.max_active, 1)
        self.assertCountEqual(
            self.client.control_device_ids, ["wideq-one", "wideq-two"]
        )

    async def test_restored_mapping_allows_control_without_startup_poll(self) -> None:
        mapping_store = FakeStore(
            {"pat_to_wideq": {"device": "wideq-device"}}
        )
        coordinator = WideqCoordinator(
            self.hass,
            None,
            self.client,
            self.limiter,
            lambda: 600,
            pat_devices=self.pat_devices,
            device_map_store=mapping_store,
        )

        await coordinator.async_restore_device_map()
        await coordinator.async_control("device", "basicCtrl")

        self.assertEqual(self.client.poll_calls, 0)
        self.assertEqual(self.client.control_device_ids, ["wideq-device"])

    async def test_duplicate_restored_mapping_is_re_resolved_before_control(self) -> None:
        mapping_store = FakeStore(
            {
                "pat_to_wideq": {
                    "device": "wideq-device",
                    "one": "wideq-device",
                }
            }
        )
        coordinator = WideqCoordinator(
            self.hass,
            None,
            self.client,
            self.limiter,
            lambda: 600,
            pat_devices=self.pat_devices,
            device_map_store=mapping_store,
        )

        await coordinator.async_restore_device_map()
        await coordinator.async_control("one", "basicCtrl")

        self.assertEqual(self.client.poll_calls, 1)
        self.assertEqual(self.client.control_device_ids, ["wideq-one"])

    async def test_first_poll_persists_stable_mapping(self) -> None:
        mapping_store = FakeStore()
        coordinator = WideqCoordinator(
            self.hass,
            None,
            self.client,
            self.limiter,
            lambda: 600,
            pat_devices=self.pat_devices,
            device_map_store=mapping_store,
        )

        await coordinator._async_update_data()

        self.assertEqual(mapping_store.save_calls, 1)
        self.assertEqual(
            mapping_store.data["pat_to_wideq"],
            {
                "device": "wideq-device",
                "one": "wideq-one",
                "two": "wideq-two",
            },
        )

    async def test_energy_history_uses_separate_cache(self) -> None:
        coordinator = WideqCoordinator(
            self.hass,
            None,
            self.client,
            self.limiter,
            lambda: 600,
            {"device": "aircon"},
            pat_devices=self.pat_devices,
        )

        result = await coordinator._async_update_data()

        self.assertEqual(result["device"], {"value": 1})
        self.assertEqual(coordinator.energy_history_value("device", "today"), 1.3)
        self.assertEqual(coordinator.energy_history_value("device", "month"), 98.6)
        self.assertEqual(self.client.energy_calls, 1)
        self.assertEqual(self.client.energy_device_ids, ["wideq-device"])
        self.assertEqual(self.limiter.calls, 2)

    async def test_energy_history_restores_current_period_without_polling(self) -> None:
        today = dt_util.now().date()
        store = FakeStore(
            {
                "schema": 3,
                "items": {
                    "device": {
                        "today": {
                            "value": 1.3,
                            "period": today.isoformat(),
                            "fetched_at": "persisted-today",
                        },
                        "month": {
                            "value": 98.6,
                            "period": today.strftime("%Y-%m"),
                            "fetched_at": "persisted-month",
                        },
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
            pat_devices=self.pat_devices,
        )

        await coordinator.async_restore_energy_history()

        self.assertEqual(self.client.poll_calls, 0)
        self.assertEqual(coordinator.energy_history_value("device", "today"), 1.3)
        self.assertEqual(coordinator.energy_history_value("device", "month"), 98.6)
        self.assertTrue(
            coordinator.energy_history_attributes("device", "today")[
                "energy_history_restored"
            ]
        )
        self.assertTrue(
            coordinator.energy_history_attributes("device", "month")[
                "energy_history_stale"
            ]
        )

    async def test_v2_energy_cache_migrates_positive_values_only(self) -> None:
        today = dt_util.now().date().isoformat()
        stable_store = FakeStore()
        previous_store = FakeStore(
            {
                "items": {
                    "device": {
                        "today": 0,
                        "month": 42.0,
                        "period_date": today,
                        "fetched_at": "v2",
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
            stable_store,
            pat_devices=self.pat_devices,
            previous_energy_history_store=previous_store,
        )

        await coordinator.async_restore_energy_history()

        self.assertIsNone(coordinator.energy_history_value("device", "today"))
        self.assertEqual(coordinator.energy_history_value("device", "month"), 42.0)
        self.assertEqual(stable_store.save_calls, 1)

    async def test_v3_single_period_restore_is_reported_as_partial(self) -> None:
        today = dt_util.now().date()
        store = FakeStore(
            {
                "schema": 3,
                "items": {
                    "device": {
                        "month": {
                            "value": 42.0,
                            "period": today.strftime("%Y-%m"),
                            "fetched_at": "v3",
                        }
                    }
                },
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
            pat_devices=self.pat_devices,
        )

        await coordinator.async_restore_energy_history()

        attrs = coordinator.energy_history_attributes("device", "month")
        self.assertEqual(coordinator.energy_history_value("device", "month"), 42.0)
        self.assertTrue(attrs["energy_history_partial"])
        self.assertEqual(attrs["energy_history_missing_fields"], ["today"])

    async def test_v2_complements_but_never_overwrites_v3_field(self) -> None:
        today = dt_util.now().date()
        stable_store = FakeStore(
            {
                "schema": 3,
                "items": {
                    "device": {
                        "today": {
                            "value": 0.0,
                            "period": today.isoformat(),
                            "fetched_at": "v3",
                        }
                    }
                },
            }
        )
        previous_store = FakeStore(
            {
                "items": {
                    "device": {
                        "today": 9.9,
                        "month": 42.0,
                        "period_date": today.isoformat(),
                        "fetched_at": "v2",
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
            stable_store,
            pat_devices=self.pat_devices,
            previous_energy_history_store=previous_store,
        )

        await coordinator.async_restore_energy_history()

        self.assertEqual(coordinator.energy_history_value("device", "today"), 0.0)
        self.assertEqual(coordinator.energy_history_value("device", "month"), 42.0)
        self.assertEqual(stable_store.save_calls, 1)

    async def test_v3_boolean_energy_value_is_rejected(self) -> None:
        today = dt_util.now().date()
        store = FakeStore(
            {
                "schema": 3,
                "items": {
                    "device": {
                        "today": {
                            "value": True,
                            "period": today.isoformat(),
                            "fetched_at": "bad",
                        }
                    }
                },
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
            pat_devices=self.pat_devices,
        )

        await coordinator.async_restore_energy_history()

        self.assertIsNone(coordinator.energy_history_value("device", "today"))

    async def test_legacy_alias_energy_cache_migrates_to_stable_id(self) -> None:
        today = dt_util.now().date().isoformat()
        stable_store = FakeStore()
        legacy_store = FakeStore(
            {
                "items": {
                    "Device": {
                        "today": 1.5,
                        "month": 42.0,
                        "period_date": today,
                        "fetched_at": "legacy",
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
            stable_store,
            pat_devices=self.pat_devices,
            legacy_energy_history_store=legacy_store,
        )

        await coordinator.async_restore_energy_history()

        self.assertEqual(coordinator.energy_history_value("device", "today"), 1.5)
        self.assertEqual(stable_store.save_calls, 1)
        self.assertIn("device", stable_store.data["items"])

    async def test_energy_history_rejects_previous_month_cache(self) -> None:
        today = dt_util.now().date()
        previous_month = today - timedelta(days=today.day)
        store = FakeStore(
            {
                "schema": 3,
                "items": {
                    "device": {
                        "today": {
                            "value": 9.9,
                            "period": previous_month.isoformat(),
                            "fetched_at": "stale",
                        },
                        "month": {
                            "value": 123.4,
                            "period": previous_month.strftime("%Y-%m"),
                            "fetched_at": "stale",
                        },
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
            pat_devices=self.pat_devices,
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
            pat_devices=self.pat_devices,
        )

        await coordinator._async_update_data()

        self.assertEqual(store.save_calls, 1)
        today = dt_util.now().date()
        self.assertEqual(
            store.data["items"]["device"]["today"]["value"], 1.3
        )
        self.assertEqual(
            store.data["items"]["device"]["today"]["period"],
            today.isoformat(),
        )
        self.assertEqual(
            store.data["items"]["device"]["month"]["value"], 98.6
        )
        self.assertEqual(store.data["schema"], 3)

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
            pat_devices=self.pat_devices,
        )
        today = dt_util.now().date()
        coordinator._energy_history["device"] = {
            "today": {
                "value": 1.3,
                "period": today.isoformat(),
                "fetched_at": "cached",
            },
            "month": {
                "value": 98.6,
                "period": today.strftime("%Y-%m"),
                "fetched_at": "cached",
            },
        }

        result = await coordinator._async_update_data()

        self.assertEqual(result["device"], {"value": 1})
        self.assertFalse(coordinator.circuit_open)
        self.assertEqual(coordinator.energy_history_value("device", "today"), 1.3)
        self.assertTrue(
            coordinator.energy_history_attributes("device", "today")[
                "energy_history_stale"
            ]
        )
        self.assertIn(
            "maintenance",
            coordinator.energy_history_attributes("device", "today")[
                "energy_history_last_error"
            ],
        )

    async def test_partial_energy_response_updates_only_verified_period(self) -> None:
        coordinator = WideqCoordinator(
            self.hass,
            None,
            self.client,
            self.limiter,
            lambda: 600,
            {"device": "aircon"},
            pat_devices=self.pat_devices,
        )
        today = dt_util.now().date()
        coordinator._energy_history["device"] = {
            "today": {
                "value": 1.3,
                "period": today.isoformat(),
                "fetched_at": "old-today",
            },
            "month": {
                "value": 98.6,
                "period": today.strftime("%Y-%m"),
                "fetched_at": "old-month",
            },
        }
        self.client.energy_values = {"month": 99.1}

        await coordinator._async_update_data()

        self.assertEqual(coordinator.energy_history_value("device", "today"), 1.3)
        self.assertEqual(coordinator.energy_history_value("device", "month"), 99.1)
        today_attrs = coordinator.energy_history_attributes("device", "today")
        month_attrs = coordinator.energy_history_attributes("device", "month")
        self.assertTrue(today_attrs["energy_history_stale"])
        self.assertTrue(today_attrs["energy_history_partial"])
        self.assertFalse(month_attrs["energy_history_stale"])
        self.assertTrue(month_attrs["energy_history_partial"])
        self.assertEqual(today_attrs["energy_history_missing_fields"], ["today"])

    async def test_one_device_failure_does_not_stale_successful_device(self) -> None:
        self.client.energy_errors_by_id["wideq-one"] = RuntimeError("bad payload")
        self.client.energy_values_by_id["wideq-two"] = {
            "today": 2.5,
            "month": 40.0,
        }
        coordinator = WideqCoordinator(
            self.hass,
            None,
            self.client,
            self.limiter,
            lambda: 600,
            {"one": "aircon", "two": "aircon"},
            pat_devices=self.pat_devices,
        )

        await coordinator._async_update_data()

        self.assertIsNone(coordinator.energy_history_value("one", "today"))
        self.assertEqual(coordinator.energy_history_value("two", "today"), 2.5)
        self.assertTrue(
            coordinator.energy_history_attributes("one", "today")[
                "energy_history_stale"
            ]
        )
        self.assertFalse(
            coordinator.energy_history_attributes("two", "today")[
                "energy_history_stale"
            ]
        )

    async def test_malformed_energy_payload_does_not_fail_snapshot_poll(self) -> None:
        self.client.energy_values = ["not", "a", "mapping"]
        coordinator = WideqCoordinator(
            self.hass,
            None,
            self.client,
            self.limiter,
            lambda: 600,
            {"device": "aircon"},
            pat_devices=self.pat_devices,
        )

        result = await coordinator._async_update_data()

        self.assertEqual(result["device"], {"value": 1})
        self.assertFalse(coordinator.circuit_open)
        self.assertIsNone(coordinator.energy_history_value("device", "today"))

    async def test_unresolved_identity_keeps_energy_unavailable_and_stale(self) -> None:
        self.client.snapshots = [
            item for item in self.client.snapshots if item.device_id != "wideq-device"
        ]
        coordinator = WideqCoordinator(
            self.hass,
            None,
            self.client,
            self.limiter,
            lambda: 600,
            {"device": "aircon"},
            pat_devices=self.pat_devices,
        )

        result = await coordinator._async_update_data()

        self.assertNotIn("device", result)
        self.assertEqual(self.client.energy_calls, 0)
        self.assertIsNone(coordinator.energy_history_value("device", "today"))
        self.assertTrue(
            coordinator.energy_history_attributes("device", "today")[
                "energy_history_stale"
            ]
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
            pat_devices=self.pat_devices,
        )
        today = dt_util.now().date()
        coordinator._energy_history["device"] = {
            "today": {
                "value": 4.2,
                "period": today.isoformat(),
                "fetched_at": "old",
            }
        }

        await coordinator._async_update_data()
        coordinator._energy_history_next_attempt = None
        await coordinator._async_update_data()

        self.assertEqual(self.client.energy_calls, 1)
        self.assertIsNone(coordinator.energy_history_value("device", "today"))
        self.assertFalse(
            coordinator.energy_history_attributes("device", "today")[
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
            pat_devices=self.pat_devices,
        )
        coordinator._fail_count = 3

        result = await coordinator._async_update_data()

        self.assertEqual(result["device"], {"value": 1})
        self.assertEqual(self.client.energy_calls, 0)
        self.assertFalse(coordinator.circuit_open)


if __name__ == "__main__":
    unittest.main()
