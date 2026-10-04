"""Account protocol tests use synthetic identities and never contact LG."""
import importlib.util
from pathlib import Path
from copy import deepcopy
import unittest

spec = importlib.util.spec_from_file_location('web_protocol', Path(__file__).resolve().parents[1] / 'custom_components/my_lg/wideq/web_settings.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.saved = dict(recommandUseYn='N', sendTime='17', repeatWeek='7',
                          todayAlarmYn='Y', beforeAlarmYn='N', afterAlarmYn='N')
        self.rows = [dict(deviceId='washer-one', alias='Washer', ownerYn='Y', pairingYn='Y'),
                     dict(deviceId='washer-two', alias='Washer', ownerYn='Y', pairingYn='O')]
        self.writes = []
        self.confirm = True

    async def request(self, method, path, body=None):
        if method == 'GET':
            return dict(item=deepcopy(self.rows)) if 'available-pairing' in path else deepcopy(self.saved)
        self.writes.append((method, path, deepcopy(body)))
        if not self.confirm: return {}
        if 'pairing-devices/' in path:
            for row in self.rows:
                if row['deviceId'] == body['pairingDeviceId']: row['pairingYn'] = 'Y' if method == 'POST' else 'N'
        else:
            self.saved['recommandUseYn'] = body['recommandUseYn']
        return {}

    async def test_recommendation_is_not_notification_switch(self):
        before = deepcopy(self.saved)
        for model in p.FOOD_MODELS:
            self.saved = deepcopy(before)
            self.assertEqual(await p.read_food(self.request,model,'test-device'),'off')
            self.assertEqual(await p.set_food(self.request,model,'test-device','on'),'on')
            self.assertEqual(self.writes[-1],('PUT','service/fridge/test-device/stored-food/keep-recommand-use',{'recommandUseYn':'Y','deviceId':'test-device'}))
            self.assertEqual(self.saved,{**before,'recommandUseYn':'Y'})
            self.assertEqual(await p.set_food(self.request,model,'test-device','off'),'off')
            self.assertEqual(self.saved,before)

    async def test_invalid_inputs_noop_and_no_automatic_retry(self):
        with self.assertRaises(ValueError): await p.set_food(self.request,'2REFO1DBN3K_U','test-device','true')
        await p.set_food(self.request,'2REFO1DBN3K_U','test-device','off')
        self.assertEqual(self.writes,[])
        self.confirm=False
        with self.assertRaisesRegex(ValueError,'not confirmed'):
            await p.set_food(self.request,'2REFO1DBN3K_U','test-device','on')
        self.assertEqual(len(self.writes),1)

    async def test_missing_state_is_not_off(self):
        self.saved['recommandUseYn']='D'
        self.assertEqual(await p.read_food(self.request,'3REK2G03VI230D_2','test-device'),'off')
        self.saved.pop('recommandUseYn')
        with self.assertRaises(ValueError): await p.read_food(self.request,'2REFO1DBN3K_U','test-device')
        with self.assertRaises(ValueError): await p.read_food(self.request,'UNKNOWN','test-device')

    async def test_pairing_requires_explicit_disconnect_and_never_steals_other_pair(self):
        with self.assertRaises(ValueError): await p.set_pairing(self.request,'test-device','washer-two','washer-one')
        self.assertEqual(self.writes,[])
        await p.set_pairing(self.request,'test-device',None,'washer-one')
        self.assertEqual(self.writes[0][0],'DELETE')
        self.assertIn('/pairing-devices/',self.writes[0][1])
        with self.assertRaises(ValueError): await p.set_pairing(self.request,'test-device','washer-two',None)
        await p.set_pairing(self.request,'test-device','washer-one',None)
        self.assertEqual(self.writes[-1][0],'POST')

    async def test_pairing_checks_fresh_state_and_requires_confirmation(self):
        with self.assertRaises(ValueError): await p.set_pairing(self.request,'test-device',None,None)
        self.confirm=False
        with self.assertRaisesRegex(ValueError,'not confirmed'): await p.set_pairing(self.request,'test-device',None,'washer-one')
        self.assertEqual(len(self.writes),1)

    def test_pairing_conditions_match_web_and_are_data_driven(self):
        rule={'states':['INITIAL'],'required':{'online':True,'child_lock':'CHILDLOCK_OFF','error':'ERROR_NO'},
              'power_off_state':'POWEROFF','power_off_support_key':'settingFuncEnableInPowerOff','power_off_content':'203-3'}
        context=dict(online=True,child_lock='CHILDLOCK_OFF',error='ERROR_NO',state='INITIAL',config={},activated=[])
        self.assertTrue(p.pairing_allowed(context,rule))
        for key,value in [('online',False),('child_lock','CHILDLOCK_ON'),('error','ERROR_OTHER'),('state','RUNNING')]:
            self.assertFalse(p.pairing_allowed({**context,key:value},rule))
        context['state']='POWEROFF'
        self.assertFalse(p.pairing_allowed(context,rule))
        self.assertTrue(p.pairing_allowed({**context,'config':{'settingFuncEnableInPowerOff':True}},rule))
        self.assertTrue(p.pairing_allowed({**context,'activated':['203-3']},rule))
        self.assertFalse(p.pairing_allowed({},rule))
        self.assertFalse(p.pairing_allowed(context,{}))


if __name__=='__main__': unittest.main()
