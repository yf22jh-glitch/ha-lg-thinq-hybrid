"""One-hop full-read sem31 to sem32 retained-publication migration."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from typing import Any

from custom_components.my_lg import local_read_provider as read

NOW = datetime(2026, 8, 24, 12, 5, tzinfo=timezone.utc)
BINDING_ID = "full_read_migration_binding_001"
PAT_DEVICE_ID = "migration-device-001"
MODEL_ID = "DHUM_056905_WW"
PROOF = "a" * 64
PUBLICATION_SESSION_ID = "1" * 32

OLD_PINS = {
    "profile_revision": "full-read-sensor-profiles-v1:02b9bce3531a1a47",
    "profile_sha256": (
        "02b9bce3531a1a4781a089e7cb4eaf9f1e05c7b2161d76da2003c4c59cf998c2"
    ),
    "read_entity_contract_revision": "full-read-entities-v1:46f5df303ebe5061",
    "read_entity_contract_sha256": (
        "46f5df303ebe50611b069ce68e8c23baa9d1dc5f68e77f0c7a93532c809dbab8"
    ),
    "catalog_sha256": (
        "e555dc723ce6d2e19f8179900406cc91add2a91156518c72fd92188526b279bc"
    ),
    "semantics_revision": 31,
}
UNRELEASED_INTERMEDIATE_PINS = {
    "profile_revision": "full-read-sensor-profiles-v1:f76553122cb9a5a9",
    "profile_sha256": (
        "f76553122cb9a5a91246c8c91c2c0403023cd9cac353c9292efe57341bc04400"
    ),
    "read_entity_contract_revision": "full-read-entities-v1:ada018b592f80857",
    "read_entity_contract_sha256": (
        "ada018b592f80857eebfa87fa067db9c4b0d0f667696bbe84089bf1812483525"
    ),
    "catalog_sha256": (
        "07b97efabfc0f059844826ef425d6ad46737bd3f527d57862202d52ac812024b"
    ),
    "semantics_revision": 32,
}


class FakePrimary:
    def __init__(self) -> None:
        self.binding_id = BINDING_ID
        self.model_id = MODEL_ID
        self.platform = "thinq2"
        self.expected_proof = PROOF
        self.binding_generation = 1
        self.cohort_generation = 1
        self.session_id = PUBLICATION_SESSION_ID
        self.listeners = []

    @property
    def read_publication_authority(self) -> tuple[int, str]:
        return self.binding_generation, self.session_id

    def async_add_listener(self, callback):
        self.listeners.append(callback)
        return lambda: self.listeners.remove(callback)


def envelope(
    profile: read.TlvReadProfile, *, sequence: int, **pin_overrides: Any
) -> bytes:
    value = {
        "schema_version": read.TLV_READ_SCHEMA_VERSION,
        "publication_plan_revision": read.TLV_READ_PUBLICATION_PLAN_REVISION,
        "profile_id": profile.profile_id,
        "profile_contract_revision": profile.contract_revision,
        "profile_revision": profile.profile_revision,
        "profile_sha256": profile.profile_sha256,
        "read_entity_contract_revision": profile.read_entity_contract_revision,
        "read_entity_contract_sha256": profile.read_entity_contract_sha256,
        "catalog_sha256": profile.catalog_sha256,
        "semantics_revision": profile.semantics_revision,
        "binding_id": BINDING_ID,
        "model_id": MODEL_ID,
        "platform": "thinq2",
        "binding_generation": 1,
        "pat_device_id_proof_sha256": PROOF,
        "publication_session_id": PUBLICATION_SESSION_ID,
        "cohort_generation": 1,
        "source_session_id": "migration_source_session_001",
        "sequence": sequence,
        "published_at": f"2026-08-24T12:00:0{sequence}.000Z",
        "fields": {
            "humidity.current_pct": {
                "value": 55,
                "value_type": "number",
                "observed_at": f"2026-08-24T12:00:0{sequence}.000Z",
                "confidence": "confirmed-migration-test",
                "exposure": "state",
                "unit": "%",
            }
        },
        "diagnostics": {
            "rejected_frames": 0,
            "unresolved_fields": 0,
            "invalid_values": 0,
            "unsupported_frames": 0,
        },
    }
    value.update(pin_overrides)
    return json.dumps(value, separators=(",", ":")).encode()


def event_envelope(
    profile: read.TlvReadProfile, *, sequence: int, **pin_overrides: Any
) -> bytes:
    value = json.loads(envelope(profile, sequence=sequence, **pin_overrides))
    value.pop("fields")
    value.pop("diagnostics")
    value.update(
        {
            "descriptor_key": f"{MODEL_ID}|event.water_tank.changed",
            "semantic_id": "event.water_tank.changed",
            "event_type": "water-tank state changed",
            "field": {
                "value": "water-tank state changed",
                "value_type": "string",
                "observed_at": f"2026-08-24T12:00:0{sequence}.000Z",
                "confidence": "confirmed-migration-test",
                "exposure": "event",
            },
        }
    )
    return json.dumps(value, separators=(",", ":")).encode()


class FullReadPinMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = read.load_tlv_read_catalogue()[MODEL_ID]
        self.primary = FakePrimary()
        self.provider = read.TlvReadShadowProvider(
            BINDING_ID,
            PAT_DEVICE_ID,
            self.profile,
            self.primary,
            allow_legacy_v1_fallback=True,
            now=lambda: NOW,
        )
        self.provider.set_transport_ready(True)

    def tearDown(self) -> None:
        self.provider.close()

    def ingest(self, payload: bytes) -> bool:
        return self.provider.ingest(
            self.provider.current_topic, payload, qos=1, retained=False
        )

    def test_exact_previous_generation_can_advance_once_to_current(self) -> None:
        self.assertIn(
            OLD_PINS,
            [
                dict(pins)
                for pins in read._RETAINED_TLV_READ_PUBLICATION_PIN_GENERATIONS
            ],
        )
        self.assertTrue(self.ingest(envelope(self.profile, sequence=1, **OLD_PINS)))
        self.assertEqual(self.provider.field_value("humidity.current_pct"), 55)
        events = []
        remove = self.provider.async_add_event_listener(events.append)
        try:
            self.assertTrue(
                self.provider.ingest(
                    self.provider.event_topic,
                    event_envelope(self.profile, sequence=1, **OLD_PINS),
                    qos=1,
                    retained=False,
                )
            )
            self.assertEqual(
                [event.semantic_id for event in events],
                ["event.water_tank.changed"],
            )
        finally:
            remove()
        self.assertTrue(self.ingest(envelope(self.profile, sequence=2)))
        self.assertTrue(self.provider.field_available("humidity.current_pct"))

    def test_current_generation_is_accepted_directly(self) -> None:
        self.assertEqual(self.profile.semantics_revision, 33)
        self.assertEqual(
            self.profile.profile_sha256,
            "b3a87ad4dde6e7ec0db0c21328744b96d4416bc1d9d028bf9b8f5182ccb1eb7f",
        )
        self.assertEqual(
            self.profile.read_entity_contract_sha256,
            "0b8bad1be19f6b4741a224d0a01d95823bee04aba4bc5a1b3bcdf985cdc53530",
        )
        self.assertTrue(self.ingest(envelope(self.profile, sequence=1)))

    def test_mixed_older_or_future_pin_sets_remain_fail_closed(self) -> None:
        bad = (
            {"semantics_revision": 31},
            UNRELEASED_INTERMEDIATE_PINS,
            {
                **OLD_PINS,
                "read_entity_contract_revision": self.profile.read_entity_contract_revision,
                "read_entity_contract_sha256": self.profile.read_entity_contract_sha256,
            },
            {
                "profile_revision": "full-read-sensor-profiles-v1:1111111111111111",
                "profile_sha256": "1" * 64,
                "read_entity_contract_revision": "full-read-entities-v1:2222222222222222",
                "read_entity_contract_sha256": "2" * 64,
                "catalog_sha256": "3" * 64,
                "semantics_revision": 31,
            },
            {**OLD_PINS, "semantics_revision": 30},
            {**OLD_PINS, "profile_sha256": "0" * 64},
            {**OLD_PINS, "semantics_revision": 33},
        )
        for pins in bad:
            with (
                self.subTest(pins=pins),
                self.assertRaises(read.TlvReadProviderContractError),
            ):
                self.ingest(envelope(self.profile, sequence=1, **pins))


if __name__ == "__main__":
    unittest.main()
