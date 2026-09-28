"""Exact new-kimchi gear owners; old ThinQ1 kimchi remains outside scope."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from homeassistant.exceptions import HomeAssistantError
from custom_components.my_lg.local_control_confirmed_features import augment_confirmed_features
from custom_components.my_lg.local_control_contract import load_local_control_entity_contract, resolve_local_control_binding_eligibility
from custom_components.my_lg.local_control_entity import MyLgApplianceSettingSelect
from custom_components.my_lg.local_command import LocalCommandClient
from tests.test_local_control_generic_entities import Coordinator, PrimaryProvider, Router
from tests.test_local_command import Response

MODEL = '3REK2G03VI230D_2'
CAP = 'kimchi.sound.door_melody'

class KimchiGearTests(unittest.IsolatedAsyncioTestCase):
    async def test_melody_select_keeps_actual_report_until_new_own_state(self):
        base = load_local_control_entity_contract()
        models = {'test_kimchi_binding': MODEL}
        contract, _ = augment_confirmed_features(base, resolve_local_control_binding_eligibility({}, base, models), models)
        descriptor = next(d for d in contract.descriptors if d.capability_id == CAP)
        router = Router(); router.async_appliance_setting_state = AsyncMock(return_value='2')
        entity = MyLgApplianceSettingSelect(Coordinator(MODEL), descriptor, router, PrimaryProvider(model=MODEL), None)
        entity.async_write_ha_state = lambda: None
        self.assertIsNone(entity.current_option)
        await entity.async_update()
        self.assertEqual(entity.current_option, '알림음 2')
        self.assertEqual(entity.options, ['기본', '알림음 1', '알림음 2', '알림음 3', '알림음 4'])
        await entity.async_select_option('알림음 3')
        self.assertEqual(entity.current_option, '알림음 2')
        self.assertEqual(router.calls[-1][1:], (CAP, '3'))
        with self.assertRaises(HomeAssistantError): await entity.async_select_option('알림음 5')
        self.assertEqual(len(router.calls), 1)
        router.async_appliance_setting_state.side_effect = TimeoutError()
        await entity.async_update(); self.assertIsNone(entity.current_option)

    async def test_readback_requires_exact_new_model_and_known_string(self):
        for model in (MODEL, '2REK1D04AR170', '2REFO1DBN3K_U'):
            for value in ('0', '1', '2', '3', '4', '5', '255', 2, True, None):
                response = Response(200, {'schema_version': 1, 'model_id': model, 'values': {CAP: value}})
                client = LocalCommandClient(SimpleNamespace(get=lambda *args, **kwargs: response))
                self.assertEqual(await client.async_appliance_setting_state('test/device', MODEL, CAP),
                                 value if model == MODEL and type(value) is str and value in ('0','1','2','3','4') else None)
