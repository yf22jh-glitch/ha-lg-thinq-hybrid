"""LG ThinQ Hybrid (my_lg) — PAT + MQTT push primary, wideq conditional (later)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_COUNTRY,
    DEFAULT_COUNTRY,
    DOMAIN,
    PLATFORMS,
    SUPPORTED_DEVICE_TYPES,
)
from .coordinator import PatDeviceCoordinator
from .mqtt import MyLgMqtt

_LOGGER = logging.getLogger(__name__)


@dataclass
class MyLgData:
    """Runtime data stored on the config entry."""

    api: object
    coordinators: dict[str, PatDeviceCoordinator] = field(default_factory=dict)
    mqtt: MyLgMqtt | None = None


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
        devices = await api.async_get_device_list()
    except Exception as err:  # noqa: BLE001
        raise ConfigEntryNotReady(f"device list failed: {err}") from err

    data = MyLgData(api=api)

    for device in devices or []:
        info = device.get("deviceInfo", {})
        if info.get("deviceType") not in SUPPORTED_DEVICE_TYPES:
            continue  # everything else stays on official lg_thinq
        coordinator = PatDeviceCoordinator(hass, entry, api, device)
        # PAT is the official low-risk API; seed initial state so entities show
        # values immediately. (The no-eager-poll rule applies to wideq only.)
        await coordinator.async_config_entry_first_refresh()
        data.coordinators[coordinator.device_id] = coordinator

    if not data.coordinators:
        _LOGGER.warning("my_lg: no supported devices found (Stage 1 = air conditioners)")

    # MQTT push (best-effort; REST fallback keeps working if this fails).
    mqtt = MyLgMqtt(hass, api, client_id, data.coordinators)
    await mqtt.async_start()
    data.mqtt = mqtt

    entry.runtime_data = data
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MyLgConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    data: MyLgData | None = getattr(entry, "runtime_data", None)
    if data and data.mqtt:
        await data.mqtt.async_stop()
    return unload_ok


async def _async_reload_on_options(hass: HomeAssistant, entry: MyLgConfigEntry) -> None:
    """Reload when options (e.g. polling intervals) change."""
    await hass.config_entries.async_reload(entry.entry_id)
