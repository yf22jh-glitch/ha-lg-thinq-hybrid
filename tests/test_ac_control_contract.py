"""Contract tests for AC controls that vary by mode or backend."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
import unittest

from homeassistant.components.climate import ClimateEntityFeature, HVACMode
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.exceptions import HomeAssistantError

from custom_components.my_lg.climate import MyLgClimate
from custom_components.my_lg.local_command import LocalCommandFailed, LocalCommandResult
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


class FakeLocalRouter:
    """Local-path double that records what the bridge was asked for and what it answered."""

    def __init__(self, outcome: Any = "confirmed") -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.cloud_tuple_barriers: list[str] = []
        self.outcome = outcome

    def _answer(self) -> Any:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        if self.outcome is None:
            return None
        return LocalCommandResult(self.outcome, {})

    async def async_set_climate(self, device_id: str, **fields: Any) -> Any:
        self.calls.append(("climate", {"device_id": device_id, **fields}))
        # The real router refuses a request naming none of the three before anything is sent, and
        # a double that did not would let a test pass against behaviour that cannot happen.
        if all(fields.get(name) is None for name in ("mode", "fan", "target_c")):
            return None
        return self._answer()

    async def async_turn_on(self, device_id: str, **fields: Any) -> Any:
        self.calls.append(("turn_on", {"device_id": device_id, **fields}))
        return self._answer()

    async def async_turn_off(self, device_id: str) -> Any:
        self.calls.append(("turn_off", {"device_id": device_id}))
        return self._answer()

    async def async_set_flag(self, device_id: str, capability: str, enabled: bool) -> Any:
        self.calls.append(("flag", {"device_id": device_id, "capability": capability, "enabled": enabled}))
        return self._answer()

    async def async_mark_cloud_tuple_dispatch(self, device_id: str) -> None:
        self.cloud_tuple_barriers.append(device_id)


class LocalRoutedClimateTests(unittest.IsolatedAsyncioTestCase):
    """What the entity does with each answer the local path can give.

    The bridge is exercised through its own tests; what is pinned here is the part only this
    entity decides - whether the cloud is asked as well, and whether Home Assistant shows a
    state the appliance never acknowledged.
    """

    def entity(self, router: FakeLocalRouter | None, job_mode: str = "COOL", powered: bool = True):
        coordinator = FakePatCoordinator(job_mode=job_mode)
        if not powered:
            coordinator.data["operation"]["airConOperationMode"] = "POWER_OFF"
        return MyLgClimate(coordinator, router), coordinator

    async def test_a_confirmed_local_write_is_not_also_sent_to_the_cloud(self) -> None:
        router = FakeLocalRouter("confirmed")
        entity, coordinator = self.entity(router)
        await entity.async_set_fan_mode("LOW")
        self.assertEqual(coordinator.controls, [])
        self.assertEqual(router.calls[0][1]["fan"], "low")
        # The appliance confirmed it, so showing it immediately is not a guess.
        self.assertEqual(coordinator.get("airFlow", "windStrength"), "LOW")

    async def test_a_local_refusal_goes_to_the_cloud_exactly_once(self) -> None:
        router = FakeLocalRouter(None)
        entity, coordinator = self.entity(router)
        await entity.async_set_fan_mode("LOW")
        self.assertEqual(coordinator.controls, [{"airFlow": {"windStrength": "LOW"}}])
        self.assertEqual(coordinator.get("airFlow", "windStrength"), "LOW")
        self.assertEqual([call[0] for call in router.calls], ["climate"])
        self.assertEqual(router.cloud_tuple_barriers, [coordinator.device_id])

    async def test_a_write_that_went_out_unconfirmed_is_neither_retried_nor_shown(self) -> None:
        router = FakeLocalRouter("unverifiable")
        entity, coordinator = self.entity(router)
        await entity.async_set_temperature(**{ATTR_TEMPERATURE: 26})
        # The frame is on the wire, so sending it again over the cloud would be a second command.
        self.assertEqual(coordinator.controls, [])
        # And the setpoint's normalizing second update must not carry the state the first declined.
        self.assertEqual(coordinator.get("temperature", "targetTemperature"), 23)

    async def test_a_frame_the_appliance_never_reported_is_raised_rather_than_retried(self) -> None:
        router = FakeLocalRouter(LocalCommandFailed("the appliance did not report the change"))
        entity, coordinator = self.entity(router)
        with self.assertRaises(HomeAssistantError):
            await entity.async_set_fan_mode("LOW")
        self.assertEqual(coordinator.controls, [])

    async def test_an_unmapped_fan_speed_is_the_cloud_s_to_take_directly(self) -> None:
        router = FakeLocalRouter("confirmed")
        entity, coordinator = self.entity(router)
        await entity.async_set_fan_mode("POWER")
        # POWER is encodable only at one exact mode and temperature, so nothing is stated locally
        # and the request is the cloud's to take directly.
        self.assertEqual(router.calls, [])
        self.assertEqual(coordinator.controls, [{"airFlow": {"windStrength": "POWER"}}])
        self.assertEqual(router.cloud_tuple_barriers, [coordinator.device_id])

    async def test_auto_cloud_fallback_marks_the_tuple_barrier(self) -> None:
        router = FakeLocalRouter("confirmed")
        entity, coordinator = self.entity(router)
        await entity.async_set_hvac_mode(HVACMode.AUTO)
        self.assertEqual(router.calls, [])
        self.assertEqual(router.cloud_tuple_barriers, [coordinator.device_id])

    async def test_swing_and_power_off_cloud_fallback_do_not_invalidate_the_tuple(self) -> None:
        router = FakeLocalRouter(None)
        entity, coordinator = self.entity(router)

        await entity.async_set_swing_mode("horizontal")
        await entity.async_turn_off()

        self.assertEqual(router.cloud_tuple_barriers, [])
        self.assertEqual(
            coordinator.controls[-1],
            {"operation": {"airConOperationMode": "POWER_OFF"}},
        )

    async def test_turning_on_states_the_mode_so_one_press_is_one_frame(self) -> None:
        router = FakeLocalRouter("confirmed")
        entity, coordinator = self.entity(router, powered=False)
        await entity.async_set_hvac_mode(HVACMode.DRY)
        self.assertEqual([call[0] for call in router.calls], ["turn_on"])
        self.assertEqual(router.calls[0][1]["mode"], "dry")
        self.assertEqual(coordinator.controls, [])
        # One state write carrying both, not two - the first of which would show the unit on in
        # the mode it was left in.
        self.assertEqual(coordinator.get("operation", "airConOperationMode"), "POWER_ON")
        self.assertEqual(coordinator.get("airConJobMode", "currentJobMode"), "AIR_DRY")

    async def test_a_mode_with_no_local_form_is_never_powered_on_in_the_previous_one(self) -> None:
        router = FakeLocalRouter("confirmed")
        entity, coordinator = self.entity(router, powered=False)
        await entity.async_set_hvac_mode(HVACMode.AUTO)
        # AUTO has no local form. Taking the local power-on would have restated the mode the unit
        # was left in and skipped the AUTO write entirely.
        self.assertEqual(router.calls, [])
        self.assertEqual(
            coordinator.controls,
            [
                {"operation": {"airConOperationMode": "POWER_ON"}},
                {"airConJobMode": {"currentJobMode": "AUTO"}},
            ],
        )

    async def test_after_a_cloud_power_on_the_mode_goes_the_same_way(self) -> None:
        router = FakeLocalRouter(None)
        entity, coordinator = self.entity(router, powered=False)
        await entity.async_set_hvac_mode(HVACMode.DRY)
        # The appliance may still be coming up; the local path would send a tuple it cannot obey
        # and wait out the bridge's report window to call it a failure. So the mode step states
        # nothing locally after a cloud power-on.
        self.assertEqual([call[0] for call in router.calls], ["turn_on"])
        self.assertEqual(
            coordinator.controls,
            [
                {"operation": {"airConOperationMode": "POWER_ON"}},
                {"airConJobMode": {"currentJobMode": "AIR_DRY"}},
            ],
        )

    async def test_both_swing_directions_are_written_even_when_the_first_fails(self) -> None:
        router = FakeLocalRouter(LocalCommandFailed("the appliance did not report the change"))
        entity, coordinator = self.entity(router)
        with self.assertRaises(HomeAssistantError):
            await entity.async_set_swing_mode("both")
        # Two halves of one setting: the second used to be left unsent by either path.
        self.assertEqual(
            [(call[1]["capability"], call[1]["enabled"]) for call in router.calls],
            [("swing.horizontal_enabled", True), ("swing.vertical_enabled", True)],
        )

    async def test_each_swing_direction_carries_its_own_selection(self) -> None:
        router = FakeLocalRouter("confirmed")
        entity, _coordinator = self.entity(router)
        await entity.async_set_swing_mode("vertical")
        self.assertEqual(
            [(call[1]["capability"], call[1]["enabled"]) for call in router.calls],
            [("swing.horizontal_enabled", False), ("swing.vertical_enabled", True)],
        )

    async def test_without_a_router_every_write_behaves_as_it_did_before(self) -> None:
        entity, coordinator = self.entity(None)
        await entity.async_set_fan_mode("LOW")
        self.assertEqual(coordinator.controls, [{"airFlow": {"windStrength": "LOW"}}])
