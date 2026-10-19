"""Push observations received during a REST request must survive its completion."""
import asyncio
import copy
import tempfile
import unittest

from homeassistant.core import HomeAssistant
from custom_components.my_lg.coordinator import PatDeviceCoordinator


class API:
    def __init__(self):
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.reply = {'operation': {'power': 'OFF'}, 'temperature': {'target': 23}}
        self.error = None

    async def async_get_device_status(self, device_id):
        self.started.set()
        await self.finish.wait()
        if self.error:
            raise self.error
        return self.reply


class PatOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='lg-pat-ordering-')
        self.hass = HomeAssistant(self.tmp.name)
        self.api = API()
        self.coordinator = PatDeviceCoordinator(self.hass, None, self.api, {'deviceId': 'TEST'})
        self.coordinator.async_set_updated_data({'operation': {'power': 'OFF'}, 'temperature': {'target': 20}})
        self.task = None

    async def asyncTearDown(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.hass.async_stop(force=True)
        self.tmp.cleanup()

    async def begin(self):
        self.task = asyncio.create_task(self.coordinator.async_refresh())
        await asyncio.wait_for(self.api.started.wait(), 2)

    async def finish(self):
        self.api.finish.set()
        await asyncio.wait_for(self.task, 2)

    async def test_new_push_overrides_old_reply_but_rest_updates_untouched_fields(self):
        await self.begin()
        self.coordinator.handle_mqtt_status({'operation': {'power': 'ON'}})
        await self.finish()
        self.assertEqual(self.coordinator.data, {'operation': {'power': 'ON'}, 'temperature': {'target': 23}})

    async def test_multiple_push_deltas_keep_all_fields_and_latest_value(self):
        await self.begin()
        self.coordinator.handle_mqtt_status({'operation': {'power': 'ON'}})
        self.coordinator.handle_mqtt_status({'temperature': {'target': 26}})
        self.coordinator.handle_mqtt_status({'temperature': {'target': 27}})
        await self.finish()
        self.assertEqual(self.coordinator.get('operation', 'power'), 'ON')
        self.assertEqual(self.coordinator.get('temperature', 'target'), 27)

    async def test_list_push_snapshot_survives_old_rest_snapshot(self):
        self.api.reply = [{'zone': 1, 'power': 'OFF'}]
        await self.begin()
        self.coordinator.handle_mqtt_status([{'zone': 1, 'power': 'ON'}])
        await self.finish()
        self.assertEqual(self.coordinator.data, [{'zone': 1, 'power': 'ON'}])

    async def test_old_push_does_not_override_a_later_poll(self):
        self.coordinator.handle_mqtt_status({'operation': {'power': 'ON'}})
        await self.begin(); await self.finish()
        self.assertEqual(self.coordinator.get('operation', 'power'), 'OFF')

    async def test_poll_after_overlap_has_no_leftover_delta(self):
        await self.begin()
        self.coordinator.handle_mqtt_status({'operation': {'power': 'ON'}})
        await self.finish()
        await self.coordinator.async_refresh()
        self.assertEqual(self.coordinator.get('operation', 'power'), 'OFF')

    async def test_failed_fallback_does_not_hide_fresh_push(self):
        self.api.error = RuntimeError('offline')
        await self.begin()
        self.coordinator.handle_mqtt_status({'operation': {'power': 'ON'}})
        await self.finish()
        self.assertTrue(self.coordinator.last_update_success)
        self.assertEqual(self.coordinator.get('operation', 'power'), 'ON')

    async def test_failed_fallback_without_push_still_marks_unavailable(self):
        self.api.error = RuntimeError('offline')
        await self.begin(); await self.finish()
        self.assertFalse(self.coordinator.last_update_success)

    async def test_invalid_rest_after_push_does_not_mark_unavailable(self):
        self.api.reply = None
        await self.begin()
        self.coordinator.handle_mqtt_status({'operation': {'power': 'ON'}})
        await self.finish()
        self.assertTrue(self.coordinator.last_update_success)

    async def test_merge_never_mutates_previous_snapshot_or_caller_payload(self):
        old = self.coordinator.data
        original = copy.deepcopy(old)
        report = {'operation': {'power': 'ON'}}
        reply = copy.deepcopy(self.api.reply)
        await self.begin(); self.coordinator.handle_mqtt_status(report)
        report['operation']['power'] = 'BAD'
        await self.finish()
        self.assertEqual(old, original)
        self.assertEqual(self.api.reply, reply)
        self.assertEqual(self.coordinator.get('operation', 'power'), 'ON')

    async def test_cancelled_poll_cleans_up_refresh_state(self):
        await self.begin(); self.task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.task
        self.coordinator.handle_mqtt_status({'operation': {'power': 'ON'}})
        self.api.finish.set()
        await self.coordinator.async_refresh()
        self.assertEqual(self.coordinator.get('operation', 'power'), 'OFF')

    async def test_invalid_push_does_not_hide_failed_poll(self):
        self.api.error = RuntimeError('offline')
        await self.begin()
        for report in [None, {}, [], 'ON', 1]:
            self.coordinator.handle_mqtt_status(report)
        await self.finish()
        self.assertFalse(self.coordinator.last_update_success)
