"""Single low-rate wideq coordinator.

One ``refresh_devices()`` call returns snapshots for ALL devices, so a single
coordinator (not per-device) covers every wideq-only field. Interval self-adjusts
from PAT/MQTT activity and never eager-polls on setup. Repeated service failures
open a circuit that retains cached data and performs one recovery probe per
interval instead of reconnecting or hammering LG's gateway.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    WIDEQ_CIRCUIT_FAILURE_THRESHOLD,
    WIDEQ_ENERGY_HISTORY_FAILURE_RETRY,
    WIDEQ_ENERGY_HISTORY_INTERVAL,
    WIDEQ_ENERGY_HISTORY_STORE_SAVE_DELAY,
    WIDEQ_PROBE_INTERVAL,
)
from .device_identity import (
    PatDeviceIdentity,
    WideqDeviceData,
    resolve_wideq_devices,
)
from .rate_limiter import GlobalRateLimiter
from .wideq_client import WideqClient, is_server_unavailable

_LOGGER = logging.getLogger(__name__)


class WideqCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Poll WideQ snapshots keyed by stable PAT id at a guarded cadence."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: WideqClient,
        rate_limiter: GlobalRateLimiter,
        interval_fn: Callable[[], int],
        energy_history_targets: dict[str, str] | None = None,
        energy_history_store: Store[dict[str, Any]] | None = None,
        pat_devices: dict[str, PatDeviceIdentity] | None = None,
        device_map_store: Store[dict[str, Any]] | None = None,
        legacy_energy_history_store: Store[dict[str, Any]] | None = None,
    ) -> None:
        self.client = client
        self.rate_limiter = rate_limiter
        self._interval_fn = interval_fn
        self._fail_count = 0
        self._failure_started_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._control_locks: dict[str, asyncio.Lock] = {}
        self._io_lock = asyncio.Lock()
        self._energy_history_targets = dict(energy_history_targets or {})
        self._energy_history: dict[str, dict[str, Any]] = {}
        self._energy_history_store = energy_history_store
        self._legacy_energy_history_store = legacy_energy_history_store
        self._energy_history_restored: set[str] = set()
        self._energy_history_unsupported: set[str] = set()
        self._energy_history_failures: dict[str, str] = {}
        self._energy_history_next_attempt: datetime | None = None
        self._energy_history_batch_stale = False
        self._pat_devices = dict(pat_devices or {})
        self._pat_to_wideq: dict[str, str] = {}
        self._device_map_store = device_map_store
        self._ambiguous_pat_ids: set[str] = set()
        self._unmatched_pat_ids: set[str] = set(self._pat_devices)

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
        try:
            # Do not overlap a dashboard refresh with a device command. The
            # limiter spaces request starts; this lock also serializes their
            # actual network lifetimes.
            async with self._io_lock:
                await self.rate_limiter.acquire()
                devices = await self.client.async_get_snapshots()
                snapshots = self._resolve_devices(devices)
                # Optional per-device history reads are deliberately independent
                # from the snapshot circuit. Their errors retain cached energy
                # and never turn an otherwise healthy all-device poll into a
                # coordinator failure. A successful recovery probe skips this
                # optional batch once, so a just-recovered service receives only
                # the single probe request.
                if self._fail_count == 0:
                    await self._async_refresh_energy_history()
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

    def _device_map_payload(self) -> dict[str, Any]:
        """Serialize stable PAT-to-WideQ identifiers only."""
        return {"pat_to_wideq": dict(self._pat_to_wideq)}

    def _schedule_device_map_save(self) -> None:
        if self._device_map_store is None:
            return
        self._device_map_store.async_delay_save(self._device_map_payload, 5)

    async def async_restore_device_map(self) -> None:
        """Restore stable identifiers without performing a WideQ request."""
        if self._device_map_store is None:
            return
        stored = await self._device_map_store.async_load()
        if not isinstance(stored, dict):
            return
        mapping = stored.get("pat_to_wideq")
        if not isinstance(mapping, dict):
            return
        candidates = {
            pat_id: wideq_id
            for pat_id, wideq_id in mapping.items()
            if pat_id in self._pat_devices
            and isinstance(pat_id, str)
            and isinstance(wideq_id, str)
            and wideq_id
        }
        duplicate_wideq_ids = {
            wideq_id
            for wideq_id in candidates.values()
            if sum(value == wideq_id for value in candidates.values()) > 1
        }
        self._ambiguous_pat_ids = {
            pat_id
            for pat_id, wideq_id in candidates.items()
            if wideq_id in duplicate_wideq_ids
        }
        self._pat_to_wideq = {
            pat_id: wideq_id
            for pat_id, wideq_id in candidates.items()
            if wideq_id not in duplicate_wideq_ids
        }
        self._unmatched_pat_ids = (
            set(self._pat_devices)
            - set(self._pat_to_wideq)
            - self._ambiguous_pat_ids
        )
        if self._ambiguous_pat_ids:
            _LOGGER.error(
                "discarded a corrupt WideQ identity mapping affecting %d "
                "PAT device(s); a fresh account snapshot is required",
                len(self._ambiguous_pat_ids),
            )

    async def async_persist_device_map(self) -> None:
        """Flush the stable mapping during config-entry unload."""
        if self._device_map_store is not None:
            await self._device_map_store.async_save(self._device_map_payload())

    def _resolve_devices(self, devices: list[WideqDeviceData]) -> dict[str, dict[str, Any]]:
        """Resolve one WideQ account response to PAT ids and retain the mapping."""
        previous = dict(self._pat_to_wideq)
        previous_ambiguous = set(self._ambiguous_pat_ids)
        resolution = resolve_wideq_devices(
            self._pat_devices, devices, self._pat_to_wideq
        )
        self._pat_to_wideq = resolution.pat_to_wideq
        self._ambiguous_pat_ids = resolution.ambiguous_pat_ids
        self._unmatched_pat_ids = resolution.unmatched_pat_ids
        if self._pat_to_wideq != previous:
            self._schedule_device_map_save()
        if self._ambiguous_pat_ids and self._ambiguous_pat_ids != previous_ambiguous:
            _LOGGER.error(
                "wideq identity is ambiguous for %d PAT device(s); "
                "their WideQ state and controls are blocked",
                len(self._ambiguous_pat_ids),
            )
        return resolution.snapshots

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
            "wideq_identity_ambiguous": len(self._ambiguous_pat_ids),
            "wideq_identity_unmatched": len(self._unmatched_pat_ids),
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

    def snapshot_for(self, device_id: str) -> dict[str, Any]:
        """Return the retained WideQ snapshot keyed by stable PAT id."""
        return (self.data or {}).get(device_id, {})

    async def async_restore_energy_history(self) -> None:
        """Restore stable-id totals, then migrate the legacy alias cache."""
        today = dt_util.now().date()
        current_day = today.isoformat()
        current_month = today.strftime("%Y-%m")

        def _restore(device_id: str, raw_item: Any) -> bool:
            if (
                device_id not in self._energy_history_targets
                or not isinstance(raw_item, dict)
            ):
                return False
            period_date = raw_item.get("period_date")
            if not isinstance(period_date, str):
                return False
            restored: dict[str, Any] = {
                "period_date": period_date,
                "fetched_at": raw_item.get("fetched_at"),
            }
            if period_date == current_day:
                value = self._energy_value(raw_item.get("today"))
                if value is not None:
                    restored["today"] = value
            if period_date[:7] == current_month:
                value = self._energy_value(raw_item.get("month"))
                if value is not None:
                    restored["month"] = value
            if "today" not in restored and "month" not in restored:
                return False
            self._energy_history[device_id] = restored
            self._energy_history_restored.add(device_id)
            return True

        if self._energy_history_store is not None:
            stored = await self._energy_history_store.async_load()
            items = stored.get("items") if isinstance(stored, dict) else None
            if isinstance(items, dict):
                for device_id, raw_item in items.items():
                    _restore(device_id, raw_item)

        if self._legacy_energy_history_store is None:
            return
        legacy = await self._legacy_energy_history_store.async_load()
        legacy_items = legacy.get("items") if isinstance(legacy, dict) else None
        if not isinstance(legacy_items, dict):
            return

        alias_to_ids: dict[str, list[str]] = {}
        for device_id, identity in self._pat_devices.items():
            if device_id in self._energy_history_targets:
                alias_to_ids.setdefault(identity.alias, []).append(device_id)

        migrated = False
        for alias, raw_item in legacy_items.items():
            candidates = alias_to_ids.get(alias, [])
            if len(candidates) != 1 or candidates[0] in self._energy_history:
                continue
            migrated = _restore(candidates[0], raw_item) or migrated

        if migrated and self._energy_history_store is not None:
            await self._energy_history_store.async_save(
                self._energy_history_payload()
            )

    @staticmethod
    def _energy_value(value: Any) -> float | None:
        """Return a finite persisted energy value."""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number < 0 or number in {float("inf"), float("-inf")} or number != number:
            return None
        return number

    def _energy_history_payload(self) -> dict[str, Any]:
        """Serialize only verified energy targets."""
        return {
            "items": {
                device_id: dict(item)
                for device_id, item in self._energy_history.items()
                if device_id in self._energy_history_targets
            }
        }

    def _schedule_energy_history_save(self) -> None:
        if self._energy_history_store is None:
            return
        self._energy_history_store.async_delay_save(
            self._energy_history_payload,
            WIDEQ_ENERGY_HISTORY_STORE_SAVE_DELAY,
        )

    async def async_persist_energy_history(self) -> None:
        """Flush the latest energy cache during config-entry unload."""
        if self._energy_history_store is None:
            return
        await self._energy_history_store.async_save(self._energy_history_payload())

    async def _async_refresh_energy_history(self) -> None:
        """Refresh verified energy endpoints at a separate low cadence."""
        if not self._energy_history_targets:
            return
        now = datetime.now(timezone.utc)
        if (
            self._energy_history_next_attempt is not None
            and now < self._energy_history_next_attempt
        ):
            return

        target_date = dt_util.now().date()
        failed = False
        server_down = False
        cache_changed = False
        for device_id, appliance in self._energy_history_targets.items():
            if device_id in self._energy_history_unsupported:
                continue
            wideq_device_id = self._pat_to_wideq.get(device_id)
            if wideq_device_id is None:
                failed = True
                self._energy_history_failures[device_id] = (
                    "WideQ identity is not currently resolved"
                )
                continue
            try:
                values = await self.client.async_get_energy_usage(
                    wideq_device_id,
                    appliance,
                    target_date=target_date,
                    before_request=self.rate_limiter.acquire,
                )
                if values is None:
                    raise ValueError("no supported energy-history response")
            except Exception as err:  # noqa: BLE001
                # 0005 is returned consistently by legacy refrigerators that do
                # not implement the ThinQ Web energy service. Probe once per HA
                # session, then leave their entities unavailable.
                if str(getattr(err, "code", "")) == "0005":
                    self._energy_history_unsupported.add(device_id)
                    self._energy_history_failures.pop(device_id, None)
                    _LOGGER.info(
                        "wideq energy history is not supported for %s",
                        device_id,
                    )
                    continue

                failed = True
                self._energy_history_failures[device_id] = (
                    f"{type(err).__name__}: {err}"
                )
                if is_server_unavailable(err):
                    server_down = True
                    break
                continue

            self._energy_history[device_id] = {
                **values,
                "period_date": target_date.isoformat(),
                "fetched_at": now.isoformat(),
            }
            self._energy_history_restored.discard(device_id)
            self._energy_history_failures.pop(device_id, None)
            cache_changed = True

        self._energy_history_batch_stale = failed
        if cache_changed:
            self._schedule_energy_history_save()
        retry = (
            WIDEQ_ENERGY_HISTORY_FAILURE_RETRY
            if failed
            else WIDEQ_ENERGY_HISTORY_INTERVAL
        )
        self._energy_history_next_attempt = now + timedelta(seconds=retry)
        if failed:
            reason = (
                "LG service unavailable" if server_down else "device response error"
            )
            _LOGGER.warning(
                "wideq energy history refresh deferred for %ds (%s); "
                "cached values retained",
                retry,
                reason,
            )

    def energy_history_value(self, device_id: str, key: str) -> float | None:
        """Return a period-safe cached energy-history value."""
        item = self._energy_history.get(device_id)
        if item is None:
            return None
        target_date = dt_util.now().date()
        period_date = item.get("period_date")
        if key == "today" and period_date != target_date.isoformat():
            return None
        if key == "month" and str(period_date)[:7] != target_date.strftime("%Y-%m"):
            return None
        value = item.get(key)
        return float(value) if value is not None else None

    def energy_history_available(self, device_id: str, key: str) -> bool:
        """Return whether a valid current-period history value is cached."""
        return self.energy_history_value(device_id, key) is not None

    def energy_history_attributes(self, device_id: str) -> dict[str, Any]:
        """Return diagnostics for one energy-history target."""
        item = self._energy_history.get(device_id, {})
        return {
            "energy_source": "wideq_energy_history",
            "energy_history_last_success": item.get("fetched_at"),
            "energy_history_restored": device_id in self._energy_history_restored,
            "energy_history_stale": (
                self._energy_history_batch_stale
                or device_id in self._energy_history_restored
                or device_id in self._energy_history_failures
                or (
                    bool(item)
                    and item.get("period_date") != dt_util.now().date().isoformat()
                )
            ),
            "energy_history_supported": (
                False if device_id in self._energy_history_unsupported else True
            ),
        }

    async def async_control(
        self,
        device_id: str,
        ctrl_key: str,
        *,
        request_factory: Callable[[], dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> None:
        """Send one wideq control command for a device (rate-limited)."""
        if self.circuit_open:
            raise HomeAssistantError(
                "LG ThinQ wideq service is unavailable; waiting for recovery probe"
            )
        # Keep read-modify-write payloads for one appliance serialized. The
        # global limiter spaces request starts, but intentionally does not hold
        # its lock for the duration of network I/O.
        lock = self._control_locks.setdefault(device_id, asyncio.Lock())
        async with lock:
            async with self._io_lock:
                # Re-check after waiting: a prior command or scheduled poll may
                # have opened the circuit while this caller was queued.
                if self.circuit_open:
                    raise HomeAssistantError(
                        "LG ThinQ wideq service is unavailable; waiting for recovery probe"
                    )
                wideq_device_id = self._pat_to_wideq.get(device_id)
                if wideq_device_id is None:
                    # Controls are allowed before the deliberately delayed first
                    # poll. Resolve once under the same global I/O lock, count
                    # that physical snapshot request separately, and retain it.
                    await self.rate_limiter.acquire()
                    devices = await self.client.async_get_snapshots()
                    snapshots = self._resolve_devices(devices)
                    if snapshots:
                        current = dict(self.data or {})
                        current.update(snapshots)
                        self.async_set_updated_data(current)
                    wideq_device_id = self._pat_to_wideq.get(device_id)
                if wideq_device_id is None:
                    reason = (
                        "ambiguous alias/model"
                        if device_id in self._ambiguous_pat_ids
                        else "no matching WideQ device"
                    )
                    raise HomeAssistantError(
                        f"LG ThinQ WideQ identity unavailable: {reason}"
                    )
                if request_factory is not None:
                    kwargs = request_factory()
                await self.rate_limiter.acquire()
                await self.client.async_control(
                    wideq_device_id, ctrl_key, **kwargs
                )

    def apply_optimistic(self, device_id: str, key: str, value: Any) -> None:
        """Reflect a just-sent value immediately; the next poll confirms it.

        wideq-only fields have no MQTT push, so without this the UI would lag
        until the next scheduled poll. A rejected write self-corrects then.
        """
        data = dict(self.data or {})
        snap = dict(data.get(device_id, {}))
        snap[key] = value
        data[device_id] = snap
        self.async_set_updated_data(data)
