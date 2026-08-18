"""Fail-closed entity contracts for exact-model WideQ fields."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "custom_components" / "my_lg"
custom = sys.modules.setdefault("custom_components", ModuleType("custom_components"))
custom.__path__ = [str(ROOT / "custom_components")]
my_lg = sys.modules.setdefault(
    "custom_components.my_lg", ModuleType("custom_components.my_lg")
)
my_lg.__path__ = [str(PACKAGE)]

from custom_components.my_lg.wideq_control import (  # noqa: E402
    exact_wideq_field_spec,
    iter_wideq_field_controls,
    verified_wideq_field_spec,
)


class WideqEntityControlTests(unittest.TestCase):
    def test_vacuum_exposes_only_twelve_verified_single_field_controls(
        self,
    ) -> None:
        controls = iter_wideq_field_controls("HWWA9X3C_F2U")
        verified = {
            control.control_name: control.field
            for control in controls
            if control.verification_available
        }

        self.assertEqual(len(controls), 19)
        self.assertEqual(
            verified,
            {
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
            },
        )
        self.assertTrue(all(control.shape == "template" for control in controls))

    def test_unverified_vacuum_control_stays_fail_closed(self) -> None:
        self.assertIsNotNone(
            exact_wideq_field_spec(
                "HWWA9X3C_F2U",
                "QMControl_RemoteUvc",
                "qmState.remoteUvc",
                False,
                shape="template",
            )
        )
        self.assertIsNone(
            verified_wideq_field_spec(
                "HWWA9X3C_F2U",
                "QMControl_RemoteUvc",
                "qmState.remoteUvc",
                False,
                shape="template",
            )
        )

    def test_air_tower_does_not_inherit_type_generic_purifier_control(
        self,
    ) -> None:
        for field in (
            "airState.miscFuncState.airFast",
            "airState.miscFuncState.airUVDisinfection",
            "airState.humidity.desired",
        ):
            self.assertIsNone(
                exact_wideq_field_spec(
                    "AIR_2C0001_WW",
                    "basicCtrl",
                    field,
                    False,
                )
            )
        self.assertEqual(iter_wideq_field_controls("AIR_2C0001_WW"), ())

    def test_template_contract_requires_exact_single_writable_field(self) -> None:
        self.assertIsNone(
            exact_wideq_field_spec(
                "HWWA9X3C_F2U",
                "QMControl_ReserveDustEmptying",
                "qmState.reserveDustEmptying_enable",
                False,
                shape="template",
            )
        )
        self.assertIsNone(
            exact_wideq_field_spec(
                "HWWA9X3C_F2U",
                "QMControl_WaterSupply",
                "qmState.notReal",
                False,
                shape="template",
            )
        )


if __name__ == "__main__":
    unittest.main()
