"""Exact Web saved-state confirmation for night anti-glare brightness."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


SOURCE = Path(__file__).resolve().parents[1] / "custom_components/my_lg/night_mode.py"
SPEC = importlib.util.spec_from_file_location("reviewed_night_mode", SOURCE)
assert SPEC is not None and SPEC.loader is not None
night_mode = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = night_mode
SPEC.loader.exec_module(night_mode)


def saved(mode="CUSTOM", brightness="30"):
    zone = ZoneInfo("Asia/Seoul")
    start = int(datetime(2026, 9, 23, 21, 0, tzinfo=zone).timestamp())
    end = int(datetime(2026, 9, 24, 6, 0, tzinfo=zone).timestamp())
    return {
        "nightMode": mode,
        "brightness": brightness,
        "startDate": start,
        "endDate": end,
        "startTime": "21:00",
        "endTime": "06:00",
    }


class NightModeTests(unittest.IsolatedAsyncioTestCase):
    def test_custom_clock_comes_from_saved_epoch(self):
        value = night_mode.parse_night_mode(saved())
        self.assertEqual((value.mode, value.brightness_pct), ("CUSTOM", 30))
        self.assertEqual((value.start_time, value.end_time), ("21:00", "06:00"))

    def test_sunset_uses_web_save_placeholders(self):
        value = night_mode.parse_night_mode({"nightMode": "SUNSET_RISE", "brightness": "40"})
        self.assertEqual((value.start_time, value.end_time), ("21:00", "06:00"))

    def test_unknown_extra_brightness_and_clock_mismatch_fail_closed(self):
        with self.assertRaises(ValueError):
            night_mode.parse_night_mode({**saved(), "brightnessScreen": "30"})
        with self.assertRaises(ValueError):
            night_mode.parse_night_mode({**saved(), "startTime": "20:00"})

    async def test_save_requires_fresh_get_and_preserves_custom_schedule(self):
        state = saved()
        writes = []

        async def read():
            return dict(state)

        async def write(body):
            writes.append(dict(body))
            state["brightness"] = body["brightness"]

        result = await night_mode.set_night_mode_brightness(
            expected_mode="CUSTOM",
            expected_brightness_pct=30,
            desired_brightness_pct=40,
            read=read,
            write=write,
        )
        self.assertEqual(result.brightness_pct, 40)
        self.assertEqual(writes, [{
            "saveType": "SAVE", "nightMode": "CUSTOM", "brightness": "40",
            "startTime": "21:00", "endTime": "06:00",
        }])

    async def test_sunset_brightness_uses_the_active_saved_mode(self):
        state = {"nightMode": "SUNSET_RISE", "brightness": "40"}
        writes = []

        async def read():
            return dict(state)

        async def write(body):
            writes.append(dict(body))
            state["brightness"] = body["brightness"]

        result = await night_mode.set_night_mode_brightness(
            expected_mode="SUNSET_RISE",
            expected_brightness_pct=40,
            desired_brightness_pct=50,
            read=read,
            write=write,
        )
        self.assertEqual(result.brightness_pct, 50)
        self.assertEqual(writes, [{
            "saveType": "SAVE", "nightMode": "SUNSET_RISE", "brightness": "50",
            "startTime": "21:00", "endTime": "06:00",
        }])

    async def test_stale_value_and_inactive_mode_send_no_write(self):
        writes = []

        async def read():
            return saved()

        async def write(body):
            writes.append(body)

        for mode, expected in (("SUNSET_RISE", 30), ("CUSTOM", 40)):
            with self.assertRaises(ValueError):
                await night_mode.set_night_mode_brightness(
                    expected_mode=mode,
                    expected_brightness_pct=expected,
                    desired_brightness_pct=50,
                    read=read,
                    write=write,
                )
        self.assertEqual(writes, [])

    async def test_ack_alone_never_confirms_and_no_automatic_second_write(self):
        writes = []

        async def read():
            return saved()

        async def write(body):
            writes.append(body)

        async def no_wait():
            return None

        with self.assertRaisesRegex(ValueError, "not confirmed"):
            await night_mode.set_night_mode_brightness(
                expected_mode="CUSTOM",
                expected_brightness_pct=30,
                desired_brightness_pct=40,
                read=read,
                write=write,
                wait=no_wait,
                attempts=2,
            )
        self.assertEqual(len(writes), 1)

    async def test_unrelated_schedule_change_rejects_confirmation(self):
        state = saved()

        async def read():
            return dict(state)

        async def write(body):
            state["brightness"] = body["brightness"]
            state["endTime"] = "05:00"

        with self.assertRaisesRegex(ValueError, "disagree"):
            await night_mode.set_night_mode_brightness(
                expected_mode="CUSTOM",
                expected_brightness_pct=30,
                desired_brightness_pct=40,
                read=read,
                write=write,
            )


if __name__ == "__main__":
    unittest.main()
