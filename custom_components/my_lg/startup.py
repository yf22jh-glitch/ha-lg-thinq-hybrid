"""Bounded startup helpers for PAT device preparation.

The ThinQ Connect device list is account-wide.  Preparing every device in a
serial loop makes the whole config entry wait for the sum of all device/API
latencies, while unbounded ``gather`` can create a restart burst.  This module
keeps the policy small and independently testable: a fixed number of devices
may prepare concurrently and every individual network operation has a hard
deadline.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from time import monotonic
from typing import Any

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DevicePreparationResult:
    """Result of the initial PAT status/profile preparation for one device."""

    device_id: str
    status_ready: bool
    profile_ready: bool
    status_timed_out: bool = False
    profile_timed_out: bool = False


@dataclass(frozen=True)
class StartupMetrics:
    """Non-sensitive measurements retained in config-entry runtime data."""

    supported_devices: int
    status_ready: int
    profile_ready: int
    status_timeouts: int
    profile_timeouts: int
    preparation_seconds: float


async def _prepare_one(
    coordinator: Any,
    semaphore: asyncio.Semaphore,
    call_timeout: float,
) -> DevicePreparationResult:
    """Prepare one coordinator without allowing a stalled call to block setup."""
    status_timed_out = False
    profile_timed_out = False

    async with semaphore:
        try:
            await asyncio.wait_for(coordinator.async_refresh(), timeout=call_timeout)
        except asyncio.TimeoutError:
            status_timed_out = True
            _LOGGER.warning(
                "%s: initial PAT status timed out after %.0fs; continuing setup",
                coordinator.alias,
                call_timeout,
            )
        except Exception as err:  # noqa: BLE001 - isolate one device at setup
            _LOGGER.warning(
                "%s: initial PAT status failed; continuing setup: %s",
                coordinator.alias,
                err,
            )
        status_ready = bool(getattr(coordinator, "last_update_success", False))

        try:
            profile_ready = bool(
                await asyncio.wait_for(
                    coordinator.async_load_profile(), timeout=call_timeout
                )
            )
        except asyncio.TimeoutError:
            profile_timed_out = True
            profile_ready = False
            _LOGGER.warning(
                "%s: PAT profile timed out after %.0fs; continuing setup",
                coordinator.alias,
                call_timeout,
            )
        except Exception as err:  # noqa: BLE001 - isolate one device at setup
            profile_ready = False
            _LOGGER.warning(
                "%s: PAT profile failed; continuing setup: %s",
                coordinator.alias,
                err,
            )

    return DevicePreparationResult(
        device_id=coordinator.device_id,
        status_ready=status_ready,
        profile_ready=profile_ready,
        status_timed_out=status_timed_out,
        profile_timed_out=profile_timed_out,
    )


async def async_prepare_coordinators(
    coordinators: list[Any],
    *,
    concurrency: int,
    call_timeout: float,
) -> tuple[dict[str, DevicePreparationResult], StartupMetrics]:
    """Prepare PAT coordinators with bounded concurrency and per-call timeout."""
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    if call_timeout <= 0:
        raise ValueError("call_timeout must be positive")

    started = monotonic()
    semaphore = asyncio.Semaphore(concurrency)
    results = await asyncio.gather(
        *(
            _prepare_one(coordinator, semaphore, call_timeout)
            for coordinator in coordinators
        )
    )
    by_device = {result.device_id: result for result in results}
    metrics = StartupMetrics(
        supported_devices=len(results),
        status_ready=sum(result.status_ready for result in results),
        profile_ready=sum(result.profile_ready for result in results),
        status_timeouts=sum(result.status_timed_out for result in results),
        profile_timeouts=sum(result.profile_timed_out for result in results),
        preparation_seconds=round(monotonic() - started, 3),
    )
    return by_device, metrics


async def async_recover_startup(
    data: Any,
    *,
    delays: tuple[float, ...] = (30, 60, 120),
    concurrency: int = 3,
    call_timeout: float = 15,
) -> None:
    """Recover cold-start reads without changing the normal polling cadence.

    PAT may report NOT_CONNECTED_DEVICE while the server reconnects. Retry
    only missing initial snapshots/profiles, with the same bounded fan-out as
    setup. WideQ gets one shared initial read through its existing limiter,
    instead of waiting a full idle interval before showing any live values.
    The config entry owns this task and cancels it on unload or HA shutdown.
    """
    if concurrency < 1 or call_timeout <= 0 or any(delay < 0 for delay in delays):
        raise ValueError("Recovery needs positive limits and nonnegative delays")
    semaphore = asyncio.Semaphore(concurrency)

    async def recover(coordinator: Any) -> None:
        async with semaphore:
            if not isinstance(coordinator.profile, dict):
                try:
                    if await asyncio.wait_for(
                        coordinator.async_load_profile(), timeout=call_timeout
                    ):
                        coordinator.async_update_listeners()
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("%s: startup profile read failed: %s", coordinator.alias, err)
            try:
                if not coordinator.startup_status_ready:
                    await asyncio.wait_for(
                        coordinator.async_refresh(), timeout=call_timeout
                    )
            except Exception as err:  # noqa: BLE001 - one device cannot block others
                _LOGGER.debug("%s: startup recovery read failed: %s", coordinator.alias, err)

    async def warm_wideq() -> None:
        coordinator = data.wideq_coordinator
        if coordinator is not None and not coordinator.data:
            # Its account-wide read can include rate-limited history requests;
            # preserve its own timeouts, quota and cancellation semantics.
            try:
                await coordinator.async_request_refresh()
            except Exception:  # noqa: BLE001 - normal periodic refresh remains
                _LOGGER.exception("Initial WideQ refresh failed")

    for attempt, delay in enumerate(delays):
        pending = [coordinator for coordinator in data.coordinators.values()
                   if not coordinator.startup_status_ready
                   or not isinstance(coordinator.profile, dict)]
        if not pending and (attempt or data.wideq_coordinator is None):
            return
        await asyncio.sleep(delay)
        jobs = [recover(coordinator) for coordinator in pending]
        if attempt == 0:
            jobs.append(warm_wideq())
        await asyncio.gather(*jobs)
