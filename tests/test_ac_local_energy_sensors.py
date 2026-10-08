"""Exact Local AC measurement and derived-energy sensor contracts."""

from __future__ import annotations

import math
import unittest
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorExtraStoredData,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy

from custom_components.my_lg import sensor
from custom_components.my_lg.const import DEVICE_TYPE_AIR_CONDITIONER, DOMAIN
from custom_components.my_lg.local_entity import local_semantic_unique_id
from custom_components.my_lg.local_read_provider import (
    TlvReadFieldContract,
    TlvReadValue,
    load_tlv_read_catalogue,
)

NOW = datetime(2026, 8, 24, 1, 0, tzinfo=timezone.utc)
MODEL = "CST_170004_WW"


class FakePatCoordinator:
    """Minimum PAT owner with deliberately different fallback readings."""

    def __init__(self, device_id: str = "test-ac") -> None:
        self.device_id = device_id
        self.device_type = DEVICE_TYPE_AIR_CONDITIONER
        self.alias = "Test AC"
        self.model = MODEL
        self.data: dict[str, Any] = {
            "temperature": {"currentTemperature": 27.0},
            "airQualitySensor": {"humidity": 61.0},
        }
        self.profile = {
            "property": {
                "temperature": {"currentTemperature": {}},
                "airQualitySensor": {"humidity": {}},
            }
        }
        self.listeners: list[Callable[[], None]] = []

    def get(self, *path: str, default: Any = None) -> Any:
        node: Any = self.data
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def supports(self, group: str) -> bool:
        return group in self.profile["property"]

    def async_add_listener(
        self, callback: Callable[[], None], *_args: object
    ) -> Callable[[], None]:
        self.listeners.append(callback)

        def remove() -> None:
            if callback in self.listeners:
                self.listeners.remove(callback)

        return remove


class FakeReadProvider:
    """Observable exact-profile provider with publication boundaries."""

    def __init__(self, contracts: tuple[TlvReadFieldContract, ...]) -> None:
        self.profile = SimpleNamespace(
            profile_id=f"{MODEL}:read-sensors-v1",
            fields=contracts,
            fields_by_semantic_id={item.semantic_id: item for item in contracts},
        )
        self.fields: dict[str, TlvReadValue] = {}
        self.available: set[str] = set()
        self.current_published_at: datetime | None = None
        self.listeners: list[Callable[[], None]] = []

    def field_available(self, semantic_id: str) -> bool:
        return semantic_id in self.available and semantic_id in self.fields

    def field_value(self, semantic_id: str) -> object | None:
        field = self.fields.get(semantic_id)
        return None if field is None else field.value

    def display_field(self, semantic_id: str) -> TlvReadValue | None:
        return self.fields.get(semantic_id) if self.field_available(semantic_id) else None

    def display_field_available(self, semantic_id: str) -> bool:
        return self.display_field(semantic_id) is not None

    def async_add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        self.listeners.append(callback)

        def remove() -> None:
            if callback in self.listeners:
                self.listeners.remove(callback)

        return remove

    def publish(
        self,
        semantic_id: str,
        value: object,
        published_at: datetime,
        *,
        available: bool = True,
        observed_at: datetime | None = None,
    ) -> None:
        contract = self.profile.fields_by_semantic_id[semantic_id]
        self.current_published_at = published_at
        self.fields[semantic_id] = TlvReadValue(
            value=value,  # type: ignore[arg-type]
            value_type=(
                "number"
                if isinstance(value, (int, float)) and not isinstance(value, bool)
                else "string"
            ),
            observed_at=observed_at or published_at,
            confidence="confirmed-exact-device",
            exposure=contract.exposure,
            unit=contract.unit,
        )
        if available:
            self.available.add(semantic_id)
        else:
            self.available.discard(semantic_id)
        self.notify()

    def tombstone(self) -> None:
        self.fields.clear()
        self.available.clear()
        self.current_published_at = None
        self.notify()

    def notify(self) -> None:
        for callback in tuple(self.listeners):
            callback()


def contract(semantic_id: str) -> TlvReadFieldContract:
    return load_tlv_read_catalogue()[MODEL].fields_by_semantic_id[semantic_id]


class TlvMeasurementMetadataTests(unittest.TestCase):
    def test_particulate_unit_and_device_classes_are_cross_version_stable(self) -> None:
        particulate = [
            description
            for description in (
                *sensor.AIR_PURIFIER_SENSORS,
                *sensor.HUMIDIFIER_SENSORS,
            )
            if description.key in {"pm1", "pm2_5", "pm10"}
        ]

        self.assertEqual(len(particulate), 6)
        self.assertTrue(
            all(
                description.native_unit_of_measurement
                == sensor.UnitOfDensity.MICROGRAMS_PER_CUBIC_METER
                for description in particulate
            )
        )
        self.assertIn(
            str(sensor.UnitOfDensity.MICROGRAMS_PER_CUBIC_METER),
            {"µg/m³", "μg/m³"},
        )
        self.assertEqual(
            {description.device_class for description in particulate},
            {SensorDeviceClass.PM1, SensorDeviceClass.PM25, SensorDeviceClass.PM10},
        )

    def test_exact_measurements_get_home_assistant_metadata(self) -> None:
        cases = {
            "power.indoor_compressor_share_w": (
                SensorDeviceClass.POWER,
                SensorStateClass.MEASUREMENT,
            ),
            "power.outdoor_unit_total_w": (
                SensorDeviceClass.POWER,
                SensorStateClass.MEASUREMENT,
            ),
            "temperature.current_c": (
                SensorDeviceClass.TEMPERATURE,
                SensorStateClass.MEASUREMENT,
            ),
            "humidity.current_pct": (
                SensorDeviceClass.HUMIDITY,
                SensorStateClass.MEASUREMENT,
            ),
            "air_quality.pm1_ug_m3": (
                SensorDeviceClass.PM1,
                SensorStateClass.MEASUREMENT,
            ),
            "air_quality.pm2_5_ug_m3": (
                SensorDeviceClass.PM25,
                SensorStateClass.MEASUREMENT,
            ),
            "air_quality.pm10_ug_m3": (
                SensorDeviceClass.PM10,
                SensorStateClass.MEASUREMENT,
            ),
            "auto_dry.remaining_min": (
                SensorDeviceClass.DURATION,
                SensorStateClass.MEASUREMENT,
            ),
            "filter.remaining_h": (
                SensorDeviceClass.DURATION,
                SensorStateClass.MEASUREMENT,
            ),
        }
        coordinator = FakePatCoordinator()
        for semantic_id, expected in cases.items():
            with self.subTest(semantic_id=semantic_id):
                owned_contract = contract(semantic_id)
                provider = FakeReadProvider((owned_contract,))
                entity = sensor.TlvReadSensor(
                    provider,  # type: ignore[arg-type]
                    coordinator,  # type: ignore[arg-type]
                    semantic_id,
                    owned_contract,
                )
                self.assertEqual((entity.device_class, entity.state_class), expected)

    def test_all_profile_power_and_energy_contracts_are_accounted_exactly(self) -> None:
        catalogue = load_tlv_read_catalogue()
        rows = [
            contract
            for profile in catalogue.values()
            for contract in profile.fields
            if contract.semantic_id.startswith(("power.", "energy."))
            or contract.semantic_id.endswith(("_w", "_wh"))
        ]

        self.assertEqual(len(rows), 10)
        self.assertEqual(
            {
                semantic_id: sum(item.semantic_id == semantic_id for item in rows)
                for semantic_id in {item.semantic_id for item in rows}
            },
            {
                "power.indoor_compressor_share_w": 2,
                "power.outdoor_unit_total_w": 2,
                "energy.interval.delta_wh": 4,
                "washer.cycle.energy_wh": 1,
                "dryer.cycle.energy_wh": 1,
            },
        )

        instantaneous = [item for item in rows if item.unit == "W"]
        cycle_energy = [
            item for item in rows if item.domain == "sensor" and item.unit == "Wh"
        ]
        interval_events = [item for item in rows if item.domain == "event"]
        self.assertEqual(len(instantaneous), 4)
        self.assertTrue(
            all(
                sensor._tlv_sensor_metadata(item)
                == (SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT)
                for item in instantaneous
            )
        )
        self.assertEqual(len(cycle_energy), 2)
        self.assertTrue(
            all(
                sensor._tlv_sensor_metadata(item) == (SensorDeviceClass.ENERGY, None)
                for item in cycle_energy
            )
        )
        self.assertEqual(len(interval_events), 4)
        self.assertTrue(
            all(
                item.semantic_id == "energy.interval.delta_wh"
                and item.unit == "Wh"
                and item.publication_mode == "transient-event"
                for item in interval_events
            )
        )


class AcReportedEnergyReferenceTests(unittest.TestCase):
    def make_entity(self, model: str):
        owner = FakePatCoordinator()
        owner.model = model
        provider = SimpleNamespace(
            semantic_ids=("energy.total_wh",),
            field_available=lambda _: True,
            total_wh=lambda _: 300,
            baseline_generation=1,
            last_counted_generation=2,
            published_at=NOW,
        )
        return sensor.LocalCumulativeEnergySensor(provider, owner, "energy.total_wh")

    def test_both_ac_native_wh_sensors_are_opt_in_references(self):
        for model in ("CST_170004_WW", "CST_570004_WW"):
            with self.subTest(model=model):
                entity = self.make_entity(model)
                self.assertEqual(entity.native_value, 0.3)
                self.assertTrue(entity.available)
                self.assertEqual(entity.entity_category.value, "diagnostic")
                self.assertFalse(entity.entity_registry_enabled_default)
                self.assertIsNone(entity.state_class)
                self.assertIn("참고", entity.name)
                self.assertTrue(entity.extra_state_attributes["excluded_from_official_energy"])

    def test_other_appliance_cumulative_energy_is_unchanged(self):
        entity = self.make_entity("WBEF3")
        self.assertEqual(entity.state_class, SensorStateClass.TOTAL_INCREASING)
        self.assertTrue(entity.entity_registry_enabled_default)
        self.assertIsNone(entity.entity_category)
        self.assertNotIn("excluded_from_official_energy", entity.extra_state_attributes)


class TlvIntegratedAcEnergyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.enterContext(patch("custom_components.my_lg.sensor.dt_util.utcnow", return_value=NOW))

    def make_entity(
        self, semantic_id: str = "power.indoor_compressor_share_w"
    ) -> tuple[FakeReadProvider, sensor.TlvIntegratedEnergySensor]:
        owned_contract = contract(semantic_id)
        provider = FakeReadProvider((owned_contract,))
        entity = sensor.TlvIntegratedEnergySensor(
            provider,  # type: ignore[arg-type]
            FakePatCoordinator(),  # type: ignore[arg-type]
            semantic_id,
            owned_contract,
        )
        entity.async_write_ha_state = Mock()
        return provider, entity

    def test_energy_contract_identity_and_scope_are_explicit(self) -> None:
        indoor_provider, indoor = self.make_entity()
        outdoor_provider, outdoor = self.make_entity("power.outdoor_unit_total_w")
        del indoor_provider, outdoor_provider

        for entity in (indoor, outdoor):
            self.assertIsInstance(entity, RestoreSensor)
            self.assertEqual(entity.device_class, SensorDeviceClass.ENERGY)
            self.assertEqual(
                entity.native_unit_of_measurement, UnitOfEnergy.KILO_WATT_HOUR
            )
            self.assertEqual(entity.state_class, SensorStateClass.TOTAL_INCREASING)
            self.assertEqual(entity.device_info["identifiers"], {(DOMAIN, "test-ac")})
            self.assertLessEqual(len(entity.unique_id), 128)

        self.assertNotEqual(indoor.unique_id, outdoor.unique_id)
        self.assertTrue(indoor.entity_registry_enabled_default)
        self.assertFalse(outdoor.entity_registry_enabled_default)
        self.assertIn("중복 합산 금지", outdoor.name)
        self.assertEqual(
            indoor.extra_state_attributes["integration_clock"],
            "source_field_observed_at",
        )
        self.assertFalse(
            indoor.extra_state_attributes["may_duplicate_across_indoor_bindings"]
        )
        self.assertTrue(
            outdoor.extra_state_attributes["may_duplicate_across_indoor_bindings"]
        )
        self.assertIn("shared outdoor", outdoor.extra_state_attributes["scope_warning"])

    async def test_uses_only_contiguous_authenticated_publication_boundaries(
        self,
    ) -> None:
        provider, entity = self.make_entity()
        entity.async_get_last_sensor_data = AsyncMock(return_value=None)
        provider.publish(entity.source_semantic_id, 1000, NOW)
        await entity.async_added_to_hass()

        # One minute at a constant 1 kW is exactly 1/60 kWh.
        provider.publish(entity.source_semantic_id, 1000, NOW + timedelta(minutes=1))
        self.assertAlmostEqual(entity.native_value, 1 / 60, places=9)

        # A duplicate authenticated boundary never adds time twice.
        provider.publish(entity.source_semantic_id, 1000, NOW + timedelta(minutes=1))
        self.assertAlmostEqual(entity.native_value, 1 / 60, places=9)

        # A tombstone breaks continuity. Reappearance only establishes a new anchor.
        provider.tombstone()
        provider.publish(entity.source_semantic_id, 500, NOW + timedelta(minutes=2))
        self.assertAlmostEqual(entity.native_value, 1 / 60, places=9)
        provider.publish(entity.source_semantic_id, 500, NOW + timedelta(minutes=3))
        self.assertAlmostEqual(entity.native_value, 1 / 60 + 0.5 / 60, places=9)

        # A gap beyond the explicit cap is skipped, then normal integration resumes.
        after_gap = NOW + sensor.MAX_TLV_POWER_INTEGRATION_GAP + timedelta(minutes=4)
        provider.publish(entity.source_semantic_id, 500, after_gap)
        self.assertAlmostEqual(entity.native_value, 1 / 60 + 0.5 / 60, places=9)
        provider.publish(
            entity.source_semantic_id, 500, after_gap + timedelta(minutes=1)
        )
        self.assertAlmostEqual(entity.native_value, 1 / 60 + 1 / 60, places=9)

        # A regressed publication clock breaks two-sided continuity rather than
        # integrating an overlapping interval.
        provider.publish(
            entity.source_semantic_id, 500, after_gap - timedelta(minutes=1)
        )
        provider.publish(
            entity.source_semantic_id, 500, after_gap + timedelta(minutes=2)
        )
        self.assertAlmostEqual(entity.native_value, 1 / 60 + 1 / 60, places=9)
        provider.publish(
            entity.source_semantic_id, 500, after_gap + timedelta(minutes=3)
        )
        self.assertAlmostEqual(entity.native_value, 1 / 60 + 1.5 / 60, places=9)

        await entity.async_will_remove_from_hass()
        self.assertEqual(provider.listeners, [])

    async def test_carried_or_stale_field_never_uses_current_publication_as_clock(
        self,
    ) -> None:
        provider, entity = self.make_entity()
        entity.async_get_last_sensor_data = AsyncMock(return_value=None)
        provider.publish(entity.source_semantic_id, 1000, NOW)
        await entity.async_added_to_hass()

        # A newer accepted current can carry the exact same old power sample.
        # Its publication boundary is authenticated, but contributes no energy.
        provider.publish(
            entity.source_semantic_id,
            1000,
            NOW + timedelta(minutes=1),
            observed_at=NOW,
        )
        self.assertEqual(entity.native_value, 0)

        # Only a genuinely newer field observation advances the integral.
        provider.publish(
            entity.source_semantic_id,
            1000,
            NOW + timedelta(minutes=2),
            observed_at=NOW + timedelta(minutes=2),
        )
        self.assertAlmostEqual(entity.native_value, 2 / 60, places=9)

        # A field carried beyond the freshness cap breaks continuity. The next
        # fresh observation becomes a new anchor instead of spanning the gap.
        provider.publish(
            entity.source_semantic_id,
            1000,
            NOW + sensor.MAX_TLV_POWER_INTEGRATION_GAP + timedelta(minutes=3),
            observed_at=NOW + timedelta(minutes=2),
        )
        self.assertTrue(entity.available)
        self.assertAlmostEqual(entity.native_value, 2 / 60, places=9)
        self.assertEqual(
            entity.extra_state_attributes["integration_status"],
            "source_unavailable_or_invalid",
        )
        fresh = NOW + sensor.MAX_TLV_POWER_INTEGRATION_GAP + timedelta(minutes=4)
        provider.publish(entity.source_semantic_id, 1000, fresh, observed_at=fresh)
        self.assertAlmostEqual(entity.native_value, 2 / 60, places=9)

    async def test_unavailable_or_invalid_power_breaks_continuity(self) -> None:
        provider, entity = self.make_entity()
        entity.async_get_last_sensor_data = AsyncMock(return_value=None)
        provider.publish(entity.source_semantic_id, 1000, NOW)
        await entity.async_added_to_hass()

        provider.publish(
            entity.source_semantic_id,
            1000,
            NOW + timedelta(minutes=1),
            available=False,
        )
        self.assertTrue(entity.available)
        provider.publish(entity.source_semantic_id, 1000, NOW + timedelta(minutes=2))
        self.assertEqual(entity.native_value, 0)
        provider.publish(entity.source_semantic_id, -1, NOW + timedelta(minutes=3))
        provider.publish(entity.source_semantic_id, 1000, NOW + timedelta(minutes=4))
        self.assertEqual(entity.native_value, 0)
        provider.publish(entity.source_semantic_id, 1000, NOW + timedelta(minutes=5))
        self.assertAlmostEqual(entity.native_value, 1 / 60, places=9)

    async def test_restores_total_but_never_restores_a_power_time_anchor(self) -> None:
        provider, entity = self.make_entity()
        entity.async_get_last_sensor_data = AsyncMock(
            return_value=SensorExtraStoredData(12.5, UnitOfEnergy.KILO_WATT_HOUR)
        )
        provider.publish(entity.source_semantic_id, 1000, NOW)
        await entity.async_added_to_hass()

        self.assertEqual(entity.native_value, 12.5)
        provider.publish(entity.source_semantic_id, 1000, NOW + timedelta(minutes=1))
        self.assertAlmostEqual(entity.native_value, 12.5 + 1 / 60, places=9)

        # Invalid, negative, non-finite, wrong-unit and boolean restores all
        # fail closed without exposing a false zero reset. A fresh authenticated
        # sample then establishes a new zero baseline and recovers availability.
        bad_values = (-1, math.inf, "12.5", True)
        for restored in bad_values:
            with self.subTest(restored=restored):
                other_provider, other = self.make_entity()
                other.async_get_last_sensor_data = AsyncMock(
                    return_value=SensorExtraStoredData(
                        restored,  # type: ignore[arg-type]
                        UnitOfEnergy.KILO_WATT_HOUR,
                    )
                )
                await other.async_added_to_hass()
                self.assertEqual(other.native_value, 0)
                self.assertFalse(other.available)
                self.assertEqual(
                    other.extra_state_attributes["integration_status"],
                    "restore_rejected",
                )
                other_provider.publish(other.source_semantic_id, 1000, NOW + timedelta(seconds=1))
                self.assertTrue(other.available)
                self.assertEqual(other.native_value, 0)
                self.assertEqual(
                    other.extra_state_attributes["integration_status"], "anchored"
                )

    async def test_new_meter_exposes_zero_without_a_current_power_sample(self) -> None:
        _provider, entity = self.make_entity()
        entity.async_get_last_sensor_data = AsyncMock(return_value=None)

        await entity.async_added_to_hass()

        self.assertTrue(entity.available)
        self.assertEqual(entity.native_value, 0)
        self.assertEqual(
            entity.extra_state_attributes["integration_status"],
            "source_unavailable_or_invalid",
        )

    async def test_retained_pre_disconnect_sample_cannot_reopen_the_gap(self) -> None:
        provider, entity = self.make_entity()
        entity.async_get_last_sensor_data = AsyncMock(return_value=None)
        provider.publish(entity.source_semantic_id, 1000, NOW)
        await entity.async_added_to_hass()
        provider.publish(entity.source_semantic_id, 1000, NOW + timedelta(minutes=1))
        provider.publish(entity.source_semantic_id, 1000, NOW + timedelta(seconds=65), available=False)
        provider.publish(entity.source_semantic_id, 1000, NOW + timedelta(minutes=2), observed_at=NOW + timedelta(minutes=1))
        provider.publish(entity.source_semantic_id, 1000, NOW + timedelta(minutes=10))
        self.assertAlmostEqual(entity.native_value, 1 / 60)
        provider.publish(entity.source_semantic_id, 1000, NOW + timedelta(minutes=11))
        self.assertAlmostEqual(entity.native_value, 2 / 60)

    async def test_multiple_regressed_samples_never_recount_a_counted_interval(self) -> None:
        provider, entity = self.make_entity()
        entity.async_get_last_sensor_data = AsyncMock(return_value=None)
        provider.publish(entity.source_semantic_id, 1000, NOW)
        await entity.async_added_to_hass()
        for seconds in (60, 30, 40, 60, 90):
            provider.publish(entity.source_semantic_id, 1000, NOW + timedelta(seconds=seconds))
        self.assertAlmostEqual(entity.native_value, 1 / 60)
        provider.publish(entity.source_semantic_id, 1000, NOW + timedelta(seconds=120))
        self.assertAlmostEqual(entity.native_value, 1.5 / 60)

    async def test_same_boundary_collision_cannot_be_repaired_by_replay(self) -> None:
        provider, entity = self.make_entity()
        entity.async_get_last_sensor_data = AsyncMock(return_value=None)
        provider.publish(entity.source_semantic_id, 1000, NOW)
        await entity.async_added_to_hass()
        for seconds, watts in ((60, 1000), (60, 2000), (60, 1000), (120, 1000)):
            provider.publish(entity.source_semantic_id, watts, NOW + timedelta(seconds=seconds))
        self.assertAlmostEqual(entity.native_value, 1 / 60)

    async def test_restore_does_not_integrate_from_a_retained_pre_start_sample(self) -> None:
        provider, entity = self.make_entity()
        entity.async_get_last_sensor_data = AsyncMock(return_value=SensorExtraStoredData(12.5, UnitOfEnergy.KILO_WATT_HOUR))
        provider.publish(entity.source_semantic_id, 1000, NOW - timedelta(minutes=2))
        await entity.async_added_to_hass()
        provider.publish(entity.source_semantic_id, 1000, NOW + timedelta(minutes=1))
        self.assertEqual(entity.native_value, 12.5)
        provider.publish(entity.source_semantic_id, 1000, NOW + timedelta(minutes=2))
        self.assertAlmostEqual(entity.native_value, 12.5 + 1 / 60)


class CanonicalAcLocalLeafTests(unittest.IsolatedAsyncioTestCase):
    def make_entity(
        self, key: str, semantic_id: str
    ) -> tuple[FakePatCoordinator, FakeReadProvider, sensor.TlvReadSensor]:
        coordinator = FakePatCoordinator()
        owned_contract = contract(semantic_id)
        provider = FakeReadProvider((owned_contract,))
        entity = sensor.TlvReadSensor(
            provider,  # type: ignore[arg-type]
            coordinator,  # type: ignore[arg-type]
            semantic_id,
            owned_contract,
        )
        return coordinator, provider, entity

    def test_existing_identity_is_local_owned_without_pat_fallback(self) -> None:
        coordinator, provider, entity = self.make_entity(
            "current_temperature", "temperature.current_c"
        )
        original_unique_id = entity.unique_id
        provider.publish("temperature.current_c", 19.5, NOW)

        self.assertEqual(entity.native_value, 19.5)
        self.assertEqual(
            original_unique_id,
            local_semantic_unique_id("test-ac", "temperature.current_c"),
        )
        provider.available.clear()
        # An unavailable Local owner must not substitute PAT's 27 C.
        self.assertIsNone(entity.native_value)
        self.assertFalse(entity.available)

        coordinator.data.clear()
        provider.available.add("temperature.current_c")
        self.assertTrue(entity.available)
        provider.available.clear()
        self.assertFalse(entity.available)

    def test_numeric_leaf_contract_rejects_non_numeric_payloads_upstream(self) -> None:
        owned_contract = contract("humidity.current_pct")

        # TlvReadShadowProvider validates every current payload before it can
        # reach an entity. Keep that authority boundary explicit instead of
        # teaching the leaf to reinterpret an impossible provider value.
        self.assertEqual(owned_contract.value_types, ("number",))

    def test_entity_without_local_owner_keeps_pat_behavior(self) -> None:
        coordinator = FakePatCoordinator()
        description = next(
            item for item in sensor.AC_SENSORS if item.key == "current_temperature"
        )

        entity = sensor.MyLgSensor(coordinator, description)  # type: ignore[arg-type]

        self.assertTrue(entity.available)
        self.assertEqual(entity.native_value, 27.0)

    async def test_provider_listener_refreshes_existing_entity_until_removal(
        self,
    ) -> None:
        _, provider, entity = self.make_entity("humidity", "humidity.current_pct")
        entity.async_write_ha_state = Mock()

        await entity.async_added_to_hass()
        provider.notify()
        entity.async_write_ha_state.assert_called_once_with()
        await entity.async_will_remove_from_hass()
        provider.notify()
        entity.async_write_ha_state.assert_called_once_with()

    async def test_setup_has_one_owner_and_two_derived_energy_entities(self) -> None:
        coordinator = FakePatCoordinator()
        profile = load_tlv_read_catalogue()[MODEL]
        provider = FakeReadProvider(profile.fields)
        data = SimpleNamespace(
            coordinators={coordinator.device_id: coordinator},
            local_providers={},
            local_read_providers={coordinator.device_id: provider},
            wideq_coordinator=None,
        )
        entry = SimpleNamespace(
            runtime_data=data,
            async_on_unload=lambda _remove: None,
        )
        entities: list[object] = []
        with patch.object(sensor, "RawSensorManager") as manager:
            manager.return_value.add_new.return_value = None
            await sensor.async_setup_entry(None, entry, entities.extend)

        for semantic_id in ("temperature.current_c", "humidity.current_pct"):
            owners = [
                item
                for item in entities
                if (
                    isinstance(item, sensor.MyLgSensor)
                    and sensor._PAT_SENSOR_SEMANTICS[DEVICE_TYPE_AIR_CONDITIONER].get(
                        item.entity_description.key
                    )
                    == semantic_id
                )
                or (
                    isinstance(item, sensor.TlvReadSensor)
                    and item.semantic_id == semantic_id
                )
            ]
            self.assertEqual(len(owners), 1)
            self.assertIsInstance(owners[0], sensor.TlvReadSensor)

        energy_entities = [
            item
            for item in entities
            if isinstance(item, sensor.TlvIntegratedEnergySensor)
        ]
        self.assertEqual(
            {item.source_semantic_id for item in energy_entities},
            set(sensor.AC_POWER_SEMANTICS),
        )
        self.assertEqual(len(energy_entities), 2)


if __name__ == "__main__":
    unittest.main()
