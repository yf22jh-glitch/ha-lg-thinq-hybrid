"""Retained app writes become two non-optimistic local switches, not HUM enums."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from custom_components.my_lg.local_command import LocalCommandClient
from custom_components.my_lg.local_control_confirmed_features import augment_confirmed_features
from custom_components.my_lg.local_control_contract import load_local_control_entity_contract, resolve_local_control_binding_eligibility
from custom_components.my_lg.local_control_entity import MyLgAirExtraSwitch, MyLgAirExtraLegacyJetSwitch, MyLgAirExtraLegacyUvSwitch, MyLgTowerLegacyUvSwitch
from tests.test_local_control_generic_entities import Coordinator, PrimaryProvider, Router

MODEL = 'AIR_910604_WW'
CAPS = ('clean_dry.enabled', 'rapid_operation.enabled')

class AirExtraTests(unittest.IsolatedAsyncioTestCase):
    async def test_rapid_setting_can_keep_the_old_jet_switch_unique_id(self):
        base = load_local_control_entity_contract()
        models = {'test_air_binding': MODEL}
        prior = resolve_local_control_binding_eligibility({}, base, models)
        contract, _ = augment_confirmed_features(base, prior, models)
        descriptor = next(d for d in contract.descriptors if d.capability_id == 'rapid_operation.enabled')
        coordinator = Coordinator(MODEL)
        entity = MyLgAirExtraLegacyJetSwitch(
            coordinator, descriptor, Router(), PrimaryProvider(model=MODEL), None,
        )
        self.assertEqual(entity.unique_id, f'{coordinator.device_id}_jet_mode')

    async def test_hygienic_dry_can_keep_the_old_uv_switch_unique_id(self):
        base = load_local_control_entity_contract()
        models = {'test_air_binding': MODEL}
        prior = resolve_local_control_binding_eligibility({}, base, models)
        contract, _ = augment_confirmed_features(base, prior, models)
        descriptor = next(d for d in contract.descriptors if d.capability_id == 'clean_dry.enabled')
        coordinator = Coordinator(MODEL)
        entity = MyLgAirExtraLegacyUvSwitch(
            coordinator, descriptor, Router(), PrimaryProvider(model=MODEL), None,
        )
        self.assertEqual(entity.unique_id, f'{coordinator.device_id}_uv_disinfection')
        self.assertEqual(entity.name, '위생 건조')
        with self.assertRaises(ValueError):
            MyLgAirExtraLegacyUvSwitch(
                coordinator,
                next(d for d in contract.descriptors if d.capability_id == 'rapid_operation.enabled'),
                Router(), PrimaryProvider(model=MODEL), None,
            )

    async def test_tower_uvnano_can_keep_the_old_uv_switch_unique_id(self):
        tower_model = 'AIR_2C0001_WW'
        base = load_local_control_entity_contract()
        descriptor = next(
            d for d in base.descriptors
            if d.model_id == tower_model and d.capability_id == 'sterilization.uvnano_enabled'
        )
        coordinator = Coordinator(tower_model)
        entity = MyLgTowerLegacyUvSwitch(
            coordinator, descriptor, Router(), PrimaryProvider(model=tower_model), None,
        )
        self.assertEqual(entity.unique_id, f'{coordinator.device_id}_uv_disinfection')
        self.assertEqual(entity._attr_name, 'UVnano 공기살균')
        with self.assertRaises(ValueError):
            MyLgTowerLegacyUvSwitch(
                Coordinator(MODEL),
                next(d for d in base.descriptors if d.model_id == MODEL and d.capability_id == 'sterilization.air_enabled'),
                Router(), PrimaryProvider(model=MODEL), None,
            )

    async def test_both_switches_use_local_send_and_own_report_display(self):
        base = load_local_control_entity_contract()
        models = {'test_air_binding': MODEL}
        prior = resolve_local_control_binding_eligibility({}, base, models)
        contract, _ = augment_confirmed_features(base, prior, models)
        for cap in CAPS:
            descriptor = next(d for d in contract.descriptors if d.capability_id == cap)
            self.assertFalse(descriptor.one_shot)
            router = Router(); router.async_air_extra_state = AsyncMock(return_value=None)
            entity = MyLgAirExtraSwitch(Coordinator(MODEL), descriptor, router, PrimaryProvider(model=MODEL), None)
            entity.async_write_ha_state = lambda: None
            self.assertIsNone(entity.is_on)
            self.assertTrue(entity.should_poll)
            for enabled in (False, True):
                router.async_air_extra_state.return_value = enabled
                await entity.async_update(); self.assertIs(entity.is_on, enabled)
                if enabled: await entity.async_turn_off()
                else: await entity.async_turn_on()
                self.assertIs(entity.is_on, enabled)
            self.assertEqual([c[1:] for c in router.calls], [(cap, 'true'), (cap, 'false')])
            router.async_air_extra_state.side_effect = TimeoutError()
            await entity.async_update(); self.assertIsNone(entity.is_on)

    async def test_client_exact_boolean_model_and_capability(self):
        from tests.test_local_command import Response
        calls = []
        for cap in CAPS:
            for value in (False, True, None, 0, 1, 'false'):
                response = Response(200, {'schema_version': 1, 'model_id': MODEL, 'values': {cap: value}})
                def get(url, **kwargs):
                    calls.append((url, kwargs)); return response
                client = LocalCommandClient(SimpleNamespace(get=get))
                self.assertIs(await client.async_air_extra_state('test/device', cap), value if type(value) is bool else None)
                self.assertIsNone(await client.async_air_extra_state('test/device', 'unknown'))
        self.assertTrue(all('/test%2Fdevice/air-extra-state' in url and not opts['allow_redirects'] for url, opts in calls))
