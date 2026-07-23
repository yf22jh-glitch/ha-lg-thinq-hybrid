"""Pure regression tests for stable PAT-to-WideQ identity resolution."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "my_lg"
    / "device_identity.py"
)
SPEC = importlib.util.spec_from_file_location("my_lg_device_identity_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
identity = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = identity
SPEC.loader.exec_module(identity)


class DeviceIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pat = {
            "pat-a": identity.PatDeviceIdentity("pat-a", "Living AC", "MODEL-A"),
            "pat-b": identity.PatDeviceIdentity("pat-b", "Fridge", "MODEL-B"),
        }
        self.wideq = [
            identity.WideqDeviceData(
                "wideq-a", "Living AC", "MODEL-A", {"power": 100}
            ),
            identity.WideqDeviceData(
                "wideq-b", "Fridge", "MODEL-B", {"temperature": 3}
            ),
        ]

    def test_unique_alias_and_model_create_stable_mapping(self) -> None:
        result = identity.resolve_wideq_devices(self.pat, self.wideq)

        self.assertEqual(
            result.pat_to_wideq,
            {"pat-a": "wideq-a", "pat-b": "wideq-b"},
        )
        self.assertEqual(result.snapshots["pat-a"], {"power": 100})
        self.assertFalse(result.ambiguous_pat_ids)
        self.assertFalse(result.unmatched_pat_ids)

    def test_persisted_ids_survive_alias_rename(self) -> None:
        renamed = [
            identity.WideqDeviceData(
                "wideq-a", "Renamed in ThinQ", "MODEL-A", {"power": 120}
            )
        ]

        result = identity.resolve_wideq_devices(
            self.pat,
            renamed,
            {"pat-a": "wideq-a"},
        )

        self.assertEqual(result.pat_to_wideq["pat-a"], "wideq-a")
        self.assertEqual(result.snapshots["pat-a"], {"power": 120})

    def test_duplicate_signature_is_blocked_instead_of_guessed(self) -> None:
        pat = {
            "pat-a": identity.PatDeviceIdentity("pat-a", "AC", "SAME"),
            "pat-b": identity.PatDeviceIdentity("pat-b", "AC", "SAME"),
        }
        devices = [
            identity.WideqDeviceData("wideq-a", "AC", "SAME", {"value": 1}),
            identity.WideqDeviceData("wideq-b", "AC", "SAME", {"value": 2}),
        ]

        result = identity.resolve_wideq_devices(pat, devices)

        self.assertEqual(result.pat_to_wideq, {})
        self.assertEqual(result.ambiguous_pat_ids, {"pat-a", "pat-b"})
        self.assertEqual(result.snapshots, {})

    def test_corrupt_duplicate_persisted_mapping_blocks_both_devices(self) -> None:
        pat = dict(self.pat)
        pat["pat-c"] = identity.PatDeviceIdentity(
            "pat-c", "Living AC", "MODEL-A"
        )
        result = identity.resolve_wideq_devices(
            pat,
            self.wideq,
            {"pat-a": "wideq-a", "pat-b": "wideq-a"},
        )

        self.assertNotIn("pat-a", result.pat_to_wideq)
        self.assertNotIn("pat-b", result.pat_to_wideq)
        self.assertEqual(result.ambiguous_pat_ids, {"pat-a", "pat-b"})
        self.assertNotIn("pat-c", result.pat_to_wideq)

    def test_offline_persisted_device_keeps_mapping_without_fake_state(self) -> None:
        result = identity.resolve_wideq_devices(
            self.pat,
            [],
            {"pat-a": "wideq-a"},
        )

        self.assertEqual(result.pat_to_wideq, {"pat-a": "wideq-a"})
        self.assertNotIn("pat-a", result.snapshots)
        self.assertEqual(result.unmatched_pat_ids, {"pat-b"})


if __name__ == "__main__":
    unittest.main()
