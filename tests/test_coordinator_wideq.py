"""Regression tests for the WideQ outage circuit and request serialization."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

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


class DelayedFakeStore(FakeStore):
    """Store double that exposes HA's deferred payload callback semantics."""

    def __init__(self, data=None) -> None:
        super().__init__(data)
        self.pending_data_func = None

    def async_delay_save(self, data_func, delay=0) -> None:
        self.save_calls += 1
        self.pending_data_func = data_func

    def flush(self) -> None:
        assert self.pending_data_func is not None
        self.data = self.pending_data_func()
        self.pending_data_func = None


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

    async def test_verified_control_polls_prestate_then_ack_then_poststate(
        self,
    ) -> None:
        self.coordinator._pat_to_wideq = {"device": "wideq-device"}
        phases: list[str] = []

        async def fresh() -> bool:
            phases.append("poll")
            self.coordinator._last_success_at = datetime.now(timezone.utc)
            self.coordinator.data = {
                "device": {
                    "state": "READY" if phases.count("poll") == 1 else "DONE"
                }
            }
            return True

        async def control(device_id, ctrl_key, **kwargs):
            phases.append("ack")

        def request_factory():
            phases.append("build")
            self.assertEqual(
                self.coordinator.snapshot_for("device")["state"], "READY"
            )
            return {"command": "Set"}

        with (
            patch.object(
                self.coordinator,
                "_async_verification_refresh",
                side_effect=fresh,
            ),
            patch.object(self.client, "async_control", side_effect=control),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            await self.coordinator.async_control_and_verify(
                "device",
                "verifiedCtrl",
                request_factory=request_factory,
                readback_delays=(5,),
                verifier=lambda snapshot: snapshot.get("state") == "DONE",
            )

        self.assertEqual(phases, ["poll", "build", "ack", "poll"])

    async def test_verified_control_blocks_before_write_when_prestate_poll_fails(
        self,
    ) -> None:
        self.coordinator._pat_to_wideq = {"device": "wideq-device"}

        with patch.object(
            self.coordinator,
            "_async_verification_refresh",
            new=AsyncMock(return_value=False),
        ):
            with self.assertRaisesRegex(HomeAssistantError, "pre-command"):
                await self.coordinator.async_control_and_verify(
                    "device",
                    "verifiedCtrl",
                    request_factory=lambda: {"command": "Set"},
                    readback_delays=(5,),
                    verifier=lambda snapshot: True,
                )

        self.assertEqual(self.client.control_calls, 0)

    async def test_verified_control_requires_ack_before_poststate_poll(self) -> None:
        self.coordinator._pat_to_wideq = {"device": "wideq-device"}
        self.coordinator._last_success_at = datetime.now(timezone.utc)
        refresh = AsyncMock(return_value=True)

        with (
            patch.object(
                self.coordinator,
                "_async_verification_refresh",
                new=refresh,
            ),
            patch.object(
                self.client,
                "async_control",
                new=AsyncMock(side_effect=RuntimeError("rejected")),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "rejected"):
                await self.coordinator.async_control_and_verify(
                    "device",
                    "verifiedCtrl",
                    request_factory=lambda: {"command": "Set"},
                    readback_delays=(5,),
                    verifier=lambda snapshot: True,
                )

        refresh.assert_awaited_once_with()

    async def test_verified_control_fails_closed_after_bounded_poststate_reads(
        self,
    ) -> None:
        self.coordinator._pat_to_wideq = {"device": "wideq-device"}
        self.coordinator._last_success_at = datetime.now(timezone.utc)

        with (
            patch.object(
                self.coordinator,
                "_async_verification_refresh",
                new=AsyncMock(return_value=True),
            ) as refresh,
            patch("asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            with self.assertRaisesRegex(HomeAssistantError, "not verified"):
                await self.coordinator.async_control_and_verify(
                    "device",
                    "verifiedCtrl",
                    request_factory=lambda: {"command": "Set"},
                    readback_delays=(5, 10),
                    verifier=lambda snapshot: False,
                )

        self.assertEqual(self.client.control_calls, 1)
        self.assertEqual(refresh.await_count, 3)
        # Two bounded verification waits plus FakeClient's zero-yield ACK.
        self.assertEqual(sleep.await_count, 3)

    async def test_verified_control_blocks_if_prestate_expires_in_rate_limit(
        self,
    ) -> None:
        self.coordinator._pat_to_wideq = {"device": "wideq-device"}
        self.coordinator._last_success_at = datetime.now(timezone.utc)

        async def expire_snapshot() -> None:
            assert self.coordinator._last_success_at is not None
            self.coordinator._last_success_at -= timedelta(seconds=31)

        with (
            patch.object(
                self.coordinator,
                "_async_verification_refresh",
                new=AsyncMock(return_value=True),
            ),
            patch.object(self.limiter, "acquire", side_effect=expire_snapshot),
        ):
            with self.assertRaisesRegex(HomeAssistantError, "snapshot expired"):
                await self.coordinator.async_control_and_verify(
                    "device",
                    "verifiedCtrl",
                    request_factory=lambda: {"command": "Set"},
                    readback_delays=(5,),
                    verifier=lambda snapshot: True,
                )

        self.assertEqual(self.client.control_calls, 0)

    async def test_verified_control_requires_identity_from_fresh_prestate(
        self,
    ) -> None:
        self.coordinator._last_success_at = datetime.now(timezone.utc)

        with patch.object(
            self.coordinator,
            "_async_verification_refresh",
            new=AsyncMock(return_value=True),
        ):
            with self.assertRaisesRegex(HomeAssistantError, "exact WideQ identity"):
                await self.coordinator.async_control_and_verify(
                    "device",
                    "verifiedCtrl",
                    request_factory=lambda: {"command": "Set"},
                    readback_delays=(5,),
                    verifier=lambda snapshot: True,
                )

        self.assertEqual(self.client.poll_calls, 0)
        self.assertEqual(self.client.control_calls, 0)

    async def test_verified_control_rate_limit_wait_is_bounded(self) -> None:
        self.coordinator._pat_to_wideq = {"device": "wideq-device"}
        self.coordinator._last_success_at = datetime.now(timezone.utc)

        async def never_acquires() -> None:
            await asyncio.sleep(60)

        with (
            patch.object(
                self.coordinator,
                "_async_verification_refresh",
                new=AsyncMock(return_value=True),
            ),
            patch.object(self.limiter, "acquire", side_effect=never_acquires),
            patch(
                "custom_components.my_lg.coordinator_wideq."
                "_CONTROL_VERIFICATION_PRESTATE_MAX_AGE",
                0.001,
            ),
        ):
            with self.assertRaisesRegex(HomeAssistantError, "snapshot expired"):
                await self.coordinator.async_control_and_verify(
                    "device",
                    "verifiedCtrl",
                    request_factory=lambda: {"command": "Set"},
                    readback_delays=(5,),
                    verifier=lambda snapshot: True,
                )

        self.assertEqual(self.client.control_calls, 0)

    async def test_verification_refresh_timeout_is_bounded_and_cleans_flag(
        self,
    ) -> None:
        async def never_finishes() -> None:
            await asyncio.sleep(60)

        with (
            patch(
                "custom_components.my_lg.coordinator_wideq."
                "_CONTROL_VERIFICATION_REFRESH_TIMEOUT",
                0.001,
            ),
            patch.object(
                self.coordinator,
                "async_refresh",
                side_effect=never_finishes,
            ),
        ):
            self.assertFalse(
                await self.coordinator._async_verification_refresh()
            )

        self.assertEqual(self.coordinator._control_verification_refreshes, 0)

    async def test_verification_refresh_skips_optional_energy_history(self) -> None:
        coordinator = WideqCoordinator(
            self.hass,
            None,
            self.client,
            self.limiter,
            lambda: 600,
            {"device": "aircon"},
            pat_devices=self.pat_devices,
        )

        self.assertTrue(await coordinator._async_verification_refresh())

        self.assertEqual(self.client.poll_calls, 1)
        self.assertEqual(self.client.energy_calls, 0)

    async def test_verified_control_rejects_invalid_offsets_without_write(
        self,
    ) -> None:
        self.coordinator._pat_to_wideq = {"device": "wideq-device"}

        for delays in ((10, 5), (5, 5), (float("nan"),), (float("inf"),)):
            with self.subTest(delays=delays), self.assertRaisesRegex(
                HomeAssistantError, "schedule is invalid"
            ):
                await self.coordinator.async_control_and_verify(
                    "device",
                    "verifiedCtrl",
                    request_factory=lambda: {"command": "Set"},
                    readback_delays=delays,
                    verifier=lambda snapshot: True,
                )

        self.assertEqual(self.client.control_calls, 0)

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

    async def test_power_save_restore_never_restores_power_or_energy(self) -> None:
        store = FakeStore(
            {
                "saved_at": "2026-08-05T00:00:00+00:00",
                "items": {
                    "device": {
                        "airState.powerSave.basic": 0,
                        "airState.powerSave.hum": 1,
                        "airState.energy.onCurrent": 9876,
                        "energy_today": 42.5,
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
            pat_devices=self.pat_devices,
            power_save_store=store,
        )

        await coordinator.async_restore_power_save()

        self.assertIsNone(coordinator.data)
        self.assertEqual(coordinator.snapshot_for("device"), {})
        self.assertEqual(
            coordinator.power_save_snapshot_for("device"),
            {
                "airState.powerSave.basic": False,
                "airState.powerSave.hum": True,
            },
        )
        self.assertTrue(
            coordinator.power_save_diagnostic_attributes("device")[
                "power_save_cache_restored"
            ]
        )

    async def test_successful_poll_persists_only_power_save_flags(self) -> None:
        store = FakeStore()
        self.client.snapshots[0] = WideqDeviceData(
            "wideq-device",
            "Device",
            "MODEL",
            {
                "airState.powerSave.basic": 0,
                "airState.powerSave.hum": 1,
                "airState.energy.onCurrent": 1234,
                "energy_today": 9.9,
            },
        )
        coordinator = WideqCoordinator(
            self.hass,
            None,
            self.client,
            self.limiter,
            lambda: 600,
            pat_devices=self.pat_devices,
            power_save_store=store,
        )

        result = await coordinator._async_update_data()

        self.assertEqual(result["device"]["airState.energy.onCurrent"], 1234)
        self.assertEqual(
            store.data["items"]["device"],
            {
                "airState.powerSave.basic": False,
                "airState.powerSave.hum": True,
            },
        )
        self.assertNotIn(
            "airState.energy.onCurrent", store.data["items"]["device"]
        )
        self.assertNotIn("energy_today", store.data["items"]["device"])

    async def test_power_save_optimistic_value_survives_one_stale_poll_within_grace(
        self,
    ) -> None:
        self.coordinator.data = {
            "device": {
                "airState.powerSave.basic": 1,
                "airState.powerSave.hum": 0,
                "airState.energy.onCurrent": 1234,
            }
        }
        self.coordinator._update_power_save_cache(self.coordinator.data)

        self.coordinator.apply_power_save_optimistic(
            "device", "airState.powerSave.hum", 1
        )
        self.assertIn(
            ("device", "airState.powerSave.hum"),
            self.coordinator._power_save_readback_refresh,
        )

        # The acknowledged command must win over the retained pre-command
        # snapshot, without copying unrelated WideQ readings into this cache.
        self.assertEqual(
            self.coordinator.power_save_snapshot_for("device"),
            {
                "airState.powerSave.basic": True,
                "airState.powerSave.hum": True,
            },
        )
        self.assertEqual(
            self.coordinator.snapshot_for("device")["airState.powerSave.hum"],
            0,
        )
        self.assertNotIn(
            "airState.energy.onCurrent",
            self.coordinator.power_save_snapshot_for("device"),
        )

        # A successful poll that omits the commanded field cannot confirm or
        # reject it, so the optimistic value remains pending.
        self.coordinator._update_power_save_cache(
            {"device": {"airState.powerSave.basic": 0}}
        )
        self.assertTrue(
            self.coordinator.power_save_snapshot_for("device")[
                "airState.powerSave.hum"
            ]
        )

        # One exact but contradictory response immediately after the command
        # can still be the pre-command snapshot. It must not flicker the UI.
        self.coordinator._update_power_save_cache(
            {"device": {"airState.powerSave.hum": 0}}
        )
        self.assertTrue(
            self.coordinator.power_save_snapshot_for("device")[
                "airState.powerSave.hum"
            ]
        )

        pending = self.coordinator._power_save_pending["device"][
            "airState.powerSave.hum"
        ]
        pending.applied_at -= timedelta(seconds=91)
        self.coordinator._update_power_save_cache(
            {"device": {"airState.powerSave.hum": 0}}
        )
        self.assertFalse(
            self.coordinator.power_save_snapshot_for("device")[
                "airState.powerSave.hum"
            ]
        )

    async def test_matching_power_save_poll_confirms_pending_immediately(
        self,
    ) -> None:
        store = FakeStore()
        self.coordinator._power_save_store = store
        self.coordinator.data = {
            "device": {"airState.powerSave.hum": 0}
        }
        self.coordinator._update_power_save_cache(self.coordinator.data)
        baseline_save_calls = store.save_calls
        self.coordinator.apply_power_save_optimistic(
            "device", "airState.powerSave.hum", 1
        )

        self.coordinator._update_power_save_cache(
            {"device": {"airState.powerSave.hum": 1}}
        )

        self.assertNotIn("device", self.coordinator._power_save_pending)
        self.assertNotIn(
            ("device", "airState.powerSave.hum"),
            self.coordinator._power_save_readback_refresh,
        )
        self.assertEqual(store.save_calls, baseline_save_calls + 2)
        self.assertTrue(
            store.data["items"]["device"]["airState.powerSave.hum"]
        )
        self.assertTrue(
            self.coordinator.power_save_snapshot_for("device")[
                "airState.powerSave.hum"
            ]
        )

    async def test_delayed_power_save_readback_schedules_exactly_one_refresh(
        self,
    ) -> None:
        path = "airState.powerSave.hum"
        self.coordinator.apply_power_save_optimistic("device", path, 1)
        pending = self.coordinator._power_save_pending["device"][path]
        self.coordinator._power_save_readback_refresh[("device", path)].cancel()

        with patch.object(
            self.coordinator,
            "async_request_refresh",
            new=AsyncMock(),
        ) as refresh:
            self.coordinator._request_power_save_readback(
                "device", path, pending.applied_at
            )
            await asyncio.sleep(0)

        refresh.assert_awaited_once_with()
        self.assertNotIn(
            ("device", path),
            self.coordinator._power_save_readback_refresh,
        )
        self.coordinator.cancel_power_save_pending()

    async def test_omitted_power_save_readback_expires_to_unavailable(
        self,
    ) -> None:
        self.coordinator.data = {
            "device": {"airState.powerSave.hum": 0}
        }
        self.coordinator._update_power_save_cache(self.coordinator.data)
        self.coordinator.apply_power_save_optimistic(
            "device", "airState.powerSave.hum", 1
        )
        pending = self.coordinator._power_save_pending["device"][
            "airState.powerSave.hum"
        ]
        pending.applied_at -= timedelta(seconds=91)

        self.coordinator._update_power_save_cache(
            {"device": {"airState.powerSave.basic": 0}}
        )

        self.assertNotIn("device", self.coordinator._power_save_pending)
        self.assertNotIn(
            "airState.powerSave.hum",
            self.coordinator.power_save_snapshot_for("device"),
        )

    async def test_omitted_power_save_readback_without_baseline_becomes_unknown(
        self,
    ) -> None:
        self.coordinator.data = {"device": {}}
        self.coordinator.apply_power_save_optimistic(
            "device", "airState.powerSave.hum", 1
        )
        pending = self.coordinator._power_save_pending["device"][
            "airState.powerSave.hum"
        ]
        pending.applied_at -= timedelta(seconds=91)

        self.coordinator._update_power_save_cache(
            {"device": {"airState.powerSave.basic": 0}}
        )

        self.assertNotIn(
            "airState.powerSave.hum",
            self.coordinator.power_save_snapshot_for("device"),
        )

    async def test_delayed_store_never_serializes_unverified_optimistic_value(
        self,
    ) -> None:
        store = DelayedFakeStore()
        self.coordinator._power_save_store = store
        self.coordinator._update_power_save_cache(
            {"device": {"airState.powerSave.hum": 0}}
        )
        self.assertIsNotNone(store.pending_data_func)

        self.coordinator.apply_power_save_optimistic(
            "device", "airState.powerSave.hum", 1
        )
        store.flush()

        self.assertNotIn(
            "device",
            store.data["items"],
        )
        self.assertTrue(
            self.coordinator.power_save_snapshot_for("device")[
                "airState.powerSave.hum"
            ]
        )

    async def test_pending_timer_expires_without_a_followup_poll(self) -> None:
        self.coordinator.data = {
            "device": {"airState.powerSave.hum": 0}
        }
        self.coordinator._update_power_save_cache(self.coordinator.data)
        self.coordinator.apply_power_save_optimistic(
            "device", "airState.powerSave.hum", 1
        )
        applied_at = self.coordinator._power_save_pending["device"][
            "airState.powerSave.hum"
        ].applied_at

        self.coordinator._expire_power_save_pending(
            "device", "airState.powerSave.hum", applied_at
        )

        self.assertNotIn("device", self.coordinator._power_save_pending)
        self.assertNotIn(
            "airState.powerSave.hum",
            self.coordinator.power_save_snapshot_for("device"),
        )

    async def test_unverified_value_stays_absent_across_unload_and_restore(
        self,
    ) -> None:
        store = FakeStore()
        self.coordinator._power_save_store = store
        self.coordinator._update_power_save_cache(
            {"device": {"airState.powerSave.hum": 0}}
        )
        self.coordinator.apply_power_save_optimistic(
            "device", "airState.powerSave.hum", 1
        )

        await self.coordinator.async_persist_power_save()
        self.assertNotIn("device", store.data["items"])

        restored = WideqCoordinator(
            self.hass,
            None,
            self.client,
            self.limiter,
            lambda: 600,
            pat_devices=self.pat_devices,
            power_save_store=store,
        )
        await restored.async_restore_power_save()
        self.assertNotIn(
            "airState.powerSave.hum",
            restored.power_save_snapshot_for("device"),
        )

    async def test_power_save_optimistic_off_notifies_before_stale_poll(
        self,
    ) -> None:
        self.coordinator.data = {
            "device": {"airState.powerSave.hum": 1}
        }
        self.coordinator._update_power_save_cache(self.coordinator.data)
        listener_calls = 0

        def listener() -> None:
            nonlocal listener_calls
            listener_calls += 1

        remove_listener = self.coordinator.async_add_listener(listener)
        try:
            self.coordinator.apply_power_save_optimistic(
                "device", "airState.powerSave.hum", 0
            )
        finally:
            remove_listener()

        self.assertEqual(listener_calls, 1)
        self.assertFalse(
            self.coordinator.power_save_snapshot_for("device")[
                "airState.powerSave.hum"
            ]
        )
        self.assertEqual(
            self.coordinator.snapshot_for("device")["airState.powerSave.hum"],
            1,
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
