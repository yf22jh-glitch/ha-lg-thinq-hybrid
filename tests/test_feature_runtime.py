"""DB edits preserve running providers, exact values and HA identities."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch

from custom_components.my_lg import feature_database as database
from custom_components.my_lg.feature_runtime import FeatureEntityRuntime
from custom_components.my_lg.local_control_confirmed_features import load_confirmed_features
from custom_components.my_lg.local_control_entity import local_control_entities_for_domain
from custom_components.my_lg.local_control_native import async_native_local_control
from custom_components.my_lg.local_control_router import LocalControlRouter, LocalFeatureDisabled
from custom_components.my_lg.local_command import LocalCommandResult
from custom_components.my_lg.local_provider import LocalSemanticShadowProvider, load_local_semantic_profile_catalogue
from custom_components.my_lg.local_read_provider import TlvReadShadowProvider, load_tlv_read_catalogue
from custom_components.my_lg.sensor import TlvIntegratedEnergySensor, TlvReadSensor
from homeassistant.helpers.entity import Entity
from scripts.manage_local_features import current_definitions
from tests.test_local_read_provider import (
    BINDING_ID, MODEL_ID, NOW, PAT_DEVICE_ID, FakePrimaryProvider, envelope, snapshot_field,
)
from tests.test_local_provider import state_payload


class MemoryPlatform:
    """Adapter retaining registry IDs and operator disables across additions."""

    def __init__(self) -> None:
        self.entities = {}
        self.registry_ids = {}
        self.user_disabled = set()
        self.added = []
        self.removed = []

    async def async_add_entities(self, entities) -> None:
        for entity in entities:
            entity_id = self.registry_ids.setdefault(
                entity.unique_id, f"sensor.feature_{len(self.registry_ids)}"
            )
            self.added.append(entity.unique_id)
            if entity.unique_id not in self.user_disabled:
                entity.entity_id = entity_id
                self.entities[entity_id] = entity

    async def async_remove_entity(self, entity_id) -> None:
        self.removed.append(entity_id)
        self.entities.pop(entity_id)


class FeatureRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "features.sqlite3"
        definitions = current_definitions()
        database.create_database(self.path, definitions)
        for model in {row["model_id"] for row in definitions}:
            database.set_model_rollout(self.path, model, True)
        profiles = load_local_semantic_profile_catalogue(self.path)[1]
        self.primary = LocalSemanticShadowProvider(
            BINDING_ID, profiles["dhum-core-state-v2"], pat_device_id=PAT_DEVICE_ID,
            now=lambda: NOW,
        )
        read_primary = FakePrimaryProvider()
        read_primary.expected_proof = self.primary.expected_proof
        self.reads = TlvReadShadowProvider(
            BINDING_ID, PAT_DEVICE_ID, load_tlv_read_catalogue(self.path)[MODEL_ID],
            read_primary, read_contract_policy="field-compatible", now=lambda: NOW,
        )
        raw = json.loads(envelope())
        raw["pat_device_id_proof_sha256"] = self.primary.expected_proof
        raw["fields"] = {"humidity.current_pct": snapshot_field(55, "number", unit="%")}
        self.reads.set_transport_ready(True)
        self.reads.ingest(self.reads.current_topic, json.dumps(raw).encode(), qos=1, retained=True)
        self.data = SimpleNamespace(
            local_providers={PAT_DEVICE_ID: self.primary},
            local_read_providers={PAT_DEVICE_ID: self.reads},
            local_control_entity_contract=None, local_control_binding_eligibility={},
            local_control_composite_domain_contract=None, local_disabled_controls=frozenset(),
            local_confirmed_features=load_confirmed_features(self.path),
            local_mqtt_subscribers={PAT_DEVICE_ID: object()},
            local_energy_providers={PAT_DEVICE_ID: object()}, local_control=object(),
        )
        self.reload = AsyncMock()

        class Hass:
            config_entries = SimpleNamespace(async_reload=self.reload)

            async def async_add_executor_job(_self, function, *args):
                return function(*args)

        self.entry = SimpleNamespace(runtime_data=self.data, options={})
        self.runtime = FeatureEntityRuntime(Hass(), self.entry, self.path)
        self.platform = MemoryPlatform()
        coordinator = SimpleNamespace(device_id=PAT_DEVICE_ID, alias="Test", model=MODEL_ID,
                                      device_type="TEST_DEVICE")
        self.data.coordinators = {PAT_DEVICE_ID: coordinator}

        def build():
            contract = self.reads.profile.fields_by_semantic_id.get("humidity.current_pct")
            return [] if contract is None else [
                TlvReadSensor(self.reads, coordinator, contract.semantic_id, contract)
            ]

        initial = build()
        self.runtime.register("sensor", self.platform, build, initial)
        await self.platform.async_add_entities(initial)
        self.original_ids = dict(self.platform.registry_ids)
        self.preserved = (
            self.primary, self.reads, self.data.local_control,
            self.data.local_mqtt_subscribers[PAT_DEVICE_ID],
            self.data.local_energy_providers[PAT_DEVICE_ID],
        )

    def update(self, sql: str, parameters=()) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(sql, parameters)

    def humidity_edit(self, **changes) -> None:
        row = next(row for row in database.load_features(self.path, "full-read")
                   if row["model_id"] == MODEL_ID and row["feature_id"] == "humidity.current_pct")
        self.update("UPDATE features SET definition_json=? WHERE channel='full-read' "
                    "AND model_id=? AND feature_id='humidity.current_pct'",
                    (json.dumps(dict(row["definition"], **changes)), MODEL_ID))

    async def test_direct_sql_disable_enable_reuses_value_and_identity_without_restart(self) -> None:
        token = database.feature_database_token(self.path)
        sequence = database.feature_change_sequence(self.path)
        self.update("UPDATE features SET enabled=0 WHERE channel='full-read' AND model_id=? "
                    "AND feature_id='humidity.current_pct'", (MODEL_ID,))
        self.assertNotEqual(database.feature_database_token(self.path), token)
        self.assertEqual(database.feature_change_sequence(self.path), sequence)
        await self.runtime.async_refresh()
        self.assertEqual(self.platform.entities, {})
        self.update("UPDATE features SET enabled=1 WHERE channel='full-read' AND model_id=? "
                    "AND feature_id='humidity.current_pct'", (MODEL_ID,))
        await self.runtime.async_refresh()
        self.assertEqual(next(iter(self.platform.entities.values())).native_value, 55)
        self.assertEqual(self.platform.registry_ids, self.original_ids)
        self.assertEqual(self.preserved, (
            self.primary, self.reads, self.data.local_control,
            self.data.local_mqtt_subscribers[PAT_DEVICE_ID],
            self.data.local_energy_providers[PAT_DEVICE_ID],
        ))
        self.assertEqual(self.reads.current_sequence, 1)
        self.reload.assert_not_awaited()

    async def test_label_change_updates_name_but_audit_edits_do_not_rebuild_entities(self) -> None:
        self.humidity_edit(labelKo="새 습도 이름")
        await self.runtime.async_refresh()
        entity = next(iter(self.platform.entities.values()))
        self.assertEqual(entity.name, "Local · 새 습도 이름")
        self.assertEqual(entity.native_value, 55)
        self.assertEqual(self.platform.registry_ids, self.original_ids)
        added = len(self.platform.added)
        await self.runtime.async_refresh()
        self.assertEqual(len(self.platform.added), added)
        token = database.feature_database_token(self.path)
        self.update("INSERT INTO feature_changes(channel,model_id,profile_id,feature_id,action) "
                    "VALUES('full-read', ?, '', 'audit-only', 'upsert')", (MODEL_ID,))
        self.assertEqual(database.feature_database_token(self.path), token)

    async def test_disabled_full_read_does_not_reappear_through_primary_fallback(self) -> None:
        original = self.runtime._platforms["sensor"].build
        coordinator = self.data.coordinators[PAT_DEVICE_ID]
        unique_id = next(iter(self.original_ids))

        class FallbackEntity(Entity):
            _attr_unique_id = unique_id
            _semantic_id = "humidity.current_pct"
            _metadata = coordinator

        self.runtime._platforms["sensor"].build = lambda: original() or [FallbackEntity()]
        self.update("UPDATE features SET enabled=0 WHERE channel='full-read' AND model_id=? "
                    "AND feature_id='humidity.current_pct'", (MODEL_ID,))
        await self.runtime.async_refresh()
        self.assertEqual(self.platform.entities, {})
        self.update("UPDATE features SET enabled=1 WHERE channel='full-read' AND model_id=? "
                    "AND feature_id='humidity.current_pct'", (MODEL_ID,))
        await self.runtime.async_refresh()
        self.assertEqual(next(iter(self.platform.entities.values())).native_value, 55)
        self.assertEqual(self.platform.registry_ids, self.original_ids)

    async def test_full_read_visibility_takes_precedence_over_hidden_old_pilot(self) -> None:
        self.update("UPDATE features SET enabled=0 WHERE channel='pilot-read' AND model_id=? "
                    "AND feature_id='humidity.current_pct'", (MODEL_ID,))
        self.assertNotIn((MODEL_ID, "humidity.current_pct"), database.disabled_read_semantics(self.path))
        await self.runtime.async_refresh()
        self.assertEqual(next(iter(self.platform.entities.values())).native_value, 55)

    async def test_user_disabled_entity_stays_disabled_after_db_reenable(self) -> None:
        unique_id = next(iter(self.original_ids))
        self.platform.user_disabled.add(unique_id)
        await self.platform.async_remove_entity(self.original_ids[unique_id])
        for enabled in (0, 1):
            self.update("UPDATE features SET enabled=? WHERE channel='full-read' AND model_id=? "
                        "AND feature_id='humidity.current_pct'", (enabled, MODEL_ID))
            await self.runtime.async_refresh()
        self.assertEqual(self.platform.entities, {})
        self.assertEqual(self.platform.user_disabled, {unique_id})

    async def test_empty_read_menu_keeps_subscription_and_can_be_enabled_again(self) -> None:
        topics = self.reads.topics
        self.update("UPDATE features SET enabled=0 WHERE channel='full-read' AND model_id=?", (MODEL_ID,))
        await self.runtime.async_refresh()
        self.assertEqual(self.reads.profile.fields, ())
        self.assertEqual(self.reads.topics, topics)
        self.assertEqual(self.platform.entities, {})
        self.update("UPDATE features SET enabled=1 WHERE channel='full-read' AND model_id=? "
                    "AND feature_id='humidity.current_pct'", (MODEL_ID,))
        await self.runtime.async_refresh()
        self.assertEqual(next(iter(self.platform.entities.values())).native_value, 55)

    async def test_pilot_field_reenable_keeps_cached_state_and_session(self) -> None:
        semantic = "water_tank.full"
        raw = json.loads(state_payload(binding_id=self.primary.binding_id))
        raw["published_at"] = "2026-08-23T11:59:59.000Z"
        raw["fields"][semantic]["observed_at"] = "2026-08-23T11:59:58.000Z"
        self.primary.set_transport_ready(True)
        self.primary.ingest(self.primary.state_topic, json.dumps(raw).encode(), qos=1, retained=True)
        self.assertTrue(self.primary.field_value(semantic))
        session = self.primary.session_id
        self.update("UPDATE features SET enabled=0 WHERE channel='pilot-read' AND model_id=? "
                    "AND feature_id=?", (MODEL_ID, semantic))
        await self.runtime.async_refresh()
        self.assertIsNone(self.primary.field_value(semantic))
        self.update("UPDATE features SET enabled=1 WHERE channel='pilot-read' AND model_id=? "
                    "AND feature_id=?", (MODEL_ID, semantic))
        await self.runtime.async_refresh()
        self.assertTrue(self.primary.field_value(semantic))
        self.assertEqual(self.primary.session_id, session)

    async def test_unit_edit_does_not_reinterpret_cached_measurement(self) -> None:
        self.humidity_edit(unit="C")
        await self.runtime.async_refresh()
        self.assertIsNone(self.reads.field_value("humidity.current_pct"))
        self.assertIsNone(next(iter(self.platform.entities.values())).native_value)

    async def test_newly_enabled_field_uses_already_accepted_current(self) -> None:
        row = next(row for row in database.load_features(self.path, "full-read")
                   if row["model_id"] == MODEL_ID and row["feature_id"] == "humidity.current_pct")
        semantic = "humidity.test_readback_pct"
        self.reads.set_transport_ready(True)
        raw = json.loads(envelope(sequence=2, fields={
            "humidity.current_pct": snapshot_field(55, "number", unit="%"),
            semantic: snapshot_field(56, "number", unit="%"),
        }))
        raw["pat_device_id_proof_sha256"] = self.primary.expected_proof
        self.reads.ingest(self.reads.current_topic, json.dumps(raw).encode(), qos=1, retained=True)
        self.assertIsNone(self.reads.field_value(semantic))
        definition = dict(row["definition"], semanticId=semantic,
                          descriptorKey=f"{MODEL_ID}|{semantic}", labelKo="추가된 습도")
        database.upsert_feature(self.path, "full-read", MODEL_ID, row["profile_id"],
                                semantic, "thinq2", definition)
        coordinator = self.data.coordinators[PAT_DEVICE_ID]
        platform = MemoryPlatform()

        def build():
            contract = self.reads.profile.fields_by_semantic_id.get(semantic)
            return [] if contract is None else [TlvReadSensor(
                self.reads, coordinator, semantic, contract,
            )]

        self.runtime.register("new-test-read", platform, build, [])
        await self.runtime.async_refresh()
        self.assertEqual(next(iter(platform.entities.values())).native_value, 56)
        self.assertEqual(self.reads.current_sequence, 2)

    async def test_conditions_direct_sql_edits_apply_without_reload_or_entity_replacement(self) -> None:
        identity = next(iter(self.platform.entities.values()))
        self.update('CREATE TABLE control_conditions(model_id TEXT,capability_id TEXT,condition_json TEXT,PRIMARY KEY(model_id,capability_id))')
        policy = {'all':[{'semanticId':'operation.power_requested','values':[True]}]}
        self.update('INSERT INTO control_conditions VALUES(?,?,?)', (MODEL_ID, 'fan.mode', json.dumps(policy)))
        await self.runtime.async_refresh()
        self.assertEqual(self.data.local_control_conditions[(MODEL_ID,'fan.mode')], policy)
        self.assertIs(next(iter(self.platform.entities.values())), identity)
        self.update("UPDATE control_conditions SET condition_json='{}'")
        await self.runtime.async_refresh()
        self.assertEqual(self.data.local_control_conditions[(MODEL_ID,'fan.mode')], {})
        self.assertIs(next(iter(self.platform.entities.values())), identity)
        self.reload.assert_not_called()

    async def test_control_label_options_and_dispatch_mapping_apply_live(self) -> None:
        router = SimpleNamespace(
            control_target_available=lambda _id: True,
            feature_condition_available=lambda *_args: True,
            feature_condition_status=lambda *_args: (True, None),
            async_set_value_strict=AsyncMock(return_value=LocalCommandResult("confirmed", {})),
        )
        self.data.local_control = router
        await self.runtime.async_refresh()
        platform = MemoryPlatform()

        def build():
            return [entity for entity in local_control_entities_for_domain(self.entry, "select")
                    if entity._descriptor.capability_id == "fan.mode"]

        initial = build()
        self.assertEqual(len(initial), 1)
        self.runtime.register("select", platform, build, initial)
        await platform.async_add_entities(initial)
        original_ids = dict(platform.registry_ids)
        row = next(row for row in database.load_features(self.path, "control-entity")
                   if row["model_id"] == MODEL_ID and row["feature_id"] == "fan.mode")
        definition = dict(row["definition"], labelKo="새 풍량 이름", valueMappings=[
            dict(mapping, homeAssistantValue=("강풍" if mapping["localRequestValue"] == "high" else "약풍"))
            for mapping in row["definition"]["valueMappings"]
        ])
        self.update("UPDATE features SET definition_json=? WHERE channel='control-entity' "
                    "AND model_id=? AND feature_id='fan.mode'", (json.dumps(definition), MODEL_ID))
        await self.runtime.async_refresh()
        entity = next(iter(platform.entities.values()))
        self.assertEqual(entity.name, "새 풍량 이름")
        self.assertEqual(entity.options, ["강풍", "약풍"])
        self.assertTrue(entity.entity_registry_enabled_default)
        self.assertEqual(platform.registry_ids, original_ids)
        with patch.object(type(self.primary), "control_alive", new_callable=PropertyMock,
                          return_value=True):
            await entity.async_select_option("강풍")
        router.async_set_value_strict.assert_awaited_once_with(PAT_DEVICE_ID, "fan.mode", "high")

    async def test_disabled_control_does_not_reappear_as_native_cloud_alias(self) -> None:
        platform = MemoryPlatform()

        def build():
            entity = Entity()
            entity._attr_unique_id = f"{PAT_DEVICE_ID}_native_fan"
            entity.coordinator = self.data.coordinators[PAT_DEVICE_ID]
            entity.entity_description = SimpleNamespace(local_scalar_semantic="fan.mode")
            return [entity]

        initial = build()
        self.runtime.register("native-test", platform, build, initial)
        await platform.async_add_entities(initial)
        self.update("UPDATE features SET enabled=0 WHERE channel='control-entity' "
                    "AND model_id=? AND feature_id='fan.mode'", (MODEL_ID,))
        await self.runtime.async_refresh()
        self.assertEqual(platform.entities, {})

    async def test_unrelated_edit_preserves_integrated_energy_object_and_total(self) -> None:
        model = "CST_570004_WW"
        semantic = "power.indoor_compressor_share_w"
        profile = load_tlv_read_catalogue(self.path)[model]
        provider = SimpleNamespace(profile=profile)
        coordinator = SimpleNamespace(device_id="test-energy", alias="Test", model=model,
                                      device_type="DEVICE_AIR_CONDITIONER")

        def build():
            return [TlvIntegratedEnergySensor(provider, coordinator, semantic,
                                             profile.fields_by_semantic_id[semantic])]

        platform = MemoryPlatform()
        initial = build()
        initial[0]._energy_kwh = 12.345
        initial[0]._total_valid = True
        self.runtime.register("energy-test", platform, build, initial)
        await platform.async_add_entities(initial)
        self.humidity_edit(labelKo="다른 기능만 변경")
        await self.runtime.async_refresh()
        self.assertIs(next(iter(platform.entities.values())), initial[0])
        self.assertEqual(initial[0].native_value, 12.345)

    async def test_failed_entry_unload_keeps_live_edits_available(self) -> None:
        self.assertFalse(await self.runtime.async_unload(AsyncMock(return_value=False)))
        self.humidity_edit(labelKo="계속 변경 가능한 습도")
        await self.runtime.async_refresh()
        self.assertEqual(next(iter(self.platform.entities.values())).name,
                         "Local · 계속 변경 가능한 습도")
        self.assertTrue(await self.runtime.async_unload(AsyncMock(return_value=True)))
        with self.assertRaisesRegex(RuntimeError, "unloading"):
            await self.runtime.async_refresh()

    async def test_disabled_control_cannot_be_restored_by_extension_or_cloud_fallback(self) -> None:
        overlap = next(iter(
            {row["feature_id"] for row in database.load_features(self.path, "control-entity")
             if row["model_id"] == MODEL_ID}
            & {row["feature_id"] for row in database.load_features(self.path, "confirmed-control")
               if row["model_id"] == MODEL_ID}
        ))
        self.update("UPDATE features SET enabled=0 WHERE channel='control-entity' AND model_id=? "
                    "AND feature_id=?", (MODEL_ID, overlap))
        await self.runtime.async_refresh()
        self.assertNotIn(overlap, {descriptor.capability_id for descriptor in
                                 self.data.local_control_entity_contract.descriptors_by_model[MODEL_ID]})
        self.assertNotIn(overlap, {feature["capability_id"] for feature in load_confirmed_features(self.path)
                                 if feature["model_id"] == MODEL_ID})
        sender = SimpleNamespace(async_send=AsyncMock())
        router = LocalControlRouter(
            sender, {}, lambda _id: None,
            capability_disabled=lambda _id, capability: (MODEL_ID, capability) in self.data.local_disabled_controls,
        )
        with self.assertRaises(LocalFeatureDisabled):
            await async_native_local_control(router, PAT_DEVICE_ID, overlap, "true")
        sender.async_send.assert_not_awaited()

        blocked_power = LocalControlRouter(
            sender, {}, lambda _id: None, capability_disabled=lambda _id, _cap: True,
        )
        with self.assertRaises(LocalFeatureDisabled):
            await blocked_power.async_turn_off(PAT_DEVICE_ID)
        with self.assertRaises(LocalFeatureDisabled):
            await blocked_power.async_turn_on(PAT_DEVICE_ID, cloud_fallback=True)


if __name__ == "__main__":
    unittest.main()
