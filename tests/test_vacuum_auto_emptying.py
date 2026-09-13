"""Automatic emptying is a two-way setting, not the one-shot empty button."""
import unittest
from types import SimpleNamespace
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
        values = {}
        router = Router()
        entity = MyLgVacuumAutoEmptyingSwitch(Coordinator(MODEL), descriptor, router,
            PrimaryProvider(model=MODEL), None, wideq=SimpleNamespace(snapshot_for=lambda _: values))
        self.assertIsNone(entity.is_on)
        values['qmState.dustEmptyingMode'] = 'MANUAL'
        self.assertFalse(entity.is_on)
        await entity.async_turn_on()
        self.assertFalse(entity.is_on)
        values['qmState.dustEmptyingMode'] = 'AUTO'
        self.assertTrue(entity.is_on)
        await entity.async_turn_off()
        values['qmState.dustEmptyingMode'] = 'MANUAL'
        self.assertFalse(entity.is_on)
        values['qmState.dustEmptyingMode'] = 'UNKNOWN'
        self.assertIsNone(entity.is_on)
        self.assertEqual([c[1:] for c in router.calls], [(CAP,'true'),(CAP,'false')])
        self.assertEqual(router.methods, ['set_value','set_value'])
