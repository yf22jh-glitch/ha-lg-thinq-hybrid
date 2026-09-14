"""Additive known commands reuse owners and preserve old authority/scopes."""
import unittest
from custom_components.my_lg.local_control_contract import (
    load_local_control_entity_contract, resolve_local_control_binding_eligibility,
    local_control_value_authorized, eligible_factory_descriptors,
)
from custom_components.my_lg.local_control_confirmed_features import augment_confirmed_features, load_confirmed_features


class ConfirmedFeaturesTests(unittest.TestCase):
    def test_exact_catalogue_and_parent_preservation(self):
        base = load_local_control_entity_contract()
        models = {'test_washtower_binding': 'WTL_KPK_BDH_KR_01', 'test_styler_binding': 'ST_R_ETH01Y_',
                  'test_water_binding': '1WPD4CMIDR__3', 'test_ac_binding_01': 'CST_170004_WW', 'test_vacuum_binding': 'HWWA9X3C_F2U'}
        models['test_air_binding'] = 'AIR_910604_WW'
        models['test_kimchi_binding'] = '3REK2G03VI230D_2'
        prior = resolve_local_control_binding_eligibility({}, base, models)
        extended, scope = augment_confirmed_features(base, prior, models)
        self.assertEqual(extended.root_sha256, base.root_sha256)
        self.assertEqual(extended.descriptors_by_model['CST_170004_WW'], base.descriptors_by_model['CST_170004_WW'])
        self.assertEqual(scope['test_ac_binding_01'], prior['test_ac_binding_01'])
        self.assertEqual(sum(len(f['values']) for f in load_confirmed_features()), 177)
        for feature in load_confirmed_features():
            binding = next(b for b, m in models.items() if m == feature['model_id'])
            for value in feature['values']:
                self.assertTrue(local_control_value_authorized(extended, scope, binding_id=binding,
                    model_id=feature['model_id'], capability_id=feature['capability_id'], local_request_value=value['value']))
            self.assertFalse(local_control_value_authorized(extended, scope, binding_id=binding,
                model_id=feature['model_id'], capability_id=feature['capability_id'], local_request_value='not-an-observed-option'))
        factories = [d for b, m in models.items() for d in eligible_factory_descriptors(extended, scope, binding_id=b, model_id=m)
                     if d not in base.descriptors]
        self.assertEqual({d.home_assistant_entity_key for d in factories},
                         {'local_kimchi_button_sound', 'local_kimchi_door_melody', 'local_water_button_sound', 'local_water_product_sound', 'local_water_sound_volume', 'local_water_lcd_brightness', 'local_water_do_not_disturb_window',
                          'local_washer_course_program', 'local_washer_fresh_care_enabled', 'local_water_custom_recipe_1_transaction', 'local_dryer_course_program', 'local_styler_course_start', 'local_vacuum_dust_emptying', 'local_vacuum_auto_dust_emptying', 'local_air_clean_dry', 'local_air_rapid_operation',
                          'local_styler_auto_course_arrange', 'local_styler_remember_last_course', 'local_styler_smart_care_night', 'local_styler_smart_care_humidity', 'local_styler_smart_care_fine_dust', 'local_styler_date_display', 'local_styler_24_hour_display', 'local_water_ice_lock', 'local_styler_recorded_resume',
                          'local_water_do_not_disturb', 'local_water_24_hour_display', 'local_water_long_unused_notice', 'local_water_voice_guidance', 'local_water_ice_priority', 'local_water_hot_water_lock', 'local_vacuum_dust_emptying_reservation', 'local_vacuum_dust_emptying_schedule'})
        self.assertEqual(len(factories), 33)
        vacuum = next(d for d in factories if d.home_assistant_entity_key == 'local_vacuum_dust_emptying')
        self.assertEqual(vacuum.entity_domain, 'button')
        self.assertTrue(vacuum.one_shot)
        self.assertEqual([(v.home_assistant_value, v.local_request_value) for v in vacuum.value_mappings], [('press', 'true')])
        self.assertNotIn('washer.operation.start_or_resume', prior['test_washtower_binding'].values_by_capability)

    def test_smart_courses_extend_the_existing_washer_owner_without_dashboard_changes(self):
        feature = next(f for f in load_confirmed_features() if f['capability_id'] == 'washer.course_program')
        self.assertEqual(feature['entity_key'], 'local_washer_course_program')
        self.assertEqual(len(feature['values']), 45)
        school = next(v for v in feature['values'] if v['value'].startswith('School Uniform|'))
        rinse = next(v for v in feature['values'] if v['value'].startswith('Deep Rinse|'))
        self.assertTrue(school['label'].startswith('교복'))
        self.assertTrue(rinse['label'].startswith('꼼꼼헹굼'))
        self.assertEqual(school['ordered_frame_sha256s'], ['f01c816c54a4ada661de061d8187d9421a9abe193ca6f76a6ad6af76aadc4729'])
        self.assertEqual(rinse['ordered_frame_sha256s'], ['ff4524b2c2d518cf074057b94558bd24b10ed87359dd80fb8d934dab861d43a5'])

    def test_missing_scope_cannot_create_or_authorize_any_new_owner(self):
        base = load_local_control_entity_contract()
        extended, scope = augment_confirmed_features(base, {}, {'test_washtower_binding': 'WTL_KPK_BDH_KR_01'})
        self.assertEqual(scope, {})
        self.assertEqual(extended.descriptors, base.descriptors)
