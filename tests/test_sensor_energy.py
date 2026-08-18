"""Energy entity contract regression tests."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from homeassistant.components.sensor import SensorStateClass

import custom_components.my_lg as my_lg

# Several fast unit-test modules intentionally replace the package initializer
# with a lightweight namespace.  Provide the type-only export sensor.py expects
# without importing the real integration setup path.
if not hasattr(my_lg, "MyLgConfigEntry"):
    my_lg.MyLgConfigEntry = object

from custom_components.my_lg.const import DEVICE_TYPE_AIR_CONDITIONER
from custom_components.my_lg.sensor import (
    WASHTOWER_SENSORS,
    WIDEQ_AC_SENSORS,
    WideqDeviceSensor,
)


class FakePatCoordinator:
    """Minimum PAT coordinator surface used by WideqDeviceSensor."""

    device_id = "ac-device"
    alias = "Living AC"
    model = "MODEL"
    device_type = DEVICE_TYPE_AIR_CONDITIONER

    def __init__(self, power: str) -> None:
        self.power = power

    def get(self, group: str, key: str):
        if (group, key) == ("operation", "airConOperationMode"):
            return self.power
        return None

    def async_add_listener(self, listener):
        return lambda: None


class EnergyEntityContractTests(unittest.TestCase):
    def test_cycle_energy_is_not_total_increasing(self) -> None:
        energy_descriptions = [
            description
            for description in WASHTOWER_SENSORS
            if description.key in {"washer_energy", "dryer_energy"}
        ]

        self.assertEqual(len(energy_descriptions), 2)
        self.assertTrue(
            all(description.state_class is None for description in energy_descriptions)
        )

    def test_period_energy_remains_total_increasing(self) -> None:
        period_descriptions = [
            description
            for description in WIDEQ_AC_SENSORS
            if description.key in {"energy_today", "energy_month"}
        ]

        self.assertTrue(
            all(
                description.state_class == SensorStateClass.TOTAL_INCREASING
                for description in period_descriptions
            )
        )

    def test_pat_confirmed_ac_power_off_reports_zero_without_snapshot(self) -> None:
        wideq = MagicMock()
        wideq.data = {}
        wideq.diagnostic_attributes = {}
        wideq.snapshot_for.return_value = {}
        description = next(
            item for item in WIDEQ_AC_SENSORS if item.key == "energy_current"
        )
        entity = WideqDeviceSensor(
            wideq,
            FakePatCoordinator("POWER_OFF"),
            description,
        )

        self.assertTrue(entity.available)
        self.assertEqual(entity.native_value, 0.0)
        self.assertEqual(
            entity.extra_state_attributes["power_source"],
            "pat_confirmed_power_off",
        )

    def test_power_save_summary_prefers_attested_local_comfort_state(self) -> None:
        wideq = MagicMock()
        wideq.data = {}
        wideq.diagnostic_attributes = {}
        wideq.power_save_snapshot_for.return_value = {
            "airState.powerSave.basic": False,
            "airState.powerSave.hum": False,
        }
        wideq.power_save_available.return_value = True
        wideq.power_save_diagnostic_attributes.return_value = {}
        pat = FakePatCoordinator("POWER_ON")
        pat.model = "CST_570004_WW"
        local = SimpleNamespace(
            mode="preferred",
            profile_id="cst570-core-state-v1",
            model_id=pat.model,
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
            shadow_healthy=True,
            field_value=lambda semantic_id: (
                True
                if semantic_id == "comfort_energy_saving.enabled"
                else None
            ),
        )
        description = next(
            item for item in WIDEQ_AC_SENSORS if item.key == "power_save_mode"
        )
        entity = WideqDeviceSensor(
            wideq,
            pat,
            description,
            local_provider=local,
        )

        self.assertEqual(entity.native_value, "comfortable")
        self.assertEqual(
            entity.extra_state_attributes["comfortable_power_save_provider"],
            "local",
        )
        self.assertEqual(
            entity.extra_state_attributes["power_save_source"],
            "mixed_local_wideq",
        )

        local.shadow_healthy = False
        self.assertEqual(entity.native_value, "off")
        self.assertEqual(
            entity.extra_state_attributes["comfortable_power_save_provider"],
            "wideq",
        )
        self.assertEqual(
            entity.extra_state_attributes["power_save_source"],
            "wideq_snapshot",
        )


if __name__ == "__main__":
    unittest.main()
