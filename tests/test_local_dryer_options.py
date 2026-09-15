import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from homeassistant.exceptions import HomeAssistantError
from tests.test_local_control_generic_entities import Coordinator, PrimaryProvider, Router
from tests.test_local_command import Response
from custom_components.my_lg.local_command import LocalCommandClient
from custom_components.my_lg.local_dryer_options import CAPABILITY, MODEL, SCHEMA, canonical_program, is_canonical_program
from custom_components.my_lg.local_control_confirmed_features import augment_confirmed_features, APPLIANCE_VALUE_MODELS
from custom_components.my_lg.local_control_contract import load_local_control_entity_contract, resolve_local_control_binding_eligibility, local_control_value_authorized, eligible_factory_descriptors
from custom_components.my_lg.local_control_entity import MyLgDryerOptionProgramText

VALUE = 'replace|TIMEDRY|NO_DRYLEVEL|ECOHYBRID_NORMAL|STEAM_OFF|240|210'


class DryerOptionsTests(unittest.TestCase):
    def test_explicit_replacement_form_and_reservation_grid(self):
        self.assertEqual(canonical_program(VALUE), VALUE)
        for bad in (None, VALUE.removeprefix('replace|'), VALUE+'|extra', VALUE.replace('|210','|0210'),
                    VALUE.replace('|210','|1141'), VALUE.replace('|240|','|70|'), VALUE.upper()):
            self.assertFalse(is_canonical_program(bad))
        legal = [n for n in range(1142) if is_canonical_program(VALUE.rsplit('|',1)[0]+'|'+str(n))]
        self.assertEqual(legal, [0, *range(180,1141,30)])
        self.assertEqual(MyLgDryerOptionProgramText._canonical(None, VALUE), VALUE)
        with self.assertRaisesRegex(ValueError, '구김방지·물기알림'):
            MyLgDryerOptionProgramText._canonical(None, VALUE.removeprefix('replace|'))

    def test_additive_scoped_owner_carries_reset_notice_without_replacing_old_select(self):
        base = load_local_control_entity_contract()
        models = {'test_dryer_options': MODEL}
        prior = resolve_local_control_binding_eligibility({},base,models)
        extended, scope = augment_confirmed_features(base,prior,models)
        owners = eligible_factory_descriptors(extended,scope,binding_id='test_dryer_options',model_id=MODEL,domain='text')
        owner = next(d for d in owners if d.capability_id == CAPABILITY)
        self.assertEqual(owner.parameter_schema, SCHEMA)
        self.assertIn('구김방지·물기알림 초기화 가능', owner.label_ko)
        self.assertIn('시작 아님', owner.label_ko)
        self.assertEqual(APPLIANCE_VALUE_MODELS[CAPABILITY], MODEL)
        self.assertEqual(len(next(d for d in extended.descriptors if d.capability_id == 'dryer.course_program').value_mappings),24)
        self.assertEqual(extended.root_sha256,base.root_sha256)
        self.assertTrue(local_control_value_authorized(extended,scope,binding_id='test_dryer_options',model_id=MODEL,capability_id=CAPABILITY,local_request_value=VALUE))
        for binding,value in [('missing',VALUE),('test_dryer_options',VALUE.removeprefix('replace|'))]:
            self.assertFalse(local_control_value_authorized(extended,scope,binding_id=binding,model_id=MODEL,capability_id=CAPABILITY,local_request_value=value))


class DryerEntityTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_reports_only_own_current_and_never_optimistic_request(self):
        base = load_local_control_entity_contract()
        models = {'test_dryer_options': MODEL}
        contract, _ = augment_confirmed_features(base,resolve_local_control_binding_eligibility({},base,models),models)
        descriptor = next(d for d in contract.descriptors if d.capability_id == CAPABILITY)
        router = Router()
        old = VALUE.replace('|240|','|30|')
        router.async_appliance_setting_state = AsyncMock(return_value=old)
        entity = MyLgDryerOptionProgramText(Coordinator(MODEL),descriptor,router,PrimaryProvider(model=MODEL),None)
        entity.async_write_ha_state = lambda:None
        self.assertIsNone(entity.native_value)
        await entity.async_update()
        await entity.async_set_value(VALUE)
        self.assertEqual(entity.native_value,old)
        self.assertEqual(router.calls[-1][1:],(CAPABILITY,VALUE))
        with self.assertRaisesRegex(HomeAssistantError,'구김방지·물기알림'):
            await entity.async_set_value(VALUE.removeprefix('replace|'))
        self.assertEqual(len(router.calls),1)
        router.async_appliance_setting_state.side_effect = TimeoutError()
        await entity.async_update()
        self.assertIsNone(entity.native_value)

    async def test_state_endpoint_rejects_wrong_model_or_noncanonical_value(self):
        for model in (MODEL,'other'):
            for value in (VALUE,VALUE.removeprefix('replace|'),None,True,'wrong'):
                response = Response(200,{'schema_version':1,'model_id':model,'values':{CAPABILITY:value}})
                client = LocalCommandClient(SimpleNamespace(get=lambda *a,**k:response))
                self.assertEqual(await client.async_appliance_setting_state('test/device',CAPABILITY),
                                 VALUE if model == MODEL and value == VALUE else None)
