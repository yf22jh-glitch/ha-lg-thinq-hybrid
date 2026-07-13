"""Single low-rate wideq coordinator.

One ``refresh_devices()`` call returns snapshots for ALL devices, so a single
coordinator (not per-device) covers every wideq-only field. Interval self-adjusts
from PAT/MQTT activity and never eager-polls on setup. Repeated service failures
open a circuit that retains cached data and performs one recovery probe per
interval instead of reconnecting or hammering LG's gateway.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    WIDEQ_CIRCUIT_FAILURE_THRESHOLD,
    WIDEQ_PROBE_INTERVAL,
)
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
        interval_fn: Callable[[], int],
    ) -> None:
        self.client = client
        self.rate_limiter = rate_limiter
        self._interval_fn = interval_fn
        self._fail_count = 0
        self._failure_started_at: datetime | None = None
        self._last_success_at: datetime | None = None

        # Initial interval reflects current state (PAT already seeded), but we do
        # NOT force an immediate poll — first refresh happens one interval later.
        initial = interval_fn()
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
            if self._failure_started_at is None:
                self._failure_started_at = datetime.now(timezone.utc)

            # After three strikes, normal data collection is suspended. The same
            # one-call snapshot refresh runs every 15 minutes as a half-open
            # recovery probe. It supplies fresh data and closes the circuit on
            # success, without a separate health endpoint or login storm.
            if self.circuit_open:
                self.update_interval = timedelta(seconds=WIDEQ_PROBE_INTERVAL)

            if self._fail_count == 2:
                _LOGGER.warning("wideq still unavailable (x2): %s", err)
            elif self._fail_count == WIDEQ_CIRCUIT_FAILURE_THRESHOLD:
                _LOGGER.warning(
                    "wideq circuit opened after %d failures; probing every %ds",
                    self._fail_count,
                    WIDEQ_PROBE_INTERVAL,
                )
            elif (
                self._fail_count > WIDEQ_CIRCUIT_FAILURE_THRESHOLD
                and (self._fail_count - WIDEQ_CIRCUIT_FAILURE_THRESHOLD) % 4 == 0
            ):
                _LOGGER.warning(
                    "wideq still unavailable (x%d, outage=%ds); next probe in %ds",
                    self._fail_count,
                    self.outage_seconds,
                    WIDEQ_PROBE_INTERVAL,
                )
            # HA emits listeners itself when x1 flips last_update_success. Later
            # failures are normally suppressed, so notify explicitly to keep
            # stale/failure diagnostic attributes current.
            if self._fail_count > 1:
                self.async_update_listeners()
            raise UpdateFailed(
                f"wideq poll failed (x{self._fail_count}): {err}"
            ) from err

        # Success closes the circuit and restores the interval selected from the
        # latest PAT/MQTT state. No extra refresh is needed: the probe result is
        # already the fresh all-device snapshot.
        if self._fail_count:
            _LOGGER.warning(
                "wideq recovered after %d failures (outage=%ds)",
                self._fail_count,
                self.outage_seconds,
            )
        self._fail_count = 0
        self._failure_started_at = None
        self._last_success_at = datetime.now(timezone.utc)
        self.update_interval = timedelta(seconds=self._interval_fn())
        return snapshots

    @property
    def circuit_open(self) -> bool:
        """Return whether only scheduled recovery probes may access wideq."""
        return self._fail_count >= WIDEQ_CIRCUIT_FAILURE_THRESHOLD

    @property
    def data_stale(self) -> bool:
        """Return whether the retained snapshot predates a failed refresh."""
        return self._fail_count > 0

    @property
    def outage_seconds(self) -> int:
        """Return elapsed seconds since the first consecutive failure."""
        if self._failure_started_at is None:
            return 0
        elapsed = datetime.now(timezone.utc) - self._failure_started_at
        return int(elapsed.total_seconds())

    @property
    def diagnostic_attributes(self) -> dict[str, Any]:
        """Attributes shared by entities backed by this snapshot."""
        return {
            "data_stale": self.data_stale,
            "wideq_consecutive_failures": self._fail_count,
            "wideq_last_success": (
                self._last_success_at.isoformat() if self._last_success_at else None
            ),
        }

    @callback
    def reconcile_interval(self) -> None:
        """Apply a PAT/MQTT-derived interval without forcing an immediate poll."""
        if self.circuit_open:
            return
        interval = timedelta(seconds=self._interval_fn())
        if interval == self.update_interval:
            return
        self.update_interval = interval
        # Changing update_interval alone does not move HA's existing timer.
        # Reschedule from now, preserving the no-eager-poll restart policy.
        if self._listeners:
            self._schedule_refresh()

    async def async_request_refresh(self) -> None:
        """Ignore event-driven refreshes while the circuit permits probes only."""
        if self.circuit_open:
            return
        await super().async_request_refresh()

    def snapshot_for(self, alias: str) -> dict[str, Any]:
        return (self.data or {}).get(alias, {})

    async def async_control(self, alias: str, ctrl_key: str, **kwargs: Any) -> None:
        """Send one wideq control command for a device (rate-limited)."""
        if self.circuit_open:
            raise HomeAssistantError(
                "LG ThinQ wideq service is unavailable; waiting for recovery probe"
            )
        await self.rate_limiter.acquire()
        await self.client.async_control(alias, ctrl_key, **kwargs)

    def apply_optimistic(self, alias: str, key: str, value: Any) -> None:
        """Reflect a just-sent value immediately; the next poll confirms it.

        wideq-only fields have no MQTT push, so without this the UI would lag
        until the next scheduled poll. A rejected write self-corrects then.
        """
        data = dict(self.data or {})
        snap = dict(data.get(alias, {}))
        snap[key] = value
        data[alias] = snap
        self.async_set_updated_data(data)
