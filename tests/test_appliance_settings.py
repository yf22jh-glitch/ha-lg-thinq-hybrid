"""Recorded Styler/purifier settings use own cache, not optimistic/cloud state."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from custom_components.my_lg.local_command import LocalCommandClient, LocalCommandResult
from custom_components.my_lg.local_control_confirmed_features import augment_confirmed_features, APPLIANCE_SETTING_MODELS
from custom_components.my_lg.local_control_contract import load_local_control_entity_contract, resolve_local_control_binding_eligibility
from custom_components.my_lg.local_control_entity import MyLgApplianceSettingSwitch, MyLgLocalContractButton
from tests.test_local_control_generic_entities import Coordinator, PrimaryProvider, Router, DEVICE_ID

class ApplianceSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_recorded_resume_is_explicit_button_not_an_automatic_course_start(self):
        model = 'ST_R_ETH01Y_'
        base = load_local_control_entity_contract()
        models = {'test_candidate_binding': model}
        prior = resolve_local_control_binding_eligibility({}, base, models)
        contract, _ = augment_confirmed_features(base, prior, models)
        descriptor = next(d for d in contract.descriptors if d.capability_id == 'styler.operation.resume_with_night_dry')
        self.assertIn('야간건조', descriptor.label_ko)
        self.assertIn('바지', descriptor.label_ko)
        self.assertTrue(descriptor.one_shot)
        router = Router()
        router.async_execute_strict = AsyncMock(return_value=LocalCommandResult('confirmed', {}))
        entity = MyLgLocalContractButton(Coordinator(model), descriptor, router, PrimaryProvider(model=model), None)
        entity.async_write_ha_state = lambda: None
        router.async_execute_strict.assert_not_called()
        await entity.async_press()
        router.async_execute_strict.assert_awaited_once_with(DEVICE_ID, descriptor.capability_id, 'true')

    async def test_all_thirty_one_switches_keep_actual_state_after_opposite_command(self):
        self.assertEqual(len(APPLIANCE_SETTING_MODELS), 31)
        base = load_local_control_entity_contract()
        for (model, cap), mapped_model in APPLIANCE_SETTING_MODELS.items():
            self.assertEqual(mapped_model, model)
            models = {'test_candidate_binding': model}
            prior = resolve_local_control_binding_eligibility({}, base, models)
            contract, _ = augment_confirmed_features(base, prior, models)
            descriptor = next(d for d in contract.descriptors if d.capability_id == cap and d.model_id == model)
            router = Router(); router.async_appliance_setting_state = AsyncMock(return_value=None)
            entity = MyLgApplianceSettingSwitch(Coordinator(model), descriptor, router, PrimaryProvider(model=model), None)
            entity.async_write_ha_state = lambda: None
            self.assertIsNone(entity.is_on)
            for enabled in (False, True):
                router.async_appliance_setting_state.return_value = enabled
                await entity.async_update()
                if enabled: await entity.async_turn_off()
                else: await entity.async_turn_on()
                self.assertIs(entity.is_on, enabled)
            self.assertEqual([c[1:] for c in router.calls], [(cap, 'true'), (cap, 'false')])
            router.async_appliance_setting_state.side_effect = TimeoutError()
            await entity.async_update(); self.assertIsNone(entity.is_on)

    async def test_reader_requires_exact_model_and_boolean_without_redirects(self):
        from tests.test_local_command import Response
        calls = []
        for (model, cap), mapped_model in APPLIANCE_SETTING_MODELS.items():
            self.assertEqual(mapped_model, model)
            for reported_model in (model, 'wrong'):
                for value in (False, True, None, 0, 1, 'false'):
                    response = Response(200, {'schema_version': 1, 'model_id': reported_model, 'values': {cap: value}})
                    def get(url, **kwargs): calls.append((url, kwargs)); return response
                    client = LocalCommandClient(SimpleNamespace(get=get))
                    result = await client.async_appliance_setting_state('test/device', model, cap)
                    self.assertIs(result, value if reported_model == model and type(value) is bool else None)
                    self.assertIsNone(await client.async_appliance_setting_state('test/device', model, 'raw.offset_83'))
        self.assertTrue(all('/test%2Fdevice/appliance-settings-state' in url and not opts['allow_redirects'] for url, opts in calls))
