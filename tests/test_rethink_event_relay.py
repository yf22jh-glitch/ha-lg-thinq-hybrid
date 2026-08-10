"""Regression tests for the local Rethink lifecycle-event relay."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "my_lg"
    / "rethink_event_relay.py"
)
SPEC = importlib.util.spec_from_file_location("my_lg_rethink_event_relay_test", MODULE_PATH)
relay = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(relay)


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _Session:
    def __init__(self, status: int = 202) -> None:
        self.status = status
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _Response(self.status)


class NormalizeLifecycleEventTests(unittest.TestCase):
    def test_registered_event_is_reduced_to_safe_metadata(self) -> None:
        event = relay.normalize_lifecycle_event(
            {
                "pushType": "DEVICE_REGISTERED",
                "deviceId": "device-1",
                "deviceInfo": {"alias": "Study AC", "macAddress": "private"},
                "accessToken": "must-not-leave-the-integration",
            }
        )

        self.assertEqual(event["kind"], "registered")
        self.assertEqual(event["deviceId"], "device-1")
        self.assertEqual(
            event["payloadKeys"],
            ["accessToken", "deviceId", "deviceInfo", "pushType"],
        )
        self.assertNotIn("Study AC", repr(event))
        self.assertNotIn("private", repr(event))
        self.assertNotIn("must-not-leave", repr(event))

    def test_nested_unregister_and_alias_events_are_recognized(self) -> None:
        unregistered = relay.normalize_lifecycle_event(
            {
                "pushType": "DEVICE_DISCOVERY",
                "eventType": "DEVICE_UNREGISTERED",
                "data": {"deviceId": "device-2"},
            }
        )
        alias_updated = relay.normalize_lifecycle_event(
            {
                "pushType": "DEVICE_ALIAS_UPDATED",
                "device": {"deviceId": "device-3"},
            }
        )

        self.assertEqual(unregistered["kind"], "unregistered")
        self.assertEqual(unregistered["deviceId"], "device-2")
        self.assertEqual(alias_updated["kind"], "alias_updated")
        self.assertEqual(alias_updated["deviceId"], "device-3")

    def test_status_and_normal_push_messages_are_not_forwarded(self) -> None:
        self.assertIsNone(
            relay.normalize_lifecycle_event(
                {"pushType": "DEVICE_STATUS", "deviceId": "device-1", "report": {}}
            )
        )
        self.assertIsNone(
            relay.normalize_lifecycle_event(
                {"pushType": "DEVICE_PUSH", "deviceId": "device-1", "pushCode": "DONE"}
            )
        )

    def test_lifecycle_code_inside_device_push_is_recognized(self) -> None:
        event = relay.normalize_lifecycle_event(
            {
                "pushType": "DEVICE_PUSH",
                "pushCode": "DEVICE_REGISTERED",
                "deviceId": "device-1",
            }
        )

        self.assertEqual(event["kind"], "registered")

    def test_unknown_discovery_shape_is_diagnostic_only(self) -> None:
        event = relay.normalize_lifecycle_event(
            {
                "pushType": "DEVICE_DISCOVERY",
                "eventType": "FUTURE_EVENT",
                "deviceId": "device-4",
            }
        )

        self.assertEqual(event["kind"], "unknown")
        self.assertEqual(event["deviceId"], "device-4")

    def test_unrelated_client_registration_does_not_trigger_home_lookup(self) -> None:
        self.assertIsNone(
            relay.normalize_lifecycle_event(
                {"pushType": "CLIENT_REGISTERED", "deviceId": "device-4"}
            )
        )

    def test_invalid_device_id_is_not_forwarded(self) -> None:
        event = relay.normalize_lifecycle_event(
            {"pushType": "DEVICE_REGISTERED", "deviceId": "x" * 257}
        )

        self.assertNotIn("deviceId", event)


class RethinkEventRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_without_owner_token(self) -> None:
        session = _Session()
        subject = relay.RethinkEventRelay(session, "short")

        self.assertFalse(subject.enabled)
        self.assertFalse(await subject.async_send({"kind": "subscription_ready"}))
        self.assertEqual(session.calls, [])

        self.assertFalse(relay.RethinkEventRelay(session, "t" * 513).enabled)
        self.assertFalse(relay.RethinkEventRelay(session, "t" * 31 + "\n" + "x").enabled)

    async def test_posts_only_to_fixed_loopback_endpoint_with_bearer_token(self) -> None:
        session = _Session()
        token = "t" * 32
        subject = relay.RethinkEventRelay(session, token)

        accepted = await subject.async_send(
            {
                "kind": "registered",
                "deviceId": "device-1",
                "payloadKeys": ["pushType", "deviceId"],
            }
        )

        self.assertTrue(accepted)
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call["url"], relay.RETHINK_EVENT_ENDPOINT)
        self.assertEqual(call["headers"], {"Authorization": f"Bearer {token}"})
        self.assertEqual(call["timeout"], relay.RETHINK_EVENT_TIMEOUT)
        self.assertEqual(call["json"]["kind"], "registered")
        self.assertEqual(call["json"]["deviceId"], "device-1")
        self.assertRegex(call["json"]["receivedAt"], r"^\d{4}-\d{2}-\d{2}T")

    async def test_http_failure_is_not_retried(self) -> None:
        session = _Session(status=503)
        subject = relay.RethinkEventRelay(session, "t" * 32)

        accepted = await subject.async_send({"kind": "subscription_ready"})

        self.assertFalse(accepted)
        self.assertEqual(len(session.calls), 1)


if __name__ == "__main__":
    unittest.main()
