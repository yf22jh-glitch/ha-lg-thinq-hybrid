"""Thin wrapper around the vendored wideq client.

Used only for the few fields the official PAT API does not expose
(air-conditioner realtime power / cumulative energy, etc.). Kept minimal and
called at a low, conditional cadence (see the polling scheduler) to avoid the
rate-limit that caused the original 24h block.
"""

from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)


def ac_energy_from_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    """Extract AC energy fields from a wideq thinq2 snapshot (dotted keys)."""
    return {
        "power_w": snap.get("airState.energy.onCurrent"),
        "energy_today_wh": snap.get("airState.energy.dailyTotal"),
        "energy_week_wh": snap.get("airState.energy.weeklyTotal"),
        "energy_month_wh": snap.get("airState.energy.monthlyTotal"),
    }


class WideqClient:
    """Persistent wideq client; one refresh_devices() call returns all snapshots."""

    def __init__(
        self,
        session,
        refresh_token: str,
        country: str,
        language: str,
        client_id: str | None,
    ) -> None:
        self._session = session
        self._token = refresh_token
        self._country = country
        self._language = language
        self._client_id = client_id
        self._client = None
        # alias -> wideq device id (differs from the PAT device id); populated
        # from each snapshot poll and, if control runs first, on demand.
        self._device_ids: dict[str, str] = {}

    async def async_connect(self):
        """Create the underlying ClientAsync from the stored refresh token."""
        from .wideq.core_async import ClientAsync

        self._client = await ClientAsync.from_token(
            self._token,
            country=self._country,
            language=self._language,
            aiohttp_session=self._session,
            client_id=self._client_id,
        )
        return self._client

    async def async_get_snapshots(self) -> dict[str, dict[str, Any]]:
        """Return {alias: snapshot} for all thinq2 devices in ONE API call.

        Keyed by alias because wideq device ids differ from the PAT device ids;
        the user-facing alias (e.g. "거실 에어컨") is shared by both APIs.
        """
        if self._client is None:
            await self.async_connect()
        try:
            # ROOT FIX: renew the wideq access token if near expiry BEFORE polling.
            # The token has a ~1h TTL and ClientAsync does NOT auto-renew it inside
            # refresh_devices, so without this call every poll fails with
            # "ThinQ APIv2 error" ~1h after connect and AC power/energy silently
            # dies (only a restart temporarily revived it). refresh_auth only hits
            # the network when the token is actually near expiry, so it's cheap.
            await self._client.refresh_auth()
            await self._client.refresh_devices()
        except Exception as err:  # noqa: BLE001
            # Backup: if the session is truly dead, fully reconnect once and retry
            # so the coordinator doesn't poll a dead client forever.
            _LOGGER.debug("wideq refresh failed (%s); reconnecting and retrying", err)
            await self.async_close()
            await self.async_connect()
            await self._client.refresh_devices()
        out: dict[str, dict[str, Any]] = {}
        for dev in self._client.devices or []:
            snap = dev.as_dict().get("snapshot")
            alias = getattr(dev, "name", None) or dev.as_dict().get("alias")
            if alias:
                self._device_ids[alias] = dev.device_id
            if isinstance(snap, dict) and alias:
                out[alias] = snap
        return out

    async def _device_id_for(self, alias: str) -> str | None:
        """Return the wideq device id for an alias, populating the map if needed.

        Control can be invoked before the first (deliberately delayed) snapshot
        poll, so refresh the device list on demand when the alias is unknown.
        """
        if alias in self._device_ids:
            return self._device_ids[alias]
        if self._client is None:
            await self.async_connect()
        await self._client.refresh_devices()
        for dev in self._client.devices or []:
            name = getattr(dev, "name", None) or dev.as_dict().get("alias")
            if name:
                self._device_ids[name] = dev.device_id
        return self._device_ids.get(alias)

    async def async_control(
        self,
        alias: str,
        ctrl_key: str,
        *,
        command: str = "Set",
        data_key: str | None = None,
        value: Any = None,
        data_set_list: dict[str, Any] | None = None,
    ) -> Any:
        """Send a single thinq2 control-sync command for a wideq-only field.

        Two shapes, matching the model's advertised control:
        - dataKey form (basicCtrl/settingInfo): one key/value.
        - dataSetList form (wModeCtrl): one key inside a dataSetList dict.
        A single write call is not a polling loop, so it is not a ban risk; the
        physical device does act, so callers gate real writes accordingly.
        """
        device_id = await self._device_id_for(alias)
        if not device_id:
            raise ValueError(f"wideq: no device id for alias {alias!r}")

        async def _do() -> Any:
            # Same root fix as polling: renew the ~1h token before the call.
            await self._client.refresh_auth()
            session = self._client.session
            if data_set_list is not None:
                payload = {
                    "ctrlKey": ctrl_key,
                    "command": command,
                    "dataSetList": data_set_list,
                }
                return await session.device_v2_controls(device_id, payload, None)
            return await session.device_v2_controls(
                device_id, ctrl_key, command, data_key, value
            )

        try:
            return await _do()
        except Exception as err:  # noqa: BLE001
            # Reconnect once if the session died, then retry (mirrors polling).
            _LOGGER.debug("wideq control failed (%s); reconnecting and retrying", err)
            await self.async_close()
            await self.async_connect()
            device_id = await self._device_id_for(alias) or device_id
            return await _do()

    async def async_close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("wideq close: %s", err)
            self._client = None
