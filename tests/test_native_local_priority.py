"""Native cards must prefer exact local commands, without post-send cloud retry."""
import unittest
from unittest.mock import AsyncMock, Mock

from custom_components.my_lg.const import DEVICE_TYPE_DEHUMIDIFIER, DEVICE_TYPE_HUMIDIFIER, DEVICE_TYPE_AIR_PURIFIER, DEVICE_TYPE_WATER_PURIFIER
from custom_components.my_lg.fan import MyLgAirPurifierFan
from custom_components.my_lg.humidifier import MyLgHumidifier, _CONFIG
from custom_components.my_lg.local_command import LocalCommandFailed, LocalCommandResult
from tests.test_local_control_generic_entities import Coordinator
from custom_components.my_lg.select import MyLgSelect, SELECTS_BY_TYPE
from custom_components.my_lg.switch import MyLgSwitch, SWITCHES_BY_TYPE
from custom_components.my_lg.button import MyLgButton, MyLgButtonDescription


class NativeLocalPriorityTests(unittest.IsolatedAsyncioTestCase):
    def setup_entity(self, kind):
        coordinator = Coordinator()
        coordinator.async_control = AsyncMock()
        coordinator.handle_mqtt_status = lambda payload: self.updates.append(payload)
        self.updates = []
        router = AsyncMock()
        router.capability_authorized = Mock(return_value=True)
        router.control_target_available = Mock(return_value=True)
        router.ensure_feature_enabled = Mock(return_value=None)
        router.async_set_value.return_value = LocalCommandResult('confirmed', {})
        if kind == 'fan':
            entity = MyLgAirPurifierFan(coordinator, router)
        else:
            entity = MyLgHumidifier(coordinator, _CONFIG[kind], router)
        return entity, coordinator, router

    async def test_native_power_all_three_types_local_first(self):
        for kind in ('fan', DEVICE_TYPE_HUMIDIFIER, DEVICE_TYPE_DEHUMIDIFIER):
            entity, coordinator, router = self.setup_entity(kind)
            await entity.async_turn_on()
            await entity.async_turn_off()
            self.assertEqual([call.args[1:] for call in router.async_set_value.call_args_list],
                             [('operation.power_requested', 'true'), ('operation.power_requested', 'false')])
            coordinator.async_control.assert_not_called()
            self.assertEqual(self.updates, [])  # local ACK is not a synthetic state report

    async def test_fan_and_humidity_use_exact_semantics(self):
        entity, coordinator, router = self.setup_entity('fan')
        for value in ('LOW', 'MID', 'HIGH', 'AUTO'):
            await entity.async_set_preset_mode(value)
            self.assertEqual(router.async_set_value.call_args.args[1:], ('fan.mode', value.lower()))
        for kind in (DEVICE_TYPE_HUMIDIFIER, DEVICE_TYPE_DEHUMIDIFIER):
            entity, coordinator, router = self.setup_entity(kind)
            await entity.async_set_humidity(46)
            self.assertEqual(router.async_set_value.call_args.args[1:], ('humidity.target_pct', '45'))
            coordinator.async_control.assert_not_called()

    async def test_modes_are_not_borrowed_between_models(self):
        cases = {
            DEVICE_TYPE_DEHUMIDIFIER: {'SMART_HUMIDITY': 'smart', 'RAPID_HUMIDITY': 'jet',
                'QUIET_HUMIDITY': 'silent', 'CLOTHES_DRY': 'laundry', 'INTENSIVE_DRY': 'intensive'},
            DEVICE_TYPE_HUMIDIFIER: {'HUMIDIFY_AND_AIR_CLEAN': 'humidify+clean', 'AIR_CLEAN': 'air clean'},
        }
        for kind, modes in cases.items():
            entity, coordinator, router = self.setup_entity(kind)
            for external, semantic in modes.items():
                await entity.async_set_mode(external)
                self.assertEqual(router.async_set_value.call_args.args[1:], ('operation.mode', semantic))
            coordinator.async_control.assert_not_called()
        entity, coordinator, router = self.setup_entity(DEVICE_TYPE_HUMIDIFIER)
        await entity.async_set_mode('HUMIDIFY')
        router.async_set_value.assert_not_called()  # no invented unobserved local enum
        coordinator.async_control.assert_awaited_once()

    async def test_only_presend_unavailable_falls_back(self):
        for kind in ('fan', DEVICE_TYPE_HUMIDIFIER, DEVICE_TYPE_DEHUMIDIFIER):
            entity, coordinator, router = self.setup_entity(kind)
            router.async_set_value.return_value = None
            await entity.async_turn_off()
            coordinator.async_control.assert_awaited_once()
            entity, coordinator, router = self.setup_entity(kind)
            router.async_set_value.side_effect = LocalCommandFailed('ambiguous local delivery')
            with self.assertRaises(Exception):
                await entity.async_turn_off()
            coordinator.async_control.assert_not_called()
            self.assertEqual(self.updates, [])

    async def test_router_absent_keeps_existing_cloud_behavior(self):
        entity, coordinator, router = self.setup_entity('fan')
        entity._local_control = None
        await entity.async_turn_off()
        router.async_set_value.assert_not_called()
        coordinator.async_control.assert_awaited_once()

    async def test_native_scalar_selects_are_local_first_and_do_not_guess_missing_values(self):
        for kind in (DEVICE_TYPE_DEHUMIDIFIER, DEVICE_TYPE_HUMIDIFIER, DEVICE_TYPE_AIR_PURIFIER, DEVICE_TYPE_WATER_PURIFIER):
            for desc in SELECTS_BY_TYPE[kind]:
                if not desc.local_scalar_semantic:
                    continue
                _, coord, router = self.setup_entity('fan')
                entity = MyLgSelect(coord, desc, router)
                for external, semantic in desc.local_scalar_values.items():
                    await entity.async_select_option(external)
                    self.assertEqual(router.async_set_value.call_args.args[1:], (desc.local_scalar_semantic, semantic))
                coord.async_control.assert_not_called()
                self.assertEqual(self.updates, [])

    async def test_humidifier_boolean_cards_use_local_both_directions(self):
        for desc in SWITCHES_BY_TYPE[DEVICE_TYPE_HUMIDIFIER]:
            if not desc.local_control_semantic:
                continue
            _, coord, router = self.setup_entity('fan')
            entity = MyLgSwitch(coord, desc, router)
            await entity.async_turn_on()
            await entity.async_turn_off()
            self.assertEqual([c.args[1:] for c in router.async_set_value.call_args_list],
                             [(desc.local_control_semantic, 'true'), (desc.local_control_semantic, 'false')])
            coord.async_control.assert_not_called()
            self.assertEqual(self.updates, [])

    async def test_unconfirmed_is_not_success_or_cloud_fallback(self):
        entity, coordinator, router = self.setup_entity('fan')
        router.async_set_value.return_value = LocalCommandResult('unverifiable', {})
        with self.assertRaises(Exception):
            await entity.async_turn_off()
        coordinator.async_control.assert_not_called()

    async def test_native_availability_does_not_depend_on_cloud_but_still_requires_local_authority(self):
        for kind in ('fan', DEVICE_TYPE_HUMIDIFIER, DEVICE_TYPE_DEHUMIDIFIER):
            entity, coord, router = self.setup_entity(kind)
            self.assertFalse(coord.data)
            self.assertTrue(entity.available)
            router.control_target_available.return_value = False
            self.assertFalse(entity.available)
            router.control_target_available.return_value = True
            router.capability_authorized.return_value = False
            self.assertFalse(entity.available)
        for capability in ('washer.operation.start_or_resume', 'dryer.operation.pause', 'styler.operation.start_or_resume'):
            _, coord, router = self.setup_entity('fan')
            desc = MyLgButtonDescription(key='test_native_button', payload={}, local_capability=capability)
            button = MyLgButton(coord, desc, router)
            self.assertTrue(button.available)
            router.control_target_available.return_value = False
            self.assertFalse(button.available)
