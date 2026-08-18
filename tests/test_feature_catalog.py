"""Structural checks for generated identifier-free feature catalogs."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


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

    def test_new_exact_model_raw_paths_and_counts_are_pinned(self) -> None:
        tower_paths = {
            tuple(path) for path in self.raw["wideq"]["AIR_2C0001_WW"]
        }
        vacuum_paths = {
            tuple(path) for path in self.raw["wideq"]["HWWA9X3C_F2U"]
        }

        self.assertEqual(len(tower_paths), 102)
        self.assertIn(("airState.catTower.weight",), tower_paths)
        self.assertIn(
            ("airState.humidifier.waterTank.remain",), tower_paths
        )
        self.assertEqual(len(vacuum_paths), 41)
        self.assertIn(("qmState", "aiSuctionForce"), vacuum_paths)
        self.assertIn(("qmState", "remoteUvc"), vacuum_paths)

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
        self.assertEqual(groups, 113)

    def test_new_exact_models_are_catalogued_without_inherited_controls(self) -> None:
        tower = self.controls["AIR_2C0001_WW"]
        self.assertEqual(tower, {"controls": {}, "subdevices": {}})

        vacuum = self.controls["HWWA9X3C_F2U"]["controls"]
        self.assertEqual(len(vacuum), 20)
        self.assertIn("QMControl_AiSuctionForce", vacuum)
        self.assertIn("QMControl_WaterSupply", vacuum)
        field_echo_controls = {
            "QMControl_AiSuctionForce": "qmState.aiSuctionForce",
            "QMControl_BatteryLifeMode": "qmState.batteryLifeMode",
            "QMControl_Brightness": "qmState.brightness",
            "QMControl_ChargingMelody": "qmState.chargingMelody",
            "QMControl_DustEmptyingMelody": "qmState.dustEmptyingMelody",
            "QMControl_MopWithSucking": "qmState.mopWithSucking",
            "QMControl_SettingButtonSound": "qmState.settingButtonSound",
            "QMControl_SteamSupply": "qmState.steamSupply",
            "QMControl_StopAndGoSetting": "qmState.stopAndGoSetting",
            "QMControl_SuctionForce": "qmState.suctionForce",
            "QMControl_Volume": "qmState.volume",
            "QMControl_WaterSupply": "qmState.waterSupply",
        }
        self.assertEqual(
            {
                name
                for name, spec in vacuum.items()
                if "verification" in spec
            },
            set(field_echo_controls),
        )
        for name, field in field_echo_controls.items():
            self.assertEqual(
                vacuum[name]["verification"],
                {
                    "mode": "field_echo",
                    "required_request_fields": [field],
                    "echo_fields": [field],
                    "readback_delays_s": [5, 10],
                },
            )
        for spec in vacuum.values():
            for field in spec.get("fields", {}).values():
                self.assertNotIn("IGNORE", field.get("options", ()))
        self.assertEqual(
            vacuum["QMControl_RemoteDustEmptying"]["fields"]
            ["qmState.remoteDustEmptying"]["options"],
            ["ON", "ROBOT_ON"],
        )
        self.assertEqual(
            vacuum["QMControl_RemoteUvc"]["fields"]["qmState.remoteUvc"]
            ["options"],
            ["ON"],
        )
        for control in (
            "QMControl_RemoteDustEmptying",
            "QMControl_RemoteUvc",
            "QMControl_ReserveDustEmptying",
            "QMControl_ReserveDustEmptyingEnable",
        ):
            self.assertEqual(vacuum[control]["risk"], "operation")

    def test_only_audited_styler_state_transitions_are_write_enabled(self) -> None:
        controls = self.controls["ST_R_ETH01Y_"]["controls"]

        self.assertNotIn("verification", controls["startCourse"])
        pause = controls["pauseCourse"]
        self.assertEqual(
            pause["template"],
            {
                "styler": {
                    "controlDataType": "PAUSE",
                    "controlDataValueLength": 1,
                    "controlDataValue": 0,
                }
            },
        )
        self.assertEqual(
            pause["verification"]["preconditions"]["styler.state"],
            [
                "RESERVED",
                "DETECTING",
                "PRESTEAM",
                "STERILIZE",
                "STEAM_SPRAY",
                "REFRESH",
                "DRYING",
                "AIRCARE",
                "DEHUME",
                "STEAMER_RUNNING",
                "STEAMER_CLEANING",
                "STEAMER_CLEANING_AUTO",
            ],
        )
        self.assertNotIn(
            "NIGHTDRY",
            pause["verification"]["preconditions"]["styler.state"],
        )
        self.assertEqual(
            pause["verification"]["postconditions"],
            {"styler.state": ["PAUSE"]},
        )
        self.assertNotIn("verification", controls["resumeCourse"])
        wakeup = controls["wakeup"]
        self.assertEqual(
            wakeup["template"],
            {
                "styler": {
                    "controlDataType": "WAKEUP",
                    "controlDataValueLength": 0,
                }
            },
        )
        self.assertEqual(
            wakeup["verification"]["preconditions"],
            {"styler.state": ["POWEROFF", "SLEEP"]},
        )
        self.assertEqual(
            wakeup["verification"]["postconditions"],
            {"styler.state": ["INITIAL"]},
        )

    def test_no_capture_identity_keys_are_stored(self) -> None:
        raw_text = (CATALOG / "raw_paths.json").read_text().casefold()
        control_text = (CATALOG / "wideq_controls.json").read_text().casefold()
        for key in ('"alias"', '"deviceid"', '"ssid"', '"token"'):
            self.assertNotIn(key, raw_text)
            self.assertNotIn(key, control_text)

    def test_control_catalog_generator_preserves_audited_scope_and_bytes(
        self,
    ) -> None:
        model_dir = (
            ROOT.parent
            / "lg_rethink_local"
            / "local"
            / "state"
            / "model-contract-assets"
            / "models"
        )
        if not model_dir.is_dir():
            self.skipTest("Rethink model assets are not present")
        catalog = CATALOG / "wideq_controls.json"
        with tempfile.TemporaryDirectory() as raw_dir:
            output_root = Path(raw_dir)
            generated = output_root / "wideq_controls.json"
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "build_control_catalog.py"),
                    "--base-catalog",
                    str(catalog),
                    "--output",
                    str(generated),
                    str(model_dir),
                ],
                check=True,
            )
            self.assertEqual(generated.read_bytes(), catalog.read_bytes())

    def test_raw_catalog_generator_preserves_audited_bytes(self) -> None:
        model_dir = (
            ROOT.parent
            / "lg_rethink_local"
            / "local"
            / "state"
            / "model-contract-assets"
            / "models"
        )
        if (
            not model_dir.is_dir()
            or not (ROOT / "lg_pat_dump.json").is_file()
            or not (ROOT / "lg_wideq_dump.json").is_file()
        ):
            self.skipTest("Rethink model assets or audited dumps are not present")
        catalog = CATALOG / "raw_paths.json"
        with tempfile.TemporaryDirectory() as raw_dir:
            generated = Path(raw_dir) / "raw_paths.json"
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "build_raw_catalog.py"),
                    "--output",
                    str(generated),
                    str(model_dir),
                ],
                check=True,
            )
            self.assertEqual(generated.read_bytes(), catalog.read_bytes())

    def test_control_catalog_generator_fails_if_audited_model_disappears(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            base = root / "base.json"
            output = root / "output.json"
            base.write_text(
                json.dumps(
                    {
                        "SYNTHETIC_MODEL": {
                            "controls": {"requiredControl": {}},
                            "subdevices": {},
                        }
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "build_control_catalog.py"),
                    "--base-catalog",
                    str(base),
                    "--output",
                    str(output),
                    str(root),
                ],
                capture_output=True,
                check=False,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("audited model disappeared", result.stderr)

    def test_generator_rejects_changed_verified_control_template(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            output = root / "output.json"
            (root / "ST_R_ETH01Y_.json").write_text(
                json.dumps(
                    {
                        "Value": {},
                        "ControlWifi": {
                            "pauseCourse": {
                                "command": "Set",
                                "dataForm": {
                                    "styler": {
                                        "controlDataType": "PAUSE",
                                        "controlDataValueLength": 1,
                                        "controlDataValue": 1,
                                    }
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "build_control_catalog.py"),
                    "--output",
                    str(output),
                    str(root),
                ],
                capture_output=True,
                check=False,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("verified control template changed", result.stderr)

    def test_generator_rejects_verification_state_outside_model_domain(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            output = root / "output.json"
            (root / "ST_R_ETH01Y_.json").write_text(
                json.dumps(
                    {
                        "Value": {
                            "State": {
                                "type": "enum",
                                "option": {"PAUSE": "Paused"},
                            }
                        },
                        "ControlWifi": {
                            "pauseCourse": {
                                "command": "Set",
                                "dataForm": {
                                    "styler": {
                                        "controlDataType": "PAUSE",
                                        "controlDataValueLength": 1,
                                        "controlDataValue": 0,
                                    }
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "build_control_catalog.py"),
                    "--output",
                    str(output),
                    str(root),
                ],
                capture_output=True,
                check=False,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("verified state values disappeared", result.stderr)

    def test_generator_rejects_changed_vacuum_field_echo_template(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            output = root / "output.json"
            (root / "HWWA9X3C_F2U.json").write_text(
                json.dumps(
                    {
                        "Value": {
                            "aiSuctionForce": {
                                "dataType": "enum",
                                "valueMapping": {
                                    "OFF": {"index": 1},
                                    "ON": {"index": 2},
                                },
                            }
                        },
                        "ControlWifi": {
                            "QMControl_AiSuctionForce": {
                                "command": "Set",
                                "data": {
                                    "qmState": {
                                        "controlDataType": "CHANGED",
                                        "controlDataValueLength": 1,
                                        "aiSuctionForce": "OFF",
                                    }
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "build_control_catalog.py"),
                    "--output",
                    str(output),
                    str(root),
                ],
                capture_output=True,
                check=False,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("verified field-echo template changed", result.stderr)

    def test_reference_generation_uses_control_conversion_not_comments(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            model_dir = root / "models"
            model_dir.mkdir()
            output = root / "output.json"
            (model_dir / "SYNTHETIC.json").write_text(
                json.dumps(
                    {
                        "Value": {
                            "Course": {
                                "type": "Reference",
                                "option": ["Course"],
                            }
                        },
                        "Course": {
                            "24": {"id": 24, "_comment": "DUPLICATE"},
                            "25": {"id": 25, "_comment": "DUPLICATE"},
                        },
                        "ConvertingRule": {
                            "Course": {
                                "ControlConvertingRule": {
                                    "24": "NIGHT_DRY_24",
                                    "25": "SCHOOL_25",
                                }
                            }
                        },
                        "ControlWifi": {
                            "action": {
                                "startCourse": {
                                    "command": "Set",
                                    "dataForm": {
                                        "styler": {"course": "{{Course}}"}
                                    },
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "build_control_catalog.py"),
                    "--output",
                    str(output),
                    str(model_dir),
                ],
                check=True,
            )
            generated = json.loads(output.read_text(encoding="utf-8"))
            field = generated["SYNTHETIC"]["controls"]["startCourse"][
                "fields"
            ]["styler.course"]
            self.assertEqual(field["options"], ["NIGHT_DRY_24", "SCHOOL_25"])
            self.assertNotIn("value_map", field)


if __name__ == "__main__":
    unittest.main()
