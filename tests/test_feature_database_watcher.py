"""DB edits reload the integration once without changing entity identity."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from custom_components import my_lg


class FeatureDatabaseWatcherTest(unittest.IsolatedAsyncioTestCase):
    async def test_committed_edit_triggers_one_reload(self) -> None:
        sequence = 1
        callback = None
        cancel = lambda: None
        unload_callbacks = []
        reload_entry = AsyncMock()

        class Hass:
            config_entries = SimpleNamespace(async_reload=reload_entry)

            async def async_add_executor_job(self, function, *args):
                return function(*args)

        class Entry:
            entry_id = "test-entry"

            def async_on_unload(self, cleanup):
                unload_callbacks.append(cleanup)

        def track(_hass, action, _interval, *, name):
            nonlocal callback
            self.assertEqual(name, "my_lg feature database")
            callback = action
            return cancel

        with (
            patch.object(my_lg, "default_database_path", return_value=Path("/tmp/test-features.sqlite3")),
            patch.object(my_lg, "feature_change_sequence", side_effect=lambda _path: sequence),
            patch.object(my_lg, "async_track_time_interval", side_effect=track),
        ):
            my_lg._watch_feature_database(Hass(), Entry(), 1)
            self.assertEqual(unload_callbacks, [cancel])
            self.assertIsNotNone(callback)
            await callback(None)
            reload_entry.assert_not_awaited()
            sequence = 2
            await callback(None)
            reload_entry.assert_awaited_once_with("test-entry")
            await callback(None)
            reload_entry.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
