"""Start draft syntax and exact binding authorization, without HA or appliance IO."""
import unittest
from custom_components.my_lg.local_styler_options import MODEL, CAPABILITY, canonical_program
from custom_components.my_lg.local_control_contract import load_local_control_entity_contract, resolve_local_control_binding_eligibility, local_control_value_authorized
from custom_components.my_lg.local_control_confirmed_features import augment_confirmed_features

class StylerOptionFormTests(unittest.TestCase):
    def test_syntax_and_exact_binding(self):
        request = 'DRY_TIME_23|on|240|60'
        self.assertEqual(canonical_program(request), request)
        for invalid in (request+'|extra', request.replace('|240|','|0240|'), request.replace('|60','|999'), request.replace('|on|','|1|')):
            with self.assertRaises(ValueError): canonical_program(invalid)
        base = load_local_control_entity_contract()
        binding = 'test_styler_option_binding'
        models = {binding:MODEL}
        extended, scope = augment_confirmed_features(base, resolve_local_control_binding_eligibility({},base,models),models)
        args = dict(binding_id=binding,model_id=MODEL,capability_id=CAPABILITY,local_request_value=request)
        self.assertTrue(local_control_value_authorized(extended,scope,**args))
        self.assertFalse(local_control_value_authorized(extended,scope,**{**args,'binding_id':'wrong'}))
