"""Selecting a desired course is never an appliance start."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock
from custom_components.my_lg.local_control_entity import MyLgLocalContractSelect, MyLgStylerOptionDraftText
from custom_components.my_lg.local_control_router import LocalControlRouter

class StylerCourseSelectionTests(unittest.IsolatedAsyncioTestCase):
    def test_draft_listener_refreshes_on_consumption_and_unsubscribes(self):
        value = 'DRY_TIME_23|on|240|60'
        router = LocalControlRouter(Mock(), {}, lambda _: 'bridge-test', authorized_values=lambda *_: (value,))
        states = []
        remove = router.subscribe_styler_choice('test-styler', lambda: states.append(router.selected_styler_options('test-styler')))
        router.select_styler_options('test-styler', value)
        router.take_styler_start('test-styler')
        self.assertEqual(states, [value, None])
        remove()
        router.select_styler_options('test-styler', value)
        self.assertEqual(states, [value, None])

    async def test_option_draft_is_not_sent_and_start_consumes_it_once(self):
        from custom_components.my_lg.local_styler_options import CAPABILITY
        value = 'DRY_TIME_23|on|240|60'
        router = LocalControlRouter(Mock(), {}, lambda _: 'bridge-test', authorized_values=lambda *_: (value,'course-test'))
        entity = object.__new__(MyLgStylerOptionDraftText)
        entity.coordinator = SimpleNamespace(device_id='test-styler')
        entity._router = router
        entity._async_send = AsyncMock()
        entity.async_write_ha_state = Mock()
        await entity.async_set_value(value)
        entity._async_send.assert_not_awaited()
        self.assertEqual(entity.native_value, value)
        self.assertEqual(router.take_styler_start('test-styler'), (CAPABILITY,value))
        self.assertIsNone(router.take_styler_start('test-styler'))
        self.assertIsNone(entity.native_value)
        await entity.async_set_value(value)
        router.select_styler_course('test-styler','course-test')
        self.assertIsNone(entity.native_value)
        self.assertEqual(router.take_styler_start('test-styler'),('styler.operation.start_or_resume','course-test'))
    async def test_selection_is_local_memory_only_and_consumed_once(self):
        router = LocalControlRouter(Mock(), {}, lambda _: 'bridge-test', authorized_values=lambda *_: ('course-test',))
        entity = object.__new__(MyLgLocalContractSelect)
        entity.coordinator = SimpleNamespace(device_id='test-styler')
        entity._descriptor = SimpleNamespace(capability_id='styler.operation.start_or_resume')
        entity._router = router
        entity._mapping_by_option = {'test-course':SimpleNamespace(local_request_value='course-test')}
        entity._async_send = AsyncMock()
        entity.async_write_ha_state = Mock()
        await entity.async_select_option('test-course')
        entity._async_send.assert_not_awaited()
        self.assertEqual(entity.current_option, 'test-course')
        self.assertEqual(router.take_styler_course('test-styler'), 'course-test')
        self.assertIsNone(entity.current_option)
        self.assertIsNone(router.take_styler_course('test-styler'))
        with self.assertRaises(ValueError):
            router.select_styler_course('test-styler', 'unknown')
