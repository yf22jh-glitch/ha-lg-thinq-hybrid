"""DB edits apply live without changing the entry or consuming failed edits."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from custom_components import my_lg


class FeatureDatabaseWatcherTest(unittest.IsolatedAsyncioTestCase):
    async def test_committed_edit_applies_once_without_reload(self) -> None:
        sequence = "before"
        callback = None
        cancel = lambda: None
        unload_callbacks = []
        reload_entry = AsyncMock()
        refresh = AsyncMock(side_effect=lambda: sequence)

        class Hass:
            config_entries = SimpleNamespace(async_reload=reload_entry)

            async def async_add_executor_job(self, function, *args):
                return function(*args)

        class Entry:
            entry_id = "test-entry"
            runtime_data = SimpleNamespace(feature_runtime=SimpleNamespace(async_refresh=refresh))

            def async_on_unload(self, cleanup):
                unload_callbacks.append(cleanup)

        def track(_hass, action, interval, *, name):
            nonlocal callback
            self.assertEqual(name, "my_lg feature database")
            self.assertEqual(interval.total_seconds(), 2)
            callback = action
            return cancel

        with (
            patch.object(my_lg, "default_database_path", return_value=Path("/tmp/test-features.sqlite3")),
            patch.object(my_lg, "feature_database_token", side_effect=lambda _path: sequence),
            patch.object(my_lg, "async_track_time_interval", side_effect=track),
        ):
            my_lg._watch_feature_database(Hass(), Entry(), sequence)
            self.assertEqual(unload_callbacks, [cancel])
            self.assertIsNotNone(callback)
            await callback(None)
            reload_entry.assert_not_awaited()
            sequence = "after"
            await callback(None)
            refresh.assert_awaited_once()
            await callback(None)
            refresh.assert_awaited_once()
            reload_entry.assert_not_awaited()
            sequence = "next-edit"
            refresh.side_effect = [ValueError("bad edit"), sequence]
            with self.assertLogs(my_lg.__name__, level="ERROR"):
                await callback(None)
            await callback(None)
            self.assertEqual(refresh.await_count, 3)
            await callback(None)
            self.assertEqual(refresh.await_count, 3)
            reload_entry.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
