"""LG ThinQ Hybrid (my_lg) — PAT + MQTT push primary, wideq conditional (later)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_COUNTRY,
    CONF_LANGUAGE,
    CONF_WIDEQ_CLIENT_ID,
    CONF_WIDEQ_TOKEN,
    DEFAULT_AC_ACTIVE_INTERVAL,
    DEFAULT_APPLIANCE_ACTIVE_INTERVAL,
    DEFAULT_COUNTRY,
    DEFAULT_IDLE_INTERVAL,
    DEFAULT_LANGUAGE,
    DEVICE_TYPE_AIR_CONDITIONER,
    DEVICE_TYPE_COOKTOP,
    DEVICE_TYPE_DEHUMIDIFIER,
    DEVICE_TYPE_KIMCHI_REFRIGERATOR,
    DEVICE_TYPE_OVEN,
    DEVICE_TYPE_REFRIGERATOR,
    DEVICE_TYPE_STYLER,
    DEVICE_TYPE_WASHTOWER,
    DEVICE_TYPE_WATER_PURIFIER,
    DOMAIN,
    OPT_AC_ACTIVE_INTERVAL,
    OPT_APPLIANCE_ACTIVE_INTERVAL,
    OPT_IDLE_INTERVAL,
    PAT_DEVICE_LIST_TIMEOUT,
    PAT_PREPARE_CALL_TIMEOUT,
    PAT_PREPARE_CONCURRENCY,
    PLATFORMS,
    SUPPORTED_DEVICE_TYPES,
    WATER_PUSH_CODES,
    WIDEQ_ENERGY_HISTORY_STORE_VERSION,
    WIDEQ_ENERGY_HISTORY_LEGACY_STORE_VERSION,
    WIDEQ_MAX_CALLS_PER_HOUR,
    WIDEQ_MIN_CALL_SPACING,
    WIDEQ_DEVICE_MAP_STORE_VERSION,
)
from .coordinator import PatDeviceCoordinator
from .coordinator_wideq import WideqCoordinator
from .device_identity import PatDeviceIdentity
from .feature_catalog import load_catalogs
from .mqtt import MyLgMqtt
from .rate_limiter import GlobalRateLimiter
from .services import async_register_services
from .startup import StartupMetrics, async_prepare_coordinators
from .wideq_client import WideqClient

_LOGGER = logging.getLogger(__name__)

POWER_ON = "POWER_ON"

# Non-running states reported through PAT/MQTT for appliances whose wideq-only
# detail is useful while a cycle is active. Everything else (including pause,
# reserved, and error) stays on the active cadence.
_INACTIVE_RUN_STATES = {
    None,
    "COMPLETE",
    "END",
    "INITIAL",
    "POWER_OFF",
    "RUNNING_END",
}


def _is_ac_active(coordinator: PatDeviceCoordinator) -> bool:
    """Return whether PAT/MQTT requests the AC-class collection cadence."""
    if coordinator.device_type == DEVICE_TYPE_DEHUMIDIFIER:
        # Preserve the existing 600s behavior while powered on; WATER_IS_FULL
        # push still requests a prompt refresh and this cadence later sees clear.
        return (
            coordinator.get("operation", "dehumidifierOperationMode") == POWER_ON
        )
    return (
        coordinator.device_type == DEVICE_TYPE_AIR_CONDITIONER
        and coordinator.get("operation", "airConOperationMode") == POWER_ON
    )


def _is_appliance_active(coordinator: PatDeviceCoordinator) -> bool:
    """Return whether PAT/MQTT reports a washer/dryer/styler cycle active."""
    if coordinator.device_type == DEVICE_TYPE_WASHTOWER:
        return any(
            coordinator.get(part, "runState", "currentState")
            not in _INACTIVE_RUN_STATES
            for part in ("washer", "dryer")
        )
    if coordinator.device_type == DEVICE_TYPE_STYLER:
        return (
            coordinator.get("runState", "currentState")
            not in _INACTIVE_RUN_STATES
        )
    return False


def _wideq_interval(
    coordinators: list[PatDeviceCoordinator],
    ac_active_interval: int,
    appliance_active_interval: int,
    idle_interval: int,
) -> int:
    """Choose collection cadence exclusively from PAT/MQTT state."""
    intervals: list[int] = []
    if any(_is_ac_active(c) for c in coordinators):
        intervals.append(ac_active_interval)
    if any(_is_appliance_active(c) for c in coordinators):
        intervals.append(appliance_active_interval)
    return min(intervals) if intervals else idle_interval


@dataclass
class MyLgData:
    """Runtime data stored on the config entry."""

    api: object
    coordinators: dict[str, PatDeviceCoordinator] = field(default_factory=dict)
    mqtt: MyLgMqtt | None = None
    wideq_client: WideqClient | None = None
    wideq_coordinator: WideqCoordinator | None = None
    startup_metrics: StartupMetrics | None = None


MyLgConfigEntry = ConfigEntry  # ConfigEntry[MyLgData] at type-check time


async def async_setup_entry(hass: HomeAssistant, entry: MyLgConfigEntry) -> bool:
    """Set up my_lg from a config entry."""
    from thinqconnect import ThinQApi

    session = async_get_clientsession(hass)
    token = entry.data[CONF_ACCESS_TOKEN]
    country = entry.data.get(CONF_COUNTRY, DEFAULT_COUNTRY)
    client_id = entry.data[CONF_CLIENT_ID]

    api = ThinQApi(
        session=session,
        access_token=token,
        country_code=country,
        client_id=client_id,
    )

    try:
        devices = await asyncio.wait_for(
            api.async_get_device_list(), timeout=PAT_DEVICE_LIST_TIMEOUT
        )
    except Exception as err:  # noqa: BLE001
        raise ConfigEntryNotReady(f"device list failed: {err}") from err

    data = MyLgData(api=api)

    for device in devices or []:
        info = device.get("deviceInfo", {})
        if info.get("deviceType") not in SUPPORTED_DEVICE_TYPES:
            continue  # everything else stays on official lg_thinq
        coordinator = PatDeviceCoordinator(hass, entry, api, device)
        data.coordinators[coordinator.device_id] = coordinator

    # Seed initial PAT state/profile with a small bounded fan-out.  One offline
    # or stalled appliance is non-fatal and later recovers through MQTT push or
    # the hourly REST fallback.  WideQ still performs no eager setup poll.
    _, data.startup_metrics = await async_prepare_coordinators(
        list(data.coordinators.values()),
        concurrency=PAT_PREPARE_CONCURRENCY,
        call_timeout=PAT_PREPARE_CALL_TIMEOUT,
    )
    _LOGGER.info(
        "my_lg PAT prepared %d devices in %.3fs "
        "(status=%d, profile=%d, timeouts=%d/%d)",
        data.startup_metrics.supported_devices,
        data.startup_metrics.preparation_seconds,
        data.startup_metrics.status_ready,
        data.startup_metrics.profile_ready,
        data.startup_metrics.status_timeouts,
        data.startup_metrics.profile_timeouts,
    )

    if not data.coordinators:
        _LOGGER.warning("my_lg: no supported devices found (Stage 1 = air conditioners)")

    # Dispatch DEVICE_PUSH notifications to that device's event entity, and on a
    # water-tank push also refresh wideq promptly (rate-limited).
    def _on_push(device_id: str, code: str) -> None:
        async_dispatcher_send(hass, f"{DOMAIN}_push_{device_id}", code)
        if code in WATER_PUSH_CODES and data.wideq_coordinator is not None:
            hass.async_create_task(data.wideq_coordinator.async_request_refresh())

    # MQTT push (best-effort; REST fallback keeps working if this fails).
    mqtt = MyLgMqtt(hass, api, client_id, data.coordinators, on_push=_on_push)
    await mqtt.async_start()
    data.mqtt = mqtt

    # wideq (optional): AC realtime power/energy, dehumidifier water tank, etc.
    if entry.data.get(CONF_WIDEQ_TOKEN):
        await _setup_wideq(hass, entry, data)

    # Generated catalogs are file-backed. Warm their process-wide caches in an
    # executor so synchronous entity factories never perform disk I/O on the
    # Home Assistant event loop.
    await hass.async_add_executor_job(load_catalogs)

    entry.runtime_data = data
    async_register_services(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options))
    return True


async def _setup_wideq(
    hass: HomeAssistant, entry: MyLgConfigEntry, data: MyLgData
) -> None:
    """Build the wideq client + single low-rate coordinator (AC energy, etc.)."""
    session = async_create_clientsession(hass)
    client = WideqClient(
        session,
        entry.data[CONF_WIDEQ_TOKEN],
        entry.data.get(CONF_COUNTRY, DEFAULT_COUNTRY),
        entry.data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
        entry.data.get(CONF_WIDEQ_CLIENT_ID),
    )
    limiter = GlobalRateLimiter(WIDEQ_MAX_CALLS_PER_HOUR, WIDEQ_MIN_CALL_SPACING)

    coordinators = list(data.coordinators.values())
    # PAT exposes none of these energy quantities. Each mapping below was
    # verified against the device's current ThinQ app module and a live
    # response. WashTower is intentionally absent: LG returns zero from its
    # history service for this combined model, so its snapshot counters remain
    # the only truthful energy source.
    energy_history_appliance_by_type = {
        DEVICE_TYPE_AIR_CONDITIONER: "aircon",
        DEVICE_TYPE_DEHUMIDIFIER: "aircon",
        DEVICE_TYPE_REFRIGERATOR: "fridge",
        DEVICE_TYPE_KIMCHI_REFRIGERATOR: "fridge",
        DEVICE_TYPE_COOKTOP: "devices",
        DEVICE_TYPE_OVEN: "devices",
        DEVICE_TYPE_WATER_PURIFIER: "devices",
        DEVICE_TYPE_STYLER: "devices",
    }
    energy_history_targets = {
        coordinator.device_id: energy_history_appliance_by_type[coordinator.device_type]
        for coordinator in coordinators
        if coordinator.device_type in energy_history_appliance_by_type
    }
    pat_devices = {
        coordinator.device_id: PatDeviceIdentity(
            device_id=coordinator.device_id,
            alias=coordinator.alias,
            model=coordinator.model,
        )
        for coordinator in coordinators
    }

    opts = entry.options
    ac_active_interval = opts.get(
        OPT_AC_ACTIVE_INTERVAL, DEFAULT_AC_ACTIVE_INTERVAL
    )
    appliance_active_interval = opts.get(
        OPT_APPLIANCE_ACTIVE_INTERVAL, DEFAULT_APPLIANCE_ACTIVE_INTERVAL
    )
    idle_interval = opts.get(OPT_IDLE_INTERVAL, DEFAULT_IDLE_INTERVAL)

    def interval_fn() -> int:
        # Device activity comes from PAT/MQTT; wideq only collects missing data.
        return _wideq_interval(
            coordinators,
            ac_active_interval,
            appliance_active_interval,
            idle_interval,
        )

    data.wideq_client = client
    energy_history_store: Store[dict[str, Any]] = Store(
        hass,
        WIDEQ_ENERGY_HISTORY_STORE_VERSION,
        f"{DOMAIN}.wideq_energy_history_v2.{entry.entry_id}",
    )
    legacy_energy_history_store: Store[dict[str, Any]] = Store(
        hass,
        WIDEQ_ENERGY_HISTORY_LEGACY_STORE_VERSION,
        f"{DOMAIN}.wideq_energy_history.{entry.entry_id}",
    )
    device_map_store: Store[dict[str, Any]] = Store(
        hass,
        WIDEQ_DEVICE_MAP_STORE_VERSION,
        f"{DOMAIN}.wideq_device_map.{entry.entry_id}",
    )
    data.wideq_coordinator = WideqCoordinator(
        hass,
        entry,
        client,
        limiter,
        interval_fn,
        energy_history_targets,
        energy_history_store,
        pat_devices,
        device_map_store,
        legacy_energy_history_store,
    )
    await data.wideq_coordinator.async_restore_device_map()
    await data.wideq_coordinator.async_restore_energy_history()
    for coordinator in coordinators:
        entry.async_on_unload(
            coordinator.async_add_listener(data.wideq_coordinator.reconcile_interval)
        )
    # No first_refresh: wideq must not eager-poll on setup (restart-burst
    # avoidance). The first poll fires one interval after entities subscribe.


async def async_unload_entry(hass: HomeAssistant, entry: MyLgConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    data: MyLgData | None = getattr(entry, "runtime_data", None)
    if data and data.mqtt:
        await data.mqtt.async_stop()
    if data and data.wideq_coordinator:
        await data.wideq_coordinator.async_persist_energy_history()
        await data.wideq_coordinator.async_persist_device_map()
    if data and data.wideq_client:
        await data.wideq_client.async_close()
    return unload_ok


async def _async_reload_on_options(hass: HomeAssistant, entry: MyLgConfigEntry) -> None:
    """Reload when options (e.g. polling intervals) change."""
    await hass.config_entries.async_reload(entry.entry_id)
