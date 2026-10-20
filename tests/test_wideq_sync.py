"""Vendor updates must preserve our API extensions and reject ambiguous merges."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import unittest


class WideQSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "scripts/sync_wideq.py"
        spec = spec_from_file_location("reviewed_wideq_sync", path)
        cls.sync = module_from_spec(spec)
        spec.loader.exec_module(cls.sync)

    def test_local_extension_and_notice_survive(self):
        local = {"core.py": b"old\n", "web_settings.py": b"local\n", "NOTICE.md": b"credit\n"}
        merged = self.sync.merge_trees({"core.py": b"old\n"}, local, {"core.py": b"new\n"})
        self.assertEqual(merged, {**local, "core.py": b"new\n"})

    def test_both_sides_changes_are_retained(self):
        base = b"first\n\nmiddle\n\nlast\n"
        local = base.replace(b"first", b"our extension")
        upstream = base.replace(b"last", b"new upstream")
        merged = self.sync.merge_trees({"core.py": base}, {"core.py": local}, {"core.py": upstream})
        self.assertEqual(merged["core.py"], b"our extension\n\nmiddle\n\nnew upstream\n")

    def test_overlap_fails_without_mutating_inputs(self):
        local = {"core.py": b"our change\n"}
        with self.assertRaisesRegex(ValueError, "core.py"):
            self.sync.merge_trees({"core.py": b"old\n"}, local, {"core.py": b"their change\n"})
        self.assertEqual(local, {"core.py": b"our change\n"})

    def test_local_extension_collision_fails(self):
        with self.assertRaisesRegex(ValueError, "web_settings.py"):
            self.sync.merge_trees({}, {"web_settings.py": b"ours"}, {"web_settings.py": b"theirs"})

    def test_upstream_cannot_remove_our_modified_module(self):
        with self.assertRaisesRegex(ValueError, "core.py"):
            self.sync.merge_trees({"core.py": b"old"}, {"core.py": b"patched"}, {})

    def test_identical_addition_and_unmodified_upstream_removal(self):
        self.assertEqual(
            self.sync.merge_trees(
                {"obsolete.py": b"old"}, {"obsolete.py": b"old", "new.py": b"same"},
                {"new.py": b"same"},
            ), {"new.py": b"same"},
        )

    def test_local_deletion_is_preserved_or_conflicts(self):
        self.assertEqual(self.sync.merge_trees({"old.py": b"old"}, {}, {"old.py": b"old"}), {})
        with self.assertRaisesRegex(ValueError, "old.py"):
            self.sync.merge_trees({"old.py": b"old"}, {}, {"old.py": b"changed"})

    def test_binary_conflict_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "binary"):
            self.sync.merge_trees({"file": b"\0a"}, {"file": b"\0b"}, {"file": b"\0c"})
