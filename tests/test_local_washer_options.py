import unittest
from custom_components.my_lg.local_washer_options import CAPABILITY, MODEL, canonical_program, is_canonical_program
from custom_components.my_lg.local_control_confirmed_features import augment_confirmed_features
from custom_components.my_lg.local_control_contract import load_local_control_entity_contract, resolve_local_control_binding_eligibility, local_control_value_authorized, eligible_factory_descriptors

VALUE = 'AI_COURSE|SOILWASH_NORMAL|TEMP_40|RINSE_2|SPIN_1000|EZCSDT_NORMAL|EZCSSO_NORMAL|TURBOWASH_ON|CREASECARE_OFF|0'

class WasherOptionsTests(unittest.TestCase):
    def test_form_and_exact_binding_text_owner_preserve_recorded_program_select(self):
        self.assertEqual(canonical_program(VALUE), VALUE)
        legal = [m for m in range(1141) if is_canonical_program(VALUE[:-1]+str(m))]
        self.assertEqual(legal, [0, *range(180, 1141, 30)])
        for bad in (None, VALUE+'|extra', VALUE[:-1]+'01', VALUE[:-1]+'1141', VALUE.lower()):
            self.assertFalse(is_canonical_program(bad))
        base = load_local_control_entity_contract()
        models = {'test_washer_options': MODEL}
        eligibility = resolve_local_control_binding_eligibility({}, base, models)
        extended, scope = augment_confirmed_features(base, eligibility, models)
        owners = eligible_factory_descriptors(extended, scope, binding_id='test_washer_options', model_id=MODEL, domain='text')
        self.assertEqual([d.capability_id for d in owners if d.capability_id.startswith('washer.')], [CAPABILITY])
        self.assertEqual(len(next(d for d in extended.descriptors if d.capability_id == 'washer.course_program').value_mappings),45)
        self.assertTrue(local_control_value_authorized(extended,scope,binding_id='test_washer_options',model_id=MODEL,capability_id=CAPABILITY,local_request_value=VALUE))
        self.assertFalse(local_control_value_authorized(extended,scope,binding_id='missing',model_id=MODEL,capability_id=CAPABILITY,local_request_value=VALUE))
