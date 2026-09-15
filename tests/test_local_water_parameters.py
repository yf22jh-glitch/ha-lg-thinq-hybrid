import unittest
from custom_components.my_lg.local_water_parameters import PRESETS, STERILIZATION, canonical_parameter, is_canonical_parameter
from custom_components.my_lg.local_control_contract import load_local_control_entity_contract, resolve_local_control_binding_eligibility, local_control_value_authorized, eligible_factory_descriptors
from custom_components.my_lg.local_control_confirmed_features import augment_confirmed_features

class WaterParameterTests(unittest.TestCase):
    def test_exact_forms_and_selected_binding(self):
        base = load_local_control_entity_contract()
        models = {'test_water_binding': '1WPD4CMIDR__3'}
        eligibility = resolve_local_control_binding_eligibility({},base,models)
        contract,scope = augment_confirmed_features(base,eligibility,models)
        for cap,value in [(PRESETS,'120,250,500,1000'),(STERILIZATION,'08-22 18:30')]:
            self.assertEqual(canonical_parameter(cap,value),value)
            self.assertTrue(local_control_value_authorized(contract,scope,binding_id='test_water_binding',model_id=models['test_water_binding'],capability_id=cap,local_request_value=value))
            self.assertFalse(local_control_value_authorized(contract,scope,binding_id='test_other_binding',model_id=models['test_water_binding'],capability_id=cap,local_request_value=value))
            self.assertTrue(any(d.capability_id==cap for d in eligible_factory_descriptors(contract,scope,binding_id='test_water_binding',model_id=models['test_water_binding'],domain='text')))
        for cap,values in [(PRESETS,['120,250,500','0120,250,500,1000','120,250,500,1001']),
                           (STERILIZATION,['00-00 18:30','02-30 18:30','08-22 24:00','08-22 18:30|start'])]:
            for value in values:
                self.assertFalse(is_canonical_parameter(cap,value))
