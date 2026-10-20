"""Committed edits must be detected even when filesystem metadata collides."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch

from tests import test_feature_database_token_cache as base

database = base.database


class CommitDetectionTest(unittest.TestCase):
    setUp = base.TokenCacheTest.setUp

    def update(self, enabled):
        with closing(sqlite3.connect(self.path)) as writer, writer:
            writer.execute("UPDATE features SET enabled=?", (enabled,))

    def test_same_metadata_commits_are_detected_in_delete_and_wal_modes(self):
        for mode in ("DELETE", "WAL"):
            with self.subTest(mode=mode), closing(sqlite3.connect(self.path)) as writer:
                writer.execute(f"PRAGMA journal_mode={mode}").fetchall()
                stamp = self.cache._files(self.path)
                with patch.object(self.cache, "_files", return_value=stamp):
                    for value in (1, 0, 1, 0, 1):
                        writer.execute("UPDATE features SET enabled=?", (value,))
                        writer.commit()
                        expected = database.feature_database_token(self.path)
                        self.assertEqual(self.cache.read(self.path), expected)
            # Close/reopen between journal modes without locking the writer.
            if hasattr(self.cache, "close"):
                self.cache.close()
            self.cache = database.FeatureDatabaseTokenCache(self.reader)
            if hasattr(self.cache, "close"):
                self.addCleanup(self.cache.close)

    def test_same_metadata_edit_during_full_read_forces_another_read(self):
        stamp = self.cache._files(self.path)
        with patch.object(self.cache, "_files", return_value=stamp):
            self.cache.read(self.path)
            self.update(0)

            def edit_after_snapshot(path):
                token = database.feature_database_token(path)
                self.update(1)
                return token

            self.reader.side_effect = edit_after_snapshot
            self.cache.read(self.path)
            self.assertIsNone(self.cache._stamp)
            self.reader.side_effect = None
            self.assertEqual(self.cache.read(self.path), database.feature_database_token(self.path))

    def test_failed_read_is_retried_with_identical_file_metadata(self):
        stamp = self.cache._files(self.path)
        with patch.object(self.cache, "_files", return_value=stamp):
            before = self.cache.read(self.path)
            self.update(0)
            self.reader.side_effect = sqlite3.OperationalError("busy")
            with self.assertRaises(sqlite3.OperationalError):
                self.cache.read(self.path)
            self.reader.side_effect = None
            self.assertNotEqual(self.cache.read(self.path), before)

    def test_executor_thread_changes_and_close_are_serialized(self):
        with ThreadPoolExecutor(max_workers=1) as first, ThreadPoolExecutor(max_workers=1) as second:
            before = first.submit(self.cache.read, self.path).result()
            self.update(0)
            self.assertNotEqual(second.submit(self.cache.read, self.path).result(), before)
            first.submit(self.cache.close).result()
            second.submit(self.cache.close).result()
            with self.assertRaisesRegex(ValueError, "closed"):
                first.submit(self.cache.read, self.path).result()

    def test_idle_observer_does_not_hold_a_read_transaction(self):
        self.cache.read(self.path)
        with closing(sqlite3.connect(self.path, timeout=0)) as writer, writer:
            writer.execute("BEGIN EXCLUSIVE")
            writer.execute("UPDATE features SET enabled=0")
        self.assertEqual(self.cache.read(self.path), database.feature_database_token(self.path))

    def test_missing_database_does_not_return_previous_token(self):
        self.cache.read(self.path)
        moved = self.path.with_suffix(".saved")
        self.path.rename(moved)
        with self.assertRaises(FileNotFoundError):
            self.cache.read(self.path)
        moved.rename(self.path)
        self.assertEqual(self.cache.read(self.path), database.feature_database_token(self.path))

    def test_wrong_identity_replacement_fails_closed_then_recovers(self):
        self.cache.read(self.path)
        replacement = self.path.with_suffix(".replacement")
        database.copy_feature_database(self.path, replacement)
        with closing(sqlite3.connect(replacement)) as writer, writer:
            writer.execute("PRAGMA application_id=0")
        replacement.replace(self.path)
        with self.assertRaises(ValueError):
            self.cache.read(self.path)
        with closing(sqlite3.connect(self.path)) as writer, writer:
            writer.execute(f"PRAGMA application_id={database.APPLICATION_ID}")
        self.assertEqual(self.cache.read(self.path), database.feature_database_token(self.path))

    def test_failed_connection_validation_closes_handle(self):
        class BrokenConnection:
            row_factory = None
            closed = False

            def execute(self, statement):
                raise sqlite3.OperationalError("locked")

            def close(self):
                self.closed = True

        connection = BrokenConnection()
        with patch.object(database.sqlite3, "connect", return_value=connection):
            with self.assertRaises(sqlite3.OperationalError):
                database._read_connection(self.path)
        self.assertTrue(connection.closed)
