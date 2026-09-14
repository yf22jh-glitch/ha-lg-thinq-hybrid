"""Styler sound and display selectors use the exact live local settings cache."""
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

MODEL = 'ST_R_ETH01Y_'
SETTINGS = {'styler.sound.volume_level': 5, 'styler.sound.melody': 17, 'styler.display.startup_image': 12}

class StylerGearValuesTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_three_owners_use_reported_values_without_optimistic_selection(self):
        base = load_local_control_entity_contract()
        models = {'test_styler_binding': MODEL}
        contract, _ = augment_confirmed_features(base, resolve_local_control_binding_eligibility({}, base, models), models)
        for cap, count in SETTINGS.items():
            descriptor = next(d for d in contract.descriptors if d.capability_id == cap)
            router = Router(); router.async_appliance_setting_state = AsyncMock(return_value='0')
            entity = MyLgApplianceSettingSelect(Coordinator(MODEL), descriptor, router, PrimaryProvider(model=MODEL), None)
            entity.async_write_ha_state = lambda: None
            self.assertIsNone(entity.current_option)
            await entity.async_update()
            self.assertEqual(len(entity.options), count)
            self.assertEqual(entity.current_option, entity.options[0])
            await entity.async_select_option(entity.options[-1])
            self.assertEqual(router.calls[-1][1:], (cap, str(count - 1)))
            self.assertEqual(entity.current_option, entity.options[0])
            with self.assertRaises(HomeAssistantError): await entity.async_select_option('unknown-option')
            self.assertEqual(len(router.calls), 1)
            router.async_appliance_setting_state.side_effect = TimeoutError()
            await entity.async_update(); self.assertIsNone(entity.current_option)

    async def test_reader_rejects_wrong_model_and_unoffered_values(self):
        for cap, count in SETTINGS.items():
            for model in (MODEL, 'WTL_KPK_BDH_KR_01'):
                for value in ('0', str(count - 1), str(count), '255', 0, True, None):
                    response = Response(200, {'schema_version': 1, 'model_id': model, 'values': {cap: value}})
                    client = LocalCommandClient(SimpleNamespace(get=lambda *args, **kwargs: response))
                    self.assertEqual(await client.async_appliance_setting_state('test/device', cap),
                                     value if model == MODEL and type(value) is str and value in tuple(str(i) for i in range(count)) else None)
