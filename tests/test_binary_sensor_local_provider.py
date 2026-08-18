"""Entity-registry invariants for read-only Local provider connections."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from custom_components.my_lg import binary_sensor
from custom_components.my_lg.const import (
    DEVICE_TYPE_AIR_PURIFIER,
    DEVICE_TYPE_DEHUMIDIFIER,
    DEVICE_TYPE_STICK_CLEANER,
    DEVICE_TYPE_STYLER,
    DOMAIN,
)
from custom_components.my_lg.local_provider import (
    LocalSemanticShadowProvider,
    LocalWaterTankShadowProvider,
    load_local_semantic_profile_catalogue,
)
from tests.test_local_provider import (
    BINDING_ID,
    availability_payload,
    runtime_payload,
    state_payload,
)

NOW = datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc)
PAT_DEVICE_ID = "pat-dehumidifier-001"
AIR_TOWER_PAT_DEVICE_ID = "pat-air-tower-001"
STYLER_PAT_DEVICE_ID = "pat-styler-001"
DHUM_DISPLAY_PAT_DEVICE_ID = "pat-dehumidifier-display-001"
VACUUM_PAT_DEVICE_ID = "pat-vacuum-001"


class FakeWideqCoordinator:
    def __init__(self, value=0) -> None:
        self.data = {
            PAT_DEVICE_ID: {binary_sensor.WATER_TANK_KEY: value},
        }
        self.diagnostic_attributes = {"wideq_circuit_open": False}

    def snapshot_for(self, device_id):
        return (self.data or {}).get(device_id, {})


def pat_coordinator():
    return SimpleNamespace(
        device_id=PAT_DEVICE_ID,
        alias="Pilot dehumidifier",
        model="DHUM_056905_WW",
        device_type=DEVICE_TYPE_DEHUMIDIFIER,
    )


def healthy_local_provider(value=True):
    provider = LocalWaterTankShadowProvider(BINDING_ID, now=lambda: NOW)
    provider.ingest(
        provider.state_topic, state_payload(value=value), qos=1, retained=True
    )
    provider.ingest(
        provider.availability_topic,
        availability_payload("online"),
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


def pat_local_coordinator(device_id, alias, model, device_type):
    return SimpleNamespace(
        device_id=device_id,
        alias=alias,
        model=model,
        device_type=device_type,
    )


def healthy_semantic_provider(
    *,
    profile_id: str,
    binding_id: str,
    semantic_id: str,
    confidence: str,
    value: bool,
    semantics_revision: int,
    extra_values: dict[str, bool] | None = None,
) -> LocalSemanticShadowProvider:
    profile = load_local_semantic_profile_catalogue()[1][profile_id]
    provider = LocalSemanticShadowProvider(binding_id, profile, now=lambda: NOW)
    payload = json.dumps(
        {
            "schema_version": 1,
            "semantics_revision": semantics_revision,
            "binding_id": binding_id,
            "model_id": profile.model_id,
            "platform": profile.platform,
            "session_id": "session_local_provider_001",
            "sequence": 1,
            "published_at": "2026-08-13T00:59:59.000Z",
            "fields": {
                field_id: {
                    "value": field_value,
                    "value_type": "boolean",
                    "observed_at": "2026-08-13T00:59:58.000Z",
                    "confidence": (
                        confidence
                        if field_id == semantic_id
                        else profile.fields[field_id].confidence[0]
                    ),
                    "exposure": "state",
                }
                for field_id, field_value in {
                    semantic_id: value,
                    **(extra_values or {}),
                }.items()
            },
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
        availability_payload("online", session_id="session_local_provider_001"),
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


class WaterTankEntityInvariantTests(unittest.TestCase):
    def test_shadow_preserves_identity_state_availability_and_attributes(self) -> None:
        coordinator = FakeWideqCoordinator(value=0)
        wideq_only = binary_sensor.WaterTankFullSensor(coordinator, pat_coordinator())
        shadow = binary_sensor.WaterTankFullSensor(
            coordinator, pat_coordinator(), healthy_local_provider(value=True)
        )

        self.assertEqual(wideq_only.unique_id, f"{PAT_DEVICE_ID}_water_tank_full")
        self.assertEqual(shadow.unique_id, wideq_only.unique_id)
        self.assertEqual(shadow.device_info, wideq_only.device_info)
        self.assertEqual(shadow.device_info["identifiers"], {(DOMAIN, PAT_DEVICE_ID)})
        self.assertEqual(shadow.is_on, wideq_only.is_on)
        self.assertFalse(shadow.is_on, "Local true must not replace WideQ false")
        self.assertEqual(shadow.available, wideq_only.available)
        self.assertTrue(shadow.available)
        self.assertEqual(
            shadow.extra_state_attributes, wideq_only.extra_state_attributes
        )
        self.assertNotIn("local", " ".join(shadow.extra_state_attributes))

        coordinator.data = {}
        self.assertFalse(shadow.available, "Local health must not mask WideQ absence")

    def test_unknown_wideq_value_is_unavailable_state_not_guessed_true(self) -> None:
        sensor = binary_sensor.WaterTankFullSensor(
            FakeWideqCoordinator(value="UNKNOWN"),
            pat_coordinator(),
            healthy_local_provider(value=True),
        )
        self.assertIsNone(sensor.is_on)


class WaterTankEntityFactoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_shadow_adds_no_duplicate_entity(self) -> None:
        wideq = FakeWideqCoordinator(value=0)
        provider = healthy_local_provider(value=True)
        data = SimpleNamespace(
            wideq_coordinator=wideq,
            coordinators={PAT_DEVICE_ID: pat_coordinator()},
            local_providers={PAT_DEVICE_ID: provider},
        )
        entry = SimpleNamespace(runtime_data=data)
        entities = []

        await binary_sensor.async_setup_entry(None, entry, entities.extend)

        self.assertEqual(len(entities), 1)
        self.assertIsInstance(entities[0], binary_sensor.WaterTankFullSensor)
        self.assertEqual(entities[0].unique_id, f"{PAT_DEVICE_ID}_water_tank_full")


class LocalSemanticBinarySensorTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_profiles_create_only_verified_read_only_entities(
        self,
    ) -> None:
        dhum_coordinator = pat_local_coordinator(
            DHUM_DISPLAY_PAT_DEVICE_ID,
            "Dehumidifier",
            "DHUM_056905_WW",
            DEVICE_TYPE_DEHUMIDIFIER,
        )
        tower_coordinator = pat_local_coordinator(
            AIR_TOWER_PAT_DEVICE_ID,
            "Tower purifier",
            "AIR_2C0001_WW",
            DEVICE_TYPE_AIR_PURIFIER,
        )
        styler_coordinator = pat_local_coordinator(
            STYLER_PAT_DEVICE_ID,
            "Styler",
            "ST_R_ETH01Y_",
            DEVICE_TYPE_STYLER,
        )
        dhum_provider = healthy_semantic_provider(
            profile_id="dhum-core-state-v2",
            binding_id="pilot_dhum_display_provider_001",
            semantic_id="display.enabled",
            confidence=(
                "confirmed-exact-device-6-writes+6-acks+6-immediate-readbacks"
            ),
            value=True,
            semantics_revision=30,
            extra_values={"operation.blocked": False},
        )
        tower_provider = healthy_semantic_provider(
            profile_id="air-tower-core-state-v1",
            binding_id="pilot_air_tower_provider_001",
            semantic_id="energy_saving.ai_enabled",
            confidence="confirmed-exact-device-7-writes+7-immediate-readbacks",
            value=True,
            semantics_revision=27,
        )
        styler_provider = healthy_semantic_provider(
            profile_id="styler-core-state-v2",
            binding_id="pilot_styler_provider_001",
            semantic_id="display.current_time_enabled",
            confidence="confirmed-exact-modeljson+4-on-off-local-cloud-cycles",
            value=False,
            semantics_revision=28,
            extra_values={"option.no_interrupt_enabled": True},
        )
        data = SimpleNamespace(
            wideq_coordinator=None,
            coordinators={
                DHUM_DISPLAY_PAT_DEVICE_ID: dhum_coordinator,
                AIR_TOWER_PAT_DEVICE_ID: tower_coordinator,
                STYLER_PAT_DEVICE_ID: styler_coordinator,
            },
            local_providers={
                DHUM_DISPLAY_PAT_DEVICE_ID: dhum_provider,
                AIR_TOWER_PAT_DEVICE_ID: tower_provider,
                STYLER_PAT_DEVICE_ID: styler_provider,
            },
        )
        entities = []

        await binary_sensor.async_setup_entry(
            None, SimpleNamespace(runtime_data=data), entities.extend
        )

        self.assertEqual(len(entities), 5)
        by_id = {entity.unique_id: entity for entity in entities}
        dhum = by_id[f"{DHUM_DISPLAY_PAT_DEVICE_ID}_status_display"]
        blocked = by_id[f"{DHUM_DISPLAY_PAT_DEVICE_ID}_operation_blocked"]
        tower = by_id[f"{AIR_TOWER_PAT_DEVICE_ID}_ai_energy_saving"]
        styler = by_id[f"{STYLER_PAT_DEVICE_ID}_current_time_display"]
        no_interrupt = by_id[f"{STYLER_PAT_DEVICE_ID}_no_interrupt"]
        self.assertIsInstance(dhum, binary_sensor.LocalSemanticBinarySensor)
        self.assertIsInstance(tower, binary_sensor.LocalSemanticBinarySensor)
        self.assertIsInstance(styler, binary_sensor.LocalSemanticBinarySensor)
        self.assertIs(dhum.is_on, True)
        self.assertIs(blocked.is_on, False)
        self.assertIs(tower.is_on, True)
        self.assertIs(styler.is_on, False)
        self.assertIs(no_interrupt.is_on, True)
        self.assertEqual(
            blocked.entity_description.device_class,
            binary_sensor.BinarySensorDeviceClass.PROBLEM,
        )
        self.assertFalse(
            blocked.entity_description.entity_registry_enabled_default
        )
        for entity in (dhum, blocked, tower, styler, no_interrupt):
            self.assertFalse(hasattr(entity, "async_turn_on"))
            self.assertFalse(hasattr(entity, "async_turn_off"))
        self.assertTrue(tower.available)
        self.assertTrue(styler.available)
        self.assertEqual(
            dhum.device_info["identifiers"],
            {(DOMAIN, DHUM_DISPLAY_PAT_DEVICE_ID)},
        )
        self.assertEqual(
            tower.device_info["identifiers"],
            {(DOMAIN, AIR_TOWER_PAT_DEVICE_ID)},
        )
        self.assertEqual(
            styler.device_info["identifiers"],
            {(DOMAIN, STYLER_PAT_DEVICE_ID)},
        )

        tower_provider.set_transport_ready(False)
        self.assertFalse(tower.available)
        self.assertIsNone(tower.is_on)

    async def test_entity_listener_publishes_provider_health_changes(self) -> None:
        coordinator = pat_local_coordinator(
            AIR_TOWER_PAT_DEVICE_ID,
            "Tower purifier",
            "AIR_2C0001_WW",
            DEVICE_TYPE_AIR_PURIFIER,
        )
        provider = healthy_semantic_provider(
            profile_id="air-tower-core-state-v1",
            binding_id="pilot_air_tower_provider_001",
            semantic_id="energy_saving.ai_enabled",
            confidence="confirmed-exact-device-7-writes+7-immediate-readbacks",
            value=True,
            semantics_revision=28,
        )
        description = binary_sensor.LOCAL_BINARY_BY_PROFILE[provider.profile_id][0]
        entity = binary_sensor.LocalSemanticBinarySensor(
            provider, coordinator, description
        )
        writer = Mock()
        entity.async_write_ha_state = writer

        await entity.async_added_to_hass()
        provider.set_transport_ready(False)

        writer.assert_called_once_with()

    async def test_older_profiles_and_unmapped_fields_create_no_entity(
        self,
    ) -> None:
        styler_coordinator = pat_local_coordinator(
            STYLER_PAT_DEVICE_ID,
            "Styler",
            "ST_R_ETH01Y_",
            DEVICE_TYPE_STYLER,
        )
        dhum_coordinator = pat_local_coordinator(
            DHUM_DISPLAY_PAT_DEVICE_ID,
            "Dehumidifier",
            "DHUM_056905_WW",
            DEVICE_TYPE_DEHUMIDIFIER,
        )
        profiles = load_local_semantic_profile_catalogue()[1]
        styler_provider = LocalSemanticShadowProvider(
            "pilot_styler_provider_001",
            profiles["styler-core-state-v1"],
            now=lambda: NOW,
        )
        dhum_provider = LocalSemanticShadowProvider(
            "pilot_dhum_display_provider_001",
            profiles["dhum-core-state-v1"],
            now=lambda: NOW,
        )
        data = SimpleNamespace(
            wideq_coordinator=None,
            coordinators={
                STYLER_PAT_DEVICE_ID: styler_coordinator,
                DHUM_DISPLAY_PAT_DEVICE_ID: dhum_coordinator,
            },
            local_providers={
                STYLER_PAT_DEVICE_ID: styler_provider,
                DHUM_DISPLAY_PAT_DEVICE_ID: dhum_provider,
            },
        )
        entities = []

        await binary_sensor.async_setup_entry(
            None, SimpleNamespace(runtime_data=data), entities.extend
        )

        self.assertEqual(entities, [])

    async def test_vacuum_profile_creates_four_read_only_boolean_entities(
        self,
    ) -> None:
        profile = load_local_semantic_profile_catalogue()[1][
            "wireless-vacuum-core-state-v1"
        ]
        provider = LocalSemanticShadowProvider(
            "pilot_vacuum_provider_001", profile, now=lambda: NOW
        )
        semantics = {
            "suction.ai_adjustment_enabled": False,
            "battery.life_extension_enabled": False,
            "operation.auto_stop_and_go_enabled": True,
            "mop.suction_enabled": False,
        }
        fields = {
            semantic_id: {
                "value": value,
                "value_type": "boolean",
                "observed_at": "2026-08-13T00:59:58.000Z",
                "confidence": profile.fields[semantic_id].confidence[0],
                "exposure": "state",
            }
            for semantic_id, value in semantics.items()
        }
        provider.ingest(
            provider.state_topic,
            json.dumps(
                {
                    "schema_version": 1,
                    "semantics_revision": 30,
                    "binding_id": provider.binding_id,
                    "model_id": profile.model_id,
                    "platform": profile.platform,
                    "session_id": "session_local_provider_001",
                    "sequence": 1,
                    "published_at": "2026-08-13T00:59:59.000Z",
                    "fields": fields,
                    "diagnostics": {
                        "rejected_frames": 0,
                        "unresolved_fields": 0,
                        "invalid_values": 0,
                        "unsupported_frames": 0,
                    },
                }
            ).encode(),
            qos=1,
            retained=True,
        )
        provider.ingest(
            provider.availability_topic,
            availability_payload(
                "online", session_id="session_local_provider_001"
            ),
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
        vacuum_coordinator = pat_local_coordinator(
            VACUUM_PAT_DEVICE_ID,
            "Vacuum",
            profile.model_id,
            DEVICE_TYPE_STICK_CLEANER,
        )
        data = SimpleNamespace(
            wideq_coordinator=None,
            coordinators={VACUUM_PAT_DEVICE_ID: vacuum_coordinator},
            local_providers={VACUUM_PAT_DEVICE_ID: provider},
        )
        entities = []

        await binary_sensor.async_setup_entry(
            None, SimpleNamespace(runtime_data=data), entities.extend
        )

        self.assertEqual(len(entities), 4)
        by_semantic = {
            entity.entity_description.semantic_id: entity for entity in entities
        }
        self.assertEqual(set(by_semantic), set(semantics))
        for semantic_id, expected in semantics.items():
            entity = by_semantic[semantic_id]
            self.assertIs(entity.is_on, expected)
            self.assertFalse(entity.should_poll)
            self.assertFalse(hasattr(entity, "async_turn_on"))
            self.assertEqual(
                entity.device_info["identifiers"], {(DOMAIN, VACUUM_PAT_DEVICE_ID)}
            )


if __name__ == "__main__":
    unittest.main()
