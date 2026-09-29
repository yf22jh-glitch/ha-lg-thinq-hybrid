"""Editable native gear domains remain exact local selections, never run buttons."""
import unittest
from unittest.mock import AsyncMock
from custom_components.my_lg.local_control_confirmed_features import augment_confirmed_features
from custom_components.my_lg.local_control_contract import load_local_control_entity_contract, resolve_local_control_binding_eligibility
from custom_components.my_lg.local_control_entity import MyLgApplianceSettingSelect
from tests.test_local_control_generic_entities import Coordinator, PrimaryProvider, Router

class GearTimeAndMelodyTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_ranges_and_actual_state_without_optimism(self):
        for model,cap,count,first,next_value in (
            ('ST_R_ETH01Y_','styler.smart_care.night_start_time',48,'22:00','21:30'),
            ('ST_R_ETH01Y_','styler.smart_care.night_end_time',48,'06:00','05:30'),
            ('HUM_056905_WW','hum.sound.melody',12,'0','1'),
        ):
            base=load_local_control_entity_contract();models={'test_gear_binding':model}
            contract,_=augment_confirmed_features(base,resolve_local_control_binding_eligibility({},base,models),models)
            desc=next(d for d in contract.descriptors if d.capability_id==cap)
            router=Router();router.async_appliance_setting_state=AsyncMock(return_value=first)
            entity=MyLgApplianceSettingSelect(Coordinator(model),desc,router,PrimaryProvider(model=model),None)
            entity.async_write_ha_state=lambda:None
            await entity.async_update();original=entity.current_option
            self.assertEqual(len(entity.options),count)
            choice=next(v.home_assistant_value for v in desc.value_mappings if v.local_request_value==next_value)
            await entity.async_select_option(choice)
            self.assertEqual(entity.current_option,original)
            self.assertEqual(router.calls[-1][1:],(cap,next_value))
            router.async_appliance_setting_state.side_effect=TimeoutError()
            await entity.async_update();self.assertIsNone(entity.current_option)
