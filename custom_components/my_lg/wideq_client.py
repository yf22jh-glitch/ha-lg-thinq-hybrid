"""Thin wrapper around the vendored wideq client.

Used only for the few fields the official PAT API does not expose
(air-conditioner realtime power / cumulative energy, etc.). Kept minimal and
called at a low, conditional cadence (see the polling scheduler) to avoid the
rate-limit that caused the original 24h block.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import math
from collections.abc import Awaitable, Callable, Iterator
from datetime import date
from typing import Any

import aiohttp

from .device_identity import WideqDeviceData
from .wideq.core_exceptions import (
    AuthenticationError,
    ClientDisconnected,
    InvalidCredentialError,
    NotLoggedInError,
    TokenError,
)

_LOGGER = logging.getLogger(__name__)

_RECONNECTABLE_ERRORS = (
    AuthenticationError,
    ClientDisconnected,
    InvalidCredentialError,
    NotLoggedInError,
    TokenError,
)


def _exception_chain(err: BaseException) -> Iterator[BaseException]:
    """Yield an exception and its explicit/implicit causes once each."""
    seen: set[int] = set()
    current: BaseException | None = err
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def is_server_unavailable(err: BaseException) -> bool:
    """Return True for transient network and LG HTTP 5xx failures."""
    for item in _exception_chain(err):
        if isinstance(item, (asyncio.TimeoutError, aiohttp.ClientConnectionError)):
            return True
        for attribute in ("status", "code"):
            raw_status = getattr(item, attribute, None)
            try:
                status = int(raw_status)
            except (TypeError, ValueError):
                continue
            if 500 <= status <= 599:
                return True
    return False


def _is_reconnectable(err: BaseException) -> bool:
    """Return True only when rebuilding the authenticated session can help."""
    return any(
        isinstance(item, _RECONNECTABLE_ERRORS) for item in _exception_chain(err)
    )


class WideqReconnectError(Exception):
    """Preserve both the initial session error and reconnect failure."""

    def __init__(self, initial: BaseException, reconnect: BaseException) -> None:
        self.initial = initial
        self.reconnect = reconnect
        super().__init__(
            f"initial={type(initial).__name__}: {initial}; "
            f"reconnect={type(reconnect).__name__}: {reconnect}"
        )


def ac_energy_from_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    """Extract AC energy fields from a wideq thinq2 snapshot (dotted keys)."""
    return {
        "power_w": snap.get("airState.energy.onCurrent"),
        "energy_today_wh": snap.get("airState.energy.dailyTotal"),
        "energy_week_wh": snap.get("airState.energy.weeklyTotal"),
        "energy_month_wh": snap.get("airState.energy.monthlyTotal"),
    }


def _history_items(history: Any) -> list[dict[str, Any]] | None:
    """Normalize ThinQ Web energy-history response shapes."""
    if isinstance(history, list):
        return history
    if isinstance(history, dict) and isinstance(history.get("item"), list):
        return history["item"]
    return None


def _energy_wh(value: Any) -> float | None:
    """Return one verified ThinQ energy quantity in Wh.

    ``NO_DATA`` and a missing field are not proof of zero consumption.  The
    app frequently omits the current day until its backend aggregation has
    completed, so turning either case into zero produces a believable but
    false daily total.  A numeric zero, on the other hand, is explicit data.
    """
    if value is None or value == "NO_DATA" or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def parse_ac_energy_history(
    history: Any, target_date: date
) -> dict[str, float] | None:
    """Parse one AC daily history response into period totals in kWh."""
    items = _history_items(history)
    if items is None:
        return None
    today_key = target_date.isoformat()
    today_wh: float | None = None
    month_wh = 0.0
    month_samples = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        used_date = str(item.get("usedDate", ""))
        if not used_date.startswith(target_date.strftime("%Y-%m")):
            continue
        value = _energy_wh(item.get("energyData"))
        if value is None:
            continue
        month_wh += value
        month_samples += 1
        if used_date[:10] == today_key:
            today_wh = (today_wh or 0.0) + value
    result: dict[str, float] = {}
    if today_wh is not None:
        result["today"] = round(today_wh / 1000, 3)
    if month_samples:
        result["month"] = round(month_wh / 1000, 3)
    return result or None


def parse_device_energy_history(
    history: Any, target_date: date
) -> dict[str, float] | None:
    """Parse a generic ThinQ device daily history response into kWh.

    Despite the response field being named ``power``, ThinQ returns one energy
    quantity in Wh for each day. This shape is used by the app for the water
    purifier, cooktop, oven/range, and styler.
    """
    items = _history_items(history)
    if items is None:
        return None
    today_key = target_date.isoformat()
    today_wh: float | None = None
    month_wh = 0.0
    month_samples = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        used_date = str(item.get("usedDate", ""))
        if not used_date.startswith(target_date.strftime("%Y-%m")):
            continue
        value = _energy_wh(item.get("power"))
        if value is None:
            continue
        month_wh += value
        month_samples += 1
        if used_date[:10] == today_key:
            today_wh = (today_wh or 0.0) + value
    result: dict[str, float] = {}
    if today_wh is not None:
        result["today"] = round(today_wh / 1000, 3)
    if month_samples:
        result["month"] = round(month_wh / 1000, 3)
    return result or None


def parse_fridge_energy_history(
    today_history: Any, month_history: Any
) -> dict[str, float] | None:
    """Parse refrigerator hourly/monthly history responses into kWh."""
    today_items = _history_items(today_history)
    month_items = _history_items(month_history)

    def total_wh(items: list[dict[str, Any]] | None) -> float | None:
        if items is None:
            return None
        total = 0.0
        samples = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            raw = item.get("power", item.get("energyData", item.get("useAmount")))
            value = _energy_wh(raw)
            if value is None:
                continue
            total += value
            samples += 1
        return total if samples else None

    # These are two independently date-scoped endpoints.  Refrigerator models
    # vary in whether hourly items contain an ISO date, an hour label, or no
    # date field, so validating the inner label would reject legitimate data.
    today_wh = total_wh(today_items)
    month_wh = total_wh(month_items)
    result: dict[str, float] = {}
    if today_wh is not None:
        result["today"] = round(today_wh / 1000, 3)
    if month_wh is not None:
        result["month"] = round(month_wh / 1000, 3)
    return result or None


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

    async def async_get_snapshots(self) -> list[WideqDeviceData]:
        """Return stable WideQ ids plus snapshots in one account API call.

        PAT/WideQ identity resolution deliberately lives in the coordinator;
        aliases are matching metadata, never runtime dictionary keys.
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
            # A server maintenance/network failure cannot be repaired by logging
            # in again. Propagate it to the coordinator circuit breaker without
            # adding gateway/OAuth traffic. Reconnect once only for an explicitly
            # dead/invalid authenticated session.
            if is_server_unavailable(err) or not _is_reconnectable(err):
                raise
            _LOGGER.debug(
                "wideq session failed (%s); reconnecting and retrying once", err
            )
            try:
                await self.async_close()
                await self.async_connect()
                await self._client.refresh_devices()
            except Exception as reconnect_err:  # noqa: BLE001
                raise WideqReconnectError(err, reconnect_err) from reconnect_err
        out: list[WideqDeviceData] = []
        for dev in self._client.devices or []:
            raw = dev.as_dict()
            snap = raw.get("snapshot")
            alias = getattr(dev, "name", None) or raw.get("alias")
            model = getattr(dev, "model_name", None) or raw.get("modelName")
            if dev.device_id and alias and model:
                out.append(
                    WideqDeviceData(
                        device_id=dev.device_id,
                        alias=str(alias),
                        model=str(model),
                        snapshot=snap if isinstance(snap, dict) else {},
                    )
                )
        return out

    async def async_get_energy_usage(
        self,
        wideq_device_id: str,
        appliance: str,
        *,
        target_date: date,
        before_request: Callable[[], Awaitable[None]],
    ) -> dict[str, float] | None:
        """Read verified ThinQ Web energy-history endpoints without retrying.

        ``before_request`` is the integration's global rate limiter. It is
        invoked once per physical HTTP request so these optional reads stay
        under the same spacing/hourly cap as snapshots and controls.
        """
        if self._client is None:
            await self.async_connect()
        month_start = target_date.replace(day=1).isoformat()
        month_end = target_date.replace(
            day=calendar.monthrange(target_date.year, target_date.month)[1]
        ).isoformat()

        if appliance == "aircon":
            await before_request()
            history = await self._client.session.get2(
                f"service/aircon/{wideq_device_id}/energy-history"
                f"?period=day&startDate={month_start}&endDate={month_end}"
                "&saveEnergyYn=N"
            )
            return parse_ac_energy_history(history, target_date)

        if appliance == "fridge":
            today = target_date.isoformat()
            await before_request()
            today_history = await self._client.session.get2(
                f"service/fridge/{wideq_device_id}/energy-history"
                f"?period=hour&startDate={today}&endDate={today}"
            )
            await before_request()
            month_history = await self._client.session.get2(
                f"service/fridge/{wideq_device_id}/energy-history"
                f"?period=month&startDate={month_start}&endDate={month_end}"
            )
            return parse_fridge_energy_history(today_history, month_history)

        if appliance == "devices":
            await before_request()
            history = await self._client.session.get2(
                f"service/devices/{wideq_device_id}/energy-history"
                f"?period=day&startDate={month_start}&endDate={month_end}"
            )
            return parse_device_energy_history(history, target_date)

        raise ValueError(f"unsupported energy-history appliance: {appliance}")

    async def async_control(
        self,
        wideq_device_id: str,
        ctrl_key: str,
        *,
        command: str = "Set",
        data_key: str | None = None,
        value: Any = None,
        data_set_list: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        legacy_payload: dict[str, Any] | None = None,
    ) -> Any:
        """Send a single thinq2 control-sync command for a wideq-only field.

        Two shapes, matching the model's advertised control:
        - dataKey form (basicCtrl/settingInfo): one key/value.
        - dataSetList form (wModeCtrl): one key inside a dataSetList dict.
        A single write call is not a polling loop, so it is not a ban risk; the
        physical device does act, so callers gate real writes accordingly.
        """
        async def _do() -> Any:
            if not wideq_device_id:
                raise ValueError("wideq: missing stable device id")
            if self._client is None:
                await self.async_connect()
            # Same root fix as polling: renew the ~1h token before the call.
            await self._client.refresh_auth()
            session = self._client.session
            if legacy_payload is not None:
                return await session.set_device_controls(
                    wideq_device_id, legacy_payload
                )
            if payload is not None:
                return await session.device_v2_controls(
                    wideq_device_id, payload, None
                )
            if data_set_list is not None:
                request_payload = {
                    "ctrlKey": ctrl_key,
                    "command": command,
                    "dataSetList": data_set_list,
                }
                return await session.device_v2_controls(
                    wideq_device_id, request_payload, None
                )
            return await session.device_v2_controls(
                wideq_device_id, ctrl_key, command, data_key, value
            )

        try:
            return await _do()
        except Exception as err:  # noqa: BLE001
            # Keep writes under the same outage policy as snapshot polling.
            # LG maintenance/5xx and ordinary command rejections cannot be
            # repaired by logging in again; reconnecting there only multiplies
            # gateway/OAuth traffic during an outage. Retry exactly once only
            # when the authenticated session itself is explicitly invalid.
            if is_server_unavailable(err) or not _is_reconnectable(err):
                raise
            _LOGGER.debug(
                "wideq control session failed (%s); reconnecting and retrying once",
                err,
            )
            try:
                await self.async_close()
                await self.async_connect()
                return await _do()
            except Exception as reconnect_err:  # noqa: BLE001
                raise WideqReconnectError(err, reconnect_err) from reconnect_err

    async def async_close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("wideq close: %s", err)
            self._client = None
