"""Structural checks for generated identifier-free feature catalogs."""

from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "custom_components" / "my_lg" / "feature_catalog"


class FeatureCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = json.loads((CATALOG / "raw_paths.json").read_text())
        cls.controls = json.loads((CATALOG / "wideq_controls.json").read_text())

    def test_raw_catalog_is_paths_only(self) -> None:
        for source in self.raw.values():
            for paths in source.values():
                self.assertIsInstance(paths, list)
                for path in paths:
                    self.assertTrue(path)
                    self.assertTrue(all(isinstance(token, str) for token in path))

    def test_sentinel_and_offline_model_paths_remain_registered(self) -> None:
        purifier_paths = {
            tuple(path) for path in self.raw["wideq"]["1WPD4CMIDR__3"]
        }
        self.assertIn(("wpState", "iceMakerInDnd"), purifier_paths)
        self.assertEqual(len(self.raw["wideq"]["2REK1D04AR170"]), 8)

    def test_every_control_has_a_supported_serializer_shape(self) -> None:
        allowed = {"binary", "command", "data_key", "dataset", "template"}
        groups = 0
        for model in self.controls.values():
            collections = [model["controls"], *model["subdevices"].values()]
            for controls in collections:
                for spec in controls.values():
                    groups += 1
                    self.assertIn(spec["shape"], allowed)
                    self.assertIn("risk", spec)
                    self.assertIn("ctrl_key", spec)
        self.assertEqual(groups, 93)

    def test_no_capture_identity_keys_are_stored(self) -> None:
        raw_text = (CATALOG / "raw_paths.json").read_text().casefold()
        control_text = (CATALOG / "wideq_controls.json").read_text().casefold()
        for key in ('"alias"', '"deviceid"', '"ssid"', '"token"'):
            self.assertNotIn(key, raw_text)
            self.assertNotIn(key, control_text)


if __name__ == "__main__":
    unittest.main()
