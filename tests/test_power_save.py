"""Tests for verified AC power-save snapshot interpretation."""

from __future__ import annotations

from pathlib import Path
import sys
from types import ModuleType
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "custom_components" / "my_lg"

custom = sys.modules.setdefault("custom_components", ModuleType("custom_components"))
custom.__path__ = [str(ROOT / "custom_components")]
my_lg = sys.modules.setdefault("custom_components.my_lg", ModuleType("custom_components.my_lg"))
my_lg.__path__ = [str(PACKAGE)]

from custom_components.my_lg.power_save import (  # noqa: E402
    ac_power_save_attributes,
    ac_power_save_mode,
    local_comfort_power_save_configured,
)


class AcPowerSaveTests(unittest.TestCase):
    @staticmethod
    def _local_provider(*, mode: str = "preferred", healthy: bool = True):
        return SimpleNamespace(
            mode=mode,
            profile_id="cst570-core-state-v1",
            model_id="CST_570004_WW",
            snapshot_schema_version=3,
            profile=SimpleNamespace(
                availability_policy="attested-session",
                fields={
                    "comfort_energy_saving.enabled": SimpleNamespace(
                        value_type="boolean",
                        exposure="state",
                    )
                },
            ),
            shadow_healthy=healthy,
        )

    def test_local_listener_gate_is_static_and_preferred_only(self) -> None:
        provider = self._local_provider(healthy=False)
        self.assertTrue(
            local_comfort_power_save_configured(provider, "CST_570004_WW")
        )

        provider.mode = "shadow"
        self.assertFalse(
            local_comfort_power_save_configured(provider, "CST_570004_WW")
        )
        provider.mode = "preferred"
        provider.snapshot_schema_version = 2
        self.assertFalse(
            local_comfort_power_save_configured(provider, "CST_570004_WW")
        )
        provider.snapshot_schema_version = 3
        self.assertFalse(
            local_comfort_power_save_configured(provider, "OTHER_MODEL")
        )

    def test_reports_comfortable_power_save(self) -> None:
        snapshot = {
            "airState.powerSave.basic": 0.0,
            "airState.powerSave.hum": 1.0,
        }

        self.assertEqual(ac_power_save_mode(snapshot), "comfortable")
        self.assertEqual(
            ac_power_save_attributes(snapshot)["comfortable_power_save"], True
        )

    def test_reports_general_and_dehumidification_modes(self) -> None:
        self.assertEqual(
            ac_power_save_mode({"airState.powerSave.basic": "1"}), "general"
        )
        self.assertEqual(
            ac_power_save_mode({"airState.powerSave.dry": True}),
            "dehumidification",
        )

    def test_multiple_flags_are_not_hidden(self) -> None:
        snapshot = {
            "airState.powerSave.basic": 1,
            "airState.powerSave.hum": 1,
            "airState.powerSave.dry": 0,
        }

        self.assertEqual(ac_power_save_mode(snapshot), "mixed")

    def test_unknown_snapshot_is_not_reported_as_off(self) -> None:
        self.assertIsNone(ac_power_save_mode({}))

    def test_percentage_stage_is_never_invented(self) -> None:
        attributes = ac_power_save_attributes(
            {"airState.powerSave.basic": 0, "airState.powerSave.hum": 0}
        )

        self.assertEqual(ac_power_save_mode({
            "airState.powerSave.basic": 0,
            "airState.powerSave.hum": 0,
        }), "off")
        self.assertFalse(attributes["percentage_level_supported"])
