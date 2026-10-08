"""Exact ThinQ Web night-mode brightness transaction, independent of HA.

The appliance reports its night mode but does not read back brightness.  A
successful PUT is therefore not confirmation: only a fresh saved-state GET is.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


FRIDGE_MODEL = "2REFO1DBN3K_U"
KIMCHI_MODEL = "3REK2G03VI230D_2"
MODES_BY_MODEL = {
    FRIDGE_MODEL: ("CUSTOM", "SUNSET_RISE"),
    KIMCHI_MODEL: ("CUSTOM", "SUNSET_RISE"),
}
DEVICE_TIME_ZONE = "Asia/Seoul"


@dataclass(frozen=True)
class NightModeSaved:
    """Saved ThinQ Web state, never inferred from an AABB acknowledgement."""

    mode: str
    brightness_pct: int
    start_time: str
    end_time: str


def _brightness(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("night-mode brightness must be 10..90 in steps of 10")
    if isinstance(value, str) and value in {str(n) for n in range(10, 91, 10)}:
        return int(value)
    if (
        isinstance(value, (int, float))
        and math.isfinite(value)
        and int(value) == value
    ):
        number = int(value)
        if 10 <= number <= 90 and number % 10 == 0:
            return number
    raise ValueError("night-mode brightness must be 10..90 in steps of 10")


def _clock(epoch: object, saved: object, zone: ZoneInfo) -> str:
    if isinstance(epoch, bool) or not isinstance(epoch, (int, str)):
        raise ValueError("saved custom night-mode time is missing")
    if isinstance(epoch, str) and (len(epoch) != 10 or not epoch.isascii() or not epoch.isdecimal()):
        raise ValueError("saved custom night-mode epoch is invalid")
    seconds = int(epoch)
    if not 0 <= seconds <= 4_102_444_800:
        raise ValueError("saved custom night-mode epoch is invalid")
    clock = datetime.fromtimestamp(seconds, zone).strftime("%H:%M")
    if saved is not None and saved != clock:
        raise ValueError("saved custom night-mode clock and epoch disagree")
    return clock


def parse_night_mode(raw: dict[str, Any], *, time_zone: str = DEVICE_TIME_ZONE) -> NightModeSaved:
    """Parse the Web saved tuple without inventing missing values or modes."""
    if not isinstance(raw, dict):
        raise ValueError("night-mode response is not an object")
    mode = raw.get("nightMode")
    if mode not in ("OFF", "CUSTOM", "SUNSET_RISE"):
        raise ValueError("night-mode has an unknown saved mode")
    for field in (
        "brightnessKnockOn", "periodKnockOn", "brightnessDispenser",
        "brightnessScreen", "brightnessWelcomeLight",
    ):
        if raw.get(field) is not None:
            raise ValueError("night-mode has an unsupported extra brightness field")
    zone = ZoneInfo(time_zone)
    brightness = _brightness(raw.get("brightness"))
    if mode == "CUSTOM":
        start = _clock(raw.get("startDate"), raw.get("startTime"), zone)
        end = _clock(raw.get("endDate"), raw.get("endTime"), zone)
    else:
        # Web's SAVE converter uses placeholders; the server owns sunrise/sunset.
        start, end = "21:00", "06:00"
    return NightModeSaved(mode, brightness, start, end)


async def set_night_mode_setting(
    *, expected: NightModeSaved, feature: str, value: str,
    read: Callable[[], Awaitable[dict[str, Any]]],
    write: Callable[[dict[str, str]], Awaitable[None]],
    wait: Callable[[], Awaitable[None]] | None = None, attempts: int = 5,
) -> NightModeSaved:
    """Edit one Web schedule setting, preserving brightness and other fields.

    OFF/sunset do not store custom times. Entering CUSTOM from either uses the
    Web defaults, 21:00–06:00. Time entities only edit an active CUSTOM schedule.
    """
    if feature == 'mode':
        if value not in ('OFF', 'SUNSET_RISE', 'CUSTOM'):
            raise ValueError('unknown night mode')
    elif feature in ('start_time', 'end_time'):
        if not isinstance(value, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value):
            raise ValueError('night-mode time must be HH:MM')
        if expected.mode != 'CUSTOM':
            raise ValueError('night-mode time requires CUSTOM mode')
    else:
        raise ValueError('unknown night-mode setting')
    if not 1 <= attempts <= 10:
        raise ValueError('night-mode confirmation budget is invalid')
    before = parse_night_mode(await read())
    if before != expected:
        raise ValueError('saved night-mode setting changed; refresh before editing')
    desired = replace(before, **{feature: value})
    if desired == before:
        return before
    if desired.mode != 'CUSTOM':
        desired = replace(desired, start_time='21:00', end_time='06:00')
    await write(dict(saveType='SAVE', nightMode=desired.mode,
                     brightness=str(desired.brightness_pct), startTime=desired.start_time,
                     endTime=desired.end_time))
    for attempt in range(attempts):
        if attempt:
            await (wait or (lambda: asyncio.sleep(1)))()
        after = parse_night_mode(await read())
        if after == desired:
            return after
        if after.brightness_pct != before.brightness_pct:
            raise ValueError('brightness changed outside the requested schedule edit')
    raise ValueError('saved night-mode setting was not confirmed by ThinQ Web')


async def preview_night_mode(
    *, model: str, expected: NightModeSaved,
    read: Callable[[], Awaitable[dict[str, Any]]],
    write: Callable[[dict[str, str]], Awaitable[None]],
    wait: Callable[[], Awaitable[None]] | None = None,
) -> NightModeSaved:
    """Request Web's ten-second preview without modifying the saved schedule.

    Confirmation means request acknowledgement and unchanged saved settings,
    not measured light output: neither exact model reports preview brightness.
    No SAVE/automatic restoration is sent, even if another client edits during
    the preview. One uncertain request is never automatically retried.
    """
    if model not in MODES_BY_MODEL or expected.mode not in MODES_BY_MODEL[model]:
        raise ValueError('night-mode preview requires a supported active mode')
    before = parse_night_mode(await read())
    if before != expected:
        raise ValueError('saved night-mode setting changed; refresh before preview')
    body = dict(saveType='PREVIEW', nightMode=before.mode, brightness=str(before.brightness_pct),
                startTime='21:00', endTime='06:00')
    if model == FRIDGE_MODEL:
        body['nightMode'] = 'SUNSET_RISE'  # Exact REF Web converter's transient mode hint.
    else:
        body['nightModeEx'] = 'Y'  # Exact KM converter, not an extra saved setting.
    await write(body)
    await (wait or (lambda: asyncio.sleep(11)))()
    after = parse_night_mode(await read())
    if after != before:
        raise ValueError('saved night-mode setting changed during preview; not overwritten')
    return after


async def set_night_mode_brightness(
    *,
    expected_mode: str,
    expected_brightness_pct: int,
    desired_brightness_pct: int,
    read: Callable[[], Awaitable[dict[str, Any]]],
    write: Callable[[dict[str, str]], Awaitable[None]],
    wait: Callable[[], Awaitable[None]] | None = None,
    attempts: int = 5,
) -> NightModeSaved:
    """Change only the active mode's brightness and confirm with a fresh GET."""
    if expected_mode not in ("CUSTOM", "SUNSET_RISE"):
        raise ValueError("night-mode capability is not reviewed")
    expected = _brightness(expected_brightness_pct)
    desired = _brightness(desired_brightness_pct)
    if not 1 <= attempts <= 10:
        raise ValueError("night-mode confirmation budget is invalid")
    before = parse_night_mode(await read())
    if before.mode != expected_mode or before.brightness_pct != expected:
        raise ValueError("saved night mode or brightness changed since the reviewed value")
    if before.brightness_pct == desired:
        return before
    await write({
        "saveType": "SAVE",
        "nightMode": before.mode,
        "brightness": str(desired),
        "startTime": before.start_time,
        "endTime": before.end_time,
    })
    for attempt in range(attempts):
        if attempt:
            await (wait or (lambda: asyncio.sleep(1)))()
        after = parse_night_mode(await read())
        if after.mode != before.mode or (
            before.mode == "CUSTOM"
            and (after.start_time, after.end_time) != (before.start_time, before.end_time)
        ):
            raise ValueError("night mode or schedule changed outside the requested brightness")
        if after.brightness_pct == desired:
            return after
    raise ValueError("saved night-mode brightness was not confirmed by ThinQ Web")
