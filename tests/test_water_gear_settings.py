"""New gear owners show own-connection reports, never optimistic native requests."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from homeassistant.exceptions import HomeAssistantError
from custom_components.my_lg.local_water_dnd import MODEL, WINDOW, canonical_window, is_canonical_window
from custom_components.my_lg.local_control_confirmed_features import augment_confirmed_features
from custom_components.my_lg.local_control_contract import load_local_control_entity_contract, resolve_local_control_binding_eligibility, local_control_value_authorized
from custom_components.my_lg.local_control_entity import (
    MyLgApplianceSettingSelect,
    MyLgWaterDndText,
    MyLgWaterParameterText,
)
from custom_components.my_lg.local_command import LocalCommandClient
from custom_components.my_lg.local_water_parameters import (
    CUSTOM_RECIPES,
    HOT_TEMPERATURE_PRESETS,
    PRESETS,
    STERILIZATION,
)
from tests.test_local_control_generic_entities import Coordinator, PrimaryProvider, Router
from tests.test_local_command import Response

def setup():
    base=load_local_control_entity_contract(); models={'test_water_binding':MODEL}
    return augment_confirmed_features(base,resolve_local_control_binding_eligibility({},base,models),models)

class WaterGearTests(unittest.IsolatedAsyncioTestCase):
    def test_parameter_text_lengths_cover_every_canonical_state(self):
        contract, _ = setup()
        limits = {
            PRESETS: (15, 19),
            STERILIZATION: (11, 11),
            HOT_TEMPERATURE_PRESETS: (8, 8),
            **{capability: (3, 64) for capability in CUSTOM_RECIPES},
        }
        for capability, expected in limits.items():
            descriptor = next(
                item for item in contract.descriptors if item.capability_id == capability
            )
            entity = MyLgWaterParameterText(
                Coordinator(MODEL), descriptor, Router(), PrimaryProvider(model=MODEL), None
            )
            self.assertEqual((entity.native_min, entity.native_max), expected)

    def test_window_form_and_exact_binding_scope(self):
        self.assertEqual(canonical_window('22:10-05:20'),'22:10-05:20')
        for invalid in ['22:11-05:20','23:00-23:00','00:00-12:00','25:00-06:00','9:00-10:00','IGNORE','22:00-06:00|ON']:
            self.assertFalse(is_canonical_window(invalid))
        contract,scope=setup()
        query=dict(binding_id='test_water_binding',model_id=MODEL,capability_id=WINDOW,local_request_value='22:10-05:20')
        self.assertTrue(local_control_value_authorized(contract,scope,**query))
        for patch in [dict(binding_id='other'),dict(model_id='other'),dict(local_request_value='00:00-12:00')]:
            self.assertFalse(local_control_value_authorized(contract,scope,**{**query,**patch}))
        self.assertFalse(local_control_value_authorized(contract,{},**query))

    async def test_select_and_window_wait_for_current_readback(self):
        contract,_=setup()
        for cap in ('water.sound.volume_percent','water.display.brightness_percent',WINDOW):
            descriptor=next(d for d in contract.descriptors if d.capability_id==cap)
            router=Router(); router.async_appliance_setting_state=AsyncMock(return_value=None)
            cls=MyLgWaterDndText if cap==WINDOW else MyLgApplianceSettingSelect
            entity=cls(Coordinator(MODEL),descriptor,router,PrimaryProvider(model=MODEL),None)
            entity.async_write_ha_state=lambda:None
            read=lambda:entity.native_value if cap==WINDOW else entity.current_option
            self.assertIsNone(read())
            router.async_appliance_setting_state.return_value='23:00-06:00' if cap==WINDOW else '60'
            await entity.async_update()
            if cap==WINDOW: await entity.async_set_value('22:10-05:20')
            else: await entity.async_select_option('40%')
            self.assertEqual(read(),'23:00-06:00' if cap==WINDOW else '60%')
            self.assertEqual(router.calls[-1][1:],(cap,'22:10-05:20' if cap==WINDOW else '40'))
            router.async_appliance_setting_state.side_effect=TimeoutError()
            await entity.async_update();self.assertIsNone(read())
            with self.assertRaises(HomeAssistantError):
                if cap==WINDOW: await entity.async_set_value('00:00-12:00')
                else: await entity.async_select_option('70%')
            self.assertEqual(len(router.calls),1)

    async def test_cache_reader_rejects_wrong_model_and_unoffered_values(self):
        for cap in ('water.sound.volume_percent','water.display.brightness_percent',WINDOW):
            good='22:10-05:20' if cap==WINDOW else '40'
            for model in (MODEL,'other'):
                for value in (good,'70',40,True,None,'IGNORE','00:00-12:00'):
                    response=Response(200,{'schema_version':1,'model_id':model,'values':{cap:value}})
                    client=LocalCommandClient(SimpleNamespace(get=lambda *args,**kwargs:response))
                    self.assertEqual(await client.async_appliance_setting_state('test/device',MODEL,cap),good if model==MODEL and value==good else None)
