"""Retained app writes become two non-optimistic local switches, not HUM enums."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from custom_components.my_lg.local_command import LocalCommandClient
from custom_components.my_lg.local_control_confirmed_features import augment_confirmed_features
from custom_components.my_lg.local_control_contract import load_local_control_entity_contract, resolve_local_control_binding_eligibility
from custom_components.my_lg.local_control_entity import MyLgAirExtraSwitch
from tests.test_local_control_generic_entities import Coordinator, PrimaryProvider, Router

MODEL = 'AIR_910604_WW'
CAPS = ('clean_dry.enabled', 'rapid_operation.enabled')

class AirExtraTests(unittest.IsolatedAsyncioTestCase):
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
