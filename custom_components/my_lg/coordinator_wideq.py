"""Single low-rate wideq coordinator.

One ``refresh_devices()`` call returns snapshots for ALL devices, so a single
coordinator (not per-device) covers every wideq-only field. Interval self-adjusts
between an active value (any AC on) and an idle baseline, and never eager-polls
on setup (first poll happens one interval later) to avoid restart bursts.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .rate_limiter import GlobalRateLimiter
from .wideq_client import WideqClient

_LOGGER = logging.getLogger(__name__)


class WideqCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Poll wideq snapshots (keyed by alias) at a conditional, rate-limited cadence."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: WideqClient,
        rate_limiter: GlobalRateLimiter,
        active_fn: Callable[[], bool],
        ac_active_interval: int,
        idle_interval: int,
    ) -> None:
        self.client = client
        self.rate_limiter = rate_limiter
        self._active_fn = active_fn
        self._ac_active_interval = ac_active_interval
        self._idle_interval = idle_interval
        self._fail_count = 0

        # Initial interval reflects current state (PAT already seeded), but we do
        # NOT force an immediate poll — first refresh happens one interval later.
        initial = ac_active_interval if active_fn() else idle_interval
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} wideq",
            update_interval=timedelta(seconds=initial),
            config_entry=entry,
        )

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        await self.rate_limiter.acquire()
        try:
            snapshots = await self.client.async_get_snapshots()
        except Exception as err:  # noqa: BLE001
            self._fail_count += 1
            # Back off hard after repeated failures. A persistently dead session
            # would otherwise trigger a reconnect (extra gateway/oauth calls)
            # every single poll — that hammering is a real LG ban risk. After 3
            # strikes, poll only hourly until it recovers (a success or HA
            # restart resets the counter).
            if self._fail_count >= 3:
                self.update_interval = timedelta(seconds=3600)
            raise UpdateFailed(
                f"wideq poll failed (x{self._fail_count}): {err}"
            ) from err

        # Success: clear the failure backoff and self-adjust the next interval
        # (takes effect on the next schedule; no forced poll here).
        self._fail_count = 0
        active = self._active_fn()
        self.update_interval = timedelta(
            seconds=self._ac_active_interval if active else self._idle_interval
        )
        return snapshots

    def snapshot_for(self, alias: str) -> dict[str, Any]:
        return (self.data or {}).get(alias, {})
