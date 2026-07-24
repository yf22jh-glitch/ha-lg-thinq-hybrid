"""Regression tests for the WideQ reconnect policy.

The module is loaded without importing Home Assistant so these guardrail tests
can run in the repository's lightweight validation job.
"""

from __future__ import annotations

import importlib
import sys
from datetime import date
from pathlib import Path
from types import ModuleType
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "custom_components" / "my_lg"


def _load_wideq_client_module():
    """Load the module in a namespace package, bypassing my_lg/__init__.py."""
    aiohttp = ModuleType("aiohttp")

    class ClientConnectionError(Exception):
        """Minimal aiohttp connection-error stand-in."""

    aiohttp.ClientConnectionError = ClientConnectionError
    sys.modules.setdefault("aiohttp", aiohttp)

    custom_components = ModuleType("custom_components")
    custom_components.__path__ = [str(ROOT / "custom_components")]
    my_lg = ModuleType("custom_components.my_lg")
    my_lg.__path__ = [str(PACKAGE)]
    wideq = ModuleType("custom_components.my_lg.wideq")
    wideq.__path__ = [str(PACKAGE / "wideq")]
    sys.modules.setdefault("custom_components", custom_components)
    sys.modules.setdefault("custom_components.my_lg", my_lg)
    sys.modules.setdefault("custom_components.my_lg.wideq", wideq)
    return importlib.import_module("custom_components.my_lg.wideq_client")


wideq_client = _load_wideq_client_module()


class _HttpError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


class _Session:
    def __init__(self, errors: list[BaseException | None]) -> None:
        self._errors = list(errors)
        self.calls = 0

    async def device_v2_controls(self, *args, **kwargs):
        self.calls += 1
        err = self._errors.pop(0) if self._errors else None
        if err is not None:
            raise err
        return {"ok": True}


class _Client:
    def __init__(self, session: _Session) -> None:
        self.session = session
        self.devices = []

    async def refresh_auth(self) -> None:
        return None


class _Device:
    def __init__(
        self, device_id: str, alias: str, model: str, snapshot: dict
    ) -> None:
        self.device_id = device_id
        self.name = alias
        self.model_name = model
        self._snapshot = snapshot

    def as_dict(self) -> dict:
        return {"snapshot": self._snapshot}


class _SnapshotClient:
    def __init__(self, errors: list[BaseException | None]) -> None:
        self._errors = errors
        self.refresh_calls = 0
        self.devices = [
            _Device("wideq-id", "Living AC", "MODEL", {"power": 123})
        ]

    async def refresh_auth(self) -> None:
        return None

    async def refresh_devices(self) -> None:
        self.refresh_calls += 1
        err = self._errors.pop(0) if self._errors else None
        if err is not None:
            raise err


class _EnergySession:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.paths: list[str] = []

    async def get2(self, path: str):
        self.paths.append(path)
        return self.responses.pop(0)


class WideqControlReconnectTests(unittest.IsolatedAsyncioTestCase):
    def _subject(self, errors: list[BaseException | None]):
        subject = wideq_client.WideqClient(None, "token", "KR", "ko-KR", None)
        session = _Session(errors)
        subject._client = _Client(session)
        subject.connect_calls = 0

        async def close() -> None:
            subject._client = None

        async def connect():
            subject.connect_calls += 1
            subject._client = _Client(session)
            return subject._client

        subject.async_close = close
        subject.async_connect = connect
        return subject, session

    async def test_http_5xx_does_not_reconnect_or_retry(self) -> None:
        subject, session = self._subject([_HttpError(504)])

        with self.assertRaises(_HttpError):
            await subject.async_control(
                "wideq-id", "basicCtrl", data_key="field", value=1
            )

        self.assertEqual(session.calls, 1)
        self.assertEqual(subject.connect_calls, 0)

    def test_api_error_code_5xx_is_server_unavailable(self) -> None:
        class ApiError(Exception):
            code = "504"

        self.assertTrue(wideq_client.is_server_unavailable(ApiError("maintenance")))

    async def test_command_rejection_does_not_reconnect_or_retry(self) -> None:
        subject, session = self._subject([ValueError("rejected")])

        with self.assertRaises(ValueError):
            await subject.async_control(
                "wideq-id", "basicCtrl", data_key="field", value=1
            )

        self.assertEqual(session.calls, 1)
        self.assertEqual(subject.connect_calls, 0)

    async def test_authentication_failure_reconnects_exactly_once(self) -> None:
        subject, session = self._subject(
            [wideq_client.AuthenticationError(), None]
        )

        result = await subject.async_control(
            "wideq-id", "basicCtrl", data_key="field", value=1
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(session.calls, 2)
        self.assertEqual(subject.connect_calls, 1)

    async def test_missing_stable_id_is_rejected_without_reconnect(self) -> None:
        subject, session = self._subject([None])
        with self.assertRaisesRegex(ValueError, "stable device id"):
            await subject.async_control(
                "", "basicCtrl", data_key="field", value=1
            )

        self.assertEqual(session.calls, 0)
        self.assertEqual(subject.connect_calls, 0)


class WideqSnapshotReconnectTests(unittest.IsolatedAsyncioTestCase):
    def _subject(self, errors: list[BaseException | None]):
        subject = wideq_client.WideqClient(None, "token", "KR", "ko-KR", None)
        client = _SnapshotClient(errors)
        subject._client = client
        subject.connect_calls = 0

        async def close() -> None:
            subject._client = None

        async def connect():
            subject.connect_calls += 1
            subject._client = client
            return client

        subject.async_close = close
        subject.async_connect = connect
        return subject, client

    async def test_snapshot_returns_stable_identity_metadata(self) -> None:
        subject, client = self._subject([None])

        result = await subject.async_get_snapshots()

        self.assertEqual(client.refresh_calls, 1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].device_id, "wideq-id")
        self.assertEqual(result[0].alias, "Living AC")
        self.assertEqual(result[0].model, "MODEL")
        self.assertEqual(result[0].snapshot, {"power": 123})

    async def test_snapshot_5xx_does_not_reconnect(self) -> None:
        subject, client = self._subject([_HttpError(504)])

        with self.assertRaises(_HttpError):
            await subject.async_get_snapshots()

        self.assertEqual(client.refresh_calls, 1)
        self.assertEqual(subject.connect_calls, 0)

    async def test_snapshot_auth_failure_reconnects_once(self) -> None:
        subject, client = self._subject(
            [wideq_client.AuthenticationError(), None]
        )

        result = await subject.async_get_snapshots()

        self.assertEqual(len(result), 1)
        self.assertEqual(client.refresh_calls, 2)
        self.assertEqual(subject.connect_calls, 1)


class WideqEnergyHistoryParserTests(unittest.TestCase):
    def test_ac_daily_history_is_converted_from_wh_to_kwh(self) -> None:
        result = wideq_client.parse_ac_energy_history(
            [
                {"usedDate": "2026-07-19", "energyData": "2500"},
                {"usedDate": "2026-07-20", "energyData": "1300"},
                {"usedDate": "2026-07-21", "energyData": "NO_DATA"},
            ],
            date(2026, 7, 20),
        )

        self.assertEqual(result, {"today": 1.3, "month": 3.8})

    def test_fridge_hour_and_month_history_are_converted_to_kwh(self) -> None:
        result = wideq_client.parse_fridge_energy_history(
            {"item": [{"power": "38"}, {"power": "40"}, {"power": "NO_DATA"}]},
            [{"usedDate": "2026-07", "power": "20659"}],
        )

        self.assertEqual(result, {"today": 0.078, "month": 20.659})

    def test_generic_daily_history_is_converted_from_wh_to_kwh(self) -> None:
        result = wideq_client.parse_device_energy_history(
            {
                "item": [
                    {"usedDate": "2026-07-19", "power": "1776"},
                    {"usedDate": "2026-07-20", "power": "328"},
                    {"usedDate": "2026-07-21", "power": "0"},
                ]
            },
            date(2026, 7, 20),
        )

        self.assertEqual(result, {"today": 0.328, "month": 2.104})

    def test_missing_current_day_is_not_fabricated_as_zero(self) -> None:
        result = wideq_client.parse_ac_energy_history(
            [
                {"usedDate": "2026-07-19", "energyData": "2500"},
                {"usedDate": "2026-07-20", "energyData": "NO_DATA"},
            ],
            date(2026, 7, 20),
        )

        self.assertEqual(result, {"month": 2.5})

    def test_out_of_period_daily_items_are_not_summed(self) -> None:
        result = wideq_client.parse_ac_energy_history(
            [
                {"usedDate": "2026-06-30", "energyData": "9000"},
                {"usedDate": "2026-07-20", "energyData": "1000"},
            ],
            date(2026, 7, 20),
        )

        self.assertEqual(result, {"today": 1.0, "month": 1.0})

    def test_explicit_numeric_zero_is_verified_energy_data(self) -> None:
        result = wideq_client.parse_device_energy_history(
            {"item": [{"usedDate": "2026-07-20", "power": 0}]},
            date(2026, 7, 20),
        )

        self.assertEqual(result, {"today": 0.0, "month": 0.0})

    def test_invalid_energy_samples_are_ignored(self) -> None:
        result = wideq_client.parse_device_energy_history(
            {
                "item": [
                    {"usedDate": "2026-07-20", "power": -1},
                    {"usedDate": "2026-07-20", "power": "nan"},
                    {"usedDate": "2026-07-20", "power": True},
                    {"usedDate": "2026-07-20", "power": 10**10000},
                ]
            },
            date(2026, 7, 20),
        )

        self.assertIsNone(result)

    def test_fridge_partial_response_keeps_verified_period_only(self) -> None:
        result = wideq_client.parse_fridge_energy_history(
            {"item": [{"power": "NO_DATA"}]},
            [{"usedDate": "2026-07", "power": "20659"}],
        )

        self.assertEqual(result, {"month": 20.659})

    def test_unexpected_history_shape_is_not_reported_as_zero(self) -> None:
        self.assertIsNone(
            wideq_client.parse_ac_energy_history({"error": True}, date(2026, 7, 20))
        )
        self.assertIsNone(
            wideq_client.parse_fridge_energy_history(
                [], {"error": True}
            )
        )
        self.assertIsNone(
            wideq_client.parse_device_energy_history(
                {"error": True}, date(2026, 7, 20)
            )
        )


class WideqEnergyHistoryRequestTests(unittest.IsolatedAsyncioTestCase):
    def _subject(self, responses):
        subject = wideq_client.WideqClient(None, "token", "KR", "ko-KR", None)
        session = _EnergySession(responses)
        subject._client = _Client(session)
        limiter_calls = 0

        async def acquire() -> None:
            nonlocal limiter_calls
            limiter_calls += 1

        return subject, session, acquire, lambda: limiter_calls

    async def test_ac_history_uses_one_rate_limited_request(self) -> None:
        subject, session, acquire, limiter_calls = self._subject(
            [[{"usedDate": "2026-07-20", "energyData": "1300"}]]
        )

        result = await subject.async_get_energy_usage(
            "wideq-id",
            "aircon",
            target_date=date(2026, 7, 20),
            before_request=acquire,
        )

        self.assertEqual(result, {"today": 1.3, "month": 1.3})
        self.assertEqual(limiter_calls(), 1)
        self.assertEqual(len(session.paths), 1)
        self.assertIn("period=day", session.paths[0])

    async def test_fridge_history_uses_two_rate_limited_requests(self) -> None:
        subject, session, acquire, limiter_calls = self._subject(
            [
                [{"usedDate": "2026-07-20 00:00:00", "power": "459"}],
                [{"usedDate": "2026-07", "power": "20679"}],
            ]
        )

        result = await subject.async_get_energy_usage(
            "wideq-id",
            "fridge",
            target_date=date(2026, 7, 20),
            before_request=acquire,
        )

        self.assertEqual(result, {"today": 0.459, "month": 20.679})
        self.assertEqual(limiter_calls(), 2)
        self.assertEqual(len(session.paths), 2)
        self.assertIn("period=hour", session.paths[0])
        self.assertIn("period=month", session.paths[1])

    async def test_generic_device_history_uses_one_rate_limited_request(self) -> None:
        subject, session, acquire, limiter_calls = self._subject(
            [
                {
                    "item": [
                        {"usedDate": "2026-07-19", "power": "1776"},
                        {"usedDate": "2026-07-20", "power": "328"},
                    ]
                }
            ]
        )

        result = await subject.async_get_energy_usage(
            "wideq-id",
            "devices",
            target_date=date(2026, 7, 20),
            before_request=acquire,
        )

        self.assertEqual(result, {"today": 0.328, "month": 2.104})
        self.assertEqual(limiter_calls(), 1)
        self.assertEqual(len(session.paths), 1)
        self.assertIn("service/devices/wideq-id/energy-history", session.paths[0])
        self.assertIn("period=day", session.paths[0])

if __name__ == "__main__":
    unittest.main()
