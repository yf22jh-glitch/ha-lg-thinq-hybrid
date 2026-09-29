"""Automatic emptying is a two-way setting, not the one-shot empty button."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from custom_components.my_lg.local_command import LocalCommandClient
from custom_components.my_lg.local_control_confirmed_features import augment_confirmed_features
from custom_components.my_lg.local_control_contract import load_local_control_entity_contract, resolve_local_control_binding_eligibility
from custom_components.my_lg.local_control_entity import MyLgVacuumAutoEmptyingSwitch
from tests.test_local_control_generic_entities import Coordinator, PrimaryProvider, Router

MODEL = 'HWWA9X3C_F2U'
CAP = 'vacuum.auto_dust_emptying_enabled'

class AutoEmptyingTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_local_polarities_and_non_optimistic_reported_state(self):
        base = load_local_control_entity_contract()
        models = {'test_vacuum_binding': MODEL}
        prior = resolve_local_control_binding_eligibility({}, base, models)
        contract, _ = augment_confirmed_features(base, prior, models)
        descriptor = next(d for d in contract.descriptors if d.capability_id == CAP)
        self.assertEqual(descriptor.entity_domain, 'switch')
        self.assertFalse(descriptor.one_shot)
        router = Router()
        router.async_vacuum_auto_emptying_state = AsyncMock(return_value=None)
        entity = MyLgVacuumAutoEmptyingSwitch(Coordinator(MODEL), descriptor, router,
            PrimaryProvider(model=MODEL), None)
        entity.async_write_ha_state = lambda: None
        self.assertIsNone(entity.is_on)
        self.assertTrue(entity.should_poll)
        router.async_vacuum_auto_emptying_state.return_value = False
        await entity.async_update()
        self.assertFalse(entity.is_on)
        await entity.async_turn_on()
        self.assertFalse(entity.is_on)
        router.async_vacuum_auto_emptying_state.return_value = True
        await entity.async_update()
        self.assertTrue(entity.is_on)
        await entity.async_turn_off()
        router.async_vacuum_auto_emptying_state.return_value = False
        await entity.async_update()
        self.assertFalse(entity.is_on)
        router.async_vacuum_auto_emptying_state.return_value = None
        await entity.async_update()
        self.assertIsNone(entity.is_on)
        router.async_vacuum_auto_emptying_state.side_effect = TimeoutError()
        await entity.async_update()
        self.assertIsNone(entity.is_on)
        self.assertEqual([c[1:] for c in router.calls], [(CAP,'true'),(CAP,'false')])
        self.assertEqual(router.methods, ['set_value_strict','set_value_strict'])

    async def test_display_client_only_gets_exact_local_boolean(self):
        from tests.test_local_command import Response
        calls = []
        for enabled in [False, True, None, 0, 1, 'false']:
            response = Response(200, {'schema_version':1, 'model_id':MODEL, 'enabled':enabled})
            def get(url, **kwargs):
                calls.append((url, kwargs))
                return response
            client = LocalCommandClient(SimpleNamespace(get=get))
            self.assertIs(await client.async_vacuum_auto_emptying_state('test/device'), enabled if type(enabled) is bool else None)
        self.assertTrue(all('/test%2Fdevice/vacuum-auto-emptying-state' in url and not options['allow_redirects'] for url, options in calls))
