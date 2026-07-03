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
        await self._client.refresh_devices()
        out: dict[str, dict[str, Any]] = {}
        for dev in self._client.devices or []:
            snap = dev.as_dict().get("snapshot")
            alias = getattr(dev, "name", None) or dev.as_dict().get("alias")
            if isinstance(snap, dict) and alias:
                out[alias] = snap
        return out

    async def async_close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("wideq close: %s", err)
            self._client = None
