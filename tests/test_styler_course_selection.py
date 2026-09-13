"""Selecting a desired course is never an appliance start."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock
from custom_components.my_lg.local_control_entity import MyLgLocalContractSelect
from custom_components.my_lg.local_control_router import LocalControlRouter

class StylerCourseSelectionTests(unittest.IsolatedAsyncioTestCase):
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
