"""Fail-closed registration policy for transient full-read events."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from custom_components.my_lg import event as event_platform
from custom_components.my_lg.local_read_provider import load_tlv_read_catalogue


class TlvReadEventSafetyTests(unittest.TestCase):
    def test_all_bundled_transient_events_are_opt_in_until_replay_is_durable(
        self,
    ) -> None:
        entities = []
        artifact_contracts = []

        for index, profile in enumerate(load_tlv_read_catalogue().values()):
            coordinator = SimpleNamespace(
                device_id=f"event-safety-{index:02d}",
                alias=f"Event safety {index:02d}",
                model=profile.model_id,
                device_type="TEST_DEVICE",
            )
            provider = SimpleNamespace(profile=profile, event_available=True)
            for contract in profile.fields:
                if contract.domain != "event":
                    continue
                artifact_contracts.append(contract)
                entities.append(
                    event_platform.TlvReadEventEntity(
                        provider,
                        coordinator,
                        contract.semantic_id,
                        contract,
                    )
                )

        self.assertEqual(len(artifact_contracts), 12)
        self.assertTrue(all(not item.enabled_by_default for item in artifact_contracts))
        self.assertTrue(
            all(not item.entity_registry_enabled_default for item in entities)
        )
        self.assertEqual(
            {item.semantic_id for item in entities},
            {
                "energy.interval.delta_wh",
                "event.water_tank.changed",
                "water.usage_delta.cold_ml",
                "water.usage_delta.hot_ml",
                "water.usage_delta.mineral_ml",
                "water.usage_delta.purified_ml",
                "water.usage_delta.soda_ml",
                "water.usage_delta.sterilization_ml",
                "water.usage_delta.total_ml",
            },
        )


if __name__ == "__main__":
    unittest.main()
