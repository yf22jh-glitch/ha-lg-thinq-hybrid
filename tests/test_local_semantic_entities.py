"""Read-only Home Assistant entities for exact Rethink Local profile fields."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from homeassistant.components.sensor import SensorDeviceClass

from custom_components.my_lg import binary_sensor, event as event_platform, sensor
from custom_components.my_lg.const import (
    DEVICE_TYPE_AIR_CONDITIONER,
    DEVICE_TYPE_AIR_PURIFIER,
    DEVICE_TYPE_COOKTOP,
    DEVICE_TYPE_DEHUMIDIFIER,
    DEVICE_TYPE_DISH_WASHER,
    DEVICE_TYPE_HUMIDIFIER,
    DEVICE_TYPE_KIMCHI_REFRIGERATOR,
    DEVICE_TYPE_OVEN,
    DEVICE_TYPE_REFRIGERATOR,
    DEVICE_TYPE_STICK_CLEANER,
    DEVICE_TYPE_STYLER,
    DEVICE_TYPE_WASHTOWER,
    DEVICE_TYPE_WATER_PURIFIER,
    DOMAIN,
)
from custom_components.my_lg.local_entity import (
    iter_local_semantic_contracts,
    local_semantic_duplicate_source,
    local_semantic_unique_id,
)
from custom_components.my_lg.local_provider import (
    LocalSemanticShadowProvider,
    local_pat_device_identity_proof,
    load_local_semantic_profile_catalogue,
)
from custom_components.my_lg.local_read_provider import (
    TlvReadEvent,
    TlvReadFieldContract,
    TlvReadProfile,
    TlvReadShadowProvider,
    load_tlv_read_catalogue,
)
from tests.test_local_provider import (
    BINDING_ID,
    SERVICE_ONE,
    availability_payload,
    runtime_payload,
)

NOW = datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc)
PAT_DEVICE_ID = "pat-dishwasher-local-001"


def pat_coordinator():
    return SimpleNamespace(
        device_id=PAT_DEVICE_ID,
        alias="Pilot dishwasher",
        model="D121110",
        device_type="TEST_DEVICE",
        async_add_listener=lambda _listener: lambda: None,
    )


def healthy_dishwasher_provider() -> LocalSemanticShadowProvider:
    profile = load_local_semantic_profile_catalogue()[1]["dishwasher-core-state-v1"]
    provider = LocalSemanticShadowProvider(BINDING_ID, profile, now=lambda: NOW)
    values = {
        "cycle.phase": "washing",
        "cycle.reserve_min": 90,
        "lock.child_enabled": True,
        "door.open": False,
    }
    fields = {}
    for semantic_id, value in values.items():
        contract = profile.fields[semantic_id]
        field = {
            "value": value,
            "value_type": contract.value_type,
            "observed_at": "2026-08-13T00:59:58.000Z",
            "confidence": contract.confidence[0],
            "exposure": contract.exposure,
        }
        if contract.unit is not None:
            field["unit"] = contract.unit
        fields[semantic_id] = field
    snapshot = {
        "schema_version": 1,
        "semantics_revision": profile.semantics_revision,
        "binding_id": BINDING_ID,
        "model_id": profile.model_id,
        "platform": profile.platform,
        "session_id": "session_local_entity_001",
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
    provider.ingest(
        provider.state_topic,
        json.dumps(snapshot, separators=(",", ":")).encode(),
        qos=1,
        retained=True,
    )
    provider.ingest(
        provider.availability_topic,
        availability_payload("online", session_id="session_local_entity_001"),
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


class LocalSemanticEntityContractTests(unittest.TestCase):
    def test_legacy_complete_read_uses_exact_raw_mapping_and_existing_identity(self) -> None:
        semantic_id = "diagnostic.washer.spin_setting_raw"
        contract = TlvReadFieldContract(
            descriptor_key=f"WTL_KPK_BDH_KR_01|{semantic_id}",
            semantic_id=semantic_id,
            domain="sensor",
            value_types=("number",),
            exposure="diagnostic",
            label_ko="세탁 탈수 설정 raw",
            entity_category="diagnostic",
            enabled_by_default=False,
            publication_mode="retained-current",
        )
        field = SimpleNamespace(value=6)
        provider = SimpleNamespace(
            profile=SimpleNamespace(fields_by_semantic_id={semantic_id: contract}),
            display_field=lambda _id: field,
            display_field_available=lambda _id: True,
        )
        coordinator = SimpleNamespace(
            device_id="pilot-washtower-001",
            alias="Washtower",
            model="WTL_KPK_BDH_KR_01",
            device_type=DEVICE_TYPE_WASHTOWER,
        )
        description = next(item for item in sensor.WASHTOWER_SENSORS if item.key == "washer_spin")
        entity = sensor.LocalLegacyReadSensor(
            provider, coordinator, semantic_id, contract, description
        )
        self.assertEqual(entity.unique_id, f"{coordinator.device_id}_washer_spin")
        self.assertEqual(entity.native_value, "SPIN_1000")
        self.assertTrue(entity.available)
        field.value = 7
        self.assertIsNone(entity.native_value)
        self.assertFalse(entity.available)

    def test_legacy_read_mappings_preserve_only_evidenced_values(self) -> None:
        self.assertEqual(sensor._legacy_read_value("washer_course", 114), "AI_COURSE")
        self.assertEqual(sensor._legacy_read_value("washer_course", 46), "NORMAL")
        self.assertEqual(sensor._legacy_read_value("washer_water_temp", 3), "TEMP_40")
        self.assertEqual(sensor._legacy_read_value("washer_door_lock", 1), "DOORLOCK_ON")
        self.assertEqual(sensor._legacy_read_value("dryer_state", 2), "RUNNING")
        self.assertEqual(sensor._legacy_read_value("dryer_state", 7), "DRYING")
        self.assertEqual(sensor._legacy_read_value("styler_door_lock", True), "DOOR_LOCK_ON")
        self.assertEqual(sensor._legacy_read_value("styler_night_dry", True), "NIGHTDRY_ON")
        self.assertEqual(sensor._legacy_read_value("styler_night_dry", False), "NIGHTDRY_OFF")
        for key in ("washer_energy", "dryer_energy", "styler_energy"):
            self.assertEqual(sensor._legacy_read_value(key, 0), 0)
            self.assertEqual(sensor._legacy_read_value(key, 219), 219)
            self.assertIsNone(sensor._legacy_read_value(key, -1))
            self.assertIsNone(sensor._legacy_read_value(key, True))
            self.assertIsNone(sensor._legacy_read_value(key, 65536))
        self.assertIsNone(sensor._legacy_read_value("dryer_error", 99))

    def test_all_sixteen_legacy_read_aliases_keep_their_established_ids(self) -> None:
        examples = {
            "washer_course": (114, "AI_COURSE"),
            "washer_spin": (6, "SPIN_1000"),
            "washer_water_temp": (8, "TEMP_COLD"),
            "washer_water_level": (0, "WATERLEVEL_1"),
            "washer_error": (0, "ERROR_NO"),
            "washer_door_lock": (1, "DOORLOCK_ON"),
            "dryer_state": (7, "DRYING"),
            "dryer_dry_level": (0, "NO_DRYLEVEL"),
            "dryer_duct_clogging": (0, "DUCT_CLOGGING_LEVEL_0"),
            "dryer_error": (0, "ERROR_NO"),
            "styler_remain": (37, 37),
            "styler_door_lock": (True, "DOOR_LOCK_ON"),
            "styler_night_dry": (True, "NIGHTDRY_ON"),
            "washer_energy": (219, 219),
            "dryer_energy": (219, 219),
            "styler_energy": (219, 219),
        }
        self.assertEqual(len(sensor._LOCAL_LEGACY_READ_SEMANTICS), len(examples))
        for (model, key), semantic_id in sensor._LOCAL_LEGACY_READ_SEMANTICS.items():
            with self.subTest(model=model, key=key):
                value, expected = examples[key]
                description = next(
                    item for item in (
                        sensor.WASHTOWER_SENSORS if model == "WTL_KPK_BDH_KR_01"
                        else sensor.STYLER_SENSORS
                    ) if item.key == key
                )
                contract = TlvReadFieldContract(
                    descriptor_key=f"{model}|{semantic_id}",
                    semantic_id=semantic_id,
                    domain="binary_sensor" if type(value) is bool else "sensor",
                    value_types=("boolean",) if type(value) is bool else ("number",),
                    exposure=("diagnostic" if model == "WTL_KPK_BDH_KR_01"
                              or key == "styler_energy" else "state"),
                    label_ko=key,
                    entity_category=("diagnostic" if model == "WTL_KPK_BDH_KR_01"
                                     or key == "styler_energy" else None),
                    enabled_by_default=model == "ST_R_ETH01Y_" and key != "styler_energy",
                    publication_mode="retained-current",
                    unit=description.native_unit_of_measurement,
                )
                field = SimpleNamespace(value=value)
                provider = SimpleNamespace(
                    profile=SimpleNamespace(fields_by_semantic_id={semantic_id: contract}),
                    display_field=lambda _id: field,
                    display_field_available=lambda _id: True,
                )
                coordinator = SimpleNamespace(
                    device_id="pilot-legacy-001", alias="Pilot", model=model,
                    device_type=(DEVICE_TYPE_WASHTOWER if model == "WTL_KPK_BDH_KR_01"
                                 else DEVICE_TYPE_STYLER),
                )
                entity = sensor.LocalLegacyReadSensor(
                    provider, coordinator, semantic_id, contract, description
                )
                self.assertEqual(entity.unique_id, f"{coordinator.device_id}_{key}")
                self.assertEqual(entity.native_value, expected)
                if key.endswith("_energy"):
                    self.assertEqual(entity.native_unit_of_measurement, "Wh")
                    self.assertIsNone(entity.entity_description.state_class)

    def test_legacy_local_sensor_keeps_entity_identity_and_rejects_unknown_state(self) -> None:
        profiles = load_local_semantic_profile_catalogue()[1]
        profile = profiles["washtower-core-state-v1"]
        provider = LocalSemanticShadowProvider(BINDING_ID, profile, now=lambda: NOW)
        coordinator = SimpleNamespace(
            device_id="pilot-washtower-001",
            alias="Washtower",
            model="WTL_KPK_BDH_KR_01",
            device_type=DEVICE_TYPE_WASHTOWER,
        )
        description = next(
            item for item in sensor.WASHTOWER_SENSORS if item.key == "washer_state"
        )
        entity = sensor.LocalLegacySensor(
            provider,
            coordinator,
            "washer.cycle.state",
            profile.fields["washer.cycle.state"],
            description,
        )
        self.assertEqual(entity.unique_id, f"{coordinator.device_id}_washer_state")
        self.assertEqual(entity._display_value("washing"), "RUNNING")
        self.assertIsNone(entity._display_value("unmapped"))

    def test_unique_id_is_bounded_and_collision_resistant_for_long_inputs(self) -> None:
        first = local_semantic_unique_id("p" * 300, "semantic." + "x" * 300)
        second = local_semantic_unique_id("p" * 300, "semantic." + "x" * 299 + "y")
        self.assertEqual(len(first), 128)
        self.assertEqual(len(second), 128)
        self.assertNotEqual(first, second)

    def test_expanded_actual_owner_maps_cover_cooktop_and_washtower(self) -> None:
        cooktop = sensor._PAT_SENSOR_SEMANTICS[DEVICE_TYPE_COOKTOP]
        self.assertEqual(
            {
                key: cooktop[key]
                for key in (
                    "left_rear_state",
                    "left_rear_power",
                    "right_front_state",
                    "right_front_power",
                )
            },
            {
                "left_rear_state": "burner.left_rear.state",
                "left_rear_power": "burner.left_rear.power_level",
                "right_front_state": "burner.right_front.state",
                "right_front_power": "burner.right_front.power_level",
            },
        )
        self.assertEqual(
            {
                key: sensor._WIDEQ_SENSOR_SEMANTICS[key]
                for key in (
                    "washer_state",
                    "washer_remain",
                    "dryer_state",
                    "dryer_remain",
                )
            },
            {
                "washer_state": "washer.cycle.state",
                "washer_remain": "washer.cycle.remaining_min",
                "dryer_state": "dryer.cycle.state",
                "dryer_remain": "dryer.cycle.remaining_min",
            },
        )

    def test_number_and_string_sensors_use_exact_profile_contract(self) -> None:
        provider = healthy_dishwasher_provider()
        coordinator = pat_coordinator()
        reserve = sensor.LocalSemanticSensor(
            provider,
            coordinator,
            "cycle.reserve_min",
            provider.profile.fields["cycle.reserve_min"],
        )
        phase = sensor.LocalSemanticSensor(
            provider,
            coordinator,
            "cycle.phase",
            provider.profile.fields["cycle.phase"],
        )

        self.assertEqual(
            reserve.unique_id,
            f"{PAT_DEVICE_ID}_local_semantic_cycle.reserve_min",
        )
        self.assertEqual(reserve.device_info["identifiers"], {(DOMAIN, PAT_DEVICE_ID)})
        self.assertEqual(reserve.native_value, 90)
        self.assertEqual(reserve.native_unit_of_measurement, "min")
        self.assertEqual(phase.native_value, "washing")
        self.assertIsNone(phase.native_unit_of_measurement)
        self.assertTrue(reserve.available)
        self.assertFalse(reserve.entity_registry_enabled_default)
        self.assertEqual(
            reserve.extra_state_attributes["semantic_id"], "cycle.reserve_min"
        )

    def test_boolean_contract_is_binary_sensor_and_health_controls_availability(
        self,
    ) -> None:
        provider = healthy_dishwasher_provider()
        entity = binary_sensor.LocalSemanticBinarySensor(
            provider,
            pat_coordinator(),
            "lock.child_enabled",
            provider.profile.fields["lock.child_enabled"],
        )

        self.assertTrue(entity.is_on)
        self.assertTrue(entity.available)
        provider.set_transport_ready(False)
        self.assertFalse(entity.available)
        self.assertTrue(
            entity.is_on,
            "last exact value remains inspectable while unavailable",
        )

    def test_string_allowlist_becomes_read_only_enum_sensor(self) -> None:
        profile = load_local_semantic_profile_catalogue()[1][
            "wireless-vacuum-core-state-v1"
        ]
        entity = sensor.LocalSemanticSensor(
            LocalSemanticShadowProvider(BINDING_ID, profile, now=lambda: NOW),
            pat_coordinator(),
            "display.charging_brightness",
            profile.fields["display.charging_brightness"],
        )

        self.assertEqual(entity.device_class, SensorDeviceClass.ENUM)
        self.assertEqual(entity.options, ["very_low", "low", "high", "very_high"])

    def test_numeric_event_uses_closed_type_and_typed_event_data(self) -> None:
        contract = TlvReadFieldContract(
            descriptor_key="WMLJ32RS|energy.interval.delta_wh",
            semantic_id="energy.interval.delta_wh",
            domain="event",
            value_types=("number",),
            exposure="event",
            label_ko="구간 에너지 변화",
            entity_category=None,
            enabled_by_default=False,
            publication_mode="transient-event",
            unit="Wh",
            event_type="observed",
        )
        profile = TlvReadProfile(
            profile_id="WMLJ32RS:read-sensors-v1",
            contract_revision=1,
            profile_revision="tlv-read-sensor-profiles-v1:test",
            profile_sha256="b" * 64,
            read_entity_contract_revision="tlv-read-entities-v1:test",
            read_entity_contract_sha256="c" * 64,
            catalog_sha256="d" * 64,
            semantics_revision=31,
            model_id="WMLJ32RS",
            platform="thinq2",
            fields=(contract,),
        )
        provider = SimpleNamespace(profile=profile, event_available=True)
        coordinator = SimpleNamespace(
            device_id="pat-oven-numeric-event-001",
            alias="Numeric event oven",
            model="WMLJ32RS",
            device_type="DEVICE_OVEN",
        )
        entity = event_platform.TlvReadEventEntity(
            provider,
            coordinator,
            contract.semantic_id,
            contract,
        )
        entity._trigger_event = Mock()
        entity.async_write_ha_state = Mock()

        entity._handle_event(
            TlvReadEvent(
                semantic_id=contract.semantic_id,
                descriptor_key=contract.descriptor_key,
                event_type="observed",
                value=18.5,
                value_type="number",
                unit="Wh",
                observed_at=NOW,
                confidence="confirmed-test",
                sequence=7,
            )
        )

        self.assertEqual(entity.event_types, ["observed"])
        entity._trigger_event.assert_called_once_with(
            "observed",
            {
                "semantic_id": "energy.interval.delta_wh",
                "descriptor_key": "WMLJ32RS|energy.interval.delta_wh",
                "value": 18.5,
                "value_type": "number",
                "unit": "Wh",
                "observed_at": NOW.isoformat(),
                "confidence": "confirmed-test",
                "sequence": 7,
            },
        )


class LocalSemanticEntityFactoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_complete_read_is_created_without_wideq(self) -> None:
        semantic_id = "diagnostic.washer.spin_setting_raw"
        contract = TlvReadFieldContract(
            descriptor_key=f"WTL_KPK_BDH_KR_01|{semantic_id}",
            semantic_id=semantic_id,
            domain="sensor",
            value_types=("number",),
            exposure="diagnostic",
            label_ko="세탁 탈수 원시 코드",
            entity_category="diagnostic",
            enabled_by_default=False,
            publication_mode="retained-current",
        )
        coordinator = SimpleNamespace(
            device_id="pilot-washtower-001",
            alias="Washtower",
            model="WTL_KPK_BDH_KR_01",
            device_type=DEVICE_TYPE_WASHTOWER,
            get=lambda *_path: None,
            supports=lambda _group: False,
            async_add_listener=lambda _listener: lambda: None,
        )
        read = SimpleNamespace(
            profile=SimpleNamespace(
                model_id=coordinator.model,
                fields_by_semantic_id={semantic_id: contract},
            ),
        )
        data = SimpleNamespace(
            wideq_coordinator=None,
            coordinators={coordinator.device_id: coordinator},
            local_providers={},
            local_read_providers={coordinator.device_id: read},
        )
        entry = SimpleNamespace(runtime_data=data, async_on_unload=lambda _remove: None)
        entities = []
        with (
            patch.object(sensor, "RawSensorManager") as manager,
            patch.object(sensor, "iter_tlv_read_contracts", return_value=[]),
            patch.object(sensor, "TLV_READ_DIAGNOSTIC_KEYS", ()),
        ):
            manager.return_value.add_new.return_value = None
            await sensor.async_setup_entry(None, entry, entities.extend)
        legacy = [item for item in entities if isinstance(item, sensor.LocalLegacyReadSensor)]
        self.assertEqual([item.entity_description.key for item in legacy], ["washer_spin"])

    async def test_course_energy_uses_local_wh_under_existing_ids(self) -> None:
        for model, device_type, semantic_id, key in (
            ("WTL_KPK_BDH_KR_01", DEVICE_TYPE_WASHTOWER, "washer.cycle.energy_wh", "washer_energy"),
            ("WTL_KPK_BDH_KR_01", DEVICE_TYPE_WASHTOWER, "dryer.cycle.energy_wh", "dryer_energy"),
            ("ST_R_ETH01Y_", DEVICE_TYPE_STYLER, "diagnostic.cycle.course_spend_power_raw", "styler_energy"),
        ):
            with self.subTest(key=key):
                contract = TlvReadFieldContract(
                    descriptor_key=f"{model}|{semantic_id}",
                    semantic_id=semantic_id,
                    domain="sensor",
                    value_types=("number",),
                    exposure="diagnostic",
                    label_ko=key,
                    entity_category="diagnostic",
                    enabled_by_default=False,
                    publication_mode="retained-current",
                    unit="Wh",
                )
                coordinator = SimpleNamespace(
                    device_id=f"pilot-{key}", alias="Pilot", model=model,
                    device_type=device_type, get=lambda *_path: None,
                    supports=lambda _group: False,
                    async_add_listener=lambda _listener: lambda: None,
                )
                provider = SimpleNamespace(
                    profile=SimpleNamespace(model_id=model, fields_by_semantic_id={semantic_id: contract}),
                    display_field=lambda _id: SimpleNamespace(value=219),
                    display_field_available=lambda _id: True,
                )
                data = SimpleNamespace(
                    wideq_coordinator=None,
                    coordinators={coordinator.device_id: coordinator},
                    local_providers={},
                    local_read_providers={coordinator.device_id: provider},
                )
                entry = SimpleNamespace(runtime_data=data, async_on_unload=lambda _remove: None)
                entities = []
                with (
                    patch.object(sensor, "RawSensorManager") as manager,
                    patch.object(sensor, "iter_tlv_read_contracts", return_value=[]),
                    patch.object(sensor, "TLV_READ_DIAGNOSTIC_KEYS", ()),
                ):
                    manager.return_value.add_new.return_value = None
                    await sensor.async_setup_entry(None, entry, entities.extend)
                aliases = [item for item in entities if isinstance(item, sensor.LocalLegacyReadSensor)]
                self.assertEqual([item.entity_description.key for item in aliases], [key])
                self.assertEqual(aliases[0].unique_id, f"{coordinator.device_id}_{key}")
                self.assertEqual(aliases[0].native_value, 219)
                self.assertTrue(aliases[0].available)
                self.assertEqual(aliases[0].native_unit_of_measurement, "Wh")
                self.assertIsNone(aliases[0].entity_description.state_class)

    async def test_legacy_local_sensors_survive_without_wideq(self) -> None:
        profile = load_local_semantic_profile_catalogue()[1][
            "washtower-core-state-v1"
        ]
        provider = LocalSemanticShadowProvider(BINDING_ID, profile, now=lambda: NOW)
        coordinator = SimpleNamespace(
            device_id="pilot-washtower-001",
            alias="Washtower",
            model=profile.model_id,
            device_type=DEVICE_TYPE_WASHTOWER,
            get=lambda *_path: None,
            supports=lambda _group: False,
            async_add_listener=lambda _listener: lambda: None,
        )
        data = SimpleNamespace(
            wideq_coordinator=None,
            coordinators={coordinator.device_id: coordinator},
            local_providers={coordinator.device_id: provider},
            local_read_providers={},
        )
        entry = SimpleNamespace(runtime_data=data, async_on_unload=lambda _remove: None)
        entities = []
        with patch.object(sensor, "RawSensorManager") as manager:
            manager.return_value.add_new.return_value = None
            await sensor.async_setup_entry(None, entry, entities.extend)
        legacy = [item for item in entities if isinstance(item, sensor.LocalLegacySensor)]
        self.assertEqual(
            {item.entity_description.key for item in legacy},
            {"washer_state", "washer_remain", "washer_child_lock", "dryer_remain"},
        )
        self.assertTrue(
            all(item.unique_id.endswith("_" + item.entity_description.key) for item in legacy)
        )

    def test_every_bundled_profile_field_is_duplicate_or_one_typed_entity(
        self,
    ) -> None:
        profiles = load_local_semantic_profile_catalogue()[1]
        created_rows: list[tuple[str, str]] = []
        skipped_rows: list[tuple[str, str]] = []
        entity_unique_ids: list[str] = []
        all_semantics: set[str] = set()
        created_semantics: set[str] = set()
        skipped_semantics: set[str] = set()
        unknown_types: list[tuple[str, str, str]] = []

        self.assertEqual(len(profiles), 19)
        for profile_id, profile in profiles.items():
            provider = LocalSemanticShadowProvider(
                f"pilot_{profile_id.replace('-', '_')}_001",
                profile,
                now=lambda: NOW,
            )
            coordinator = SimpleNamespace(
                device_id=f"pat-{profile_id}",
                alias=f"Pilot {profile_id}",
                model=profile.model_id,
                device_type="TEST_DEVICE",
            )
            yielded = {
                semantic_id
                for value_type in ("boolean", "number", "string")
                for semantic_id, _contract in iter_local_semantic_contracts(
                    provider,
                    value_type,
                    wideq_configured=True,
                )
            }
            for semantic_id, contract in profile.fields.items():
                all_semantics.add(semantic_id)
                duplicate_source = local_semantic_duplicate_source(
                    provider,
                    semantic_id,
                    wideq_configured=True,
                )
                if contract.value_type not in ("boolean", "number", "string"):
                    unknown_types.append((profile_id, semantic_id, contract.value_type))
                    continue
                if duplicate_source is not None:
                    self.assertNotIn(semantic_id, yielded)
                    skipped_rows.append((profile_id, semantic_id))
                    skipped_semantics.add(semantic_id)
                    continue

                self.assertIn(semantic_id, yielded)
                entity = (
                    binary_sensor.LocalSemanticBinarySensor(
                        provider, coordinator, semantic_id, contract
                    )
                    if contract.value_type == "boolean"
                    else sensor.LocalSemanticSensor(
                        provider, coordinator, semantic_id, contract
                    )
                )
                created_rows.append((profile_id, semantic_id))
                created_semantics.add(semantic_id)
                entity_unique_ids.append(entity.unique_id)
                self.assertEqual(
                    entity.unique_id,
                    f"{coordinator.device_id}_local_semantic_{semantic_id}",
                )

        self.assertEqual(unknown_types, [])
        profile_rows = {
            (profile_id, semantic_id)
            for profile_id, profile in profiles.items()
            for semantic_id in profile.fields
        }
        self.assertEqual(set(created_rows) | set(skipped_rows), profile_rows)
        self.assertFalse(set(created_rows) & set(skipped_rows))
        self.assertEqual(all_semantics, created_semantics | skipped_semantics)
        self.assertEqual(len(entity_unique_ids), len(set(entity_unique_ids)))

        first = profiles["dhum-core-state-v1"]
        second = profiles["dhum-core-state-v2"]
        same_device = pat_coordinator()
        first_entity = sensor.LocalSemanticSensor(
            LocalSemanticShadowProvider(BINDING_ID, first, now=lambda: NOW),
            same_device,
            "error.code",
            first.fields["error.code"],
        )
        second_entity = sensor.LocalSemanticSensor(
            LocalSemanticShadowProvider(BINDING_ID, second, now=lambda: NOW),
            same_device,
            "error.code",
            second.fields["error.code"],
        )
        self.assertEqual(first_entity.unique_id, second_entity.unique_id)

    async def test_entity_listener_unsubscribes_and_only_sla_entities_poll(
        self,
    ) -> None:
        provider = healthy_dishwasher_provider()
        entity = sensor.LocalSemanticSensor(
            provider,
            pat_coordinator(),
            "cycle.reserve_min",
            provider.profile.fields["cycle.reserve_min"],
        )
        entity.async_write_ha_state = Mock()

        self.assertFalse(entity.should_poll)
        await entity.async_added_to_hass()
        provider.set_transport_ready(False)
        entity.async_write_ha_state.assert_called_once_with()
        await entity.async_will_remove_from_hass()
        provider.set_transport_ready(True)
        entity.async_write_ha_state.assert_called_once_with()

        freshness_profile = load_local_semantic_profile_catalogue()[1][
            "kimchi-thinq1-core-state-v1"
        ]
        freshness_entity = sensor.LocalSemanticSensor(
            LocalSemanticShadowProvider(
                "pilot_kimchi_provider_001",
                freshness_profile,
                now=lambda: NOW,
            ),
            pat_coordinator(),
            "compartment.middle.storage_mode",
            freshness_profile.fields["compartment.middle.storage_mode"],
        )
        self.assertTrue(
            freshness_entity.should_poll,
            "clock polling must publish an unavailable state when the SLA expires",
        )

    async def test_factories_materialize_every_nonduplicate_exact_field_by_type(
        self,
    ) -> None:
        provider = healthy_dishwasher_provider()
        data = SimpleNamespace(
            wideq_coordinator=None,
            coordinators={PAT_DEVICE_ID: pat_coordinator()},
            local_providers={PAT_DEVICE_ID: provider},
        )
        entry = SimpleNamespace(runtime_data=data, async_on_unload=lambda _remove: None)
        binary_entities = []
        sensor_entities = []

        await binary_sensor.async_setup_entry(None, entry, binary_entities.extend)
        with patch.object(sensor, "RawSensorManager") as manager:
            manager.return_value.add_new.return_value = None
            await sensor.async_setup_entry(None, entry, sensor_entities.extend)

        binary_semantics = {
            entity.semantic_id
            for entity in binary_entities
            if isinstance(entity, binary_sensor.LocalSemanticBinarySensor)
        }
        sensor_semantics = {
            entity.semantic_id
            for entity in sensor_entities
            if isinstance(entity, sensor.LocalSemanticSensor)
        }
        self.assertEqual(
            binary_semantics,
            {
                "consumable.rinse_aid_refill_required",
                "lock.child_enabled",
                "sound.chime_enabled",
                "consumable.salt_refill_required",
                "door.open",
            },
        )
        self.assertEqual(
            sensor_semantics,
            {
                "cycle.course",
                "cycle.course_type",
                "cycle.phase",
                "cycle.remaining_min",
                "cycle.reserve_min",
                "cycle.state",
                "cycle.total_min",
                "error.code",
            },
        )
        self.assertIn("door.open", binary_semantics)
        self.assertIn("cycle.state", sensor_semantics)
        self.assertIn("cycle.remaining_min", sensor_semantics)

    async def test_factories_fill_all_189_rows_when_no_pat_or_wideq_owner_exists(
        self,
    ) -> None:
        profiles = load_local_semantic_profile_catalogue()[1]
        coordinators = {}
        providers = {}
        expected = set()
        for index, profile in enumerate(profiles.values()):
            device_id = f"ownerless-pat-{index:02d}"
            coordinators[device_id] = SimpleNamespace(
                device_id=device_id,
                alias=f"Ownerless {index}",
                model=profile.model_id,
                device_type="TEST_DEVICE",
                async_add_listener=lambda _listener: lambda: None,
            )
            providers[device_id] = LocalSemanticShadowProvider(
                f"pilot_ownerless_profile_{index:02d}", profile, now=lambda: NOW
            )
            expected.update((device_id, semantic_id) for semantic_id in profile.fields)

        data = SimpleNamespace(
            wideq_coordinator=None,
            coordinators=coordinators,
            local_providers=providers,
            local_read_providers={},
        )
        entry = SimpleNamespace(runtime_data=data, async_on_unload=lambda _remove: None)
        entities = []
        await binary_sensor.async_setup_entry(None, entry, entities.extend)
        with patch.object(sensor, "RawSensorManager") as manager:
            manager.return_value.add_new.return_value = None
            await sensor.async_setup_entry(None, entry, entities.extend)

        actual = {
            (entity.device_info["identifiers"].copy().pop()[1], entity.semantic_id)
            for entity in entities
            if isinstance(
                entity,
                (binary_sensor.LocalSemanticBinarySensor, sensor.LocalSemanticSensor),
            )
        }
        self.assertEqual(len(expected), 198)
        self.assertEqual(actual, expected)

    async def test_actual_owners_outside_full_profile_still_dedupe_legacy_rows(
        self,
    ) -> None:
        profile = load_local_semantic_profile_catalogue()[1]["styler-core-state-v1"]
        coordinator = SimpleNamespace(
            device_id="pat-styler-union-001",
            alias="Union styler",
            model=profile.model_id,
            device_type=DEVICE_TYPE_STYLER,
            data={"present": True},
            get=lambda *_path, **_kwargs: 1,
            supports=lambda _group: True,
            async_add_listener=lambda _listener: lambda: None,
        )
        wideq = Mock()
        wideq.async_add_listener.return_value = lambda: None
        read_provider = SimpleNamespace(
            profile=SimpleNamespace(fields_by_semantic_id={}, fields=())
        )
        data = SimpleNamespace(
            wideq_coordinator=wideq,
            coordinators={coordinator.device_id: coordinator},
            local_providers={
                coordinator.device_id: LocalSemanticShadowProvider(
                    "pilot_styler_union_001", profile, now=lambda: NOW
                )
            },
            local_read_providers={coordinator.device_id: read_provider},
        )
        entry = SimpleNamespace(runtime_data=data, async_on_unload=lambda _remove: None)
        entities = []
        with patch.object(sensor, "RawSensorManager") as manager:
            manager.return_value.add_new.return_value = None
            await sensor.async_setup_entry(None, entry, entities.extend)

        local_semantics = {
            entity.semantic_id
            for entity in entities
            if isinstance(entity, sensor.LocalSemanticSensor)
        }
        self.assertNotIn("cycle.state", local_semantics)
        self.assertNotIn("cycle.course", local_semantics)
        self.assertTrue(
            any(
                isinstance(entity, sensor.MyLgSensor)
                and entity.entity_description.key == "styler_status"
                for entity in entities
            )
        )
        self.assertTrue(
            any(
                isinstance(entity, sensor.LocalLegacySensor)
                and entity.entity_description.key == "styler_course"
                for entity in entities
            )
        )


class FullReadFactoryTests(unittest.IsolatedAsyncioTestCase):
    class Primary:
        expected_proof = "a" * 64
        binding_generation = 1
        cohort_generation = 1
        session_id = "1" * 32
        transport_ready = True
        control_alive = True

        def __init__(self, binding_id, model_id, platform) -> None:
            self.binding_id = binding_id
            self.model_id = model_id
            self.platform = platform
            self.listeners = []

        @property
        def read_publication_authority(self):
            if not self.transport_ready or not self.control_alive:
                return None
            return self.binding_generation, self.session_id

        def async_add_listener(self, callback):
            self.listeners.append(callback)
            return lambda: self.listeners.remove(callback)

    class Coordinator:
        def __init__(self, device_id, model, device_type) -> None:
            self.device_id = device_id
            self.alias = f"Full {model}"
            self.model = model
            self.device_type = device_type
            self.data = {"present": True}

        def async_add_listener(self, _listener):
            return lambda: None

        def supports(self, _group):
            return True

        def get(self, *path, **_kwargs):
            # Mirror the audited production owner seams rather than promoting
            # every static PAT description synthetically. The dishwasher does
            # not report current course, WashTower PAT status is absent, and a
            # Kimchi layout query must remain a list-shaped value.
            if self.device_type == DEVICE_TYPE_WASHTOWER:
                return None
            if self.device_type == DEVICE_TYPE_DISH_WASHER and path[:1] == (
                "dishWashingCourse",
            ):
                return None
            if self.device_type == DEVICE_TYPE_KIMCHI_REFRIGERATOR and path == (
                "temperature",
            ):
                return []
            return 1

        def get_location(self, *_path, **_kwargs):
            return 1

        def get_zone(self, *_path, **_kwargs):
            return 1

        def push_codes(self):
            return []

    async def test_presence_only_primary_authorizes_the_first_full_read(self) -> None:
        clock = [NOW]
        binding_id = "pilot_presence_full_read_001"
        pat_device_id = "presence-only-full-read-device"
        pilot_profile = load_local_semantic_profile_catalogue()[1][
            "dhum-core-state-v2"
        ]
        read_profile = load_tlv_read_catalogue()[pilot_profile.model_id]
        proof = local_pat_device_identity_proof(
            binding_id,
            pilot_profile.model_id,
            pilot_profile.platform,
            pat_device_id,
        )
        primary = LocalSemanticShadowProvider(
            binding_id,
            pilot_profile,
            pat_device_id=pat_device_id,
            require_identity=True,
            now=lambda: clock[0],
        )
        presence = json.dumps(
            {
                "schema_version": 1,
                "status": "online",
                "evidence": "attested-session",
                "binding_generation": 1,
                "sequence": 1,
                "pat_device_id_proof_sha256": proof,
                "profile_id": pilot_profile.profile_id,
                "service_instance_id": SERVICE_ONE,
                "observed_at": "2026-08-13T00:59:57.000Z",
                "valid_until": None,
            },
            separators=(",", ":"),
        ).encode()
        primary.ingest(
            primary.runtime_availability_topic,
            runtime_payload("online"),
            qos=1,
            retained=True,
        )
        primary.ingest(primary.presence_topic, presence, qos=1, retained=True)
        primary.set_transport_ready(True)
        primary.ingest(primary.presence_topic, presence, qos=1, retained=False)
        self.assertTrue(primary.control_alive)
        self.assertIsNone(primary.session_id)
        self.assertIsNone(primary.cohort_generation)

        contract = next(
            item
            for item in read_profile.fields
            if item.publication_mode == "retained-current"
        )
        value = {
            "boolean": True,
            "number": 1,
            "string": "observed",
        }[contract.value_types[0]]
        field = {
            "value": value,
            "value_type": contract.value_types[0],
            "observed_at": "2026-08-13T00:59:58.000Z",
            "confidence": "confirmed-presence-only-read",
            "exposure": contract.exposure,
        }
        if contract.unit is not None:
            field["unit"] = contract.unit
        current = {
            "schema_version": 2,
            "publication_plan_revision": 2,
            "profile_id": read_profile.profile_id,
            "profile_contract_revision": read_profile.contract_revision,
            "profile_revision": read_profile.profile_revision,
            "profile_sha256": read_profile.profile_sha256,
            "read_entity_contract_revision": read_profile.read_entity_contract_revision,
            "read_entity_contract_sha256": read_profile.read_entity_contract_sha256,
            "catalog_sha256": read_profile.catalog_sha256,
            "semantics_revision": read_profile.semantics_revision,
            "binding_id": binding_id,
            "model_id": read_profile.model_id,
            "platform": read_profile.platform,
            "binding_generation": 1,
            "pat_device_id_proof_sha256": proof,
            "publication_session_id": SERVICE_ONE,
            "cohort_generation": 3,
            "source_session_id": "capture_epoch_001",
            "sequence": 1,
            "published_at": "2026-08-13T00:59:59.000Z",
            "fields": {contract.semantic_id: field},
            "diagnostics": {
                "rejected_frames": 0,
                "unresolved_fields": 0,
                "invalid_values": 0,
                "unsupported_frames": 0,
            },
        }
        provider = TlvReadShadowProvider(
            binding_id,
            pat_device_id,
            read_profile,
            primary,
            allow_legacy_v1_fallback=True,
            now=lambda: clock[0],
        )
        try:
            provider.set_transport_ready(True)
            self.assertTrue(
                provider.ingest(
                    provider.current_topic,
                    json.dumps(current, separators=(",", ":")).encode(),
                    qos=1,
                    retained=False,
                )
            )
            self.assertTrue(provider.field_available(contract.semantic_id))
            updates = []
            remove = provider.async_add_listener(lambda: updates.append(clock[0]))
            try:
                expiry = primary.read_publication_authority_expiry
                assert expiry is not None
                clock[0] = expiry[1] + timedelta(milliseconds=1)
                self.assertTrue(primary.expire_read_publication_authority(expiry))
                # Presence TTL is a command liveness guard; a retained online
                # presence still authorizes read-only state until explicit offline.
                self.assertFalse(primary.control_alive)
                self.assertTrue(provider.field_available(contract.semantic_id))
                self.assertEqual(len(updates), 1)
                self.assertFalse(primary.expire_read_publication_authority(expiry))
                self.assertEqual(len(updates), 1)
            finally:
                remove()
        finally:
            provider.close()

    _PHYSICAL_MODELS = (
        "AIR_2C0001_WW",
        "AIR_910604_WW",
        "CST_170004_WW",
        "CST_170004_WW",
        "CST_570004_WW",
        "CST_570004_WW",
        "DHUM_056905_WW",
        "HUM_056905_WW",
        "1WPD4CMIDR__3",
        "2REFO1DBN3K_U",
        "2REK1D04AR170",
        "3REK2G03VI230D_2",
        "ST_R_ETH01Y_",
        "WBEF3",
        "WMLJ32RS",
        "WTL_KPK_BDH_KR_01",
        "D121110",
        "HWWA9X3C_F2U",
    )
    _PILOT_PROFILE_BY_MODEL = {
        "AIR_2C0001_WW": "air-tower-core-state-v1",
        "AIR_910604_WW": "air-core-state-v1",
        "CST_170004_WW": "cst170-core-state-v1",
        "CST_570004_WW": "cst570-core-state-v1",
        "DHUM_056905_WW": "dhum-core-state-v2",
        "HUM_056905_WW": "humidifier-core-state-v1",
        "1WPD4CMIDR__3": "purifier-core-state-v1",
        "2REFO1DBN3K_U": "fridge-core-state-v1",
        "2REK1D04AR170": "kimchi-thinq1-core-state-v1",
        "3REK2G03VI230D_2": "kimchi-aabb-core-state-v1",
        "ST_R_ETH01Y_": "styler-core-state-v2",
        "WBEF3": "cooktop-left-front-state-v1",
        "WMLJ32RS": "oven-core-state-v1",
        "WTL_KPK_BDH_KR_01": "washtower-core-state-v1",
        "D121110": "dishwasher-core-state-v1",
        "HWWA9X3C_F2U": "wireless-vacuum-core-state-v1",
    }
    _DEVICE_TYPE_BY_MODEL = {
        "AIR_2C0001_WW": DEVICE_TYPE_AIR_PURIFIER,
        "AIR_910604_WW": DEVICE_TYPE_AIR_PURIFIER,
        "CST_170004_WW": DEVICE_TYPE_AIR_CONDITIONER,
        "CST_570004_WW": DEVICE_TYPE_AIR_CONDITIONER,
        "DHUM_056905_WW": DEVICE_TYPE_DEHUMIDIFIER,
        "HUM_056905_WW": DEVICE_TYPE_HUMIDIFIER,
        "1WPD4CMIDR__3": DEVICE_TYPE_WATER_PURIFIER,
        "2REFO1DBN3K_U": DEVICE_TYPE_REFRIGERATOR,
        "2REK1D04AR170": DEVICE_TYPE_KIMCHI_REFRIGERATOR,
        "3REK2G03VI230D_2": DEVICE_TYPE_KIMCHI_REFRIGERATOR,
        "ST_R_ETH01Y_": DEVICE_TYPE_STYLER,
        "WBEF3": DEVICE_TYPE_COOKTOP,
        "WMLJ32RS": DEVICE_TYPE_OVEN,
        "WTL_KPK_BDH_KR_01": DEVICE_TYPE_WASHTOWER,
        "D121110": DEVICE_TYPE_DISH_WASHER,
        "HWWA9X3C_F2U": DEVICE_TYPE_STICK_CLEANER,
    }

    async def test_all_357_contract_rows_have_exactly_one_actual_ha_owner(self) -> None:
        catalogue = load_tlv_read_catalogue()
        coordinators = {}
        providers = {}
        expected = set()
        profiles_by_device = {}
        for index, (model_id, profile) in enumerate(catalogue.items()):
            device_id = f"full-read-pat-{index:02d}"
            coordinator = self.Coordinator(
                device_id, model_id, self._DEVICE_TYPE_BY_MODEL[model_id]
            )
            coordinators[device_id] = coordinator
            binding_id = f"pilot_full_read_binding_{index:02d}"
            providers[device_id] = TlvReadShadowProvider(
                binding_id,
                device_id,
                profile,
                self.Primary(binding_id, model_id, profile.platform),
                now=lambda: NOW,
            )
            profiles_by_device[device_id] = profile
            expected.update(
                (device_id, contract.semantic_id) for contract in profile.fields
            )

        data = SimpleNamespace(
            wideq_coordinator=None,
            coordinators=coordinators,
            local_providers={},
            local_read_providers=providers,
        )
        entry = SimpleNamespace(runtime_data=data, async_on_unload=lambda _remove: None)
        entities = []
        await binary_sensor.async_setup_entry(None, entry, entities.extend)
        with patch.object(sensor, "RawSensorManager") as manager:
            manager.return_value.add_new.return_value = None
            await sensor.async_setup_entry(None, entry, entities.extend)
        await event_platform.async_setup_entry(None, entry, entities.extend)

        owners = {}
        semantic_entities = []
        for entity in entities:
            semantic_id = None
            if isinstance(
                entity,
                (
                    binary_sensor.TlvReadBinarySensor,
                    sensor.TlvReadSensor,
                    sensor.LocalLegacyReadSensor,
                    event_platform.TlvReadEventEntity,
                ),
            ):
                semantic_id = (
                    entity.semantic_id
                    if hasattr(entity, "semantic_id")
                    else entity._semantic_id
                )
            elif isinstance(entity, sensor.MyLgSensor):
                semantic_id = sensor._PAT_SENSOR_SEMANTICS.get(
                    entity.coordinator.device_type, {}
                ).get(entity.entity_description.key)
            elif isinstance(entity, binary_sensor.MyLgBinarySensor):
                semantic_id = binary_sensor._PAT_BINARY_SEMANTICS.get(
                    entity.coordinator.device_type, {}
                ).get(entity.entity_description.key)
            if semantic_id is None:
                continue
            device_id = entity.device_info["identifiers"].copy().pop()[1]
            key = (device_id, semantic_id)
            if key not in expected:
                continue
            owners.setdefault(key, []).append(type(entity).__name__)
            semantic_entities.append(entity)

        self.assertEqual(len(expected), 370)
        self.assertEqual(set(owners), expected)
        self.assertTrue(all(len(owner) == 1 for owner in owners.values()))
        self.assertEqual(
            len({entity.unique_id for entity in semantic_entities}),
            len(semantic_entities),
        )
        local_entities = [
            entity
            for entity in semantic_entities
            if isinstance(
                entity,
                (
                    binary_sensor.TlvReadBinarySensor,
                    sensor.TlvReadSensor,
                    event_platform.TlvReadEventEntity,
                ),
            )
        ]
        self.assertTrue(
            all(
                entity.name == f"Local · {entity._contract.label_ko}"
                for entity in local_entities
            )
        )
        self.assertTrue(all(len(entity.unique_id) <= 128 for entity in local_entities))
        self.assertTrue(
            all(
                entity.entity_registry_enabled_default
                is (entity._contract.exposure not in ("diagnostic", "event"))
                for entity in local_entities
            )
        )
        tlv_models = {
            "AIR_2C0001_WW",
            "AIR_910604_WW",
            "CST_170004_WW",
            "CST_570004_WW",
            "DHUM_056905_WW",
            "HUM_056905_WW",
        }
        reviewed_owners = {
            (device_id, contract.semantic_id): contract.owner
            for device_id, profile in profiles_by_device.items()
            if profile.model_id not in tlv_models
            for contract in profile.fields
        }
        self.assertEqual(len(reviewed_owners), 80)
        self.assertEqual(sum(owner == "PAT" for owner in reviewed_owners.values()), 4)
        for key, owner in reviewed_owners.items():
            with self.subTest(key=key, owner=owner):
                if owner == "PAT":
                    self.assertEqual(owners[key], ["MyLgSensor"])
                elif key[1] in {"washer.cycle.energy_wh", "dryer.cycle.energy_wh"}:
                    self.assertEqual(owners[key], ["LocalLegacyReadSensor"])
                else:
                    self.assertEqual(len(owners[key]), 1)
                    self.assertTrue(owners[key][0].startswith("TlvRead"))

    async def test_physical_read_and_pilot_union_has_one_actual_owner_per_row(
        self,
    ) -> None:
        read_catalogue = load_tlv_read_catalogue()
        pilot_profiles = load_local_semantic_profile_catalogue()[1]
        read_inventory = {
            (profile.model_id, contract.semantic_id)
            for profile in read_catalogue.values()
            for contract in profile.fields
        }
        pilot_inventory = {
            (profile.model_id, semantic_id)
            for profile in pilot_profiles.values()
            for semantic_id in profile.fields
        }
        self.assertEqual(len(read_inventory), 370)
        self.assertEqual(len(pilot_inventory), 184)
        self.assertEqual(len(read_inventory & pilot_inventory), 96)
        self.assertEqual(len(read_inventory | pilot_inventory), 458)
        coordinators = {}
        read_providers = {}
        pilot_providers = {}
        expected = set()
        expected_exposure = {}
        read_expected = set()
        first_device_by_model = {}

        for index, model_id in enumerate(self._PHYSICAL_MODELS):
            device_id = f"physical-owner-pat-{index:02d}"
            first_device_by_model.setdefault(model_id, device_id)
            coordinator = self.Coordinator(
                device_id,
                model_id,
                self._DEVICE_TYPE_BY_MODEL[model_id],
            )
            coordinators[device_id] = coordinator
            pilot_profile = pilot_profiles[self._PILOT_PROFILE_BY_MODEL[model_id]]
            pilot_providers[device_id] = LocalSemanticShadowProvider(
                f"pilot_physical_owner_binding_{index:02d}",
                pilot_profile,
                now=lambda: NOW,
            )
            expected.update(
                (device_id, semantic_id) for semantic_id in pilot_profile.fields
            )
            expected_exposure.update(
                {
                    (device_id, semantic_id): contract.exposure
                    for semantic_id, contract in pilot_profile.fields.items()
                }
            )
            read_profile = read_catalogue.get(model_id)
            if read_profile is not None:
                binding_id = f"pilot_full_physical_owner_{index:02d}"
                read_providers[device_id] = TlvReadShadowProvider(
                    binding_id,
                    device_id,
                    read_profile,
                    self.Primary(binding_id, model_id, read_profile.platform),
                    now=lambda: NOW,
                )
                expected.update(
                    (device_id, contract.semantic_id)
                    for contract in read_profile.fields
                )
                read_expected.update(
                    (device_id, contract.semantic_id)
                    for contract in read_profile.fields
                )
                expected_exposure.update(
                    {
                        (device_id, contract.semantic_id): contract.exposure
                        for contract in read_profile.fields
                    }
                )

        wideq = Mock()
        wideq.data = {}
        wideq.diagnostic_attributes = {}
        wideq.snapshot_for.return_value = {}
        wideq.async_add_listener.return_value = lambda: None
        data = SimpleNamespace(
            wideq_coordinator=wideq,
            coordinators=coordinators,
            local_providers=pilot_providers,
            local_read_providers=read_providers,
        )
        entry = SimpleNamespace(runtime_data=data, async_on_unload=lambda _remove: None)
        entities = []
        await binary_sensor.async_setup_entry(None, entry, entities.extend)
        with patch.object(sensor, "RawSensorManager") as manager:
            manager.return_value.add_new.return_value = None
            await sensor.async_setup_entry(None, entry, entities.extend)
        await event_platform.async_setup_entry(None, entry, entities.extend)

        feed_diagnostics = [
            entity
            for entity in entities
            if isinstance(entity, sensor.TlvReadDiagnosticSensor)
        ]
        expected_read_devices = {
            f"physical-owner-pat-{index:02d}"
            for index, model_id in enumerate(self._PHYSICAL_MODELS)
            if model_id in read_catalogue
        }
        self.assertEqual(set(read_providers), expected_read_devices)
        self.assertEqual(len(feed_diagnostics), len(read_providers) * 4)
        self.assertTrue(
            all(not entity.entity_registry_enabled_default for entity in feed_diagnostics)
        )
        self.assertEqual(
            {
                entity.device_info["identifiers"].copy().pop()[1]
                for entity in feed_diagnostics
            },
            set(read_providers),
        )
        integrated_energy = [
            entity
            for entity in entities
            if isinstance(entity, sensor.TlvIntegratedEnergySensor)
        ]
        self.assertEqual(len(integrated_energy), 8)
        self.assertEqual(
            {
                semantic_id: sum(
                    entity.source_semantic_id == semantic_id
                    for entity in integrated_energy
                )
                for semantic_id in sensor.AC_POWER_SEMANTICS
            },
            {
                "power.indoor_compressor_share_w": 4,
                "power.outdoor_unit_total_w": 4,
            },
        )
        self.assertEqual(
            sum(
                not entity.entity_registry_enabled_default
                for entity in integrated_energy
            ),
            8,
        )

        owners = {}
        owner_entities = {}
        semantic_entities = []
        for entity in entities:
            semantic_id = None
            if isinstance(
                entity,
                (
                    binary_sensor.TlvReadBinarySensor,
                    sensor.TlvReadSensor,
                    sensor.LocalLegacyReadSensor,
                    event_platform.TlvReadEventEntity,
                    binary_sensor.LocalSemanticBinarySensor,
                    sensor.LocalSemanticSensor,
                    sensor.LocalLegacySensor,
                ),
            ):
                # A legacy read alias is an additional registry identity, not
                # a second canonical semantic owner. Styler state and washer
                # child lock already have PAT/binary Local owners.
                if isinstance(entity, sensor.LocalLegacySensor) and entity.entity_description.key in {
                    "styler_state", "washer_child_lock"
                }:
                    continue
                semantic_id = entity.semantic_id
            elif isinstance(entity, sensor.MyLgSensor):
                semantic_id = sensor._PAT_SENSOR_SEMANTICS.get(
                    entity.coordinator.device_type, {}
                ).get(entity.entity_description.key)
            elif isinstance(entity, binary_sensor.MyLgBinarySensor):
                semantic_id = binary_sensor._PAT_BINARY_SEMANTICS.get(
                    entity.coordinator.device_type, {}
                ).get(entity.entity_description.key)
            elif isinstance(entity, sensor.WideqDeviceSensor):
                semantic_id = sensor._WIDEQ_SENSOR_SEMANTICS.get(
                    entity.entity_description.key
                )
            elif isinstance(entity, binary_sensor.WaterTankFullSensor):
                semantic_id = "water_tank.full"
            if semantic_id is None:
                continue
            device_id = entity.device_info["identifiers"].copy().pop()[1]
            key = (device_id, semantic_id)
            if key not in expected:
                continue
            owners.setdefault(key, []).append(type(entity).__name__)
            owner_entities.setdefault(key, []).append(entity)
            semantic_entities.append(entity)

        self.assertEqual(set(owners), expected)
        self.assertTrue(all(len(owner) == 1 for owner in owners.values()))
        self.assertEqual(set(expected_exposure), expected)
        self.assertEqual(
            len({entity.unique_id for entity in semantic_entities}),
            len(semantic_entities),
        )

        local_types = {
            "TlvReadBinarySensor",
            "TlvReadSensor",
            "TlvReadEventEntity",
            "LocalSemanticBinarySensor",
            "LocalSemanticSensor",
            "LocalLegacySensor",
            "LocalLegacyReadSensor",
        }
        pat_types = {"MyLgSensor", "MyLgBinarySensor"}
        wideq_types = {"WideqDeviceSensor", "WaterTankFullSensor"}

        def count_owned(keys, types):
            return sum(owners[key][0] in types for key in keys)

        canonical_ac_leaves = {
            (f"physical-owner-pat-{index:02d}", semantic_id)
            for index, model_id in enumerate(self._PHYSICAL_MODELS)
            if model_id in {"CST_170004_WW", "CST_570004_WW"}
            for semantic_id in (
                "temperature.current_c",
                "humidity.current_pct",
            )
        }
        self.assertEqual(len(canonical_ac_leaves), 8)
        self.assertEqual(
            {key: owners[key] for key in canonical_ac_leaves},
            {key: ["TlvReadSensor"] for key in canonical_ac_leaves},
        )
        self.assertEqual(
            count_owned(read_expected, local_types)
            + count_owned(read_expected, pat_types | wideq_types),
            len(read_expected),
        )
        pilot_only_expected = expected - read_expected
        self.assertEqual(
            count_owned(pilot_only_expected, local_types)
            + count_owned(pilot_only_expected, pat_types | wideq_types),
            len(pilot_only_expected),
        )
        self.assertEqual(
            count_owned(expected, local_types)
            + count_owned(expected, pat_types)
            + count_owned(expected, wideq_types),
            len(expected),
        )

        binary_types = {
            "TlvReadBinarySensor",
            "LocalSemanticBinarySensor",
            "MyLgBinarySensor",
            "WaterTankFullSensor",
        }
        event_types = {"TlvReadEventEntity"}
        self.assertEqual(
            count_owned(expected, event_types),
            sum(expected_exposure[key] == "event" for key in expected),
        )
        self.assertEqual(
            count_owned(expected, binary_types)
            + count_owned(expected, event_types)
            + sum(owners[key][0] not in binary_types | event_types for key in expected),
            len(expected),
        )
        self.assertEqual(
            {expected_exposure[key] for key in expected},
            {"state", "diagnostic", "event"},
        )
        local_keys = {key for key in expected if owners[key][0] in local_types}
        for key in local_keys:
            with self.subTest(key=key):
                is_full_read = owners[key][0].startswith("TlvRead")
                self.assertEqual(
                    owner_entities[key][0].entity_registry_enabled_default,
                    owners[key][0] == "LocalLegacyReadSensor"
                    or ((is_full_read or owners[key][0] == "LocalLegacySensor")
                        and expected_exposure[key] == "state"),
                )

        unique_keys = {
            (first_device_by_model[model_id], semantic_id)
            for model_id, semantic_id in read_inventory | pilot_inventory
        }
        self.assertEqual(
            len(unique_keys),
            len(read_inventory | pilot_inventory),
        )
        self.assertEqual(
            count_owned(unique_keys, local_types)
            + count_owned(unique_keys, pat_types)
            + count_owned(unique_keys, wideq_types),
            len(unique_keys),
        )

        dishwasher = first_device_by_model["D121110"]
        self.assertEqual(owners[(dishwasher, "cycle.state")], ["MyLgSensor"])
        self.assertEqual(owners[(dishwasher, "cycle.course")], ["LocalSemanticSensor"])
        washtower = first_device_by_model["WTL_KPK_BDH_KR_01"]
        for semantic_id in (
            "washer.cycle.state",
            "washer.cycle.remaining_min",
            "dryer.cycle.state",
            "dryer.cycle.remaining_min",
        ):
            expected_owner = (
                "WideqDeviceSensor"
                if semantic_id == "dryer.cycle.state"
                else "LocalLegacySensor"
            )
            self.assertEqual(owners[(washtower, semantic_id)], [expected_owner])


if __name__ == "__main__":
    unittest.main()
