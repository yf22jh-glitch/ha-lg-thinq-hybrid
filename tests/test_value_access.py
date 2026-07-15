"""Tests for RAW access and PAT capability discovery."""

from __future__ import annotations

from pathlib import Path
import sys
from types import ModuleType
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "custom_components" / "my_lg"


def _package() -> None:
    custom = sys.modules.setdefault("custom_components", ModuleType("custom_components"))
    custom.__path__ = [str(ROOT / "custom_components")]
    my_lg = sys.modules.setdefault("custom_components.my_lg", ModuleType("custom_components.my_lg"))
    my_lg.__path__ = [str(PACKAGE)]


_package()
from custom_components.my_lg.feature import FeatureAccess  # noqa: E402
from custom_components.my_lg.feature_catalog.pat import discover_pat_features  # noqa: E402
from custom_components.my_lg.value_access import (  # noqa: E402
    flatten_values,
    nested_payload,
    read_path,
    stable_feature_key,
)


class ValueAccessTests(unittest.TestCase):
    def test_flat_dotted_wideq_key_is_atomic(self) -> None:
        data = {"airState.energy.onCurrent": 42}
        path = ("airState.energy.onCurrent",)
        self.assertEqual(read_path(data, path), 42)
        self.assertEqual(list(flatten_values(data)), [path])

    def test_location_list_uses_stable_selector(self) -> None:
        data = [
            {
                "location": {"locationName": "LEFT_FRONT"},
                "power": {"powerLevel": 7},
            }
        ]
        path = ("@location=LEFT_FRONT", "power", "powerLevel")
        self.assertIn(path, list(flatten_values(data)))
        self.assertEqual(read_path(data, path), 7)

    def test_course_list_remains_one_logical_value(self) -> None:
        data = {"washer": {"panelCrsList": [{"courseValue": "NORMAL"}]}}
        self.assertEqual(
            list(flatten_values(data)), [("washer", "panelCrsList")]
        )

    def test_nested_payload(self) -> None:
        self.assertEqual(
            nested_payload(("timer", "absoluteHourToStart"), 8),
            {"timer": {"absoluteHourToStart": 8}},
        )

    def test_feature_keys_are_stable_and_collision_resistant(self) -> None:
        first = stable_feature_key("wideq", ("a.b",))
        self.assertEqual(first, stable_feature_key("wideq", ("a.b",)))
        self.assertNotEqual(first, stable_feature_key("wideq", ("a", "b")))


class PatCatalogTests(unittest.TestCase):
    def test_discovers_nested_writable_enum(self) -> None:
        profile = {
            "property": {
                "airFlow": {
                    "windStrength": {
                        "type": "enum",
                        "mode": ["r", "w"],
                        "value": {"r": ["LOW", "HIGH"], "w": ["LOW", "HIGH"]},
                    }
                }
            }
        }
        feature = discover_pat_features(profile)[0]
        self.assertEqual(feature.path, ("airFlow", "windStrength"))
        self.assertEqual(feature.access, FeatureAccess.READ_WRITE)
        self.assertEqual(feature.options, ("LOW", "HIGH"))

    def test_discovers_washtower_subprofile(self) -> None:
        spec = {
            "type": "range",
            "mode": ["r", "w"],
            "value": {"w": {"min": 3, "max": 19, "step": 1}},
        }
        features = discover_pat_features(
            {"washer": {"property": {"timer": {"relativeHourToStop": spec}}}}
        )
        self.assertEqual(features[0].path, ("washer", "timer", "relativeHourToStop"))
        self.assertEqual(features[0].minimum, 3)
        self.assertEqual(features[0].maximum, 19)


if __name__ == "__main__":
    unittest.main()
