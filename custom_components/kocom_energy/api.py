"""Async client for the Kocom apartment energy server."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime
import logging
import math
import struct

from .exceptions import (
    AuthenticationError,
    EnergyDataPendingError,
    KocomEnergyError,
    ProtocolError,
)
from .util import hex_to_ascii, hex_to_double, string_to_hex

_LOGGER = logging.getLogger(__name__)

_MAGIC = bytes.fromhex("78563412")
_ERROR_HEADER_PREFIX = bytes.fromhex("7856341210")
_AUTH_RESPONSE = bytes.fromhex(
    "7856341201001001040000000000000000000000000000000000000000000000"
)

_READ_TIMEOUT = 10.0
_READ_IDLE_TIMEOUT = 0.20
_MAX_RESPONSE_SIZE = 65536


def _month_at_offset(now: datetime, offset: int) -> str:
    """Return YYYYMM for a month relative to *now*."""
    month_index = now.year * 12 + now.month - 1 + offset
    year, month_zero_based = divmod(month_index, 12)
    return f"{year:04d}{month_zero_based + 1:02d}"


def _decode_usage(response_hex: str, start: int, end: int) -> float:
    """Decode and validate a cumulative usage field."""
    try:
        value = hex_to_double(response_hex[start:end])
    except (ValueError, UnicodeDecodeError, struct.error) as err:
        raise ProtocolError(f"Invalid usage field at {start}:{end}") from err
    if not math.isfinite(value) or value < 0:
        raise ProtocolError(f"Invalid cumulative usage value {value!r}")
    return value


def parse_energy_response(response: bytes, energy_type: str) -> dict[str, object]:
    """Parse a complete type 0100 or 0300 energy response.

    A compact response that ends before the first monthly record is the server's
    observed month-boundary "not ready" response.  It must not be interpreted as
    zero and must not replace the last known cumulative values.
    """
    if not response.startswith(_MAGIC):
        raise ProtocolError("Energy response has an invalid protocol signature")
    if response.startswith(_ERROR_HEADER_PREFIX):
        raise ProtocolError("Energy response contains the Kocom error header")

    response_hex = response.hex()
    minimum_hex_length = {"0100": 904, "0300": 600}.get(energy_type)
    if minimum_hex_length is None:
        raise ProtocolError(f"Unsupported energy display type: {energy_type!r}")
    if len(response_hex) < minimum_hex_length:
        raise EnergyDataPendingError(len(response))

    result: dict[str, object] = {}
    try:
        if energy_type == "0100":
            periods = ("two_months_ago", "last_month", "this_month")
            utilities = ("electricity", "gas", "water", "hot_water", "heating")
            start = 64
            for period in periods:
                for utility in utilities:
                    if utility == "electricity":
                        result[period] = hex_to_ascii(
                            response_hex[start + 8 : start + 24]
                        )
                    result[f"{utility}_usage_{period}"] = _decode_usage(
                        response_hex, start + 40, start + 56
                    )
                    start += 56
        else:
            utilities = ("electricity", "water", "hot_water", "gas", "heating")
            start = 184
            for utility in utilities:
                if utility == "electricity":
                    result["this_month"] = hex_to_ascii(
                        response_hex[start + 8 : start + 22]
                    )
                result[f"{utility}_usage_this_month"] = _decode_usage(
                    response_hex, start + 48, start + 64
                )
                start += 88
    except (ValueError, UnicodeDecodeError) as err:
        raise ProtocolError("Energy response contains an invalid encoded field") from err

    month = str(result.get("this_month", "")).strip()
    if not month:
        raise ProtocolError("Energy response does not identify the current month")
    return result


class API:
    """Minimal client for the legacy Kocom energy protocol."""

    auth_req_format = (
        "78563412000010017c010000000000000000000000000000000000000000000000"
        "00000000000000000000000000000000000000{username}{password}02000000"
        "{fcm}{phone}"
    )
    menu_req = "78563412b80b1001040000000000000000000000000000000000000000000000"
    addr_req = (
        "7856341202001001200000000000000000000000000000000000000000000000"
        "18000000f00000000000000000000000000000000000000000000000"
    )
    energy_req_type_1_format = (
        "785634127800100120000000{town}0000{dong}0000{ho}000000000000"
        "{months_str}000000000000000000000000"
    )
    energy_req_type_3_format = (
        "785634129001100148000000{town}0000{dong}0000{ho}000000000000"
        "020000000200000001000000{months_str}00{months_str}00"
        "312c322c332c342c350000000000000000000000"
    )

    def __init__(
        self,
        ip: str,
        username: str,
        password: str,
        fcm: str,
        phone: str,
        *,
        port: int = 15000,
    ) -> None:
        self.ip = ip
        self.port = port
        self.auth_req = self.auth_req_format.format(
            username=username,
            password=password,
            fcm=fcm,
            phone=phone,
        )
        self.energy_disp_type = ""
        self.last_response_bytes: int | None = None

    async def _read_response(self, reader: asyncio.StreamReader) -> bytes:
        """Collect one response until the server goes briefly idle.

        The previous implementation used one ``read(1024)`` call.  TCP does not
        preserve application message boundaries, so a valid response could be
        split and incorrectly rejected.  Requests are strictly sequential here,
        making a short idle boundary safe for this protocol.
        """
        try:
            first = await asyncio.wait_for(reader.read(4096), timeout=_READ_TIMEOUT)
        except TimeoutError as err:
            raise ProtocolError("Timed out waiting for the Kocom server") from err
        if not first:
            raise ProtocolError("Kocom server closed the connection without a response")

        response = bytearray(first)
        while len(response) < _MAX_RESPONSE_SIZE:
            try:
                chunk = await asyncio.wait_for(
                    reader.read(min(4096, _MAX_RESPONSE_SIZE - len(response))),
                    timeout=_READ_IDLE_TIMEOUT,
                )
            except TimeoutError:
                break
            if not chunk:
                break
            response.extend(chunk)
        if len(response) >= _MAX_RESPONSE_SIZE:
            raise ProtocolError("Kocom response exceeds the safety limit")
        return bytes(response)

    async def _exchange(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, packet: str
    ) -> bytes:
        writer.write(bytes.fromhex(packet))
        await writer.drain()
        return await self._read_response(reader)

    async def _authenticate_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        response = await self._exchange(reader, writer, self.auth_req)
        if response != _AUTH_RESPONSE:
            raise AuthenticationError("Kocom account authentication failed")

    @staticmethod
    async def _close_writer(writer: asyncio.StreamWriter | None) -> None:
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass

    async def authenticate(self) -> bool:
        """Validate credentials for the config flow without leaking secrets."""
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.ip, self.port), timeout=_READ_TIMEOUT
            )
            await self._authenticate_connection(reader, writer)
            return True
        except (KocomEnergyError, TimeoutError, ConnectionError, OSError) as err:
            _LOGGER.warning("Kocom authentication check failed: %s", err)
            return False
        finally:
            await self._close_writer(writer)

    def _build_energy_request(
        self, address: Mapping[str, str], now: datetime
    ) -> str:
        if self.energy_disp_type == "0100":
            months = ",".join(_month_at_offset(now, offset) for offset in (-2, -1, 0))
            return self.energy_req_type_1_format.format(
                **address, months_str=string_to_hex(months)
            )
        if self.energy_disp_type == "0300":
            month_start = now.strftime("%Y-%m-00 00:00:00")
            return self.energy_req_type_3_format.format(
                **address, months_str=string_to_hex(month_start)
            )
        raise ProtocolError(
            f"Unsupported energy display type: {self.energy_disp_type!r}"
        )

    async def get_energy_data(self) -> dict[str, object]:
        """Fetch and parse the current cumulative utility values."""
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.ip, self.port), timeout=_READ_TIMEOUT
            )
            await self._authenticate_connection(reader, writer)

            menu_response = await self._exchange(reader, writer, self.menu_req)
            if not menu_response.startswith(_MAGIC) or len(menu_response) < 50:
                raise ProtocolError("Menu response is incomplete")
            self.energy_disp_type = menu_response.hex()[96:100]

            address_response = await self._exchange(reader, writer, self.addr_req)
            if not address_response.startswith(_MAGIC) or len(address_response) < 22:
                raise ProtocolError("Address response is incomplete")
            address_hex = address_response.hex()
            address = {
                "town": address_hex[24:28],
                "dong": address_hex[32:36],
                "ho": address_hex[40:44],
            }

            request = self._build_energy_request(address, datetime.now())
            response = await self._exchange(reader, writer, request)
            self.last_response_bytes = len(response)
            return parse_energy_response(response, self.energy_disp_type)
        finally:
            await self._close_writer(writer)
