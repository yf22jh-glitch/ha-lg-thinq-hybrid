import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from tests.test_local_control_generic_entities import Coordinator, PrimaryProvider, Router, BINDING_ID
from tests.test_local_command import Response
from custom_components.my_lg.local_command import LocalCommandClient
from custom_components.my_lg.local_control_confirmed_features import augment_confirmed_features, load_confirmed_features
from custom_components.my_lg.local_control_contract import load_local_control_entity_contract, resolve_local_control_binding_eligibility, local_control_value_authorized
from custom_components.my_lg.local_control_entity import MyLgApplianceSettingSelect, local_control_entities_for_domain

MODEL='WTL_KPK_BDH_KR_01'
CAP='washer.sound.volume_level'


class WasherVolumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_factory_exact_scope_and_own_value_polling(self):
        base=load_local_control_entity_contract()
        models={BINDING_ID:MODEL}
        contract,scope=augment_confirmed_features(base,resolve_local_control_binding_eligibility({},base,models),models)
        feature=next(f for f in load_confirmed_features() if f['capability_id']==CAP)
        self.assertEqual(feature['wire_evidence'],'own-model-observed-command-not-accepted')
        self.assertEqual(contract.root_sha256,base.root_sha256)
        coordinator=Coordinator(MODEL)
        primary=PrimaryProvider(model=MODEL)
        router=Router()
        router.async_appliance_setting_state=AsyncMock(return_value='4')
        data=SimpleNamespace(local_control_entity_contract=contract,local_control=router,
            local_control_binding_eligibility=scope,coordinators={'test':coordinator},
            local_providers={coordinator.device_id:primary},local_read_providers={})
        entities=local_control_entities_for_domain(SimpleNamespace(runtime_data=data),'select')
        selected=[e for e in entities if e._descriptor.capability_id==CAP]
        self.assertEqual(len(selected),1)
        entity=selected[0]
        self.assertIsInstance(entity,MyLgApplianceSettingSelect)
        entity.async_write_ha_state=lambda:None
        self.assertEqual(entity.options,['끄기','1단계','2단계','3단계','4단계'])
        await entity.async_update()
        self.assertEqual(entity.current_option,'4단계')
        await entity.async_select_option('3단계')
        self.assertEqual(router.calls[-1][1:],(CAP,'3'))
        self.assertEqual(entity.current_option,'4단계')
        for value in ('0','1','2','3','4','5','03'):
            self.assertEqual(local_control_value_authorized(contract,scope,binding_id=BINDING_ID,
                model_id=MODEL,capability_id=CAP,local_request_value=value),value in ('0','1','2','3','4'))
        self.assertFalse(any(d.capability_id=='dryer.sound.volume_level' for d in contract.descriptors))
        router.async_appliance_setting_state.side_effect=TimeoutError()
        await entity.async_update()
        self.assertIsNone(entity.current_option)

    async def test_state_endpoint_requires_exact_model_and_canonical_level(self):
        for model in (MODEL,'other'):
            for value in ('0','4','5','04',4,True,None):
                response=Response(200,{'schema_version':1,'model_id':model,'values':{CAP:value}})
                client=LocalCommandClient(SimpleNamespace(get=lambda *a,**k:response))
                self.assertEqual(await client.async_appliance_setting_state('test-device',CAP),
                    value if model==MODEL and value in ('0','4') else None)
