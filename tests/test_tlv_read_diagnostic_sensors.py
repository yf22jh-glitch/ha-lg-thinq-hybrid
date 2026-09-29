"""Full-read envelope diagnostics retained as safe HA counters."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from homeassistant.helpers.entity import EntityCategory

from custom_components.my_lg import sensor
from custom_components.my_lg.const import DOMAIN
from tests import test_local_read_provider as fixtures


class FullReadDiagnosticProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.primary = fixtures.FakePrimaryProvider()
        self.provider = fixtures.read.TlvReadShadowProvider(
            fixtures.BINDING_ID,
            fixtures.PAT_DEVICE_ID,
            fixtures.profile(),
            self.primary,
            allow_legacy_v1_fallback=True,
            now=lambda: fixtures.NOW,
        )
        self.provider.set_transport_ready(True)

    def tearDown(self) -> None:
        self.provider.close()

    def ingest(self, *, sequence: int, counters: dict[str, int]) -> None:
        value = json.loads(fixtures.envelope(sequence=sequence))
        value["diagnostics"] = counters
        if sequence > 1:
            value["published_at"] = "2026-08-23T12:00:00.000Z"
        self.assertTrue(
            self.provider.ingest(
                self.provider.current_topic,
                json.dumps(value, separators=(",", ":")).encode(),
                qos=1,
                retained=False,
            )
        )

    def test_latest_accepted_counters_and_publication_boundary_are_preserved(
        self,
    ) -> None:
        first = {
            "rejected_frames": 1,
            "unresolved_fields": 2,
            "invalid_values": 3,
            "unsupported_frames": 4,
        }
        second = {
            "rejected_frames": 5,
            "unresolved_fields": 6,
            "invalid_values": 7,
            "unsupported_frames": 8,
        }
        self.ingest(sequence=1, counters=first)
        self.assertEqual(dict(self.provider.current_diagnostics), first)
        self.assertEqual(
            self.provider.current_published_at,
            datetime(2026, 8, 23, 11, 59, 59, tzinfo=timezone.utc),
        )
        self.assertTrue(self.provider.diagnostics_available)
        with self.assertRaises(TypeError):
            self.provider.current_diagnostics["rejected_frames"] = 9

        self.ingest(sequence=2, counters=second)
        self.assertEqual(dict(self.provider.current_diagnostics), second)
        self.assertEqual(
            self.provider.current_published_at,
            datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc),
        )

        # Authority loss makes retained counters unavailable without replacing
        # the last accepted values with unauthenticated data.
        self.primary.control_alive = False
        self.primary.notify()
        self.assertFalse(self.provider.diagnostics_available)
        self.assertEqual(dict(self.provider.current_diagnostics), second)

        self.primary.control_alive = True
        self.primary.notify()
        self.assertTrue(self.provider.diagnostics_available)
        self.assertTrue(
            self.provider.ingest(
                self.provider.current_topic, b"", qos=1, retained=False
            )
        )
        self.assertEqual(dict(self.provider.current_diagnostics), {})
        self.assertIsNone(self.provider.current_published_at)
        self.assertFalse(self.provider.diagnostics_available)

    def test_counters_are_exact_nonnegative_json_safe_integers(self) -> None:
        for invalid in (-1, True, 1.5, fixtures.read.MAX_JSON_SAFE_INTEGER + 1):
            value = json.loads(fixtures.envelope())
            value["diagnostics"]["rejected_frames"] = invalid
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(fixtures.read.TlvReadProviderContractError),
            ):
                self.provider.ingest(
                    self.provider.current_topic,
                    json.dumps(value, separators=(",", ":")).encode(),
                    qos=1,
                    retained=False,
                )
        self.assertEqual(dict(self.provider.current_diagnostics), {})
        self.assertFalse(self.provider.diagnostics_available)


class FullReadDiagnosticEntityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.primary = fixtures.FakePrimaryProvider()
        self.provider = fixtures.read.TlvReadShadowProvider(
            fixtures.BINDING_ID,
            fixtures.PAT_DEVICE_ID,
            fixtures.profile(),
            self.primary,
            allow_legacy_v1_fallback=True,
            now=lambda: fixtures.NOW,
        )
        self.provider.set_transport_ready(True)
        self.coordinator = SimpleNamespace(
            device_id=fixtures.PAT_DEVICE_ID,
            alias="Diagnostic test appliance",
            model=fixtures.MODEL_ID,
            device_type="TEST_DEVICE",
        )

    def tearDown(self) -> None:
        self.provider.close()

    def entities(self) -> list[sensor.TlvReadDiagnosticSensor]:
        return [
            sensor.TlvReadDiagnosticSensor(
                self.provider,  # type: ignore[arg-type]
                self.coordinator,
                key,
            )
            for key in fixtures.read.TLV_READ_DIAGNOSTIC_KEYS
        ]

    async def test_four_disabled_device_owned_entities_expose_only_safe_counters(
        self,
    ) -> None:
        entities = self.entities()
        self.assertEqual(len(entities), 4)
        self.assertEqual(len({item.unique_id for item in entities}), 4)
        self.assertTrue(
            all(item.entity_category == EntityCategory.DIAGNOSTIC for item in entities)
        )
        self.assertTrue(
            all(not item.entity_registry_enabled_default for item in entities)
        )
        self.assertTrue(
            all(
                item.device_info["identifiers"] == {(DOMAIN, fixtures.PAT_DEVICE_ID)}
                for item in entities
            )
        )

        counters = {
            "rejected_frames": 10,
            "unresolved_fields": 11,
            "invalid_values": 12,
            "unsupported_frames": 13,
        }
        value = json.loads(fixtures.envelope())
        value["diagnostics"] = counters
        self.provider.ingest(
            self.provider.current_topic,
            json.dumps(value, separators=(",", ":")).encode(),
            qos=1,
            retained=False,
        )

        for entity in entities:
            self.assertTrue(entity.available)
            self.assertEqual(entity.native_value, counters[entity.diagnostic_key])
            self.assertEqual(
                set(entity.extra_state_attributes),
                {"diagnostic_counter", "profile_id"},
            )
            exposed = repr(entity.extra_state_attributes)
            self.assertNotIn(fixtures.BINDING_ID, exposed)
            self.assertNotIn(fixtures.PROOF, exposed)

        # One provider update refreshes each counter and listeners detach cleanly.
        first = entities[0]
        first.async_write_ha_state = Mock()
        await first.async_added_to_hass()
        self.primary.notify()
        first.async_write_ha_state.assert_called_once_with()
        await first.async_will_remove_from_hass()
        self.primary.notify()
        first.async_write_ha_state.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
