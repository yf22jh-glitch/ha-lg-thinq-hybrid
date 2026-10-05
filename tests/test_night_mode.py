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
    def test_off_is_saved_state_but_not_an_active_brightness_mode(self):
        value = night_mode.parse_night_mode(saved('OFF'))
        self.assertEqual(value.mode, 'OFF')

    async def test_schedule_edits_preserve_other_fields_and_restore(self):
        state = saved()
        writes = []
        async def read(): return dict(state)
        async def write(body):
            writes.append(body)
            state.update(body)
            zone = ZoneInfo('Asia/Seoul')
            for clock, epoch in [('startTime', 'startDate'), ('endTime', 'endDate')]:
                hour, minute = map(int, body[clock].split(':'))
                state[epoch] = int(datetime(2026, 9, 23, hour, minute, tzinfo=zone).timestamp())
        for feature, value in [('start_time','21:01'), ('end_time','06:02'),
                               ('mode','OFF'), ('mode','SUNSET_RISE'), ('mode','CUSTOM')]:
            before = night_mode.parse_night_mode(await read())
            result = await night_mode.set_night_mode_setting(expected=before, feature=feature,
                value=value, read=read, write=write)
            self.assertEqual(result.brightness_pct, 30)
            self.assertEqual(getattr(result, feature), value)
            if feature != 'mode':
                untouched = 'end_time' if feature == 'start_time' else 'start_time'
                self.assertEqual(getattr(result, untouched), getattr(before, untouched))
        self.assertEqual(len(writes), 5)

    async def test_schedule_rejects_bad_clock_stale_state_and_inactive_time_without_writing(self):
        writes = []
        async def read(): return saved()
        async def write(body): writes.append(body)
        before = night_mode.parse_night_mode(saved())
        for feature, value in [('start_time','25:00'), ('end_time','1:00'),
                               ('start_time','12:00:01'), ('mode','AUTO'), ('other','21:00')]:
            with self.assertRaises(ValueError):
                await night_mode.set_night_mode_setting(expected=before, feature=feature,
                    value=value, read=read, write=write)
        with self.assertRaisesRegex(ValueError, 'changed'):
            await night_mode.set_night_mode_setting(expected=night_mode.parse_night_mode(saved(brightness='40')),
                feature='mode', value='OFF', read=read, write=write)
        async def read_off(): return saved('OFF')
        with self.assertRaisesRegex(ValueError, 'CUSTOM'):
            await night_mode.set_night_mode_setting(expected=night_mode.parse_night_mode(saved('OFF')),
                feature='start_time', value='21:01', read=read_off, write=write)
        self.assertEqual(writes, [])

    async def test_schedule_ack_only_never_confirms_or_retries_write(self):
        writes = []
        async def read(): return saved()
        async def write(body): writes.append(body)
        async def no_wait(): pass
        with self.assertRaisesRegex(ValueError, 'not confirmed'):
            await night_mode.set_night_mode_setting(expected=night_mode.parse_night_mode(saved()),
                feature='start_time', value='21:01', read=read, write=write,
                wait=no_wait, attempts=2)
        self.assertEqual(len(writes), 1)

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
