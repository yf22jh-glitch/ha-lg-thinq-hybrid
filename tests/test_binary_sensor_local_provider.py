"""Entity-registry invariants for the Local water-tank shadow connection."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.my_lg import binary_sensor
from custom_components.my_lg.const import (
    DEVICE_TYPE_DEHUMIDIFIER,
    DOMAIN,
    OPT_LOCAL_READ_DUPLICATE_OVERLAY,
)
from custom_components.my_lg.local_provider import LocalWaterTankShadowProvider
from tests.test_local_provider import (
    BINDING_ID,
    availability_payload,
    runtime_payload,
    state_payload,
)

NOW = datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc)
PAT_DEVICE_ID = "pat-dehumidifier-001"


class FakeWideqCoordinator:
    def __init__(self, value=0) -> None:
        self.data = {
            PAT_DEVICE_ID: {binary_sensor.WATER_TANK_KEY: value},
        }
        self.diagnostic_attributes = {"wideq_circuit_open": False}

    def snapshot_for(self, device_id):
        return (self.data or {}).get(device_id, {})


def pat_coordinator():
    return SimpleNamespace(
        device_id=PAT_DEVICE_ID,
        alias="Pilot dehumidifier",
        model="DHUM_056905_WW",
        device_type=DEVICE_TYPE_DEHUMIDIFIER,
    )


def healthy_local_provider(value=True):
    provider = LocalWaterTankShadowProvider(BINDING_ID, now=lambda: NOW)
    provider.ingest(
        provider.state_topic, state_payload(value=value), qos=1, retained=True
    )
    provider.ingest(
        provider.availability_topic,
        availability_payload("online"),
        qos=1,
        retained=True,
    )
    provider.ingest(
        provider.runtime_availability_topic,
        runtime_payload("online"),
        qos=1,
        retained=True,
    )
    provider.set_transport_ready(True)
    return provider


class WaterTankEntityInvariantTests(unittest.IsolatedAsyncioTestCase):
    def test_local_owner_preserves_identity_and_replaces_wideq_state(self) -> None:
        coordinator = FakeWideqCoordinator(value=0)
        wideq_only = binary_sensor.WaterTankFullSensor(coordinator, pat_coordinator())
        shadow = binary_sensor.WaterTankFullSensor(
            coordinator, pat_coordinator(), healthy_local_provider(value=True)
        )

        self.assertEqual(wideq_only.unique_id, f"{PAT_DEVICE_ID}_water_tank_full")
        self.assertEqual(shadow.unique_id, wideq_only.unique_id)
        self.assertEqual(shadow.device_info, wideq_only.device_info)
        self.assertEqual(shadow.device_info["identifiers"], {(DOMAIN, PAT_DEVICE_ID)})
        self.assertFalse(wideq_only.is_on)
        self.assertTrue(shadow.is_on, "Local must replace the disagreeing WideQ value")
        self.assertEqual(shadow.available, wideq_only.available)
        self.assertTrue(shadow.available)
        self.assertEqual(
            shadow.extra_state_attributes, wideq_only.extra_state_attributes
        )
        self.assertNotIn("local", " ".join(shadow.extra_state_attributes))

        coordinator.data = {}
        self.assertTrue(shadow.available, "WideQ absence must not mask Local health")

    def test_configured_local_owner_never_falls_back_when_unavailable(self) -> None:
        provider = healthy_local_provider(value=True)
        sensor = binary_sensor.WaterTankFullSensor(
            FakeWideqCoordinator(value=0), pat_coordinator(), provider
        )

        provider.set_transport_ready(False)

        self.assertFalse(sensor.available)
        self.assertTrue(sensor.is_on)

    def test_unknown_wideq_value_is_unavailable_state_not_guessed_true(self) -> None:
        sensor = binary_sensor.WaterTankFullSensor(
            FakeWideqCoordinator(value="UNKNOWN"),
            pat_coordinator(),
        )
        self.assertIsNone(sensor.is_on)

    async def test_local_updates_schedule_state_without_a_wideq_poll(self) -> None:
        provider = healthy_local_provider(value=False)
        sensor = binary_sensor.WaterTankFullSensor(
            FakeWideqCoordinator(value=0), pat_coordinator(), provider
        )
        sensor.async_write_ha_state = Mock()

        with patch.object(
            CoordinatorEntity, "async_added_to_hass", new=AsyncMock()
        ), patch.object(
            CoordinatorEntity, "async_will_remove_from_hass", new=AsyncMock()
        ):
            await sensor.async_added_to_hass()
            provider._notify_listeners()
            sensor.async_write_ha_state.assert_called_once_with()
            await sensor.async_will_remove_from_hass()
            provider._notify_listeners()
            sensor.async_write_ha_state.assert_called_once_with()


class WaterTankEntityFactoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_promoted_owner_adds_no_duplicate_even_in_overlay_mode(self) -> None:
        wideq = FakeWideqCoordinator(value=0)
        provider = healthy_local_provider(value=True)
        data = SimpleNamespace(
            wideq_coordinator=wideq,
            coordinators={PAT_DEVICE_ID: pat_coordinator()},
            local_providers={PAT_DEVICE_ID: provider},
        )
        entry = SimpleNamespace(
            runtime_data=data,
            options={OPT_LOCAL_READ_DUPLICATE_OVERLAY: True},
        )
        entities = []

        await binary_sensor.async_setup_entry(None, entry, entities.extend)

        self.assertEqual(len(entities), 1)
        self.assertIsInstance(entities[0], binary_sensor.WaterTankFullSensor)
        self.assertEqual(entities[0].unique_id, f"{PAT_DEVICE_ID}_water_tank_full")


if __name__ == "__main__":
    unittest.main()
