"""The editable feature DB preserves the existing Local feature inventory."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


database = load_module(
    "my_lg_feature_database_test",
    ROOT / "custom_components" / "my_lg" / "feature_database.py",
)
manager = load_module(
    "my_lg_feature_manager_test", ROOT / "scripts" / "manage_local_features.py"
)
read = load_module(
    "my_lg_read_feature_database_test",
    ROOT / "custom_components" / "my_lg" / "local_read_provider.py",
)
control = load_module(
    "my_lg_control_feature_database_test",
    ROOT / "custom_components" / "my_lg" / "local_control_contract.py",
)
pilot = load_module(
    "my_lg_pilot_feature_database_test",
    ROOT / "custom_components" / "my_lg" / "local_provider.py",
)


class FeatureDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "features.sqlite3"
        self.counts = database.create_database(self.path, manager.current_definitions())

    def test_rollout_is_explicit_and_model_scoped(self) -> None:
        self.assertEqual(database.feature_change_sequence(self.path), 0)
        self.assertEqual(database.enabled_models(self.path), frozenset())
        database.set_model_rollout(self.path, "AIR_2C0001_WW", True)
        self.assertEqual(database.enabled_models(self.path), {"AIR_2C0001_WW"})
        self.assertEqual(database.feature_change_sequence(self.path), 1)
        database.set_model_rollout(self.path, "AIR_2C0001_WW", False)
        self.assertEqual(database.enabled_models(self.path), frozenset())
        self.assertEqual(database.feature_change_sequence(self.path), 2)

    def test_seed_keeps_disabled_read_entities_disabled_without_copying_identity(self) -> None:
        field = database.load_features(self.path, "full-read")[0]
        receipt = Path(self.directory.name) / "read-cleanup.json"
        receipt.write_text(json.dumps({"targets": [{
            "model_id": field["model_id"],
            "semantic_id": field["feature_id"],
            "entity_id": "sensor.private_entity_identifier",
            "new_disabled_by": "user",
        }]}))
        disabled = manager.disabled_read_keys_from_receipt(receipt)
        another = Path(self.directory.name) / "with-disabled.sqlite3"
        database.create_database(another, manager.current_definitions(disabled))
        selected = next(row for row in database.load_features(
            another, "full-read", include_disabled=True
        ) if (row["model_id"], row["feature_id"]) in disabled)
        self.assertFalse(selected["enabled"])
        self.assertNotIn(b"private_entity_identifier", another.read_bytes())

    def test_producer_registration_survives_entity_disable(self) -> None:
        row = database.load_features(self.path, "full-read")[0]
        database.upsert_feature(
            self.path, "full-read", row["model_id"], row["profile_id"],
            row["feature_id"], row["platform"], row["definition"],
            register_producer=True,
        )
        database.set_feature_enabled(
            self.path, "full-read", row["model_id"], row["profile_id"],
            row["feature_id"], False,
        )
        with sqlite3.connect(self.path) as connection:
            state = connection.execute(
                "SELECT enabled, producer_registered FROM features "
                "WHERE channel = 'full-read' AND model_id = ? AND profile_id = ? "
                "AND feature_id = ?",
                (row["model_id"], row["profile_id"], row["feature_id"]),
            ).fetchone()
        self.assertEqual(state, (0, 1))

    def test_new_model_declaration_starts_outside_rollout(self) -> None:
        row = database.load_features(self.path, "full-read")[0]
        model_id = "TEST_MODEL_ONLY"
        definition = dict(row["definition"])
        definition["descriptorKey"] = f"{model_id}|test.state"
        definition["semanticId"] = "test.state"
        database.upsert_feature(
            self.path, "full-read", model_id,
            f"{model_id}:read-sensors-v1", "test.state", "thinq2", definition,
        )
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute(
                "SELECT enabled FROM model_rollout WHERE model_id = ?", (model_id,)
            ).fetchone(), (0,))

    def test_one_model_cutover_does_not_change_other_models(self) -> None:
        model_id = "AIR_2C0001_WW"
        baseline_read = read.load_tlv_read_catalogue()
        baseline_control = control.load_local_control_entity_contract()
        baseline_pilot = pilot.load_local_semantic_profile_catalogue()[1]
        self.assertEqual(read.load_tlv_read_catalogue(self.path), baseline_read)
        self.assertEqual(
            control.load_local_control_entity_contract(self.path).descriptors,
            baseline_control.descriptors,
        )
        self.assertEqual(
            pilot.load_local_semantic_profile_catalogue(self.path)[1],
            baseline_pilot,
        )
        read_field = baseline_read[model_id].fields[0]
        database.set_feature_enabled(
            self.path, "full-read", model_id, f"{model_id}:read-sensors-v1",
            read_field.semantic_id, False,
        )
        control_field = baseline_control.descriptors_by_model[model_id][0]
        database.set_feature_enabled(
            self.path, "control-entity", model_id, "",
            control_field.capability_id, False,
        )
        pilot_profile = next(
            profile for profile in baseline_pilot.values()
            if profile.model_id == model_id
        )
        pilot_field = next(iter(pilot_profile.fields))
        database.set_feature_enabled(
            self.path, "pilot-read", model_id, pilot_profile.profile_id,
            pilot_field, False,
        )
        database.set_model_rollout(self.path, model_id, True)
        changed_read = read.load_tlv_read_catalogue(self.path)
        self.assertNotIn(read_field.semantic_id, changed_read[model_id].fields_by_semantic_id)
        for other_model, profile in baseline_read.items():
            if other_model != model_id:
                self.assertEqual(changed_read[other_model], profile)
        changed_control = control.load_local_control_entity_contract(self.path)
        self.assertNotIn(
            control_field.capability_id,
            {item.capability_id for item in changed_control.descriptors_by_model[model_id]},
        )
        for other_model, descriptors in baseline_control.descriptors_by_model.items():
            if other_model != model_id:
                self.assertEqual(changed_control.descriptors_by_model[other_model], descriptors)
        changed_pilot = pilot.load_local_semantic_profile_catalogue(self.path)[1]
        self.assertNotIn(pilot_field, changed_pilot[pilot_profile.profile_id].fields)
        for profile_id, profile in baseline_pilot.items():
            if profile.model_id != model_id:
                self.assertEqual(changed_pilot[profile_id], profile)

    def test_complete_rollout_does_not_require_old_read_control_or_pilot_bundles(self) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE model_rollout SET enabled = 1")
        self.assertTrue(database.all_models_enabled(self.path))
        with (
            patch.object(read, "_load_bundled_tlv_read_catalogue", side_effect=AssertionError("old read bundle used")),
            patch.object(control, "_load_bundled_local_control_entity_contract", side_effect=AssertionError("old control bundle used")),
            patch.object(pilot, "_load_bundled_local_semantic_profiles", side_effect=AssertionError("old pilot bundle used")),
        ):
            self.assertEqual(len(read.load_tlv_read_catalogue(self.path)), 16)
            self.assertEqual(len(control.load_local_control_entity_contract(self.path).descriptors), 107)
            self.assertEqual(len(pilot.load_local_semantic_profile_catalogue(self.path)[1]), 19)

    def test_existing_read_and_control_inventory_is_preserved(self) -> None:
        self.assertEqual(
            self.counts,
            {
                "full-read": 370,
                "pilot-read": 198,
                "pilot-profile": 19,
                "control-entity": 107,
                "confirmed-control": 128,
            },
        )
        legacy = read.load_tlv_read_catalogue()
        imported = read.load_tlv_read_catalogue_from_database(self.path)
        self.assertEqual(set(imported), set(legacy))
        for model_id, profile in imported.items():
            self.assertEqual(
                {field.descriptor_key: field for field in profile.fields},
                {field.descriptor_key: field for field in legacy[model_id].fields},
            )
        self.assertEqual(len(database.load_features(self.path, "pilot-read")), 198)
        self.assertEqual(len(database.load_features(self.path, "pilot-profile")), 19)
        self.assertEqual(len(database.load_features(self.path, "control-entity")), 107)
        self.assertEqual(len(database.load_features(self.path, "confirmed-control")), 128)
        self.assertEqual(
            control.load_local_control_entity_contract_from_database(self.path).descriptors,
            control.load_local_control_entity_contract().descriptors,
        )

    def test_pilot_profiles_match_bundle_without_requiring_a_global_revision(self) -> None:
        bundled = pilot.load_local_semantic_profile_catalogue()[1]
        imported = pilot._load_database_local_semantic_profiles(self.path)[1]
        self.assertEqual(set(imported), set(bundled))
        for profile_id, profile in imported.items():
            self.assertEqual(profile.fields, bundled[profile_id].fields)
            self.assertEqual(profile.model_id, bundled[profile_id].model_id)
            self.assertEqual(profile.platform, bundled[profile_id].platform)
            self.assertEqual(profile.availability_policy, bundled[profile_id].availability_policy)
            self.assertEqual(profile.authoritative_invalidations, bundled[profile_id].authoritative_invalidations)
            self.assertEqual(profile.freshness_max_age_ms, bundled[profile_id].freshness_max_age_ms)
            self.assertTrue(profile.revision_independent)

    def test_pilot_db_ignores_disabled_field_in_an_older_publication(self) -> None:
        profile_id = "dhum-core-state-v1"
        model_id = "DHUM_056905_WW"
        database.set_feature_enabled(
            self.path, "pilot-read", model_id, profile_id, "operation.mode", False
        )
        profile = pilot._load_database_local_semantic_profiles(self.path)[1][profile_id]
        payload = {
            "schema_version": 1,
            "semantics_revision": 34,
            "binding_id": "pilot_dhum_provider_001",
            "model_id": model_id,
            "platform": "thinq2",
            "session_id": "session_dhum_provider_001",
            "sequence": 1,
            "published_at": "2026-08-13T00:59:59.000Z",
            "fields": {
                "water_tank.full": {
                    "value": False, "value_type": "boolean",
                    "observed_at": "2026-08-13T00:59:59.000Z",
                    "confidence": profile.fields["water_tank.full"].confidence[0],
                    "exposure": "state",
                },
                "operation.mode": {
                    "value": "unknown", "value_type": "string",
                    "observed_at": "2026-08-13T00:59:59.000Z",
                    "confidence": "confirmed", "exposure": "state",
                },
            },
            "diagnostics": {
                "rejected_frames": 0, "unresolved_fields": 0,
                "invalid_values": 0, "unsupported_frames": 0,
            },
        }
        parsed = pilot._parse_state(
            json.dumps(payload).encode(), payload["binding_id"], profile,
            datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(set(parsed[3]), {"water_tank.full"})

    def test_empty_pilot_read_menu_keeps_binding_for_presence_and_energy(self) -> None:
        database.set_feature_enabled(
            self.path, "pilot-read", "DHUM_056905_WW",
            "dhum-water-tank-v1", "water_tank.full", False,
        )
        profiles = pilot._load_database_local_semantic_profiles(self.path)[1]
        self.assertIn("dhum-water-tank-v1", profiles)
        self.assertEqual(profiles["dhum-water-tank-v1"].fields, {})

    def test_one_field_can_be_disabled_and_added_without_rebuilding_others(self) -> None:
        before = read.load_tlv_read_catalogue_from_database(self.path)
        model_id = "AIR_2C0001_WW"
        profile_id = f"{model_id}:read-sensors-v1"
        field = before[model_id].fields[0]
        other_model = "HUM_056905_WW"
        original_other = before[other_model].fields
        database.set_feature_enabled(
            self.path, "full-read", model_id, profile_id, field.semantic_id, False
        )
        after_disable = read.load_tlv_read_catalogue_from_database(self.path)
        self.assertNotIn(field.semantic_id, after_disable[model_id].fields_by_semantic_id)
        self.assertEqual(after_disable[other_model].fields, original_other)

        definition = {
            "descriptorKey": f"{model_id}|test.new_state",
            "semanticId": "test.new_state",
            "domain": "sensor",
            "valueTypes": ["number"],
            "exposure": "state",
            "labelKo": "시험 상태",
            "owner": "none",
            "entityCategory": None,
            "enabledByDefault": True,
            "publicationMode": "retained-current",
        }
        database.upsert_feature(
            self.path, "full-read", model_id, profile_id, "test.new_state",
            "thinq2", definition,
        )
        after_add = read.load_tlv_read_catalogue_from_database(self.path)
        self.assertIn("test.new_state", after_add[model_id].fields_by_semantic_id)
        self.assertEqual(after_add[other_model].fields, original_other)
        self.assertEqual(
            len(database.load_features(self.path, "full-read")),
            370,
        )
        self.assertEqual(
            len(database.load_features(self.path, "full-read", include_disabled=True)),
            371,
        )

    def test_bad_definition_does_not_hide_other_models(self) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE features SET definition_json = ? WHERE channel = 'full-read' "
                "AND model_id = ? AND feature_id = ?",
                (json.dumps({"semanticId": "wrong"}), "AIR_2C0001_WW", "operation.mode"),
            )
        profiles = read.load_tlv_read_catalogue_from_database(self.path)
        self.assertIn("HUM_056905_WW", profiles)
        self.assertNotIn("operation.mode", profiles["AIR_2C0001_WW"].fields_by_semantic_id)

    def test_existing_database_cannot_be_overwritten_by_seed(self) -> None:
        with self.assertRaises(FileExistsError):
            database.create_database(self.path, manager.current_definitions())

    def test_control_scope_uses_exact_binding_and_values_without_release_pin(self) -> None:
        contract = control.load_local_control_entity_contract_from_database(self.path)
        model_id = "AIR_2C0001_WW"
        descriptor = contract.descriptors_by_model[model_id][0]
        binding = "binding_00000000000001"
        scoped = control.resolve_local_control_binding_eligibility(
            {
                "local_control_eligibility": {
                    "contract_sha256": "obsolete-release-pin",
                    "bindings": [{
                        "binding_id": binding,
                        "entries": [{
                            "capability_id": descriptor.capability_id,
                            "exact_values": [descriptor.exact_local_request_values[0]],
                        }],
                    }],
                }
            },
            contract,
            {binding: model_id},
        )
        self.assertEqual(
            scoped[binding].values_by_capability[descriptor.capability_id],
            (descriptor.exact_local_request_values[0],),
        )


if __name__ == "__main__":
    unittest.main()
