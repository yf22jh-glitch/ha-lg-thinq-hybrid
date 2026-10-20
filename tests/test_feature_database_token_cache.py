"""Live-edit detection without a full menu read on every idle poll."""
import importlib.util
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    "token_cache_database", Path(__file__).parents[1] / "custom_components/my_lg/feature_database.py")
database = importlib.util.module_from_spec(spec)
spec.loader.exec_module(database)


class TokenCacheTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "features.sqlite3"
        database.create_database(self.path, [{
            "channel": "full-read", "model_id": "test-model", "profile_id": "test-profile",
            "feature_id": "state.example", "platform": "thinq2", "definition": {"name": "before"}}])
        self.reader = Mock(wraps=database.feature_database_token)
        self.cache = database.FeatureDatabaseTokenCache(self.reader)
        if hasattr(self.cache, "close"):
            self.addCleanup(self.cache.close)

    def test_idle_poll_does_not_read_menu_and_direct_wal_edit_does(self):
        writer = sqlite3.connect(self.path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL").fetchall()
        writer.execute("UPDATE features SET enabled=1")
        writer.commit()
        before = self.cache.read(self.path)
        # A first reader may create/remove SQLite sidecars; stabilize once.
        self.cache.read(self.path)
        self.reader.reset_mock()
        for _ in range(10):
            self.assertEqual(self.cache.read(self.path), before)
        self.reader.assert_not_called()
        writer.execute("UPDATE features SET definition_json=?", ('{"name":"after"}',))
        writer.commit()
        self.assertNotEqual(self.cache.read(self.path), before)

    def test_atomic_replacement_and_periodic_safety_read(self):
        before = self.cache.read(self.path)
        replacement = self.path.with_suffix(".new")
        database.copy_feature_database(self.path, replacement)
        with closing(sqlite3.connect(replacement)) as writer, writer:
            writer.execute("UPDATE features SET enabled=0")
        replacement.replace(self.path)
        self.assertNotEqual(self.cache.read(self.path), before)
        self.reader.reset_mock()
        with patch.object(database.time, "monotonic", return_value=self.cache._checked_at + 61):
            self.cache.read(self.path)
        self.reader.assert_called_once()

    def test_wal_permission_change_does_not_invalidate_unchanged_content(self):
        writer = sqlite3.connect(self.path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL").fetchall()
        writer.execute("UPDATE features SET enabled=1")
        writer.commit()
        before = self.cache.read(self.path)
        self.cache.read(self.path)
        self.reader.reset_mock()
        wal = Path(str(self.path) + "-wal")
        modified_at = wal.stat().st_mtime_ns
        wal.chmod(wal.stat().st_mode ^ 0o020)
        self.assertEqual(wal.stat().st_mtime_ns, modified_at)
        self.assertEqual(self.cache.read(self.path), before)
        self.reader.assert_not_called()

    def test_read_failure_retries_and_edit_during_read_does_not_get_cached(self):
        token = self.cache.read(self.path)
        with closing(sqlite3.connect(self.path)) as writer, writer:
            writer.execute("UPDATE features SET enabled=0")
        self.reader.side_effect = sqlite3.OperationalError("busy")
        with self.assertRaises(sqlite3.OperationalError):
            self.cache.read(self.path)
        self.reader.side_effect = None
        self.assertNotEqual(self.cache.read(self.path), token)
        self.cache._stamp = None
        def editing_reader(path):
            previous = database.feature_database_token(path)
            with closing(sqlite3.connect(path)) as writer, writer:
                writer.execute("UPDATE features SET enabled=1")
            return previous
        self.reader.side_effect = editing_reader
        self.cache.read(self.path)
        self.assertIsNone(self.cache._stamp)
        self.reader.side_effect = None
        self.assertEqual(self.cache.read(self.path), token)


if __name__ == "__main__":
    unittest.main()
