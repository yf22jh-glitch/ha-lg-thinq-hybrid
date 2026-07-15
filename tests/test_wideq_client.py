"""Regression tests for the WideQ reconnect policy.

The module is loaded without importing Home Assistant so these guardrail tests
can run in the repository's lightweight validation job.
"""

from __future__ import annotations

import importlib
import sys
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


class WideqControlReconnectTests(unittest.IsolatedAsyncioTestCase):
    def _subject(self, errors: list[BaseException | None]):
        subject = wideq_client.WideqClient(None, "token", "KR", "ko-KR", None)
        session = _Session(errors)
        subject._client = _Client(session)
        subject._device_ids["device"] = "wideq-id"
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
                "device", "basicCtrl", data_key="field", value=1
            )

        self.assertEqual(session.calls, 1)
        self.assertEqual(subject.connect_calls, 0)

    async def test_command_rejection_does_not_reconnect_or_retry(self) -> None:
        subject, session = self._subject([ValueError("rejected")])

        with self.assertRaises(ValueError):
            await subject.async_control(
                "device", "basicCtrl", data_key="field", value=1
            )

        self.assertEqual(session.calls, 1)
        self.assertEqual(subject.connect_calls, 0)

    async def test_authentication_failure_reconnects_exactly_once(self) -> None:
        subject, session = self._subject(
            [wideq_client.AuthenticationError(), None]
        )

        result = await subject.async_control(
            "device", "basicCtrl", data_key="field", value=1
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(session.calls, 2)
        self.assertEqual(subject.connect_calls, 1)

    async def test_device_lookup_auth_failure_uses_same_single_reconnect(self) -> None:
        subject, session = self._subject([None])
        lookups = 0

        async def lookup(alias: str):
            nonlocal lookups
            lookups += 1
            if lookups == 1:
                raise wideq_client.AuthenticationError()
            return "wideq-id"

        subject._device_id_for = lookup
        result = await subject.async_control(
            "device", "basicCtrl", data_key="field", value=1
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(lookups, 2)
        self.assertEqual(session.calls, 1)
        self.assertEqual(subject.connect_calls, 1)

    async def test_device_lookup_5xx_does_not_reconnect(self) -> None:
        subject, session = self._subject([])

        async def lookup(alias: str):
            raise _HttpError(504)

        subject._device_id_for = lookup
        with self.assertRaises(_HttpError):
            await subject.async_control(
                "device", "basicCtrl", data_key="field", value=1
            )

        self.assertEqual(session.calls, 0)
        self.assertEqual(subject.connect_calls, 0)


if __name__ == "__main__":
    unittest.main()
