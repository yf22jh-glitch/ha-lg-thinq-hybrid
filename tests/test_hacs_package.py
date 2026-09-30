"""The Git checkout must expose only the LG integration to HACS."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class HacsPackageTest(unittest.TestCase):
    def test_hacs_selects_only_my_lg(self) -> None:
        integrations = sorted(
            path.name for path in (ROOT / "custom_components").iterdir()
            if path.is_dir() and (path / "manifest.json").is_file()
        )
        self.assertEqual(integrations, ["my_lg"])
        manifest = json.loads((ROOT / "custom_components/my_lg/manifest.json").read_text())
        self.assertEqual(manifest["domain"], "my_lg")
        self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+$")
        hacs = json.loads((ROOT / "hacs.json").read_text())
        self.assertIs(hacs["content_in_root"], False)

    def test_kocom_source_is_preserved_outside_lg_distribution(self) -> None:
        package = ROOT / "extras/kocom_energy/custom_components/kocom_energy"
        manifest = json.loads((package / "manifest.json").read_text())
        self.assertEqual(manifest["domain"], "kocom_energy")
        self.assertTrue((package / "api.py").is_file())
        self.assertTrue((package / "translations/ko.json").is_file())


if __name__ == "__main__":
    unittest.main()
