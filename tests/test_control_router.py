"""Golden tests for the audited WideQ control serializers."""

from __future__ import annotations

import base64
import json
import re
import sys
import unittest
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "custom_components" / "my_lg"
custom = sys.modules.setdefault("custom_components", ModuleType("custom_components"))
custom.__path__ = [str(ROOT / "custom_components")]
my_lg = sys.modules.setdefault("custom_components.my_lg", ModuleType("custom_components.my_lg"))
my_lg.__path__ = [str(PACKAGE)]

from custom_components.my_lg.control_router import (  # noqa: E402
    ControlValidationError,
    build_wideq_request,
    control_readback_verified,
    control_uses_experimental_values,
    control_verification_schedule,
    pat_priority_requested,
    prepare_control_verification,
    remote_control_authorized,
)
from custom_components.my_lg.feature_catalog.wideq import get_wideq_control  # noqa: E402


class WideqControlRouterTests(unittest.TestCase):
    @staticmethod
    def _placeholder_snapshot(spec: dict) -> dict[str, object]:
        """Supply inert values for preservation-only template placeholders."""
        tokens = re.findall(r"\{\{([^{}]+)\}\}", json.dumps(spec["template"]))
        return {token: 0 for token in tokens}

    def test_fresh_wideq_remote_off_overrides_stale_pat_true(self) -> None:
        self.assertFalse(
            remote_control_authorized(
                "ST_R_ETH01Y_",
                pat_data={"remoteControlEnabled": True},
                wideq_snapshot={"styler.remoteStart": "REMOTE_START_OFF"},
            )
        )

    def test_styler_ignores_other_remote_keys_when_exact_gate_is_off(self) -> None:
        self.assertFalse(
            remote_control_authorized(
                "ST_R_ETH01Y_",
                pat_data={"remoteControlEnabled": True},
                wideq_snapshot={
                    "styler.remoteStart": "REMOTE_START_OFF",
                    "remoteControlEnabled": "ON",
                },
            )
        )

    def test_styler_accepts_only_exact_audited_remote_enum(self) -> None:
        for value in (True, 1, "1", "ON", "ENABLE", "UNKNOWN"):
            with self.subTest(value=value):
                self.assertFalse(
                    remote_control_authorized(
                        "ST_R_ETH01Y_",
                        pat_data={"remoteControlEnabled": True},
                        wideq_snapshot={"styler.remoteStart": value},
                    )
                )

    def test_conflicting_styler_snapshot_shapes_fail_closed(self) -> None:
        self.assertFalse(
            remote_control_authorized(
                "ST_R_ETH01Y_",
                pat_data={"remoteControlEnabled": True},
                wideq_snapshot={
                    "styler.remoteStart": "REMOTE_START_ON",
                    "styler": {"remoteStart": "REMOTE_START_OFF"},
                },
            )
        )
        self.assertFalse(
            remote_control_authorized(
                "ST_R_ETH01Y_",
                pat_data={"remoteControlEnabled": True},
                wideq_snapshot={"remoteControlEnabled": "ON"},
            )
        )

    def test_styler_requires_explicit_fresh_wideq_remote_gate(self) -> None:
        self.assertFalse(
            remote_control_authorized(
                "ST_R_ETH01Y_",
                pat_data={"remoteControlEnabled": True},
                wideq_snapshot={"styler": {"state": "INITIAL"}},
            )
        )
        self.assertTrue(
            remote_control_authorized(
                "ST_R_ETH01Y_",
                pat_data={"remoteControlEnabled": False},
                wideq_snapshot={"styler": {"remoteStart": "REMOTE_START_ON"}},
            )
        )

    def test_non_styler_can_fall_back_to_pat_when_wideq_has_no_gate(self) -> None:
        self.assertTrue(
            remote_control_authorized(
                "HWWA9X3C_F2U",
                pat_data={"remote_control_enabled": "ON"},
                wideq_snapshot={"qmState": {"state": "sleep"}},
            )
        )

    def test_conflicting_generic_remote_gates_fail_closed(self) -> None:
        self.assertFalse(
            remote_control_authorized(
                "SYNTHETIC",
                pat_data={"remoteControlEnabled": True},
                wideq_snapshot={
                    "remoteStart": "REMOTE_START_ON",
                    "remoteControlEnabled": "OFF",
                },
            )
        )

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

    def test_styler_start_validates_course_reference_and_reservation_ranges(
        self,
    ) -> None:
        spec = get_wideq_control("ST_R_ETH01Y_", "startCourse")
        snapshot = self._placeholder_snapshot(spec)

        with self.assertRaisesRegex(ControlValidationError, "is not one of"):
            build_wideq_request(
                spec,
                command="Set",
                values={"styler.course": "NOT_A_REAL_COURSE"},
                snapshot=snapshot,
            )

        request = build_wideq_request(
            spec,
            command="Set",
            values={
                "styler.course": "STYLING_SPEED_3",
                "styler.reserveTimeHour": 3,
                "styler.reserveTimeMinute": 30,
            },
            snapshot=snapshot,
        )
        styler = request["payload"]["dataSetList"]["styler"]
        self.assertEqual(styler["course"], "STYLING_SPEED_3")
        self.assertEqual(styler["reserveTimeHour"], 3)
        self.assertEqual(styler["reserveTimeMinute"], 30)

        for field, value in (
            ("styler.reserveTimeHour", 2),
            ("styler.reserveTimeHour", 20),
            ("styler.reserveTimeMinute", -1),
            ("styler.reserveTimeMinute", 60),
        ):
            with self.subTest(field=field, value=value), self.assertRaisesRegex(
                ControlValidationError, "value must be"
            ):
                build_wideq_request(
                    spec,
                    command="Set",
                    values={field: value},
                    snapshot=snapshot,
                )

    def test_reference_uses_authoritative_protocol_value_despite_bad_comment(self) -> None:
        spec = get_wideq_control("ST_R_ETH01Y_", "startCourse")
        for course in ("STYLING_NIGHTDRY_24", "STYLING_SCHOOL_25"):
            with self.subTest(course=course):
                request = build_wideq_request(
                    spec,
                    command="Set",
                    values={"styler.course": course},
                    snapshot=self._placeholder_snapshot(spec),
                )
                self.assertEqual(
                    request["payload"]["dataSetList"]["styler"]["course"],
                    course,
                )

    def test_dishwasher_references_use_protocol_values(self) -> None:
        spec = get_wideq_control("D121110", "downloadCourse")
        request = build_wideq_request(
            spec,
            command="Set",
            values={
                "dishwasher.course": "AUTO",
                "dishwasher.smartCourse": "POTS_PANS",
            },
            snapshot=self._placeholder_snapshot(spec),
        )
        dishwasher = request["payload"]["dataSetList"]["dishwasher"]
        self.assertEqual(dishwasher["course"], "AUTO")
        self.assertEqual(dishwasher["smartCourse"], "POTS_PANS")

    def test_numeric_range_enforces_model_step(self) -> None:
        spec = get_wideq_control("DHUM_056905_WW", "basicCtrl")
        accepted = build_wideq_request(
            spec,
            command="Set",
            values={"airState.humidity.desired": 35},
            snapshot={},
        )
        self.assertEqual(accepted["value"], 35)

        accepted_string = build_wideq_request(
            spec,
            command="Set",
            values={"airState.humidity.desired": "35"},
            snapshot={},
        )
        self.assertEqual(accepted_string["value"], 35)
        self.assertIsInstance(accepted_string["value"], int)

        with self.assertRaisesRegex(ControlValidationError, "align to step 5"):
            build_wideq_request(
                spec,
                command="Set",
                values={"airState.humidity.desired": 31},
                snapshot={},
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

    def test_start_course_is_blocked_without_full_recipe_verification(self) -> None:
        spec = get_wideq_control("ST_R_ETH01Y_", "startCourse")

        with self.assertRaisesRegex(
            ControlValidationError, "no audited.*verification contract"
        ):
            control_verification_schedule(
                spec, {"styler.course": "STYLING_SPEED_3"}
            )

    def test_vacuum_field_echo_accepts_same_value_after_fresh_prestate(self) -> None:
        spec = get_wideq_control(
            "HWWA9X3C_F2U", "QMControl_AiSuctionForce"
        )
        values = {"qmState.aiSuctionForce": "ON"}
        snapshot = {"qmState.aiSuctionForce": "ON"}

        self.assertEqual(control_verification_schedule(spec, values), (5.0, 10.0))
        self.assertEqual(
            prepare_control_verification(spec, values, snapshot),
            (5.0, 10.0),
        )
        self.assertTrue(control_readback_verified(spec, values, snapshot))

    def test_vacuum_field_echo_requires_fresh_field_and_exact_echo(self) -> None:
        spec = get_wideq_control("HWWA9X3C_F2U", "QMControl_WaterSupply")
        values = {"qmState.waterSupply": "HIGH"}

        with self.assertRaisesRegex(
            ControlValidationError, "fresh pre-command state"
        ):
            prepare_control_verification(spec, values, {})
        self.assertFalse(
            control_readback_verified(
                spec, values, {"qmState.waterSupply": "LOW"}
            )
        )

    def test_field_echo_contract_rejects_extra_unverified_field(self) -> None:
        spec = get_wideq_control("HWWA9X3C_F2U", "QMControl_WaterSupply")

        with self.assertRaisesRegex(
            ControlValidationError, "does not cover request field"
        ):
            control_verification_schedule(
                spec,
                {
                    "qmState.waterSupply": "HIGH",
                    "qmState.notAudited": "ON",
                },
            )

    def test_verification_requires_every_requested_field_to_echo(self) -> None:
        spec = get_wideq_control("ST_R_ETH01Y_", "pauseCourse")

        with self.assertRaisesRegex(
            ControlValidationError, "does not cover request field"
        ):
            control_verification_schedule(spec, {"styler.nightDry": "ON"})

    def test_verification_requires_observable_pre_post_transition(self) -> None:
        spec = {
            "verification": {
                "preconditions": {"styler.state": ["INITIAL"]},
                "postconditions": {"styler.state": ["INITIAL"]},
                "required_request_fields": [],
                "echo_fields": [],
                "readback_delays_s": [5, 10],
            }
        }

        with self.assertRaisesRegex(ControlValidationError, "contract is invalid"):
            control_verification_schedule(spec, {})

    def test_verification_offsets_must_be_finite_and_strictly_increasing(
        self,
    ) -> None:
        for delays in ([10, 5], [5, 5], [float("nan")], [float("inf")]):
            spec = {
                "verification": {
                    "preconditions": {"styler.state": ["INITIAL"]},
                    "postconditions": {"styler.state": ["PAUSE"]},
                    "required_request_fields": [],
                    "echo_fields": [],
                    "readback_delays_s": delays,
                }
            }
            with self.subTest(delays=delays), self.assertRaisesRegex(
                ControlValidationError, "contract is invalid"
            ):
                control_verification_schedule(spec, {})

    def test_precondition_and_postcondition_are_separate_fail_closed_gates(
        self,
    ) -> None:
        spec = get_wideq_control("ST_R_ETH01Y_", "pauseCourse")

        with self.assertRaisesRegex(ControlValidationError, "precondition"):
            prepare_control_verification(
                spec, {}, {"styler": {"state": "INITIAL"}}
            )
        self.assertFalse(
            control_readback_verified(
                spec, {}, {"styler": {"state": "DRYING"}}
            )
        )
        self.assertTrue(
            control_readback_verified(
                spec, {}, {"styler": {"state": "PAUSE"}}
            )
        )


if __name__ == "__main__":
    unittest.main()
