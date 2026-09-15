"""Additive known commands reuse owners and preserve old authority/scopes."""
import unittest
from custom_components.my_lg.local_control_contract import (
    load_local_control_entity_contract, resolve_local_control_binding_eligibility,
    local_control_value_authorized, eligible_factory_descriptors,
)
from custom_components.my_lg.local_control_confirmed_features import augment_confirmed_features, load_confirmed_features


class ConfirmedFeaturesTests(unittest.TestCase):
    def test_cst570_enum_extension_reuses_native_owners_and_their_existing_request_vocabulary(self):
        base = load_local_control_entity_contract()
        model = 'CST_570004_WW'
        models = {'test_cst570_binding': model}
        prior = resolve_local_control_binding_eligibility({}, base, models)
        extended, scope = augment_confirmed_features(base, prior, models)
        # MyLgWideqSelect's existing local_value_map uses these exact requests.
        expected = {'auto_dry.mode': {'off', '10 min or firmware ON', '30 min', '60 min', 'smart'},
                    'display.brightness_level': {'off', '50%', '100%'}}
        for cap, values in expected.items():
            old = next(d for d in base.descriptors_by_model[model] if d.capability_id == cap)
            new = next(d for d in extended.descriptors_by_model[model] if d.capability_id == cap)
            self.assertFalse(new.factory_eligible)  # no second select entity
            self.assertEqual(new.home_assistant_entity_key, old.home_assistant_entity_key)
            self.assertEqual(set(scope['test_cst570_binding'].values_by_capability[cap]), values)
            self.assertEqual(set(new.exact_local_request_values), values)
            self.assertEqual(set(new.supported_values), values)
        from dataclasses import replace
        row = prior['test_cst570_binding']
        restricted = {'test_cst570_binding': replace(row, values_by_capability={**row.values_by_capability,
            'auto_dry.mode': ('smart',)})}
        _, scope = augment_confirmed_features(base, restricted, models)
        self.assertNotIn('60 min', scope['test_cst570_binding'].values_by_capability['auto_dry.mode'])
        self.assertIn('30 min', scope['test_cst570_binding'].values_by_capability['auto_dry.mode'])

    def test_existing_label_changes_require_explicit_migration(self):
        from unittest.mock import patch
        import sys
        features = [dict(f) for f in load_confirmed_features()]
        for feature in features:
            feature.pop('relabels_existing', None)
        base = load_local_control_entity_contract()
        models = {'test_hum_binding': 'HUM_056905_WW'}
        prior = resolve_local_control_binding_eligibility({}, base, models)
        with patch.object(sys.modules[augment_confirmed_features.__module__], 'load_confirmed_features', return_value=features):
            with self.assertRaisesRegex(ValueError, 'explicit migration'):
                augment_confirmed_features(base, prior, models)

    def test_numeric_overlay_keeps_base_pins_and_exact_choices_without_restoring_old_denials(self):
        base = load_local_control_entity_contract()
        models = {'test_numeric_hum': 'HUM_056905_WW', 'test_numeric_air': 'AIR_910604_WW'}
        prior = resolve_local_control_binding_eligibility({}, base, models)
        extended, scope = augment_confirmed_features(base, prior, models)
        self.assertEqual(extended.root_sha256, base.root_sha256)
        hum = next(d for d in extended.descriptors_by_model['HUM_056905_WW'] if d.capability_id == 'humidity.target_pct')
        self.assertEqual(hum.supported_values, tuple(range(30, 71, 5)))
        self.assertEqual((hum.number_min, hum.number_max, hum.number_step), (30,70,5))
        brightness = next(d for d in extended.descriptors_by_model['HUM_056905_WW'] if d.capability_id == 'water_tank.light_brightness_raw')
        self.assertEqual(brightness.entity_domain, 'select')
        self.assertEqual([(v.home_assistant_value, v.local_request_value) for v in brightness.value_mappings],
                         [(f'{n}%', str(n+100)) for n in range(10,101,10)])
        color = next(d for d in extended.descriptors_by_model['HUM_056905_WW'] if d.capability_id == 'mood_light.color_raw')
        self.assertEqual(color.entity_domain, 'select')
        self.assertIn(('lavender','21'), [(v.home_assistant_value, v.local_request_value) for v in color.value_mappings])
        for model, cap, illegal in [('HUM_056905_WW','timer.off_remaining_min','90'),
                                    ('AIR_910604_WW','timer.sleep_remaining_min','360')]:
            d = next(d for d in extended.descriptors_by_model[model] if d.capability_id == cap)
            self.assertEqual(d.entity_domain, 'select')
            self.assertNotIn(illegal, d.exact_local_request_values)
            old = next(d for d in base.descriptors_by_model[model] if d.capability_id == cap)
            self.assertEqual(d.home_assistant_entity_key, old.home_assistant_entity_key)
        from dataclasses import replace
        restricted = dict(prior)
        row = restricted['test_numeric_hum']
        restricted['test_numeric_hum'] = replace(row, values_by_capability={**row.values_by_capability, 'humidity.target_pct': ('60',)})
        _, restricted_scope = augment_confirmed_features(base, restricted, models)
        values = restricted_scope['test_numeric_hum'].values_by_capability['humidity.target_pct']
        self.assertIn('65', values)
        self.assertIn('60', values)
        self.assertNotIn('55', values)

    def test_exact_catalogue_and_parent_preservation(self):
        base = load_local_control_entity_contract()
        models = {'test_washtower_binding': 'WTL_KPK_BDH_KR_01', 'test_styler_binding': 'ST_R_ETH01Y_',
                  'test_water_binding': '1WPD4CMIDR__3', 'test_ac_binding_01': 'CST_170004_WW', 'test_vacuum_binding': 'HWWA9X3C_F2U'}
        models['test_air_binding'] = 'AIR_910604_WW'
        models['test_air_tower_binding'] = 'AIR_2C0001_WW'
        models['test_kimchi_binding'] = '3REK2G03VI230D_2'
        models['test_cst570_binding'] = 'CST_570004_WW'
        models['test_dhum_binding'] = 'DHUM_056905_WW'
        models['test_hum_binding'] = 'HUM_056905_WW'
        prior = resolve_local_control_binding_eligibility({}, base, models)
        extended, scope = augment_confirmed_features(base, prior, models)
        self.assertEqual(extended.root_sha256, base.root_sha256)
        for model, rows in base.descriptors_by_model.items():
            for row in rows:
                current = next(d for d in extended.descriptors_by_model[model] if d.key == row.key)
                numeric = any(f.get('value_source') == 'exact-model-web-domain' and f['model_id'] == model
                              and f['capability_id'] == row.capability_id for f in load_confirmed_features())
                if numeric:
                    self.assertEqual(current.home_assistant_entity_key, row.home_assistant_entity_key)
                    self.assertTrue(set(row.exact_local_request_values) <= set(current.exact_local_request_values))
                    binding = next(b for b, m in models.items() if m == model)
                    self.assertEqual(current.factory_eligible, row.factory_eligible)
                    if row.factory_eligible:
                        self.assertIn(current, eligible_factory_descriptors(extended, scope, binding_id=binding, model_id=model))
                    else:
                        self.assertNotIn(current, eligible_factory_descriptors(extended, scope, binding_id=binding, model_id=model))
                    labelled = any(f.get('display_labels') and f['model_id'] == model and f['capability_id'] == row.capability_id for f in load_confirmed_features())
                    if row.entity_domain == 'select' and not labelled:
                        labels = {v.local_request_value: v.home_assistant_value for v in current.value_mappings}
                        for value in row.value_mappings:
                            self.assertEqual(labels[value.local_request_value], value.home_assistant_value)
                else:
                    self.assertEqual(current, row)
        for cap, values in prior['test_ac_binding_01'].values_by_capability.items():
            self.assertTrue(set(values) <= set(scope['test_ac_binding_01'].values_by_capability[cap]))
        self.assertEqual(sum(len(f['values']) for f in load_confirmed_features()), 435)
        for feature in load_confirmed_features():
            binding = next(b for b, m in models.items() if m == feature['model_id'])
            for value in feature['values']:
                self.assertTrue(local_control_value_authorized(extended, scope, binding_id=binding,
                    model_id=feature['model_id'], capability_id=feature['capability_id'], local_request_value=value['value']))
            self.assertFalse(local_control_value_authorized(extended, scope, binding_id=binding,
                model_id=feature['model_id'], capability_id=feature['capability_id'], local_request_value='not-an-observed-option'))
        factories = [d for b, m in models.items() for d in eligible_factory_descriptors(extended, scope, binding_id=b, model_id=m)
                     if d.key not in {original.key for original in base.descriptors}]
        self.assertEqual({d.home_assistant_entity_key for d in factories} - {'local_cst170_button_sound', 'local_cst570_button_sound', 'local_dhum_button_sound', 'local_hum_button_sound', 'local_hum_sound_melody', 'local_styler_night_start_time', 'local_styler_night_end_time'},
                         {'local_water_amount_presets', 'local_water_sterilization_time', 'local_styler_sound_volume', 'local_styler_sound_melody', 'local_styler_startup_image', 'local_styler_remote_maintain', 'local_styler_time_display', 'local_kimchi_button_sound', 'local_kimchi_door_melody', 'local_water_button_sound', 'local_water_product_sound', 'local_water_sound_volume', 'local_water_lcd_brightness', 'local_water_do_not_disturb_window',
                          'local_washer_course_program', 'local_washer_fresh_care_enabled', 'local_water_custom_recipe_1_transaction', 'local_dryer_course_program', 'local_styler_course_start', 'local_vacuum_dust_emptying', 'local_vacuum_auto_dust_emptying', 'local_air_clean_dry', 'local_air_rapid_operation',
                          'local_styler_auto_course_arrange', 'local_styler_remember_last_course', 'local_styler_smart_care_night', 'local_styler_smart_care_humidity', 'local_styler_smart_care_fine_dust', 'local_styler_date_display', 'local_styler_24_hour_display', 'local_water_ice_lock', 'local_styler_recorded_resume',
                          'local_water_do_not_disturb', 'local_water_24_hour_display', 'local_water_long_unused_notice', 'local_water_voice_guidance', 'local_water_ice_priority', 'local_water_hot_water_lock', 'local_vacuum_dust_emptying_reservation', 'local_vacuum_dust_emptying_schedule'})
        self.assertEqual(len(factories), 47)
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
