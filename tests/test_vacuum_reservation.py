"""Exact reservation binding scope and non-optimistic Local-only HA owners."""
import unittest
from unittest.mock import AsyncMock
from homeassistant.exceptions import HomeAssistantError
from custom_components.my_lg.local_vacuum_reservation import MODEL, ENABLED, SCHEDULE, canonical_schedule, is_canonical_schedule
from custom_components.my_lg.local_control_confirmed_features import augment_confirmed_features
from custom_components.my_lg.local_control_contract import load_local_control_entity_contract, resolve_local_control_binding_eligibility, local_control_value_authorized
from custom_components.my_lg.local_control_entity import MyLgVacuumReservationSwitch, MyLgVacuumReservationText
from tests.test_local_control_generic_entities import Coordinator, PrimaryProvider, Router


def setup():
    base = load_local_control_entity_contract()
    models = {'test_vacuum_binding': MODEL}
    return augment_confirmed_features(base, resolve_local_control_binding_eligibility({}, base, models), models)


class ReservationTests(unittest.IsolatedAsyncioTestCase):
    def test_korean_form_and_exact_parameter_scope(self):
        self.assertEqual(canonical_schedule('21:30|금,월,수'), '21:30|mon,wed,fri')
        self.assertEqual(canonical_schedule('21:30|한번'), '21:30|once')
        for invalid in ['24:00|once', '12:60|once', '12:00|mon,mon', '12:00|unknown', '12:00|sun,mon', 'unset']:
            self.assertFalse(is_canonical_schedule(invalid))
        contract, scope = setup()
        query = dict(binding_id='test_vacuum_binding', model_id=MODEL, capability_id=SCHEDULE, local_request_value='21:30|mon,wed,fri')
        self.assertTrue(local_control_value_authorized(contract, scope, **query))
        for change in [dict(binding_id='other'), dict(model_id='other'), dict(capability_id=ENABLED), dict(local_request_value='24:00|once')]:
            self.assertFalse(local_control_value_authorized(contract, scope, **{**query, **change}))
        self.assertFalse(local_control_value_authorized(contract, {}, **query))

    async def test_edit_sends_only_canonical_request_and_waits_for_report(self):
        contract, _ = setup()
        descriptor = next(d for d in contract.descriptors if d.capability_id == SCHEDULE)
        router = Router()
        router.async_vacuum_reservation_state = AsyncMock(return_value={SCHEDULE: '21:52|mon,wed,fri,sun', ENABLED: False})
        entity = MyLgVacuumReservationText(Coordinator(MODEL), descriptor, router, PrimaryProvider(model=MODEL), None)
        entity.async_write_ha_state = lambda: None
        await entity.async_update()
        self.assertEqual(entity.native_value, '21:52|월,수,금,일')
        await entity.async_set_value('23:17|한번')
        self.assertEqual(router.calls[-1][1:], (SCHEDULE, '23:17|once'))
        self.assertEqual(entity.native_value, '21:52|월,수,금,일')
        router.async_vacuum_reservation_state.return_value = {SCHEDULE: '23:17|once', ENABLED: False}
        await entity.async_update()
        self.assertEqual(entity.native_value, '23:17|한번')
        with self.assertRaises(HomeAssistantError):
            await entity.async_set_value('garbage')
        self.assertEqual(len(router.calls), 1)
        router.async_vacuum_reservation_state.side_effect = TimeoutError()
        await entity.async_update()
        self.assertIsNone(entity.native_value)

    async def test_reservation_switch_does_not_alias_automatic_emptying(self):
        contract, _ = setup()
        descriptor = next(d for d in contract.descriptors if d.capability_id == ENABLED)
        router = Router()
        router.async_vacuum_reservation_state = AsyncMock(return_value={ENABLED: False, SCHEDULE: '23:17|once'})
        entity = MyLgVacuumReservationSwitch(Coordinator(MODEL), descriptor, router, PrimaryProvider(model=MODEL), None)
        entity.async_write_ha_state = lambda: None
        await entity.async_update()
        await entity.async_turn_on()
        self.assertFalse(entity.is_on)
        await entity.async_turn_off()
        self.assertEqual([c[1:] for c in router.calls], [(ENABLED, 'true'), (ENABLED, 'false')])
