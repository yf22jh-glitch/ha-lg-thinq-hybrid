"""Entity invariants for read-only Local wireless-vacuum enum sensors."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import custom_components.my_lg as my_lg

# Fast entity tests can import a lightweight package initializer first.
if not hasattr(my_lg, "MyLgConfigEntry"):
    my_lg.MyLgConfigEntry = object

from custom_components.my_lg import sensor
from custom_components.my_lg.const import (
    DEVICE_TYPE_DEHUMIDIFIER,
    DEVICE_TYPE_STICK_CLEANER,
    DOMAIN,
)
from custom_components.my_lg.local_provider import (
    LocalPilotIdentityExpectation,
    LocalSemanticShadowProvider,
    create_local_pat_device_identity_proof,
    load_local_semantic_profile_catalogue,
)
from tests.test_local_provider import availability_payload, runtime_payload

NOW = datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc)
PAT_DEVICE_ID = "11111111-2222-4333-8444-555555555554"
BINDING_ID = "pilot_vacuum_provider_001"
SESSION_ID = "session_vacuum_provider_001"

EXPECTED_VALUES = {
    "display.charging_brightness": "very_high",
    "suction.default_level": "normal",
    "mop.water_supply_level": "high",
    "mop.steam_supply_level": "high",
    "sound.charging_volume": "high",
    "sound.settings_button_volume": "off",
    "sound.charging_melody": "melody_2",
    "sound.dust_emptying_melody": "melody_1",
}

DHUM_PAT_DEVICE_ID = "11111111-2222-4333-8444-555555555553"
DHUM_BINDING_ID = "pilot_dhum_scalar_provider_001"
DHUM_EXPECTED_VALUES = {
    "airflow.direction": "front",
    "error.code": 0,
    "operation.block_reason": "none",
}


def coordinator(*, model: str = "HWWA9X3C_F2U"):
    return SimpleNamespace(
        device_id=PAT_DEVICE_ID,
        alias="Vacuum",
        model=model,
        device_type=DEVICE_TYPE_STICK_CLEANER,
        data={},
        profile={},
        get=lambda *_args: None,
        supports=lambda *_args: False,
        async_add_listener=lambda _listener: (lambda: None),
    )


def healthy_provider() -> LocalSemanticShadowProvider:
    profile = load_local_semantic_profile_catalogue()[1][
        "wireless-vacuum-core-state-v1"
    ]
    provider = LocalSemanticShadowProvider(BINDING_ID, profile, now=lambda: NOW)
    fields = {}
    for semantic_id, value in EXPECTED_VALUES.items():
        contract = profile.fields[semantic_id]
        fields[semantic_id] = {
            "value": value,
            "value_type": "string",
            "observed_at": "2026-08-13T00:59:58.000Z",
            "confidence": contract.confidence[0],
            "exposure": "state",
        }
    payload = json.dumps(
        {
            "schema_version": 1,
            "semantics_revision": 30,
            "binding_id": BINDING_ID,
            "model_id": profile.model_id,
            "platform": profile.platform,
            "session_id": SESSION_ID,
            "sequence": 1,
            "published_at": "2026-08-13T00:59:59.000Z",
            "fields": fields,
            "diagnostics": {
                "rejected_frames": 0,
                "unresolved_fields": 0,
                "invalid_values": 0,
                "unsupported_frames": 0,
            },
        },
        separators=(",", ":"),
    ).encode()
    provider.ingest(provider.state_topic, payload, qos=1, retained=True)
    provider.ingest(
        provider.availability_topic,
        availability_payload("online", session_id=SESSION_ID),
        qos=1,
        retained=True,
    )
    provider.ingest(
        provider.runtime_availability_topic,
        runtime_payload("online"),
        qos=1,
        retained=True,
    )
    provider.set_transport_ready(True)
    return provider


def healthy_dhum_provider() -> LocalSemanticShadowProvider:
    profile = load_local_semantic_profile_catalogue()[1]["dhum-core-state-v2"]
    provider = LocalSemanticShadowProvider(
        DHUM_BINDING_ID, profile, now=lambda: NOW
    )
    fields = {
        semantic_id: {
            "value": value,
            "value_type": profile.fields[semantic_id].value_type,
            "observed_at": "2026-08-13T00:59:58.000Z",
            "confidence": profile.fields[semantic_id].confidence[0],
            "exposure": "state",
        }
        for semantic_id, value in DHUM_EXPECTED_VALUES.items()
    }
    provider.ingest(
        provider.state_topic,
        json.dumps(
            {
                "schema_version": 1,
                "semantics_revision": 30,
                "binding_id": DHUM_BINDING_ID,
                "model_id": profile.model_id,
                "platform": profile.platform,
                "session_id": SESSION_ID,
                "sequence": 1,
                "published_at": "2026-08-13T00:59:59.000Z",
                "fields": fields,
                "diagnostics": {
                    "rejected_frames": 0,
                    "unresolved_fields": 0,
                    "invalid_values": 0,
                    "unsupported_frames": 0,
                },
            },
            separators=(",", ":"),
        ).encode(),
        qos=1,
        retained=True,
    )
    provider.ingest(
        provider.availability_topic,
        availability_payload("online", session_id=SESSION_ID),
        qos=1,
        retained=True,
    )
    provider.ingest(
        provider.runtime_availability_topic,
        runtime_payload("online"),
        qos=1,
        retained=True,
    )
    provider.set_transport_ready(True)
    return provider


class LocalVacuumSensorTests(unittest.IsolatedAsyncioTestCase):
    async def test_v3_cohort_contract_does_not_change_entity_identity(self) -> None:
        profile = load_local_semantic_profile_catalogue()[1][
            "wireless-vacuum-core-state-v1"
        ]
        expectation = LocalPilotIdentityExpectation(
            binding_id=BINDING_ID,
            binding_generation=1,
            model_id=profile.model_id,
            platform=profile.platform,
            pat_device_id_proof_sha256=create_local_pat_device_identity_proof(
                binding_id=BINDING_ID,
                model_id=profile.model_id,
                platform=profile.platform,
                pat_device_id=PAT_DEVICE_ID,
            ),
        )
        legacy = LocalSemanticShadowProvider(BINDING_ID, profile, now=lambda: NOW)
        cohort = LocalSemanticShadowProvider(
            BINDING_ID,
            profile,
            identity_expectation=expectation,
            snapshot_schema_version=3,
            now=lambda: NOW,
        )
        description = sensor.LOCAL_SENSOR_BY_PROFILE[profile.profile_id][0]
        legacy_entity = sensor.LocalSemanticSensor(
            legacy, coordinator(), description
        )
        cohort_entity = sensor.LocalSemanticSensor(
            cohort, coordinator(), description
        )

        self.assertEqual(cohort_entity.unique_id, legacy_entity.unique_id)
        self.assertEqual(
            cohort_entity.unique_id,
            f"{PAT_DEVICE_ID}_{description.key}",
        )
        self.assertEqual(cohort_entity.device_info, legacy_entity.device_info)

    async def test_creates_exactly_eight_read_only_enums_under_the_pat_device(
        self,
    ) -> None:
        provider = healthy_provider()
        entry = SimpleNamespace(
            runtime_data=SimpleNamespace(
                coordinators={PAT_DEVICE_ID: coordinator()},
                local_providers={PAT_DEVICE_ID: provider},
                wideq_coordinator=None,
            ),
            async_on_unload=lambda _callback: None,
        )
        entities = []

        await sensor.async_setup_entry(None, entry, entities.extend)

        local_entities = [
            entity for entity in entities if isinstance(entity, sensor.LocalSemanticSensor)
        ]
        self.assertEqual(len(local_entities), 8)
        by_semantic = {
            entity.entity_description.semantic_id: entity for entity in local_entities
        }
        self.assertEqual(set(by_semantic), set(EXPECTED_VALUES))
        for semantic_id, expected in EXPECTED_VALUES.items():
            entity = by_semantic[semantic_id]
            contract = provider.profile.fields[semantic_id]
            self.assertEqual(
                tuple(entity.entity_description.options or ()),
                contract.allowed_values,
            )
            self.assertEqual(entity.native_value, expected)
            self.assertTrue(entity.available)
            self.assertFalse(entity.should_poll)
            self.assertEqual(
                entity.device_info["identifiers"], {(DOMAIN, PAT_DEVICE_ID)}
            )
            self.assertFalse(hasattr(entity, "async_select_option"))

        provider.set_transport_ready(False)
        self.assertTrue(all(not entity.available for entity in local_entities))
        self.assertTrue(all(entity.native_value is None for entity in local_entities))

    async def test_listener_and_exact_model_contract_fail_closed(self) -> None:
        provider = healthy_provider()
        description = sensor.LOCAL_SENSOR_BY_PROFILE[provider.profile_id][0]
        entity = sensor.LocalSemanticSensor(provider, coordinator(), description)
        writer = Mock()
        entity.async_write_ha_state = writer
        await entity.async_added_to_hass()
        provider.set_transport_ready(False)
        writer.assert_called_once_with()

        for bad_coordinator in (
            coordinator(model="OTHER_MODEL"),
            SimpleNamespace(
                **{
                    **coordinator().__dict__,
                    "device_id": "11111111-2222-4333-8444-555555555599",
                }
            ),
        ):
            entry = SimpleNamespace(
                runtime_data=SimpleNamespace(
                    coordinators={PAT_DEVICE_ID: bad_coordinator},
                    local_providers={PAT_DEVICE_ID: healthy_provider()},
                    wideq_coordinator=None,
                ),
                async_on_unload=lambda _callback: None,
            )
            entities = []
            await sensor.async_setup_entry(None, entry, entities.extend)
            self.assertFalse(
                any(
                    isinstance(candidate, sensor.LocalSemanticSensor)
                    for candidate in entities
                )
            )


class LocalDhumScalarSensorTests(unittest.IsolatedAsyncioTestCase):
    async def test_creates_three_new_read_only_scalar_entities(self) -> None:
        provider = healthy_dhum_provider()
        dhum_coordinator = SimpleNamespace(
            device_id=DHUM_PAT_DEVICE_ID,
            alias="Dehumidifier",
            model=provider.model_id,
            device_type=DEVICE_TYPE_DEHUMIDIFIER,
            data={},
            profile={},
            get=lambda *_args: None,
            supports=lambda *_args: False,
            async_add_listener=lambda _listener: (lambda: None),
        )
        entry = SimpleNamespace(
            runtime_data=SimpleNamespace(
                coordinators={DHUM_PAT_DEVICE_ID: dhum_coordinator},
                local_providers={DHUM_PAT_DEVICE_ID: provider},
                wideq_coordinator=None,
            ),
            async_on_unload=lambda _callback: None,
        )
        entities = []

        await sensor.async_setup_entry(None, entry, entities.extend)

        local_entities = [
            entity
            for entity in entities
            if isinstance(entity, sensor.LocalSemanticSensor)
        ]
        self.assertEqual(len(local_entities), 3)
        by_semantic = {
            entity.entity_description.semantic_id: entity
            for entity in local_entities
        }
        self.assertEqual(set(by_semantic), set(DHUM_EXPECTED_VALUES))
        for semantic_id, expected in DHUM_EXPECTED_VALUES.items():
            entity = by_semantic[semantic_id]
            self.assertEqual(entity.native_value, expected)
            self.assertTrue(entity.available)
            self.assertFalse(entity.should_poll)
            self.assertFalse(
                entity.entity_description.entity_registry_enabled_default
            )
            self.assertEqual(
                entity.device_info["identifiers"],
                {(DOMAIN, DHUM_PAT_DEVICE_ID)},
            )
            self.assertFalse(hasattr(entity, "async_select_option"))

        provider.set_transport_ready(False)
        self.assertTrue(all(not entity.available for entity in local_entities))
        self.assertTrue(
            all(entity.native_value is None for entity in local_entities)
        )


if __name__ == "__main__":
    unittest.main()
