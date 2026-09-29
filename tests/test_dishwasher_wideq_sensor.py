"""Dishwasher cloud-only maintenance counter regression tests."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import custom_components.my_lg as my_lg
from homeassistant.components.sensor import SensorStateClass

# Fast unit-test modules can replace the package initializer with a lightweight
# namespace.  Provide the type-only export sensor.py expects in that case.
if not hasattr(my_lg, "MyLgConfigEntry"):
    my_lg.MyLgConfigEntry = object

from custom_components.my_lg.const import DEVICE_TYPE_DISH_WASHER
from custom_components.my_lg.sensor import (
    DISHWASHER_WIDEQ_SENSORS,
    WIDEQ_SENSORS_BY_TYPE,
    WideqDeviceSensor,
)


class FakeDishwasherCoordinator:
    """Minimum PAT identity surface used by WideqDeviceSensor."""

    device_id = "dishwasher-device"
    alias = "Dishwasher"
    model = "D121110"
    device_type = DEVICE_TYPE_DISH_WASHER


class DishwasherWideqSensorTests(unittest.TestCase):
    def test_registers_one_default_enabled_cloud_counter(self) -> None:
        self.assertEqual(
            WIDEQ_SENSORS_BY_TYPE[DEVICE_TYPE_DISH_WASHER],
            DISHWASHER_WIDEQ_SENSORS,
        )
        self.assertEqual(len(DISHWASHER_WIDEQ_SENSORS), 1)
        description = DISHWASHER_WIDEQ_SENSORS[0]
        self.assertEqual(description.key, "dishwasher_tub_clean_count")
        self.assertEqual(description.translation_key, "dishwasher_tub_clean_count")
        self.assertTrue(description.entity_registry_enabled_default)
        self.assertEqual(description.state_class, SensorStateClass.TOTAL_INCREASING)

    def test_reads_exact_reset_transition_and_keeps_zero(self) -> None:
        description = DISHWASHER_WIDEQ_SENSORS[0]
        self.assertEqual(
            description.value_fn({"dishwasher": {"tclCount": 52}}), 52
        )
        self.assertEqual(
            description.value_fn({"dishwasher": {"tclCount": 0}}), 0
        )
        self.assertEqual(
            description.value_fn({"dishwasher": {"tclCount": "0"}}), 0
        )

    def test_rejects_missing_or_invalid_counter_values(self) -> None:
        description = DISHWASHER_WIDEQ_SENSORS[0]
        for snapshot in (
            {},
            {"dishwasher": {}},
            {"dishwasher": {"tclCount": -1}},
            {"dishwasher": {"tclCount": True}},
            {"dishwasher": {"tclCount": "N/A"}},
        ):
            with self.subTest(snapshot=snapshot):
                self.assertIsNone(description.value_fn(snapshot))

    def test_entity_exposes_zero_from_existing_wideq_snapshot(self) -> None:
        wideq = MagicMock()
        wideq.data = {FakeDishwasherCoordinator.device_id: {}}
        wideq.diagnostic_attributes = {}
        wideq.snapshot_for.return_value = {"dishwasher": {"tclCount": 0}}
        entity = WideqDeviceSensor(
            wideq,
            FakeDishwasherCoordinator(),
            DISHWASHER_WIDEQ_SENSORS[0],
        )

        self.assertTrue(entity.available)
        self.assertEqual(entity.native_value, 0)
        self.assertEqual(
            entity.unique_id,
            "dishwasher-device_dishwasher_tub_clean_count",
        )


if __name__ == "__main__":
    unittest.main()
