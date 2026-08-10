"""Contract tests for AC controls that vary by mode or backend."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
import unittest

from homeassistant.components.climate import ClimateEntityFeature, HVACMode
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.exceptions import HomeAssistantError

from custom_components.my_lg.climate import MyLgClimate
from custom_components.my_lg.coordinator import deep_merge
from custom_components.my_lg.switch import (
    SWITCHES_BY_TYPE,
    WIDEQ_SWITCHES_BY_TYPE,
    MyLgSwitch,
    MyLgWideqSwitch,
)
from custom_components.my_lg.const import DEVICE_TYPE_AIR_CONDITIONER


class FakePatCoordinator:
    """Small coordinator double that records acknowledged controls."""

    def __init__(self, job_mode: str = "COOL") -> None:
        self.device_id = "test-device"
        self.device_type = DEVICE_TYPE_AIR_CONDITIONER
        self.alias = "Test AC"
        self.model = "CST_170004_WW"
        self.profile = {
            "property": {
                "windDirection": {
                    "rotateLeftRight": {},
                    "rotateUpDown": {},
                }
            }
        }
        self.data: dict[str, Any] = {
            "operation": {"airConOperationMode": "POWER_ON"},
            "airConJobMode": {"currentJobMode": job_mode},
            "temperature": {"targetTemperature": 23},
            "powerSave": {"powerSaveEnabled": False},
            "windDirection": {"forestWind": False},
        }
        self.controls: list[dict[str, Any]] = []
        self.control_error: Exception | None = None

    def async_add_listener(self, *_args: Any, **_kwargs: Any):
        return lambda: None

    def get(self, *path: str, default: Any = None) -> Any:
        node: Any = self.data
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def supports_field(self, group: str, field: str) -> bool:
        return field in self.profile.get("property", {}).get(group, {})

    async def async_control(self, payload: dict[str, Any]) -> None:
        if self.control_error is not None:
            raise self.control_error
        self.controls.append(deepcopy(payload))

    def handle_mqtt_status(self, payload: dict[str, Any]) -> None:
        deep_merge(self.data, deepcopy(payload))


class FakeWideqCoordinator:
    """WideQ double that keeps power-save state separate from normal data."""

    def __init__(self) -> None:
        self.controls: list[tuple[str, str, dict[str, Any]]] = []
        self.control_error: Exception | None = None
        self.power_save: dict[str, dict[str, Any]] = {
            "test-device": {"airState.powerSave.hum": False}
        }

    def async_add_listener(self, *_args: Any, **_kwargs: Any):
        return lambda: None

    async def async_control(
        self, device_id: str, ctrl_key: str, **kwargs: Any
    ) -> None:
        if self.control_error is not None:
            raise self.control_error
        self.controls.append((device_id, ctrl_key, deepcopy(kwargs)))

    def snapshot_for(self, _device_id: str) -> dict[str, Any]:
        return {}

    def power_save_snapshot_for(self, device_id: str) -> dict[str, Any]:
        return dict(self.power_save.get(device_id, {}))

    def power_save_field_available(self, device_id: str, path: str) -> bool:
        return path in self.power_save.get(device_id, {})

    def power_save_diagnostic_attributes(self, _device_id: str) -> dict[str, Any]:
        return {"power_save_cache_scope": "mode_flags_only"}

    @property
    def diagnostic_attributes(self) -> dict[str, Any]:
        return {}

    def apply_power_save_optimistic(
        self, device_id: str, path: str, value: Any
    ) -> None:
        self.power_save.setdefault(device_id, {})[path] = bool(value)


def _pat_switch(key: str, coordinator: FakePatCoordinator) -> MyLgSwitch:
    description = next(
        item for item in SWITCHES_BY_TYPE[DEVICE_TYPE_AIR_CONDITIONER]
        if item.key == key
    )
    return MyLgSwitch(coordinator, description)  # type: ignore[arg-type]


def _wideq_switch(
    key: str,
    wideq: FakeWideqCoordinator,
    pat: FakePatCoordinator,
) -> MyLgWideqSwitch:
    description = next(
        item for item in WIDEQ_SWITCHES_BY_TYPE[DEVICE_TYPE_AIR_CONDITIONER]
        if item.key == key
    )
    return MyLgWideqSwitch(wideq, pat, description)  # type: ignore[arg-type]


class AcClimateControlContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_cool_and_auto_use_mode_specific_temperature_fields(self) -> None:
        coordinator = FakePatCoordinator("COOL")
        climate = MyLgClimate(coordinator)  # type: ignore[arg-type]

        await climate.async_set_temperature(**{ATTR_TEMPERATURE: 24})
        self.assertEqual(
            coordinator.controls[-1],
            {"temperature": {"coolTargetTemperature": 24}},
        )
        self.assertEqual(climate.target_temperature, 24)

        coordinator.data["airConJobMode"]["currentJobMode"] = "AUTO"
        await climate.async_set_temperature(**{ATTR_TEMPERATURE: 22.5})
        self.assertEqual(
            coordinator.controls[-1],
            {"temperature": {"autoTargetTemperature": 22.5}},
        )
        self.assertEqual(climate.target_temperature, 22.5)

    async def test_dry_and_fan_reject_temperature_before_network(self) -> None:
        for job_mode, expected in (("AIR_DRY", "제습"), ("FAN", "송풍")):
            coordinator = FakePatCoordinator(job_mode)
            climate = MyLgClimate(coordinator)  # type: ignore[arg-type]

            with self.assertRaisesRegex(HomeAssistantError, expected):
                await climate.async_set_temperature(**{ATTR_TEMPERATURE: 24})
            self.assertEqual(coordinator.controls, [])
            self.assertIsNone(climate.target_temperature)
            self.assertFalse(
                climate.supported_features
                & ClimateEntityFeature.TARGET_TEMPERATURE
            )

    async def test_failed_temperature_ack_does_not_change_state(self) -> None:
        coordinator = FakePatCoordinator("COOL")
        coordinator.control_error = HomeAssistantError("rejected")
        climate = MyLgClimate(coordinator)  # type: ignore[arg-type]

        with self.assertRaisesRegex(HomeAssistantError, "rejected"):
            await climate.async_set_temperature(**{ATTR_TEMPERATURE: 25})
        self.assertEqual(climate.target_temperature, 23)

    async def test_hvac_mode_control_keeps_power_ack_before_mode(self) -> None:
        coordinator = FakePatCoordinator("COOL")
        coordinator.data["operation"]["airConOperationMode"] = "POWER_OFF"
        climate = MyLgClimate(coordinator)  # type: ignore[arg-type]

        await climate.async_set_hvac_mode(HVACMode.AUTO)

        self.assertEqual(
            coordinator.controls,
            [
                {"operation": {"airConOperationMode": "POWER_ON"}},
                {"airConJobMode": {"currentJobMode": "AUTO"}},
            ],
        )


class AcSwitchControlContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_general_power_save_is_encoded_only_in_cool(self) -> None:
        coordinator = FakePatCoordinator("AIR_DRY")
        switch = _pat_switch("power_save", coordinator)

        with self.assertRaisesRegex(HomeAssistantError, "냉방 모드"):
            await switch.async_turn_on()
        self.assertEqual(coordinator.controls, [])

        coordinator.data["airConJobMode"]["currentJobMode"] = "COOL"
        await switch.async_turn_on()
        self.assertEqual(
            coordinator.controls[-1],
            {"powerSave": {"powerSaveEnabled": True}},
        )
        self.assertTrue(switch.is_on)

    async def test_special_wind_accepts_dry_but_rejects_fan(self) -> None:
        coordinator = FakePatCoordinator("AIR_DRY")
        switch = _pat_switch("wind_forest", coordinator)

        await switch.async_turn_on()
        self.assertEqual(
            coordinator.controls[-1],
            {"windDirection": {"forestWind": True}},
        )

        coordinator.data["airConJobMode"]["currentJobMode"] = "FAN"
        with self.assertRaisesRegex(HomeAssistantError, "특수 바람"):
            await switch.async_turn_on()
        self.assertEqual(len(coordinator.controls), 1)

    async def test_failed_switch_ack_does_not_change_optimistic_state(self) -> None:
        coordinator = FakePatCoordinator("COOL")
        coordinator.control_error = HomeAssistantError("rejected")
        switch = _pat_switch("power_save", coordinator)

        with self.assertRaisesRegex(HomeAssistantError, "rejected"):
            await switch.async_turn_on()
        self.assertFalse(switch.is_on)

    async def test_comfort_power_save_uses_verified_setting_info_shape(self) -> None:
        pat = FakePatCoordinator("COOL")
        wideq = FakeWideqCoordinator()
        switch = _wideq_switch("comfortable_power_save", wideq, pat)

        await switch.async_turn_on()
        self.assertEqual(
            wideq.controls,
            [
                (
                    "test-device",
                    "settingInfo",
                    {"data_key": "airState.powerSave.hum", "value": 1},
                )
            ],
        )
        self.assertTrue(switch.is_on)

    async def test_comfort_power_save_mode_gate_and_failed_ack_are_safe(self) -> None:
        pat = FakePatCoordinator("AUTO")
        wideq = FakeWideqCoordinator()
        switch = _wideq_switch("comfortable_power_save", wideq, pat)

        with self.assertRaisesRegex(HomeAssistantError, "냉방 모드"):
            await switch.async_turn_on()
        self.assertEqual(wideq.controls, [])

        pat.data["airConJobMode"]["currentJobMode"] = "COOL"
        wideq.control_error = HomeAssistantError("rejected")
        with self.assertRaisesRegex(HomeAssistantError, "rejected"):
            await switch.async_turn_on()
        self.assertFalse(switch.is_on)

        # Turning off remains available in every mode so a stale/active mode can
        # always be cleared; a successful control is reflected after its ack.
        wideq.control_error = None
        pat.data["airConJobMode"]["currentJobMode"] = "AUTO"
        wideq.power_save["test-device"]["airState.powerSave.hum"] = True
        await switch.async_turn_off()
        self.assertFalse(switch.is_on)


if __name__ == "__main__":
    unittest.main()
