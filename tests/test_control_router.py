"""Golden tests for the audited WideQ control serializers."""

from __future__ import annotations

import base64
from pathlib import Path
import sys
from types import ModuleType
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "custom_components" / "my_lg"
custom = sys.modules.setdefault("custom_components", ModuleType("custom_components"))
custom.__path__ = [str(ROOT / "custom_components")]
my_lg = sys.modules.setdefault("custom_components.my_lg", ModuleType("custom_components.my_lg"))
my_lg.__path__ = [str(PACKAGE)]

from custom_components.my_lg.control_router import (  # noqa: E402
    ControlValidationError,
    build_wideq_request,
    control_uses_experimental_values,
    pat_priority_requested,
)
from custom_components.my_lg.feature_catalog.wideq import get_wideq_control  # noqa: E402


class WideqControlRouterTests(unittest.TestCase):
    def test_ac_data_key_validates_model_enum(self) -> None:
        spec = get_wideq_control("CST_170004_WW", "settingInfo")
        request = build_wideq_request(
            spec,
            command="Set",
            values={"airState.miscFuncState.autoDry": 255},
            snapshot={},
        )
        self.assertEqual(
            request,
            {
                "command": "Set",
                "data_key": "airState.miscFuncState.autoDry",
                "value": 255,
            },
        )

    def test_ac_rejects_unknown_field_before_network(self) -> None:
        spec = get_wideq_control("CST_170004_WW", "settingInfo")
        with self.assertRaises(ControlValidationError):
            build_wideq_request(
                spec,
                command="Set",
                values={"airState.notReal": 1},
                snapshot={},
            )

    def test_dataset_shape_uses_only_requested_fields(self) -> None:
        spec = get_wideq_control("CST_170004_WW", "wModeCtrl")
        request = build_wideq_request(
            spec,
            command="Set",
            values={"airState.wMode.jet": 1},
            snapshot={},
        )
        self.assertEqual(
            request["data_set_list"], {"airState.wMode.jet": 1}
        )

    def test_template_preserves_current_fields_and_applies_override(self) -> None:
        spec = {
            "ctrl_key": "basicCtrl",
            "shape": "template",
            "commands": ["Set"],
            "template": {
                "refState": {
                    "fridgeTemp": "{{fridgeTemp}}",
                    "fridgeDoorOpen": "{{fridgeDoorOpen}}",
                }
            },
        }
        request = build_wideq_request(
            spec,
            command="Set",
            values={"refState.fridgeTemp": 4},
            snapshot={"refState": {"fridgeTemp": 3, "fridgeDoorOpen": 0}},
        )
        self.assertEqual(
            request["payload"]["dataSetList"],
            {"refState": {"fridgeTemp": 4, "fridgeDoorOpen": 0}},
        )

    def test_template_matches_legacy_underscore_placeholder(self) -> None:
        spec = {
            "ctrl_key": "startCourse",
            "shape": "template",
            "commands": ["Set"],
            "template": {"styler": {"cooling1FanRPM": "{{Cooling1_Fan_RPM}}"}},
        }
        request = build_wideq_request(
            spec,
            command="Set",
            values={},
            snapshot={"styler": {"cooling1FanRPM": 1200}},
        )
        self.assertEqual(
            request["payload"]["dataSetList"],
            {"styler": {"cooling1FanRPM": 1200}},
        )

    def test_thinq1_binary_refuses_write_without_snapshot(self) -> None:
        spec = get_wideq_control("2REK1D04AR170", "SetControl")
        with self.assertRaisesRegex(ControlValidationError, "current full snapshot"):
            build_wideq_request(
                spec, command=None, values={}, snapshot={}
            )

    def test_thinq1_binary_preserves_all_fields_and_changes_one(self) -> None:
        spec = get_wideq_control("2REK1D04AR170", "SetControl")
        snapshot = {
            "LeftOrTopRoom": 4,
            "RightRoom": 4,
            "MiddleRoom": 1,
            "BottomRoom": 1,
            "FreshAirFilter": 255,
            "OneTouchFilter": 0,
            "LockingStatus": 1,
            "DoorOpenState": 0,
        }
        request = build_wideq_request(
            spec,
            command=None,
            values={"OneTouchFilter": 1},
            snapshot=snapshot,
        )
        decoded = base64.b64decode(request["legacy_payload"]["data"])
        self.assertEqual(list(decoded), [4, 4, 1, 1, 255, 1, 1, 0])

    def test_thinq1_binary_rejects_preservation_only_override(self) -> None:
        spec = get_wideq_control("2REK1D04AR170", "SetControl")
        snapshot = {field: 0 for field in spec["fields"]}
        with self.assertRaisesRegex(ControlValidationError, "preservation-only"):
            build_wideq_request(
                spec,
                command=None,
                values={"DoorOpenState": 1},
                snapshot=snapshot,
            )

    def test_washtower_subdevice_catalog_is_separate(self) -> None:
        self.assertIsNotNone(
            get_wideq_control("WTL_KPK_BDH_KR_01", "WMDownload", "washer")
        )
        self.assertIsNone(
            get_wideq_control("WTL_KPK_BDH_KR_01", "WMDownload")
        )

    def test_composite_command_requires_explicit_data(self) -> None:
        spec = get_wideq_control("D121110", "setOption")
        with self.assertRaisesRegex(ControlValidationError, "explicit data"):
            build_wideq_request(spec, command=None, values={}, snapshot={})

    def test_composite_enum_is_validated_before_network(self) -> None:
        spec = get_wideq_control("D121110", "setOption")
        with self.assertRaisesRegex(ControlValidationError, "is not one of"):
            build_wideq_request(
                spec,
                command=None,
                values={"rinseLevel": "LEVEL_99"},
                snapshot={
                    "dishwasher": {
                        "MCReminderSetting": "OFF",
                        "RinseLevel": "LEVEL_1",
                        "SignalLevel": "LEVEL_ON",
                        "SofteningLevel": "LEVEL_1",
                    }
                },
            )

    def test_parameterless_action_rejects_payload_override(self) -> None:
        spec = get_wideq_control("WBEF3", "setCookStop")
        with self.assertRaisesRegex(ControlValidationError, "fixed"):
            build_wideq_request(
                spec,
                command=None,
                values={"cooktopPowerOff": "anything"},
                snapshot={},
            )

    def test_unverified_composite_field_requires_experimental_option(self) -> None:
        spec = get_wideq_control("WMLJ32RS", "SetPreference")
        self.assertTrue(
            control_uses_experimental_values(
                spec, {"mwoSettingClockSetTimeHour": 12}
            )
        )

    def test_pat_priority_field_is_identified_for_raw_service(self) -> None:
        spec = get_wideq_control("2REFO1DBN3K_U", "basicCtrl")
        self.assertEqual(
            pat_priority_requested(spec, {"fridgeTemp": 4}),
            {"refState.fridgeTemp"},
        )


if __name__ == "__main__":
    unittest.main()
