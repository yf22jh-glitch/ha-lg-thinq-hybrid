"""Local-first read overlay contract for the existing AC climate entity."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable
import unittest
from unittest.mock import Mock

from homeassistant.components.climate import (
    SWING_BOTH,
    SWING_HORIZONTAL,
    SWING_OFF,
    SWING_VERTICAL,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.exceptions import HomeAssistantError

from custom_components.my_lg import climate
from custom_components.my_lg.const import DEVICE_TYPE_AIR_CONDITIONER
from custom_components.my_lg.coordinator import deep_merge
from custom_components.my_lg.local_command import (
    LocalCommandPending,
    LocalCommandResult,
)
from custom_components.my_lg.local_control_composite_domain import (
    load_local_control_composite_domain_contract,
)
from tests.test_ac_control_contract import FakeLocalRouter


class FakePatCoordinator:
    """PAT coordinator with deliberately different fallback values."""

    def __init__(
        self,
        device_id: str = "test-ac",
        model: str = "CST_170004_WW",
    ) -> None:
        self.device_id = device_id
        self.device_type = DEVICE_TYPE_AIR_CONDITIONER
        self.alias = "Test AC"
        self.model = model
        self.profile = {
            "property": {
                "windDirection": {
                    "rotateLeftRight": {},
                    "rotateUpDown": {},
                }
            }
        }
        self.controls: list[dict[str, Any]] = []
        self.data: dict[str, Any] = {
            "operation": {"airConOperationMode": "POWER_ON"},
            "airConJobMode": {"currentJobMode": "COOL"},
            "airFlow": {"windStrength": "HIGH"},
            "temperature": {
                "currentTemperature": 27,
                "targetTemperature": 23,
                "minTargetTemperature": 16,
                "maxTargetTemperature": 30,
            },
            "airQualitySensor": {"humidity": 61},
            "windDirection": {
                "rotateLeftRight": False,
                "rotateUpDown": True,
            },
        }

    def async_add_listener(
        self, _listener: Callable[[], None], *_args: object
    ) -> Callable[[], None]:
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

    async def async_control(self, _payload: dict[str, Any]) -> None:
        self.controls.append(_payload)
        raise AssertionError("read overlay tests must not send controls")

    def handle_mqtt_status(self, payload: dict[str, Any]) -> None:
        deep_merge(self.data, payload)


class FakeTlvReadProvider:
    """Minimal provider implementing both legacy full-read and primary Local views."""

    def __init__(
        self,
        values: dict[str, object] | None = None,
        available: set[str] | None = None,
        *,
        event_available: bool = True,
    ) -> None:
        self.values = values or {}
        self.available = set(self.values) if available is None else set(available)
        self.event_available = event_available
        semantic_ids = {
            "operation.power_requested",
            "operation.mode",
            "fan.mode",
            "temperature.current_c",
            "temperature.target_c",
            "comfort.preference_step",
            "humidity.current_pct",
            "swing.horizontal_enabled",
            "swing.vertical_enabled",
        }
        contracts = {
            semantic_id: SimpleNamespace(
                value_type=(
                    "boolean"
                    if semantic_id.startswith("swing.")
                    or semantic_id == "operation.power_requested"
                    else "number"
                    if semantic_id.startswith(("temperature.", "humidity."))
                    or semantic_id == "comfort.preference_step"
                    else "string"
                )
            )
            for semantic_id in semantic_ids
        }
        self.profile = SimpleNamespace(
            fields=contracts,
            fields_by_semantic_id=contracts,
        )
        self.shadow_healthy = event_available
        self.listeners: list[Callable[[], None]] = []

    def field_available(self, semantic_id: str) -> bool:
        return semantic_id in self.available

    def semantic_field_available(self, semantic_id: str) -> bool:
        return self.field_available(semantic_id)

    def field_value(self, semantic_id: str) -> object | None:
        return self.values.get(semantic_id)

    def async_add_listener(
        self, callback: Callable[[], None]
    ) -> Callable[[], None]:
        self.listeners.append(callback)

        def remove() -> None:
            if callback in self.listeners:
                self.listeners.remove(callback)

        return remove

    def notify(self) -> None:
        for callback in tuple(self.listeners):
            callback()


class FakeLocalOnlyRouter:
    """Local-only router surface that records intent and never calls PAT."""

    def __init__(self, outcome: object | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.outcome = outcome or LocalCommandResult("confirmed", {})
        self.target_available = True
        self.authorized = True

    def control_target_available(self, _device_id: str) -> bool:
        return self.target_available

    def capability_authorized(self, _device_id: str, _capability: str) -> bool:
        return self.authorized

    def value_authorized(
        self, _device_id: str, _capability: str, _value: str
    ) -> bool:
        return self.authorized

    def _answer(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def async_set_climate(self, device_id: str, **kwargs: object):
        self.calls.append(("climate", {"device_id": device_id, **kwargs}))
        return self._answer()

    async def async_turn_on(self, device_id: str, **kwargs: object):
        self.calls.append(("turn_on", {"device_id": device_id, **kwargs}))
        return self._answer()

    async def async_turn_off(self, device_id: str):
        self.calls.append(("turn_off", {"device_id": device_id}))
        return self._answer()

    async def async_set_flag(
        self, device_id: str, capability: str, enabled: bool
    ):
        self.calls.append(
            (
                "flag",
                {
                    "device_id": device_id,
                    "capability": capability,
                    "enabled": enabled,
                },
            )
        )
        return self._answer()


def make_local_only_entity(
    provider: FakeTlvReadProvider,
    coordinator: FakePatCoordinator | None = None,
    router: FakeLocalOnlyRouter | None = None,
) -> tuple[climate.MyLgLocalClimate, FakeLocalOnlyRouter, FakePatCoordinator]:
    metadata = coordinator or FakePatCoordinator()
    local_router = router or FakeLocalOnlyRouter()
    return (
        climate.MyLgLocalClimate(
            metadata,
            local_router,  # type: ignore[arg-type]
            provider,  # type: ignore[arg-type]
            load_local_control_composite_domain_contract(),
        ),
        local_router,
        metadata,
    )


def make_entity(
    provider: FakeTlvReadProvider | None,
    coordinator: FakePatCoordinator | None = None,
) -> climate.MyLgClimate:
    return climate.MyLgClimate(
        coordinator or FakePatCoordinator(),
        None,
        provider,  # type: ignore[arg-type]
    )


class ClimateLocalReadSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_injects_only_the_matching_device_provider(self) -> None:
        first = FakePatCoordinator("first-ac")
        second = FakePatCoordinator("second-ac")
        provider = FakeTlvReadProvider()
        entry = SimpleNamespace(
            runtime_data=SimpleNamespace(
                coordinators={first.device_id: first, second.device_id: second},
                local_control=None,
                local_providers={first.device_id: provider},
                local_read_providers={},
                local_control_composite_domain_contract=(
                    load_local_control_composite_domain_contract()
                ),
            )
        )
        entities: list[object] = []

        await climate.async_setup_entry(None, entry, entities.extend)

        self.assertEqual([entity.unique_id for entity in entities], [
            "first-ac_climate",
            "second-ac_climate",
        ])
        self.assertIsInstance(entities[0], climate.MyLgLocalClimate)
        self.assertIsInstance(entities[1], climate.MyLgClimate)
        self.assertIs(entities[0]._local_read, provider)
        self.assertIsNone(entities[1]._local_read)

    async def test_setup_never_suppresses_legacy_climate_without_full_local_tuple(
        self,
    ) -> None:
        coordinator = FakePatCoordinator()
        provider = FakeTlvReadProvider()
        provider.profile.fields["comfort.preference_step"].value_type = "string"
        entry = SimpleNamespace(
            runtime_data=SimpleNamespace(
                coordinators={coordinator.device_id: coordinator},
                local_control=None,
                local_providers={coordinator.device_id: provider},
                local_read_providers={},
                local_control_composite_domain_contract=(
                    load_local_control_composite_domain_contract()
                ),
            )
        )
        entities: list[object] = []

        await climate.async_setup_entry(None, entry, entities.extend)

        self.assertEqual(len(entities), 1)
        self.assertIsInstance(entities[0], climate.MyLgClimate)

    async def test_exact_local_contract_does_not_depend_on_pat_device_type(self) -> None:
        coordinator = FakePatCoordinator()
        coordinator.device_type = ""
        provider = FakeTlvReadProvider()
        entry = SimpleNamespace(
            runtime_data=SimpleNamespace(
                coordinators={coordinator.device_id: coordinator},
                local_control=None,
                local_providers={coordinator.device_id: provider},
                local_read_providers={},
                local_control_composite_domain_contract=(
                    load_local_control_composite_domain_contract()
                ),
            )
        )
        entities: list[object] = []

        await climate.async_setup_entry(None, entry, entities.extend)

        self.assertEqual(len(entities), 1)
        self.assertIsInstance(entities[0], climate.MyLgLocalClimate)


class ClimateLocalReadStateTests(unittest.TestCase):
    def test_exact_local_core_keeps_climate_available_without_pat_cache(self) -> None:
        cases = (
            ({"operation.power_requested": False}, True),
            (
                {
                    "operation.power_requested": True,
                    "operation.mode": "cool",
                },
                True,
            ),
            ({"operation.power_requested": True}, True),
            (
                {
                    "operation.power_requested": True,
                    "operation.mode": "unknown",
                },
                True,
            ),
        )
        for values, expected in cases:
            with self.subTest(values=values):
                coordinator = FakePatCoordinator()
                coordinator.data = {}
                entity = make_entity(FakeTlvReadProvider(values), coordinator)
                self.assertIs(entity.available, expected)

    def test_each_available_local_field_overlays_the_pat_state(self) -> None:
        provider = FakeTlvReadProvider(
            {
                "operation.power_requested": True,
                "operation.mode": "dry",
                "fan.mode": "medium",
                "temperature.current_c": 19.5,
                "temperature.target_c": 25.5,
                "humidity.current_pct": 47,
                "swing.horizontal_enabled": True,
                "swing.vertical_enabled": False,
            }
        )
        entity = make_entity(provider)

        self.assertEqual(entity.hvac_mode, HVACMode.DRY)
        self.assertEqual(entity.fan_mode, "MID")
        self.assertEqual(entity.current_temperature, 19.5)
        self.assertEqual(entity.current_humidity, 47)
        # This compatibility object is never selected for a Local-owned AC in
        # production. Without a verified Local write route it must not expose
        # a writable climate swing surface; the canonical Local booleans remain.
        self.assertEqual(entity.swing_mode, SWING_OFF)
        self.assertFalse(
            entity.supported_features & ClimateEntityFeature.SWING_MODE
        )
        # The local mode is authoritative too: a retained cooling setpoint must
        # not surface as an active target while the appliance is drying.
        self.assertIsNone(entity.target_temperature)
        self.assertFalse(
            entity.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE
        )

    def test_configured_local_owner_never_falls_back_to_pat(self) -> None:
        provider = FakeTlvReadProvider(
            {
                "operation.power_requested": False,
                "operation.mode": "dry",
                "fan.mode": "low",
                "temperature.current_c": 19.5,
                "temperature.target_c": 25.5,
                "humidity.current_pct": 47,
                "swing.horizontal_enabled": True,
                "swing.vertical_enabled": False,
            },
            available={"temperature.current_c", "swing.horizontal_enabled"},
        )
        entity = make_entity(provider)

        self.assertIsNone(entity.hvac_mode)
        self.assertIsNone(entity.fan_mode)
        self.assertEqual(entity.current_temperature, 19.5)
        self.assertIsNone(entity.target_temperature)
        self.assertIsNone(entity.current_humidity)
        self.assertEqual(entity.swing_mode, SWING_OFF)
        self.assertTrue(entity.available)
        self.assertFalse(
            entity.supported_features & ClimateEntityFeature.SWING_MODE
        )

    def test_climate_without_a_local_owner_keeps_pat_behavior(self) -> None:
        entity = make_entity(None)

        self.assertTrue(entity.available)
        self.assertEqual(entity.hvac_mode, HVACMode.COOL)
        self.assertEqual(entity.fan_mode, "HIGH")
        self.assertEqual(entity.current_temperature, 27)
        self.assertEqual(entity.target_temperature, 23)
        self.assertEqual(entity.current_humidity, 61)
        self.assertEqual(entity.swing_mode, SWING_VERTICAL)

    def test_local_power_false_wins_without_requiring_a_local_mode(self) -> None:
        provider = FakeTlvReadProvider(
            {"operation.power_requested": False},
        )

        self.assertEqual(make_entity(provider).hvac_mode, HVACMode.OFF)

    def test_local_mode_tokens_are_transformed_to_ha_hvac_modes(self) -> None:
        expected = {
            "cool": HVACMode.COOL,
            "dry": HVACMode.DRY,
            "fan_only": HVACMode.FAN_ONLY,
            "auto": HVACMode.AUTO,
        }
        for token, hvac_mode in expected.items():
            with self.subTest(token=token):
                provider = FakeTlvReadProvider(
                    {
                        "operation.power_requested": True,
                        "operation.mode": token,
                    }
                )
                self.assertEqual(make_entity(provider).hvac_mode, hvac_mode)

    def test_unknown_local_mode_never_falls_back_to_pat(self) -> None:
        provider = FakeTlvReadProvider(
            {
                "operation.power_requested": True,
                "operation.mode": "unreviewed-mode",
            }
        )
        self.assertIsNone(make_entity(provider).hvac_mode)

    def test_unknown_local_value_logs_only_the_semantic_once(self) -> None:
        provider = FakeTlvReadProvider(
            {
                "operation.power_requested": True,
                "operation.mode": "private-raw-value",
            }
        )
        entity = make_entity(provider)

        with self.assertLogs(climate.__name__, level="WARNING") as captured:
            self.assertIsNone(entity.hvac_mode)
            self.assertIsNone(entity.hvac_mode)

        self.assertEqual(len(captured.output), 1)
        self.assertIn("operation.mode", captured.output[0])
        self.assertNotIn("private-raw-value", captured.output[0])

    def test_local_fan_tokens_keep_the_existing_climate_contract(self) -> None:
        expected = {
            "very low": "LOW",
            "low": "LOW",
            "medium": "MID",
            "high": "HIGH",
            "power": "POWER",
            "auto": "AUTO",
        }
        for token, fan_mode in expected.items():
            with self.subTest(token=token):
                provider = FakeTlvReadProvider({"fan.mode": token})
                entity = make_entity(provider)
                self.assertEqual(entity.fan_mode, fan_mode)
                self.assertIn(entity.fan_mode, entity.fan_modes)

    def test_unknown_local_fan_never_falls_back_to_pat(self) -> None:
        provider = FakeTlvReadProvider({"fan.mode": "unreviewed-fan"})

        self.assertIsNone(make_entity(provider).fan_mode)

    def test_legacy_local_dry_and_fan_modes_both_suppress_target_temperature(self) -> None:
        for token in ("dry", "fan_only"):
            with self.subTest(token=token):
                provider = FakeTlvReadProvider(
                    {
                        "operation.mode": token,
                        "temperature.target_c": 25.5,
                    }
                )
                entity = make_entity(provider)
                self.assertIsNone(entity.target_temperature)
                self.assertFalse(
                    entity.supported_features
                    & ClimateEntityFeature.TARGET_TEMPERATURE
                )

    def test_local_cool_target_is_used_without_changing_temperature_limits(self) -> None:
        provider = FakeTlvReadProvider(
            {
                "operation.mode": "cool",
                "temperature.target_c": 25.5,
            }
        )
        entity = make_entity(provider)

        self.assertEqual(entity.target_temperature, 25.5)
        self.assertEqual(entity.min_temp, 16)
        self.assertEqual(entity.max_temp, 30)

    def test_legacy_hybrid_does_not_guess_an_auto_grid_without_model_authority(self) -> None:
        provider = FakeTlvReadProvider(
            {
                "operation.mode": "auto",
                "temperature.target_c": 7,
            }
        )
        entity = make_entity(provider)

        # Production Local ownership selects MyLgLocalClimate. This directly
        # injected legacy compatibility object lacks the pinned model domain,
        # so it must not guess one or reintroduce PAT as a fallback.
        self.assertIsNone(entity.target_temperature)
        self.assertTrue(
            entity.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE
        )

    def test_unknown_mode_does_not_classify_the_local_target_as_celsius(self) -> None:
        coordinator = FakePatCoordinator()
        coordinator.data["airConJobMode"].pop("currentJobMode")
        provider = FakeTlvReadProvider({"temperature.target_c": 7})

        self.assertIsNone(make_entity(provider, coordinator).target_temperature)

    def test_read_only_local_swing_does_not_create_an_unwritable_climate_surface(
        self,
    ) -> None:
        cases = (
            {"swing.horizontal_enabled"},
            {"swing.vertical_enabled"},
        )
        values = {
            "swing.horizontal_enabled": True,
            "swing.vertical_enabled": False,
        }
        for available in cases:
            with self.subTest(available=available):
                provider = FakeTlvReadProvider(values, available=available)
                entity = make_entity(provider)
                self.assertEqual(entity.swing_mode, SWING_OFF)
                self.assertFalse(
                    entity.supported_features & ClimateEntityFeature.SWING_MODE
                )


class LocalNativeClimateTests(unittest.IsolatedAsyncioTestCase):
    def _provider(self, **overrides: object) -> FakeTlvReadProvider:
        values: dict[str, object] = {
            "operation.power_requested": True,
            "operation.mode": "cool",
            "fan.mode": "high",
            "temperature.current_c": 27,
            "temperature.target_c": 25.5,
            "humidity.current_pct": 51,
            "swing.horizontal_enabled": False,
            "swing.vertical_enabled": True,
        }
        values.update(overrides)
        return FakeTlvReadProvider(values)

    def test_surface_is_derived_from_composite_authority_and_state_is_local_only(
        self,
    ) -> None:
        provider = self._provider()
        entity, _router, coordinator = make_local_only_entity(provider)
        coordinator.data = {
            "operation": {"airConOperationMode": "POWER_OFF"},
            "airConJobMode": {"currentJobMode": "AUTO"},
            "airFlow": {"windStrength": "LOW"},
            "temperature": {"currentTemperature": -99, "targetTemperature": -99},
        }

        self.assertEqual(
            entity.hvac_modes,
            [
                HVACMode.OFF,
                HVACMode.AUTO,
                HVACMode.COOL,
            ],
        )
        self.assertEqual(
            entity.fan_modes,
            ["AUTO", "HIGH", "LOW", "MID", "POWER", "VERY_LOW"],
        )
        self.assertEqual(entity.hvac_mode, HVACMode.COOL)
        self.assertEqual(entity.fan_mode, "HIGH")
        self.assertEqual(entity.current_temperature, 27)
        self.assertEqual(entity.target_temperature, 25.5)
        self.assertEqual(entity.current_humidity, 51)
        self.assertEqual((entity.min_temp, entity.max_temp), (16, 30))
        self.assertEqual(entity.target_temperature_step, 0.5)

    async def test_cst570_horizontal_extension_is_a_native_climate_control_not_a_second_switch(self):
        from custom_components.my_lg.local_control_contract import load_local_control_entity_contract, resolve_local_control_binding_eligibility, eligible_factory_descriptors
        from custom_components.my_lg.local_control_confirmed_features import augment_confirmed_features
        model, binding = 'CST_570004_WW', 'test_swing_binding_570'
        base = load_local_control_entity_contract()
        models = {binding: model}
        contract, scope = augment_confirmed_features(base, resolve_local_control_binding_eligibility({}, base, models), models)
        router = FakeLocalOnlyRouter()
        # Drive the real native entity using the actual augmented value grants.
        router.capability_authorized = lambda _device, cap: cap in scope[binding].values_by_capability
        router.value_authorized = lambda _device, cap, value: value in scope[binding].values_by_capability.get(cap, ())
        entity, _, coordinator = make_local_only_entity(self._provider(), FakePatCoordinator(model=model), router)
        self.assertIn(SWING_HORIZONTAL, entity.swing_modes)
        self.assertIn(SWING_BOTH, entity.swing_modes)
        await entity.async_set_swing_mode(SWING_VERTICAL)
        self.assertTrue(any(kind == 'flag' and call['capability'] == 'swing.horizontal_enabled' and call['enabled'] is False for kind, call in router.calls))
        self.assertFalse(any(d.capability_id == 'swing.horizontal_enabled' for d in eligible_factory_descriptors(contract, scope, binding_id=binding, model_id=model)))
        self.assertEqual(coordinator.controls, [])

    async def test_powered_off_device_remains_available_and_turn_on_is_local_only(
        self,
    ) -> None:
        provider = self._provider(**{"operation.power_requested": False})
        entity, router, coordinator = make_local_only_entity(provider)

        self.assertTrue(entity.available)
        self.assertEqual(entity.hvac_mode, HVACMode.OFF)
        await entity.async_turn_on()

        self.assertEqual(coordinator.controls, [])
        self.assertEqual(router.calls[0][0], "turn_on")
        self.assertIs(router.calls[0][1]["cloud_fallback"], False)

    async def test_every_offered_mode_fan_and_temperature_uses_only_local_router(
        self,
    ) -> None:
        provider = self._provider()
        entity, router, coordinator = make_local_only_entity(
            provider, coordinator=FakePatCoordinator(model="CST_570004_WW")
        )

        for mode in (HVACMode.COOL, HVACMode.DRY, HVACMode.FAN_ONLY):
            await entity.async_set_hvac_mode(mode)
        for fan in ("AUTO", "HIGH", "LOW", "MID", "POWER", "VERY_LOW"):
            await entity.async_set_fan_mode(fan)
        for target in (16, 16.5, 25.5, 30):
            await entity.async_set_temperature(**{ATTR_TEMPERATURE: target})

        self.assertEqual(coordinator.controls, [])
        self.assertEqual(len(router.calls), 13)
        self.assertTrue(
            all(
                call[1].get("cloud_fallback") is False
                for call in router.calls
                if call[0] in {"climate", "turn_on"}
            )
        )

    async def test_dry_and_fan_only_targets_follow_the_exact_local_domain(self) -> None:
        for token in ("dry", "fan_only"):
            with self.subTest(token=token):
                provider = self._provider(**{"operation.mode": token})
                entity, router, coordinator = make_local_only_entity(
                    provider,
                    coordinator=FakePatCoordinator(model="CST_570004_WW"),
                )

                self.assertEqual(entity.target_temperature, 25.5)
                self.assertTrue(
                    entity.supported_features
                    & ClimateEntityFeature.TARGET_TEMPERATURE
                )
                await entity.async_set_temperature(**{ATTR_TEMPERATURE: 26})

                self.assertEqual(coordinator.controls, [])
                self.assertEqual(router.calls[0][0], "climate")
                self.assertEqual(router.calls[0][1]["target_c"], 26)
                self.assertIs(router.calls[0][1]["cloud_fallback"], False)

    async def test_both_cst_models_offer_auto_but_never_expose_its_carrier_as_celsius(
        self,
    ) -> None:
        for model in ("CST_170004_WW", "CST_570004_WW"):
            with self.subTest(model=model):
                provider = self._provider(
                    **{
                        "operation.mode": "auto",
                        "comfort.preference_step": 2,
                        # Deliberate stale/wrong-shape carrier: even if present,
                        # AUTO must never present it as degrees Celsius.
                        "temperature.target_c": 17,
                    }
                )
                coordinator = FakePatCoordinator(model=model)
                entity, router, coordinator = make_local_only_entity(
                    provider, coordinator=coordinator
                )

                self.assertEqual(entity.hvac_mode, HVACMode.AUTO)
                self.assertIn(HVACMode.AUTO, entity.hvac_modes)
                self.assertIsNone(entity.target_temperature)
                self.assertFalse(
                    entity.supported_features
                    & ClimateEntityFeature.TARGET_TEMPERATURE
                )
                with self.assertRaises(HomeAssistantError):
                    await entity.async_set_temperature(
                        **{ATTR_TEMPERATURE: 17}
                    )
                self.assertEqual(router.calls, [])
                self.assertEqual(coordinator.controls, [])

    async def test_auto_fan_change_retains_the_unitless_preference_not_a_temperature(
        self,
    ) -> None:
        provider = self._provider(
            **{
                "operation.mode": "auto",
                "comfort.preference_step": 1,
                "temperature.target_c": 17,
            }
        )
        coordinator = FakePatCoordinator(model="CST_570004_WW")
        entity, router, coordinator = make_local_only_entity(
            provider, coordinator=coordinator
        )

        self.assertIn(HVACMode.AUTO, entity.hvac_modes)
        self.assertEqual(entity.hvac_mode, HVACMode.AUTO)
        self.assertIsNone(entity.target_temperature)
        self.assertFalse(
            entity.supported_features
            & ClimateEntityFeature.TARGET_TEMPERATURE
        )

        await entity.async_set_fan_mode("LOW")

        self.assertEqual(coordinator.controls, [])
        self.assertEqual(router.calls[0][1]["fan"], "low")
        self.assertEqual(router.calls[0][1]["retained_comfort_preference"], 1)
        self.assertIsNone(router.calls[0][1]["retained_target_c"])
        self.assertIs(router.calls[0][1]["cloud_fallback"], False)

    async def test_auto_cannot_be_combined_with_a_celsius_temperature(
        self,
    ) -> None:
        provider = self._provider()
        coordinator = FakePatCoordinator(model="CST_570004_WW")
        entity, router, coordinator = make_local_only_entity(
            provider, coordinator=coordinator
        )

        with self.assertRaises(HomeAssistantError):
            await entity.async_set_temperature(
                **{ATTR_TEMPERATURE: 18, "hvac_mode": HVACMode.AUTO}
            )

        self.assertEqual(coordinator.controls, [])
        self.assertEqual(router.calls, [])

    async def test_off_auto_can_power_on_with_its_observed_preference(
        self,
    ) -> None:
        provider = self._provider(
            **{
                "operation.power_requested": False,
                "operation.mode": "auto",
                "comfort.preference_step": -1,
            }
        )
        coordinator = FakePatCoordinator(model="CST_570004_WW")
        entity, router, _coordinator = make_local_only_entity(
            provider, coordinator=coordinator
        )

        await entity.async_set_hvac_mode(HVACMode.AUTO)

        self.assertEqual(router.calls[0][0], "turn_on")
        self.assertEqual(router.calls[0][1]["mode"], "auto")
        self.assertEqual(router.calls[0][1]["retained_comfort_preference"], -1)
        self.assertIsNone(router.calls[0][1]["retained_target_c"])
        self.assertIs(router.calls[0][1]["cloud_fallback"], False)

    async def test_power_fan_hides_placeholder_and_leaves_with_local_cached_target(
        self,
    ) -> None:
        provider = self._provider(**{"temperature.target_c": 25.5})
        entity, router, _coordinator = make_local_only_entity(provider)
        provider.values["fan.mode"] = "power"
        provider.values["temperature.target_c"] = 18
        provider.notify()

        self.assertEqual(entity.fan_mode, "POWER")
        self.assertIsNone(entity.target_temperature)
        self.assertFalse(
            entity.supported_features
            & ClimateEntityFeature.TARGET_TEMPERATURE
        )
        with self.assertRaises(HomeAssistantError):
            await entity.async_set_temperature(**{ATTR_TEMPERATURE: 24})
        await entity.async_set_fan_mode("LOW")

        self.assertEqual(router.calls[-1][1]["fan"], "low")
        self.assertEqual(router.calls[-1][1]["retained_target_c"], 25.5)
        self.assertIs(router.calls[-1][1]["cloud_fallback"], False)

    async def test_prewire_refusal_is_an_error_but_postwire_pending_is_not_retried(
        self,
    ) -> None:
        provider = self._provider()
        refused = FakeLocalOnlyRouter(outcome=object())
        refused.outcome = None
        entity, refused, coordinator = make_local_only_entity(
            provider, router=refused
        )
        with self.assertRaises(HomeAssistantError):
            await entity.async_set_fan_mode("LOW")
        self.assertEqual(len(refused.calls), 1)
        self.assertEqual(coordinator.controls, [])

        pending = FakeLocalOnlyRouter(LocalCommandPending("already on wire"))
        entity, pending, coordinator = make_local_only_entity(
            provider, router=pending
        )
        await entity.async_set_fan_mode("LOW")
        self.assertEqual(len(pending.calls), 1)
        self.assertEqual(coordinator.controls, [])

    async def test_local_listener_refreshes_until_entity_is_removed(self) -> None:
        provider = self._provider()
        entity, _router, _coordinator = make_local_only_entity(provider)
        entity.async_write_ha_state = Mock()

        await entity.async_added_to_hass()
        self.assertEqual(len(provider.listeners), 1)
        provider.notify()
        entity.async_write_ha_state.assert_called_once_with()

        await entity.async_will_remove_from_hass()
        self.assertEqual(provider.listeners, [])
        provider.notify()
        entity.async_write_ha_state.assert_called_once_with()


class ClimateLocalReadLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_power_state_is_the_power_on_write_gate(self) -> None:
        """A stale PAT ON must not skip the Local power-on tuple."""
        coordinator = FakePatCoordinator(model="CST_570004_WW")
        provider = FakeTlvReadProvider(
            {
                "operation.power_requested": False,
                "operation.mode": "cool",
            }
        )
        router = FakeLocalOnlyRouter()
        entity, router, _coordinator = make_local_only_entity(
            provider,
            coordinator=coordinator,
            router=router,
        )

        await entity.async_set_hvac_mode(HVACMode.DRY)

        self.assertEqual([call[0] for call in router.calls], ["turn_on"])
        self.assertEqual(router.calls[0][1]["mode"], "dry")

    async def test_local_mode_is_the_temperature_write_gate(self) -> None:
        """A stale PAT COOL must not authorize a target while Local says DRY."""
        coordinator = FakePatCoordinator()
        provider = FakeTlvReadProvider({"operation.mode": "dry"})
        entity = climate.MyLgClimate(
            coordinator,
            None,
            provider,  # type: ignore[arg-type]
        )

        with self.assertRaisesRegex(HomeAssistantError, "제습 모드"):
            await entity.async_set_temperature(**{ATTR_TEMPERATURE: 25})

    async def test_provider_updates_refresh_entity_until_removal(self) -> None:
        provider = FakeTlvReadProvider({"temperature.current_c": 20})
        entity = make_entity(provider)
        entity.async_write_ha_state = Mock()

        await entity.async_added_to_hass()
        self.assertEqual(len(provider.listeners), 1)
        provider.notify()
        entity.async_write_ha_state.assert_called_once_with()

        await entity.async_will_remove_from_hass()
        self.assertEqual(provider.listeners, [])
        provider.notify()
        entity.async_write_ha_state.assert_called_once_with()

    async def test_entity_without_local_provider_keeps_normal_lifecycle(self) -> None:
        entity = make_entity(None)
        entity.async_write_ha_state = Mock()

        await entity.async_added_to_hass()
        await entity.async_will_remove_from_hass()


if __name__ == "__main__":
    unittest.main()
