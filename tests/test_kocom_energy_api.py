"""Protocol regression tests for the vendored Kocom Energy integration."""

from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import importlib.util
from pathlib import Path
import struct
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "custom_components" / "kocom_energy"

custom = sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
custom.__path__ = [str(ROOT / "custom_components")]
package = sys.modules.setdefault(
    "custom_components.kocom_energy", types.ModuleType("custom_components.kocom_energy")
)
package.__path__ = [str(PACKAGE)]

for module_name in ("exceptions", "util", "api"):
    qualified = f"custom_components.kocom_energy.{module_name}"
    if qualified in sys.modules:
        continue
    spec = importlib.util.spec_from_file_location(qualified, PACKAGE / f"{module_name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    spec.loader.exec_module(module)

from custom_components.kocom_energy.api import (  # noqa: E402
    API,
    _month_at_offset,
    parse_energy_response,
)
from custom_components.kocom_energy.exceptions import (  # noqa: E402
    EnergyDataPendingError,
    ProtocolError,
)


def _put_ascii(payload: bytearray, hex_offset: int, value: str) -> None:
    data = value.encode("ascii")
    payload[hex_offset // 2 : hex_offset // 2 + len(data)] = data


def _put_double(payload: bytearray, hex_offset: int, value: float) -> None:
    payload[hex_offset // 2 : hex_offset // 2 + 8] = struct.pack("<d", value)


def _type_0300_response(values: tuple[float, ...]) -> bytes:
    payload = bytearray(300)
    payload[:4] = bytes.fromhex("78563412")
    start = 184
    for index, value in enumerate(values):
        if index == 0:
            _put_ascii(payload, start + 8, "2026-08")
        _put_double(payload, start + 48, value)
        start += 88
    return bytes(payload)


def _type_0100_response() -> bytes:
    payload = bytearray(452)
    payload[:4] = bytes.fromhex("78563412")
    start = 64
    value = 1.0
    for month in ("202606", "202607", "202608"):
        for utility_index in range(5):
            if utility_index == 0:
                _put_ascii(payload, start + 8, month)
            _put_double(payload, start + 40, value)
            value += 1.0
            start += 56
    return bytes(payload)


class KocomEnergyParserTests(unittest.TestCase):
    def test_wire_packets_keep_upstream_byte_layout(self) -> None:
        self.assertEqual(
            hashlib.sha256(bytes.fromhex(API.addr_req)).hexdigest(),
            "0c38299aa5f6f128467a0fbe43c68dd478027aa833a7f22dc7e543bb6a15b808",
        )
        for packet in (API.menu_req, API.addr_req):
            self.assertEqual(len(packet) % 2, 0)
            bytes.fromhex(packet)

    def test_type_0300_maps_server_order_to_named_utilities(self) -> None:
        data = parse_energy_response(
            _type_0300_response((27.0, 0.5, 0.0, 0.0, 0.0)), "0300"
        )
        self.assertEqual(data["this_month"], "2026-08")
        self.assertEqual(data["electricity_usage_this_month"], 27.0)
        self.assertEqual(data["water_usage_this_month"], 0.5)
        self.assertEqual(data["gas_usage_this_month"], 0.0)

    def test_type_0100_decodes_all_fifteen_records(self) -> None:
        data = parse_energy_response(_type_0100_response(), "0100")
        self.assertEqual(data["two_months_ago"], "202606")
        self.assertEqual(data["last_month"], "202607")
        self.assertEqual(data["this_month"], "202608")
        self.assertEqual(data["electricity_usage_this_month"], 11.0)
        self.assertEqual(data["heating_usage_this_month"], 15.0)

    def test_compact_month_start_response_is_pending_not_zero(self) -> None:
        compact = bytes.fromhex("78563412") + bytes(88)
        with self.assertRaises(EnergyDataPendingError) as context:
            parse_energy_response(compact, "0300")
        self.assertEqual(context.exception.response_bytes, 92)

    def test_invalid_signature_is_rejected(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "signature"):
            parse_energy_response(bytes(300), "0300")

    def test_negative_cumulative_value_is_rejected(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "cumulative"):
            parse_energy_response(
                _type_0300_response((-1.0, 0.0, 0.0, 0.0, 0.0)), "0300"
            )

    def test_month_offsets_cross_year_boundary(self) -> None:
        now = datetime(2026, 1, 2)
        self.assertEqual(
            [_month_at_offset(now, offset) for offset in (-2, -1, 0)],
            ["202511", "202512", "202601"],
        )


class _ChunkReader:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def read(self, size: int) -> bytes:
        await asyncio.sleep(0)
        return self.chunks.pop(0) if self.chunks else b""


class KocomEnergyTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_split_tcp_chunks_are_collected(self) -> None:
        api = API("127.0.0.1", "", "", "", "")
        response = await api._read_response(_ChunkReader([b"abc", b"def", b"ghi"]))
        self.assertEqual(response, b"abcdefghi")


if __name__ == "__main__":
    unittest.main()
