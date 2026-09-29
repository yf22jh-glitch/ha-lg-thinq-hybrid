"""Fail-closed Local read duplicate overlay and live-cohort goldens."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from custom_components.my_lg import _async_reload_on_options, binary_sensor, sensor
from custom_components.my_lg import event as event_platform
from custom_components.my_lg.config_flow import MyLgOptionsFlow
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
    OPT_LOCAL_READ_DUPLICATE_OVERLAY,
)
from custom_components.my_lg.local_entity import (
    local_semantic_duplicate_source,
    local_semantic_unique_id,
)
from custom_components.my_lg.local_control_composite_domain import (
    load_local_control_composite_domain_contract,
)
from custom_components.my_lg.local_read_owner import (
    local_climate_promoted_semantics,
)
from custom_components.my_lg.local_provider import (
    OPT_LOCAL_BINDINGS,
    LocalSemanticShadowProvider,
    load_local_semantic_profile_catalogue,
)
from custom_components.my_lg.local_read_provider import (
    TlvReadShadowProvider,
    load_tlv_read_catalogue,
)
from custom_components.my_lg.rethink_event_relay import CONF_RETHINK_EVENT_TOKEN
import voluptuous as vol

NOW = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]

PHYSICAL_MODELS = (
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

# These are the live release's exact profile revisions.  In particular, DHUM
# and Styler use v1 here; the broader catalogue fixture also contains v2.
PILOT_PROFILE_BY_MODEL = {
    "AIR_2C0001_WW": "air-tower-core-state-v1",
    "AIR_910604_WW": "air-core-state-v1",
    "CST_170004_WW": "cst170-core-state-v1",
    "CST_570004_WW": "cst570-core-state-v1",
    "DHUM_056905_WW": "dhum-core-state-v1",
    "HUM_056905_WW": "humidifier-core-state-v1",
    "1WPD4CMIDR__3": "purifier-core-state-v1",
    "2REFO1DBN3K_U": "fridge-core-state-v1",
    "2REK1D04AR170": "kimchi-thinq1-core-state-v1",
    "3REK2G03VI230D_2": "kimchi-aabb-core-state-v1",
    "ST_R_ETH01Y_": "styler-core-state-v1",
    "WBEF3": "cooktop-left-front-state-v1",
    "WMLJ32RS": "oven-core-state-v1",
    "WTL_KPK_BDH_KR_01": "washtower-core-state-v1",
    "D121110": "dishwasher-core-state-v1",
    "HWWA9X3C_F2U": "wireless-vacuum-core-state-v1",
}

DEVICE_TYPE_BY_MODEL = {
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


class Primary:
    """Presence authority sufficient for entity construction."""

    expected_proof = "a" * 64
    binding_generation = 1
    cohort_generation = 1
    session_id = "1" * 32
    transport_ready = True
    control_alive = True

    def __init__(self, binding_id: str, model_id: str, platform: str) -> None:
        self.binding_id = binding_id
        self.model_id = model_id
        self.platform = platform
        self.listeners = []

    @property
    def read_publication_authority(self):
        return self.binding_generation, self.session_id

    def async_add_listener(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)


class Coordinator:
    """Synthetic coordinator reproducing only audited owner-creation seams."""

    def __init__(self, device_id: str, model: str, device_type: str) -> None:
        self.device_id = device_id
        self.alias = f"Overlay {model}"
        self.model = model
        self.device_type = device_type
        self.data = {"present": True}

    def async_add_listener(self, _listener):
        return lambda: None

    def supports(self, _group):
        return True

    def get(self, *path, **_kwargs):
        if self.device_type == DEVICE_TYPE_WASHTOWER:
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


def _semantic_key(entity):
    """Return a synthetic device/semantic pair for reviewed read entities."""
    semantic_id = None
    if isinstance(
        entity,
        (
            binary_sensor.TlvReadBinarySensor,
            sensor.TlvReadSensor,
            event_platform.TlvReadEventEntity,
            binary_sensor.LocalSemanticBinarySensor,
            sensor.LocalSemanticSensor,
        ),
    ):
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
        semantic_id = sensor._WIDEQ_SENSOR_SEMANTICS.get(entity.entity_description.key)
    elif isinstance(entity, binary_sensor.WaterTankFullSensor):
        semantic_id = "water_tank.full"
    if semantic_id is None:
        return None
    device_id = entity.device_info["identifiers"].copy().pop()[1]
    return device_id, semantic_id


def _domain(entity) -> str:
    if isinstance(
        entity,
        (binary_sensor.TlvReadBinarySensor, binary_sensor.LocalSemanticBinarySensor),
    ):
        return "binary_sensor"
    if isinstance(entity, event_platform.TlvReadEventEntity):
        return "event"
    return "sensor"


class LocalReadDuplicateOverlayTests(unittest.IsolatedAsyncioTestCase):
    def _runtime(self):
        read_catalogue = load_tlv_read_catalogue()
        pilot_catalogue = load_local_semantic_profile_catalogue()[1]
        coordinators = {}
        pilot_providers = {}
        read_providers = {}
        model_device_ids: dict[str, list[str]] = {}

        for index, model_id in enumerate(PHYSICAL_MODELS):
            device_id = f"overlay-device-{index:02d}"
            model_device_ids.setdefault(model_id, []).append(device_id)
            coordinators[device_id] = Coordinator(
                device_id, model_id, DEVICE_TYPE_BY_MODEL[model_id]
            )
            pilot_profile = pilot_catalogue[PILOT_PROFILE_BY_MODEL[model_id]]
            pilot_providers[device_id] = LocalSemanticShadowProvider(
                f"pilot_overlay_binding_{index:02d}",
                pilot_profile,
                now=lambda: NOW,
            )
            read_profile = read_catalogue.get(model_id)
            if read_profile is not None:
                binding_id = f"full_overlay_binding_{index:02d}"
                read_providers[device_id] = TlvReadShadowProvider(
                    binding_id,
                    device_id,
                    read_profile,
                    Primary(binding_id, model_id, read_profile.platform),
                    now=lambda: NOW,
                )

        wideq = Mock()
        wideq.data = {}
        wideq.diagnostic_attributes = {}
        wideq.snapshot_for.return_value = {}
        wideq.async_add_listener.return_value = lambda: None
        return (
            SimpleNamespace(
                wideq_coordinator=wideq,
                coordinators=coordinators,
                local_providers=pilot_providers,
                local_read_providers=read_providers,
                local_control_composite_domain_contract=(
                    load_local_control_composite_domain_contract()
                ),
            ),
            model_device_ids,
        )

    async def _entities(self, option=...):
        runtime, model_device_ids = self._runtime()
        options = {} if option is ... else {OPT_LOCAL_READ_DUPLICATE_OVERLAY: option}
        entry = SimpleNamespace(
            runtime_data=runtime,
            options=options,
            async_on_unload=lambda _remove: None,
        )
        entities = []
        await binary_sensor.async_setup_entry(None, entry, entities.extend)
        with patch.object(sensor, "RawSensorManager") as manager:
            manager.return_value.add_new.return_value = None
            await sensor.async_setup_entry(None, entry, entities.extend)
        await event_platform.async_setup_entry(None, entry, entities.extend)
        return entities, runtime, model_device_ids

    @staticmethod
    def _local_map(entities):
        local_types = (
            binary_sensor.TlvReadBinarySensor,
            sensor.TlvReadSensor,
            event_platform.TlvReadEventEntity,
            binary_sensor.LocalSemanticBinarySensor,
            sensor.LocalSemanticSensor,
        )
        result = {}
        for entity in entities:
            if not isinstance(entity, local_types):
                continue
            key = _semantic_key(entity)
            assert key is not None
            if key in result:
                raise AssertionError(f"duplicate Local read key: {key[1]}")
            result[key] = entity
        return result

    @staticmethod
    def _external_owner_map(entities):
        owner_types = (
            sensor.MyLgSensor,
            binary_sensor.MyLgBinarySensor,
            sensor.WideqDeviceSensor,
            binary_sensor.WaterTankFullSensor,
        )
        result = {}
        for entity in entities:
            if not isinstance(entity, owner_types):
                continue
            key = _semantic_key(entity)
            if key is None:
                continue
            result.setdefault(key, []).append(
                (
                    type(entity).__name__,
                    entity.unique_id,
                    entity.entity_registry_enabled_default,
                    # PAT leaves are cloud-owned only. Exact Local AC
                    # temperature/humidity leaves replace them in the factory
                    # instead of being injected into this legacy class.
                    False,
                    isinstance(entity, binary_sensor.WaterTankFullSensor)
                    and entity._local_provider is not None,
                )
            )
        return {key: tuple(sorted(values)) for key, values in result.items()}

    def _track_b_policy(self, runtime, external_owners):
        pilot = set()
        pilot_sources = {}
        contract = set()
        contract_sources = {}
        for device_id, provider in runtime.local_providers.items():
            full_provider = runtime.local_read_providers.get(device_id)
            full_semantics = (
                set(full_provider.profile.fields_by_semantic_id)
                if full_provider is not None
                else set()
            )
            promoted = local_climate_promoted_semantics(
                provider,
                runtime.coordinators[device_id].model,
                runtime.local_control_composite_domain_contract,
            )
            for semantic_id in provider.profile.fields:
                if semantic_id in promoted:
                    continue
                source = local_semantic_duplicate_source(
                    provider, semantic_id, wideq_configured=True
                )
                if source is not None and semantic_id not in full_semantics:
                    key = (device_id, semantic_id)
                    pilot.add(key)
                    pilot_sources[key] = source

        for device_id, provider in runtime.local_read_providers.items():
            for item in provider.profile.fields:
                key = (device_id, item.semantic_id)
                if (
                    key in external_owners
                    and item.owner == "none"
                    and item.enabled_by_default
                ):
                    owner_types = {row[0] for row in external_owners[key]}
                    if any(row[4] for row in external_owners[key]):
                        continue
                    contract.add(key)
                    if owner_types & {"WideqDeviceSensor", "WaterTankFullSensor"}:
                        contract_sources[key] = "wideq"
                    elif any(row[3] for row in external_owners[key]):
                        contract_sources[key] = "tlv_local_overlay"
                    else:
                        contract_sources[key] = "pat"
        return pilot, contract, {**pilot_sources, **contract_sources}

    async def test_default_off_is_fail_closed_and_owner_identity_is_unchanged(
        self,
    ) -> None:
        absent, _, _ = await self._entities()
        explicit_false, _, _ = await self._entities(False)
        malformed_truthy, _, _ = await self._entities("true")
        enabled, _, enabled_models = await self._entities(True)

        absent_local = self._local_map(absent)
        self.assertEqual(set(absent_local), set(self._local_map(explicit_false)))
        self.assertEqual(set(absent_local), set(self._local_map(malformed_truthy)))
        self.assertEqual(
            self._external_owner_map(absent), self._external_owner_map(enabled)
        )

        enabled_local = self._local_map(enabled)
        self.assertEqual(
            {key: entity.unique_id for key, entity in absent_local.items()},
            {key: enabled_local[key].unique_id for key in absent_local},
        )
        self.assertEqual(
            len({entity.unique_id for entity in enabled_local.values()}),
            len(enabled_local),
        )
        dehumidifier = enabled_models["DHUM_056905_WW"][0]
        self.assertNotIn(
            (dehumidifier, "water_tank.full"),
            enabled_local,
            "a finalized existing owner must not retain its comparison duplicate",
        )

    async def test_live_cohort_exact_track_b_and_registry_goldens(self) -> None:
        disabled_entities, disabled_runtime, model_device_ids = await self._entities()
        enabled_entities, _, _ = await self._entities(True)
        disabled_local = self._local_map(disabled_entities)
        enabled_local = self._local_map(enabled_entities)
        external = self._external_owner_map(disabled_entities)
        pilot, contract, sources = self._track_b_policy(disabled_runtime, external)
        track_b = pilot | contract

        promoted = {
            (model_device_ids["DHUM_056905_WW"][0], "water_tank.full")
        }
        promoted_climate = {
            (device_id, semantic_id)
            for model_id in ("CST_170004_WW", "CST_570004_WW")
            for device_id in model_device_ids[model_id]
            for semantic_id in (
                "temperature.target_c",
                "comfort.preference_step",
            )
        }
        canonical_ac_leaves = {
            (device_id, semantic_id)
            for model_id in ("CST_170004_WW", "CST_570004_WW")
            for device_id in model_device_ids[model_id]
            for semantic_id in (
                "temperature.current_c",
                "humidity.current_pct",
            )
        }
        self.assertEqual(len(canonical_ac_leaves), 8)
        self.assertTrue(canonical_ac_leaves <= set(disabled_local))
        self.assertFalse(canonical_ac_leaves & track_b)
        self.assertEqual(
            {type(disabled_local[key]).__name__ for key in canonical_ac_leaves},
            {"TlvReadSensor"},
        )
        self.assertEqual((len(pilot), len(contract), len(track_b)), (18, 10, 28))
        self.assertFalse(promoted & track_b)
        self.assertFalse(promoted_climate & track_b)
        self.assertEqual(
            {
                domain: sum(_domain(enabled_local[key]) == domain for key in track_b)
                for domain in ("sensor", "binary_sensor", "event")
            },
            {"sensor": 24, "binary_sensor": 4, "event": 0},
        )
        self.assertEqual(
            {
                source: sum(value == source for value in sources.values())
                for source in ("pat", "tlv_local_overlay", "wideq")
            },
            {"pat": 27, "tlv_local_overlay": 0, "wideq": 1},
        )
        self.assertEqual(
            {
                "MyLg": sum(
                    source in {"pat", "tlv_local_overlay"}
                    for source in sources.values()
                ),
                "WideQ": sum(source == "wideq" for source in sources.values()),
            },
            {"MyLg": 27, "WideQ": 1},
        )

        historical_state = {
            (
                model_device_ids["2REK1D04AR170"][0],
                "filter.one_touch_enabled",
            ),
            (
                model_device_ids["3REK2G03VI230D_2"][0],
                "filter.one_touch_enabled",
            ),
            (model_device_ids["WMLJ32RS"][0], "oven.upper.remaining_s"),
        }
        historical_ghost = {(model_device_ids["D121110"][0], "cycle.course")}
        existing_registry = historical_state | historical_ghost
        explicit_pat = {
            (device_id, item.semantic_id)
            for device_id, provider in disabled_runtime.local_read_providers.items()
            for item in provider.profile.fields
            if item.owner == "PAT"
        }
        self.assertEqual(len(explicit_pat), 4)

        self.assertEqual(track_b & set(disabled_local), historical_state)
        self.assertEqual(
            set(enabled_local) - set(disabled_local),
            (track_b - historical_state) | explicit_pat,
        )
        self.assertEqual(len(track_b - historical_state), 25)
        self.assertEqual(len(track_b - existing_registry), 24)
        self.assertEqual(len(pilot - existing_registry), 14)
        added = [enabled_local[key] for key in track_b - historical_state]
        self.assertEqual(
            {
                domain: sum(_domain(entity) == domain for entity in added)
                for domain in ("sensor", "binary_sensor", "event")
            },
            {"sensor": 23, "binary_sensor": 2, "event": 0},
        )
        self.assertEqual(
            sum(entity.entity_registry_enabled_default for entity in added), 10
        )
        self.assertEqual(
            enabled_local[next(iter(historical_ghost))].unique_id,
            local_semantic_unique_id(*next(iter(historical_ghost))),
        )

        reviewed_union = set()
        for device_id, provider in disabled_runtime.local_read_providers.items():
            for item in provider.profile.fields:
                key = (device_id, item.semantic_id)
                reviewed_union.add(key)
        for device_id, provider in disabled_runtime.local_providers.items():
            reviewed_union.update(
                (device_id, semantic_id) for semantic_id in provider.profile.fields
            )
        self.assertEqual(
            set(enabled_local),
            reviewed_union - promoted - promoted_climate,
        )

    async def test_event_and_control_surfaces_are_outside_the_option(self) -> None:
        disabled, _, _ = await self._entities()
        enabled, _, _ = await self._entities(True)
        disabled_events = {
            (_semantic_key(entity), entity.unique_id)
            for entity in disabled
            if isinstance(entity, event_platform.TlvReadEventEntity)
        }
        enabled_events = {
            (_semantic_key(entity), entity.unique_id)
            for entity in enabled
            if isinstance(entity, event_platform.TlvReadEventEntity)
        }
        self.assertEqual(disabled_events, enabled_events)
        self.assertEqual(len(enabled_events), 12)

        control_modules = (
            "button.py",
            "climate.py",
            "fan.py",
            "humidifier.py",
            "number.py",
            "select.py",
            "switch.py",
            "text.py",
            "time.py",
            "local_control_entity.py",
            "local_control_router.py",
        )
        for name in control_modules:
            source = (ROOT / "custom_components" / "my_lg" / name).read_text()
            with self.subTest(module=name):
                self.assertNotIn("OPT_LOCAL_READ_DUPLICATE_OVERLAY", source)
                self.assertNotIn("iter_local_semantic_contracts", source)
                self.assertNotIn("iter_tlv_read_contracts", source)

    async def test_official_options_flow_activates_and_rolls_back_with_reload(
        self,
    ) -> None:
        async def executor(function, *args):
            return function(*args)

        async def run_flow(existing: bool | None, submitted: bool):
            options = {OPT_LOCAL_BINDINGS: []}
            if existing is not None:
                options[OPT_LOCAL_READ_DUPLICATE_OVERLAY] = existing
            sentinel = object()
            fake_flow = SimpleNamespace(
                config_entry=SimpleNamespace(options=options),
                hass=SimpleNamespace(async_add_executor_job=executor),
                async_create_entry=Mock(return_value=sentinel),
            )
            result = await MyLgOptionsFlow.async_step_init(
                fake_flow,
                {
                    OPT_LOCAL_BINDINGS: "[]",
                    OPT_LOCAL_READ_DUPLICATE_OVERLAY: submitted,
                    CONF_RETHINK_EVENT_TOKEN: "",
                },
            )
            self.assertIs(result, sentinel)
            return fake_flow.async_create_entry.call_args.kwargs["data"]

        activated = await run_flow(None, True)
        self.assertIs(activated[OPT_LOCAL_READ_DUPLICATE_OVERLAY], True)
        rolled_back = await run_flow(True, False)
        self.assertIs(rolled_back[OPT_LOCAL_READ_DUPLICATE_OVERLAY], False)

        form_sentinel = object()
        form_flow = SimpleNamespace(
            config_entry=SimpleNamespace(options={OPT_LOCAL_BINDINGS: []}),
            hass=SimpleNamespace(async_add_executor_job=executor),
            async_show_form=Mock(return_value=form_sentinel),
        )
        self.assertIs(await MyLgOptionsFlow.async_step_init(form_flow), form_sentinel)
        schema = form_flow.async_show_form.call_args.kwargs["data_schema"]
        defaults = schema({OPT_LOCAL_BINDINGS: "[]"})
        self.assertIs(defaults[OPT_LOCAL_READ_DUPLICATE_OVERLAY], False)
        with self.assertRaises(vol.Invalid):
            schema(
                {
                    OPT_LOCAL_BINDINGS: "[]",
                    "schema_outside_installer_key": True,
                }
            )

        hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_reload=AsyncMock(return_value=True))
        )
        entry = SimpleNamespace(entry_id="synthetic-overlay-entry", options=activated)
        await _async_reload_on_options(hass, entry)
        entry.options = rolled_back
        await _async_reload_on_options(hass, entry)
        self.assertEqual(hass.config_entries.async_reload.await_count, 2)
        hass.config_entries.async_reload.assert_awaited_with(entry.entry_id)

        for relative in (
            "custom_components/my_lg/strings.json",
            "custom_components/my_lg/translations/en.json",
            "custom_components/my_lg/translations/ko.json",
        ):
            translated = json.loads((ROOT / relative).read_text())
            self.assertIn(
                OPT_LOCAL_READ_DUPLICATE_OVERLAY,
                translated["options"]["step"]["init"]["data"],
            )


if __name__ == "__main__":
    unittest.main()
