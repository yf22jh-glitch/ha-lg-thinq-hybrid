"""Local-first state and routing contracts for existing AC select/switch entities."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable
import unittest

from homeassistant.exceptions import HomeAssistantError

from custom_components.my_lg import select as select_platform
from custom_components.my_lg import switch as switch_platform
from custom_components.my_lg.const import DEVICE_TYPE_AIR_CONDITIONER
from custom_components.my_lg.local_command import (
    CLIMATE_POWER_ON_CAPABILITY,
    CLIMATE_TUPLE_CAPABILITY,
    LocalCommandFailed,
    LocalCommandPending,
)
from custom_components.my_lg.local_control_composite_domain import (
    load_local_control_composite_domain_contract,
)
from custom_components.my_lg.local_read_owner import local_climate_control_domain
from custom_components.my_lg.select import (
    SELECTS_BY_TYPE,
    WIDEQ_SELECTS_BY_TYPE,
    MyLgLocalAutoComfortSelect,
    MyLgSelect,
    MyLgWideqSelect,
)
from custom_components.my_lg.switch import (
    SWITCHES_BY_TYPE,
    WIDEQ_SWITCHES_BY_TYPE,
    MyLgSwitch,
    MyLgWideqSwitch,
)
from tests.test_ac_control_contract import (
    FakeLocalRouter,
    FakePatCoordinator,
    FakeWideqCoordinator,
)


class FakeReadProvider:
    """Observable local read provider with independently controlled availability."""

    def __init__(
        self,
        values: dict[str, Any] | None = None,
        available: set[str] | None = None,
        contract_semantics: set[str] | None = None,
    ) -> None:
        self.values = dict(values or {})
        self.available = set(self.values if available is None else available)
        semantics = set(self.values) if contract_semantics is None else set(contract_semantics)
        contracts = {
            semantic_id: SimpleNamespace(
                value_type=(
                    "boolean"
                    if semantic_id == "operation.power_requested"
                    else "number"
                    if semantic_id in {
                        "temperature.target_c",
                        "comfort.preference_step",
                    }
                    else "string"
                )
            )
            for semantic_id in semantics
        }
        self.profile = SimpleNamespace(
            fields=contracts,
            fields_by_semantic_id=contracts,
        )
        self.shadow_healthy = True
        self.listeners: list[Callable[[], None]] = []
        self.remove_calls = 0

    def field_available(self, semantic_id: str) -> bool:
        return semantic_id in self.available

    def field_value(self, semantic_id: str) -> Any:
        return self.values.get(semantic_id)

    def semantic_field_available(self, semantic_id: str) -> bool:
        return self.field_available(semantic_id)

    def async_add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        self.listeners.append(callback)
        removed = False

        def remove() -> None:
            nonlocal removed
            if removed:
                return
            removed = True
            self.remove_calls += 1
            self.listeners.remove(callback)

        return remove

    def emit(self) -> None:
        for listener in tuple(self.listeners):
            listener()


def _pat_select_description(key: str):
    return next(
        item
        for item in SELECTS_BY_TYPE[DEVICE_TYPE_AIR_CONDITIONER]
        if item.key == key
    )


def _wideq_select_description(key: str):
    return next(
        item
        for item in WIDEQ_SELECTS_BY_TYPE[DEVICE_TYPE_AIR_CONDITIONER]
        if item.key == key
    )


def _pat_switch_description(key: str):
    return next(
        item
        for item in SWITCHES_BY_TYPE[DEVICE_TYPE_AIR_CONDITIONER]
        if item.key == key
    )


def _wideq_switch_description(key: str):
    return next(
        item
        for item in WIDEQ_SWITCHES_BY_TYPE[DEVICE_TYPE_AIR_CONDITIONER]
        if item.key == key
    )


SPECIAL_WIND_CASES = (
    (
        "wind_auto_fit",
        "autoFitWind",
        "airflow.auto_temperature_enabled",
    ),
    ("wind_forest", "forestWind", "airflow.forest_enabled"),
    (
        "wind_concentration",
        "concentrationWind",
        "airflow.study_enabled",
    ),
    ("wind_manner", "mannerWind", "airflow.quiet_enabled"),
    (
        "wind_long_power",
        "longPowerWind",
        "airflow.long_distance_enabled",
    ),
)
SPECIAL_WIND_MODELS = ("CST_170004_WW", "CST_570004_WW")


def _local_auto_comfort_entity(
    *,
    power: bool = True,
    mode: str = "auto",
    preference: int = 0,
    router: FakeLocalRouter | None = None,
) -> tuple[
    MyLgLocalAutoComfortSelect,
    FakeReadProvider,
    FakeLocalRouter,
    FakePatCoordinator,
]:
    coordinator = FakePatCoordinator()
    provider = FakeReadProvider(
        {
            "operation.power_requested": power,
            "operation.mode": mode,
            "fan.mode": "medium",
            "temperature.target_c": 24.0,
            "comfort.preference_step": preference,
        }
    )
    contract = load_local_control_composite_domain_contract()
    domain = local_climate_control_domain(
        provider,  # type: ignore[arg-type]
        coordinator.model,
        contract,
    )
    assert domain is not None
    selected_router = router or FakeLocalRouter()
    return (
        MyLgLocalAutoComfortSelect(
            coordinator,
            provider,  # type: ignore[arg-type]
            selected_router,  # type: ignore[arg-type]
            domain,
        ),
        provider,
        selected_router,
        coordinator,
    )


class AcLocalAutoComfortSelectTests(unittest.IsolatedAsyncioTestCase):
    async def test_platform_materializes_exactly_one_owner_from_the_shared_contract(
        self,
    ) -> None:
        coordinator = FakePatCoordinator()
        coordinator.device_type = ""
        provider = FakeReadProvider(
            contract_semantics={
                "operation.power_requested",
                "operation.mode",
                "fan.mode",
                "temperature.target_c",
                "comfort.preference_step",
            }
        )
        runtime = SimpleNamespace(
            coordinators={coordinator.device_id: coordinator},
            wideq_coordinator=None,
            local_control=FakeLocalRouter(),
            local_providers={coordinator.device_id: provider},
            local_read_providers={coordinator.device_id: provider},
            local_control_composite_domain_contract=(
                load_local_control_composite_domain_contract()
            ),
        )
        entry = SimpleNamespace(runtime_data=runtime, options={})
        entities: list[Any] = []

        await select_platform.async_setup_entry(None, entry, entities.extend)

        self.assertEqual(
            sum(isinstance(entity, MyLgLocalAutoComfortSelect) for entity in entities),
            1,
        )

        provider.profile.fields["comfort.preference_step"].value_type = "string"
        entities = []
        await select_platform.async_setup_entry(None, entry, entities.extend)
        self.assertFalse(
            any(isinstance(entity, MyLgLocalAutoComfortSelect) for entity in entities)
        )

    def test_exact_app_vocabulary_and_local_readback(self) -> None:
        expected = (
            (-2, "warmer"),
            (-1, "slightly_warmer"),
            (0, "comfortable"),
            (1, "slightly_cooler"),
            (2, "cooler"),
        )
        entity, provider, _router, _coordinator = _local_auto_comfort_entity()
        self.assertEqual(entity.options, [option for _, option in expected])
        for value, option in expected:
            with self.subTest(value=value):
                provider.values["comfort.preference_step"] = value
                self.assertEqual(entity.current_option, option)

        provider.values["operation.mode"] = "cool"
        self.assertIsNone(entity.current_option)
        self.assertTrue(
            entity.available,
            "the select must remain usable to enter AUTO with an explicit preference",
        )

    async def test_powered_on_and_off_use_only_the_matching_local_capability(
        self,
    ) -> None:
        on_router = FakeLocalRouter()
        on_router.authorized_capabilities = {CLIMATE_TUPLE_CAPABILITY}
        on, _provider, on_router, coordinator = _local_auto_comfort_entity(
            power=True, router=on_router
        )
        self.assertTrue(on.available)
        await on.async_select_option("slightly_cooler")
        self.assertEqual(
            on_router.calls,
            [
                (
                    "climate",
                    {
                        "device_id": coordinator.device_id,
                        "mode": "auto",
                        "comfort_preference": 1,
                        "cloud_fallback": False,
                    },
                )
            ],
        )

        off_router = FakeLocalRouter()
        off_router.authorized_capabilities = {CLIMATE_TUPLE_CAPABILITY}
        off, _provider, off_router, coordinator = _local_auto_comfort_entity(
            power=False, router=off_router
        )
        self.assertFalse(off.available)
        off_router.authorized_capabilities = {CLIMATE_POWER_ON_CAPABILITY}
        self.assertTrue(off.available)
        await off.async_select_option("warmer")
        self.assertEqual(
            off_router.calls,
            [
                (
                    "turn_on",
                    {
                        "device_id": coordinator.device_id,
                        "mode": "auto",
                        "comfort_preference": -2,
                        "cloud_fallback": False,
                    },
                )
            ],
        )

    async def test_missing_power_and_postwire_pending_never_fall_back(self) -> None:
        entity, provider, router, coordinator = _local_auto_comfort_entity()
        provider.available.remove("operation.power_requested")
        self.assertFalse(entity.available)
        with self.assertRaises(HomeAssistantError):
            await entity.async_select_option("comfortable")
        self.assertEqual(router.calls, [])
        self.assertEqual(coordinator.controls, [])

        pending_router = FakeLocalRouter(LocalCommandPending("already on wire"))
        entity, _provider, pending_router, coordinator = _local_auto_comfort_entity(
            router=pending_router
        )
        await entity.async_select_option("cooler")
        self.assertEqual(len(pending_router.calls), 1)
        self.assertEqual(coordinator.controls, [])

    async def test_provider_listener_is_removed_with_entity(self) -> None:
        entity, provider, _router, _coordinator = _local_auto_comfort_entity()
        writes: list[str | None] = []
        entity.async_write_ha_state = lambda: writes.append(entity.current_option)

        await entity.async_added_to_hass()
        provider.values["comfort.preference_step"] = 2
        provider.emit()
        self.assertEqual(writes, ["cooler"])

        await entity.async_will_remove_from_hass()
        provider.values["comfort.preference_step"] = -2
        provider.emit()
        self.assertEqual(writes, ["cooler"])
        self.assertEqual(provider.remove_calls, 1)


class AcLocalSelectReadTests(unittest.IsolatedAsyncioTestCase):
    def test_detailed_fan_uses_exact_local_readback_before_pat(self) -> None:
        expected = {
            "very low": "SLOW_LOW",
            "low": "LOW",
            "medium": "MID",
            "high": "HIGH",
            "power": "POWER",
            "auto": "AUTO",
        }
        for local_value, option in expected.items():
            with self.subTest(local_value=local_value):
                coordinator = FakePatCoordinator()
                coordinator.data.setdefault("airFlow", {})[
                    "windStrengthDetail"
                ] = "LOW"
                provider = FakeReadProvider({"fan.mode": local_value})
                entity = MyLgSelect(
                    coordinator,
                    _pat_select_description("wind_strength_detail"),
                    local_control=FakeLocalRouter(),
                    local_read_provider=provider,
                )

                self.assertEqual(entity.current_option, option)

    def test_detailed_fan_local_owner_never_falls_back_to_pat(self) -> None:
        coordinator = FakePatCoordinator()
        coordinator.data.setdefault("airFlow", {})[
            "windStrengthDetail"
        ] = "HIGH"
        unavailable = FakeReadProvider({"fan.mode": "low"}, available=set())
        entity = MyLgSelect(
            coordinator,
            _pat_select_description("wind_strength_detail"),
            local_read_provider=unavailable,
        )
        self.assertIsNone(entity.current_option)
        self.assertFalse(entity.available)

        for invalid_value in ("unreviewed", 1):
            with self.subTest(invalid_value=invalid_value):
                invalid_available = FakeReadProvider(
                    {"fan.mode": invalid_value}
                )
                entity = MyLgSelect(
                    coordinator,
                    _pat_select_description("wind_strength_detail"),
                    local_read_provider=invalid_available,
                )
                self.assertIsNone(entity.current_option)
                self.assertFalse(entity.available)

    def test_wideq_selects_use_only_reviewed_local_read_values(self) -> None:
        expected = {
            "auto_dry": (
                "auto_dry.mode",
                {
                    "off": "off",
                    "10 min or firmware ON": "on",
                    "30 min": "30min",
                    "60 min": "60min",
                    "smart": "ai_auto",
                },
            ),
            "display_brightness": (
                "display.brightness_level",
                {"off": "off", "50%": "50", "100%": "100"},
            ),
        }
        for key, (semantic_id, values) in expected.items():
            for local_value, option in values.items():
                with self.subTest(key=key, local_value=local_value):
                    pat = FakePatCoordinator()
                    wideq = FakeWideqCoordinator()
                    provider = FakeReadProvider({semantic_id: local_value})
                    entity = MyLgWideqSelect(
                        wideq,
                        pat,
                        _wideq_select_description(key),
                        local_control=FakeLocalRouter(),
                        local_read_provider=provider,
                    )
                    self.assertEqual(entity.current_option, option)

    def test_wideq_select_local_owner_never_falls_back_to_wideq(
        self,
    ) -> None:
        pat = FakePatCoordinator()
        wideq = FakeWideqCoordinator()
        wideq.snapshots[pat.device_id][
            "airState.miscFuncState.autoDry"
        ] = 2
        unavailable = FakeReadProvider(
            {"auto_dry.mode": "off"}, available=set()
        )
        entity = MyLgWideqSelect(
            wideq,
            pat,
            _wideq_select_description("auto_dry"),
            local_read_provider=unavailable,
        )
        self.assertIsNone(entity.current_option)
        self.assertFalse(entity.available)

        for invalid_value in ("unknown", 30):
            with self.subTest(invalid_value=invalid_value):
                invalid_available = FakeReadProvider(
                    {"auto_dry.mode": invalid_value}
                )
                entity = MyLgWideqSelect(
                    wideq,
                    pat,
                    _wideq_select_description("auto_dry"),
                    local_read_provider=invalid_available,
                )
                self.assertIsNone(entity.current_option)
                self.assertFalse(entity.available)

    def test_selects_without_a_local_owner_keep_the_established_cloud_source(
        self,
    ) -> None:
        pat = FakePatCoordinator()
        pat.data.setdefault("airFlow", {})["windStrengthDetail"] = "HIGH"
        self.assertEqual(
            MyLgSelect(
                pat,
                _pat_select_description("wind_strength_detail"),
            ).current_option,
            "HIGH",
        )

        wideq = FakeWideqCoordinator()
        wideq.snapshots[pat.device_id]["airState.miscFuncState.autoDry"] = 2
        self.assertEqual(
            MyLgWideqSelect(
                wideq,
                pat,
                _wideq_select_description("auto_dry"),
            ).current_option,
            "30min",
        )

    def test_local_read_keeps_select_available_without_a_cloud_snapshot(self) -> None:
        pat = FakePatCoordinator()
        pat.data = {}
        fan = MyLgSelect(
            pat,
            _pat_select_description("wind_strength_detail"),
            local_control=FakeLocalRouter(),
            local_read_provider=FakeReadProvider({"fan.mode": "very low"}),
        )
        self.assertTrue(fan.available)
        self.assertEqual(fan.current_option, "SLOW_LOW")

        wideq = FakeWideqCoordinator()
        wideq.snapshots[pat.device_id] = {}
        auto_dry = MyLgWideqSelect(
            wideq,
            pat,
            _wideq_select_description("auto_dry"),
            local_control=FakeLocalRouter(),
            local_read_provider=FakeReadProvider({"auto_dry.mode": "smart"}),
        )
        self.assertTrue(auto_dry.available)
        self.assertEqual(auto_dry.current_option, "ai_auto")

    def test_invalid_local_only_select_is_unavailable_and_logs_semantic_once(
        self,
    ) -> None:
        pat = FakePatCoordinator()
        pat.data = {}
        invalid_fan = MyLgSelect(
            pat,
            _pat_select_description("wind_strength_detail"),
            local_read_provider=FakeReadProvider(
                {"fan.mode": "raw-value-must-not-be-logged"}
            ),
        )
        wideq = FakeWideqCoordinator()
        wideq.snapshots[pat.device_id] = {}
        invalid_auto_dry = MyLgWideqSelect(
            wideq,
            pat,
            _wideq_select_description("auto_dry"),
            local_read_provider=FakeReadProvider(
                {"auto_dry.mode": "another-private-raw-value"}
            ),
        )

        with self.assertLogs(
            "custom_components.my_lg.select", level="WARNING"
        ) as caught:
            self.assertIsNone(invalid_fan.current_option)
            self.assertFalse(invalid_fan.available)
            self.assertFalse(invalid_fan.available)
            self.assertIsNone(invalid_auto_dry.current_option)
            self.assertFalse(invalid_auto_dry.available)
            self.assertFalse(invalid_auto_dry.available)

        self.assertEqual(len(caught.records), 2)
        rendered = "\n".join(caught.output)
        self.assertIn("fan.mode", rendered)
        self.assertIn("auto_dry.mode", rendered)
        self.assertNotIn("raw-value-must-not-be-logged", rendered)
        self.assertNotIn("another-private-raw-value", rendered)
        self.assertNotIn(pat.device_id, rendered)

    async def test_provider_listener_updates_state_and_is_removed_with_entity(self) -> None:
        coordinator = FakePatCoordinator()
        coordinator.data.setdefault("airFlow", {})[
            "windStrengthDetail"
        ] = "LOW"
        provider = FakeReadProvider({"fan.mode": "low"})
        router = FakeLocalRouter()
        entity = MyLgSelect(
            coordinator,
            _pat_select_description("wind_strength_detail"),
            local_control=router,
            local_read_provider=provider,
        )
        writes: list[str | None] = []
        entity.async_write_ha_state = lambda: writes.append(entity.current_option)

        await entity.async_added_to_hass()
        self.assertEqual(len(router.condition_listeners), 1)
        provider.values["fan.mode"] = "high"
        provider.emit()
        self.assertEqual(writes, ["HIGH"])

        await entity.async_will_remove_from_hass()
        self.assertEqual(router.condition_listeners, [])
        provider.values["fan.mode"] = "auto"
        provider.emit()
        self.assertEqual(writes, ["HIGH"])
        self.assertEqual(provider.remove_calls, 1)


class AcLocalSwitchReadTests(unittest.IsolatedAsyncioTestCase):
    def test_both_power_save_switches_use_exact_local_boolean_readback(self) -> None:
        cases = (
            (
                "energy_saving.enabled",
                MyLgSwitch,
                _pat_switch_description("power_save"),
            ),
            (
                "comfort_energy_saving.enabled",
                MyLgWideqSwitch,
                _wideq_switch_description("comfortable_power_save"),
            ),
        )
        for semantic_id, entity_type, description in cases:
            for value in (False, True):
                with self.subTest(semantic_id=semantic_id, value=value):
                    pat = FakePatCoordinator()
                    provider = FakeReadProvider({semantic_id: value})
                    if entity_type is MyLgSwitch:
                        entity = entity_type(
                            pat,
                            description,
                            local_read_provider=provider,
                        )
                    else:
                        entity = entity_type(
                            FakeWideqCoordinator(),
                            pat,
                            description,
                            local_read_provider=provider,
                        )
                    self.assertIs(entity.is_on, value)

    def test_switch_local_owner_never_falls_back_to_pat_or_wideq(self) -> None:
        pat = FakePatCoordinator()
        pat.data["powerSave"]["powerSaveEnabled"] = True
        unavailable = FakeReadProvider(
            {"energy_saving.enabled": False}, available=set()
        )
        entity = MyLgSwitch(
            pat,
            _pat_switch_description("power_save"),
            local_read_provider=unavailable,
        )
        self.assertIsNone(entity.is_on)
        self.assertFalse(entity.available)

        invalid_available = FakeReadProvider({"energy_saving.enabled": 1})
        entity = MyLgSwitch(
            pat,
            _pat_switch_description("power_save"),
            local_read_provider=invalid_available,
        )
        self.assertIsNone(entity.is_on)
        self.assertFalse(entity.available)

        wideq = FakeWideqCoordinator()
        wideq.power_save[pat.device_id]["airState.powerSave.hum"] = True
        comfort = MyLgWideqSwitch(
            wideq,
            pat,
            _wideq_switch_description("comfortable_power_save"),
            local_read_provider=FakeReadProvider(
                {"comfort_energy_saving.enabled": "true"}
            ),
        )
        self.assertIsNone(comfort.is_on)
        self.assertFalse(comfort.available)

    def test_switches_without_a_local_owner_keep_the_established_cloud_source(
        self,
    ) -> None:
        pat = FakePatCoordinator()
        pat.data["powerSave"]["powerSaveEnabled"] = True
        self.assertTrue(
            MyLgSwitch(pat, _pat_switch_description("power_save")).is_on
        )

        wideq = FakeWideqCoordinator()
        wideq.power_save[pat.device_id]["airState.powerSave.hum"] = True
        self.assertTrue(
            MyLgWideqSwitch(
                wideq,
                pat,
                _wideq_switch_description("comfortable_power_save"),
            ).is_on
        )

    def test_switch_local_only_availability_requires_an_exact_boolean(self) -> None:
        pat = FakePatCoordinator()
        pat.data = {}
        valid = MyLgSwitch(
            pat,
            _pat_switch_description("power_save"),
            local_read_provider=FakeReadProvider(
                {"energy_saving.enabled": True}
            ),
        )
        self.assertTrue(valid.available)
        self.assertTrue(valid.is_on)

        invalid = MyLgSwitch(
            pat,
            _pat_switch_description("power_save"),
            local_read_provider=FakeReadProvider(
                {"energy_saving.enabled": "private-invalid-value"}
            ),
        )
        wideq = FakeWideqCoordinator()
        wideq.power_save[pat.device_id] = {}
        valid_comfort = MyLgWideqSwitch(
            wideq,
            pat,
            _wideq_switch_description("comfortable_power_save"),
            local_read_provider=FakeReadProvider(
                {"comfort_energy_saving.enabled": False}
            ),
        )
        invalid_comfort = MyLgWideqSwitch(
            wideq,
            pat,
            _wideq_switch_description("comfortable_power_save"),
            local_read_provider=FakeReadProvider(
                {"comfort_energy_saving.enabled": 1}
            ),
        )

        with self.assertLogs(
            "custom_components.my_lg.switch", level="WARNING"
        ) as caught:
            self.assertFalse(invalid.available)
            self.assertFalse(invalid.available)
            self.assertTrue(valid_comfort.available)
            self.assertFalse(valid_comfort.is_on)
            self.assertFalse(invalid_comfort.available)
            self.assertFalse(invalid_comfort.available)

        self.assertEqual(len(caught.records), 2)
        rendered = "\n".join(caught.output)
        self.assertIn("energy_saving.enabled", rendered)
        self.assertIn("comfort_energy_saving.enabled", rendered)
        self.assertNotIn("private-invalid-value", rendered)
        self.assertNotIn(pat.device_id, rendered)

    def test_special_winds_use_the_same_exact_local_semantic_for_read_and_control(
        self,
    ) -> None:
        for model in SPECIAL_WIND_MODELS:
            for key, pat_field, semantic_id in SPECIAL_WIND_CASES:
                for pat_value, sparse_local_value in (
                    (False, True),
                    (True, False),
                ):
                    with self.subTest(
                        model=model,
                        key=key,
                        pat_value=pat_value,
                        sparse_local_value=sparse_local_value,
                    ):
                        pat = FakePatCoordinator()
                        pat.model = model
                        pat.data.setdefault("windDirection", {})[
                            pat_field
                        ] = pat_value
                        description = _pat_switch_description(key)
                        entity = MyLgSwitch(
                            pat,
                            description,
                            local_read_provider=FakeReadProvider(
                                {semantic_id: sparse_local_value}
                            ),
                        )

                        self.assertEqual(
                            description.local_control_semantic, semantic_id
                        )
                        self.assertEqual(description.local_read_semantic, semantic_id)
                        self.assertTrue(description.local_control_on_only)
                        self.assertIs(entity.is_on, sparse_local_value)

    async def test_switch_provider_listener_updates_and_unsubscribes(self) -> None:
        pat = FakePatCoordinator()
        provider = FakeReadProvider({"energy_saving.enabled": False})
        entity = MyLgSwitch(
            pat,
            _pat_switch_description("power_save"),
            local_read_provider=provider,
        )
        writes: list[bool | None] = []
        entity.async_write_ha_state = lambda: writes.append(entity.is_on)

        await entity.async_added_to_hass()
        provider.values["energy_saving.enabled"] = True
        provider.emit()
        self.assertEqual(writes, [True])

        await entity.async_will_remove_from_hass()
        provider.values["energy_saving.enabled"] = False
        provider.emit()
        self.assertEqual(writes, [True])
        self.assertEqual(provider.remove_calls, 1)

    async def test_special_wind_subscribes_to_its_exact_local_readback(
        self,
    ) -> None:
        pat = FakePatCoordinator()
        pat.data["windDirection"]["forestWind"] = True
        provider = FakeReadProvider({"airflow.forest_enabled": False})
        entity = MyLgSwitch(
            pat,
            _pat_switch_description("wind_forest"),
            local_read_provider=provider,
        )
        writes: list[bool | None] = []
        entity.async_write_ha_state = lambda: writes.append(entity.is_on)

        await entity.async_added_to_hass()
        self.assertEqual(len(provider.listeners), 1)
        self.assertFalse(entity.is_on)
        provider.values["airflow.forest_enabled"] = True
        provider.emit()
        self.assertEqual(writes, [True])

        await entity.async_will_remove_from_hass()
        provider.values["airflow.forest_enabled"] = False
        provider.emit()
        self.assertEqual(writes, [True])
        self.assertEqual(provider.remove_calls, 1)


class AcLocalSpecialWindWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_off_extension_completes_native_switches_without_cloud_fallback(self):
        class ExtendedRouter(FakeLocalRouter):
            def authorized_values(self, device_id, capability):
                if capability.startswith('airflow.'):
                    return ('false', 'true')
                return super().authorized_values(device_id, capability)
        for model in SPECIAL_WIND_MODELS:
            pat = FakePatCoordinator('COOL')
            pat.model = model
            router = ExtendedRouter()
            semantics = {s for _, _, s in SPECIAL_WIND_CASES}
            provider = FakeReadProvider({s: True for s in semantics})
            runtime = SimpleNamespace(coordinators={pat.device_id: pat},
                wideq_coordinator=None, local_control=router,
                local_read_providers={pat.device_id: provider})
            entities = []
            await switch_platform.async_setup_entry(None, SimpleNamespace(runtime_data=runtime), entities.extend)
            winds = [e for e in entities if e.entity_description.local_control_semantic in semantics]
            self.assertEqual(len(winds), 5)
            for entity in winds:
                await entity.async_turn_off()
                self.assertEqual(router.calls[-1], ('flag', {'device_id': pat.device_id,
                    'capability': entity.entity_description.local_control_semantic, 'enabled': False}))
                # Never optimistically clear an own-state switch.
                self.assertTrue(entity.is_on)
            self.assertEqual(pat.controls, [])
            router.outcome = None
            with self.assertRaisesRegex(HomeAssistantError, '기기나 LG 앱에서 꺼 주세요'):
                await winds[0].async_turn_off()
            self.assertEqual(pat.controls, [])

    async def test_old_on_only_authority_cannot_send_off_even_with_an_existing_entity(self):
        pat = FakePatCoordinator('COOL')
        router = FakeLocalRouter()
        for key, _, semantic in SPECIAL_WIND_CASES:
            entity = MyLgSwitch(pat, _pat_switch_description(key), router,
                FakeReadProvider({semantic: True}))
            with self.assertRaises(HomeAssistantError):
                await entity.async_turn_off()
        self.assertEqual(router.calls, [])
        self.assertEqual(pat.controls, [])

    async def test_one_sided_local_special_winds_are_not_created_as_switches(
        self,
    ) -> None:
        for model in SPECIAL_WIND_MODELS:
            with self.subTest(model=model):
                pat = FakePatCoordinator("COOL")
                pat.model = model
                semantics = {semantic_id for _, _, semantic_id in SPECIAL_WIND_CASES}
                runtime = SimpleNamespace(
                    coordinators={pat.device_id: pat},
                    wideq_coordinator=None,
                    local_control=FakeLocalRouter(),
                    local_read_providers={
                        pat.device_id: FakeReadProvider(
                            contract_semantics=semantics
                        )
                    },
                )
                entities: list[Any] = []

                await switch_platform.async_setup_entry(
                    None, SimpleNamespace(runtime_data=runtime), entities.extend
                )

                self.assertTrue(
                    semantics.isdisjoint(
                        {
                            item.entity_description.local_control_semantic
                            for item in entities
                        }
                    )
                )

    async def test_without_local_ownership_the_existing_cloud_switch_remains(
        self,
    ) -> None:
        pat = FakePatCoordinator("COOL")
        entity = MyLgSwitch(pat, _pat_switch_description("wind_forest"))

        await entity.async_turn_on()

        self.assertEqual(
            pat.controls, [{"windDirection": {"forestWind": True}}]
        )


class AcLocalSwitchModeGateTests(unittest.IsolatedAsyncioTestCase):
    def test_exact_local_operation_tokens_map_to_existing_pat_vocabulary(self) -> None:
        pat = FakePatCoordinator()
        provider = FakeReadProvider({"operation.mode": "cool"})
        entity = MyLgSwitch(
            pat,
            _pat_switch_description("power_save"),
            local_read_provider=provider,
        )
        expected = {
            "cool": "COOL",
            "dry": "AIR_DRY",
            "fan_only": "FAN",
            "auto": "AUTO",
        }
        for local_mode, pat_mode in expected.items():
            with self.subTest(local_mode=local_mode):
                provider.values["operation.mode"] = local_mode
                self.assertEqual(
                    entity._local_first_job_mode("PAT_FALLBACK"), pat_mode
                )

    async def test_general_and_special_wind_gates_use_local_mode_first(self) -> None:
        pat = FakePatCoordinator("AUTO")
        local_cool = FakeReadProvider({"operation.mode": "cool"})
        power_save = MyLgSwitch(
            pat,
            _pat_switch_description("power_save"),
            local_read_provider=local_cool,
        )
        await power_save.async_turn_on()
        self.assertEqual(
            pat.controls, [{"powerSave": {"powerSaveEnabled": True}}]
        )

        pat = FakePatCoordinator("FAN")
        local_dry = FakeReadProvider({"operation.mode": "dry"})
        special_wind = MyLgSwitch(
            pat,
            _pat_switch_description("wind_forest"),
            local_read_provider=local_dry,
        )
        await special_wind.async_turn_on()
        self.assertEqual(
            pat.controls, [{"windDirection": {"forestWind": True}}]
        )

        pat = FakePatCoordinator("COOL")
        local_fan = FakeReadProvider({"operation.mode": "fan_only"})
        power_save = MyLgSwitch(
            pat,
            _pat_switch_description("power_save"),
            local_read_provider=local_fan,
        )
        with self.assertRaisesRegex(HomeAssistantError, "냉방 모드"):
            await power_save.async_turn_on()
        self.assertEqual(pat.controls, [])

    async def test_comfort_gate_uses_local_mode_first(self) -> None:
        pat = FakePatCoordinator("AUTO")
        wideq = FakeWideqCoordinator()
        comfort = MyLgWideqSwitch(
            wideq,
            pat,
            _wideq_switch_description("comfortable_power_save"),
            local_read_provider=FakeReadProvider({"operation.mode": "cool"}),
        )
        await comfort.async_turn_on()
        self.assertEqual(len(wideq.controls), 1)

        pat = FakePatCoordinator("COOL")
        wideq = FakeWideqCoordinator()
        comfort = MyLgWideqSwitch(
            wideq,
            pat,
            _wideq_switch_description("comfortable_power_save"),
            local_read_provider=FakeReadProvider({"operation.mode": "dry"}),
        )
        with self.assertRaisesRegex(HomeAssistantError, "냉방 모드"):
            await comfort.async_turn_on()
        self.assertEqual(wideq.controls, [])

    async def test_local_owned_unavailable_or_invalid_mode_never_falls_back_to_pat(
        self,
    ) -> None:
        providers = (
            FakeReadProvider({"operation.mode": "cool"}, available=set()),
            FakeReadProvider({"operation.mode": "unknown"}),
            FakeReadProvider({"operation.mode": 1}),
        )
        for provider in providers:
            with self.subTest(value=provider.values["operation.mode"]):
                pat = FakePatCoordinator("COOL")
                entity = MyLgSwitch(
                    pat,
                    _pat_switch_description("power_save"),
                    local_read_provider=provider,
                )
                with self.assertRaises(HomeAssistantError):
                    await entity.async_turn_on()
                self.assertEqual(pat.controls, [])

    def test_invalid_operation_mode_logs_only_the_semantic_once(self) -> None:
        pat = FakePatCoordinator("COOL")
        entity = MyLgSwitch(
            pat,
            _pat_switch_description("power_save"),
            local_read_provider=FakeReadProvider(
                {"operation.mode": "private-unreviewed-mode"}
            ),
        )
        with self.assertLogs(
            "custom_components.my_lg.switch", level="WARNING"
        ) as caught:
            self.assertIsNone(entity._local_first_job_mode("COOL"))
            self.assertIsNone(entity._local_first_job_mode("COOL"))

        self.assertEqual(len(caught.records), 1)
        self.assertIn("operation.mode", caught.output[0])
        self.assertNotIn("private-unreviewed-mode", caught.output[0])
        self.assertNotIn(pat.device_id, caught.output[0])


class AcLocalSetupAndFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_platform_setup_injects_router_and_per_device_read_provider(
        self,
    ) -> None:
        pat = FakePatCoordinator()
        pat.data.setdefault("airFlow", {})["windStrengthDetail"] = "LOW"
        wideq = FakeWideqCoordinator()
        router = FakeLocalRouter()
        provider = FakeReadProvider(
            contract_semantics={
                "fan.mode",
                "auto_dry.mode",
                "display.brightness_level",
                "energy_saving.enabled",
                "comfort_energy_saving.enabled",
                *(semantic_id for _, _, semantic_id in SPECIAL_WIND_CASES),
            }
        )
        runtime = SimpleNamespace(
            coordinators={pat.device_id: pat},
            wideq_coordinator=wideq,
            local_control=router,
            local_read_providers={pat.device_id: provider},
        )
        entry = SimpleNamespace(runtime_data=runtime, options={})

        selects: list[Any] = []
        await select_platform.async_setup_entry(None, entry, selects.extend)
        fan = next(
            item
            for item in selects
            if item.entity_description.key == "wind_strength_detail"
        )
        auto_dry = next(
            item
            for item in selects
            if item.entity_description.key == "auto_dry"
        )
        self.assertIs(fan._local_control, router)
        self.assertIs(fan._local_read_provider, provider)
        self.assertIs(auto_dry._local_control, router)
        self.assertIs(auto_dry._local_read_provider, provider)

        switches: list[Any] = []
        await switch_platform.async_setup_entry(None, entry, switches.extend)
        power_save = next(
            item
            for item in switches
            if item.entity_description.key == "power_save"
        )
        self.assertNotIn(
            "wind_forest",
            {item.entity_description.key for item in switches},
        )
        comfort = next(
            item
            for item in switches
            if item.entity_description.key == "comfortable_power_save"
        )
        self.assertIs(power_save._local_control, router)
        self.assertIs(power_save._local_read_provider, provider)
        self.assertIs(comfort._local_control, router)
        self.assertIs(comfort._local_read_provider, provider)

    async def test_scalar_and_boolean_post_wire_failures_never_use_cloud(self) -> None:
        pat = FakePatCoordinator()
        wideq = FakeWideqCoordinator()
        cases = (
            MyLgWideqSelect(
                wideq,
                pat,
                _wideq_select_description("auto_dry"),
                local_control=FakeLocalRouter(LocalCommandFailed("uncertain")),
                local_read_provider=FakeReadProvider(
                    {"auto_dry.mode": "off"}
                ),
            ),
            MyLgWideqSelect(
                wideq,
                pat,
                _wideq_select_description("display_brightness"),
                local_control=FakeLocalRouter(LocalCommandFailed("uncertain")),
                local_read_provider=FakeReadProvider(
                    {"display.brightness_level": "off"}
                ),
            ),
            MyLgSwitch(
                pat,
                _pat_switch_description("power_save"),
                local_control=FakeLocalRouter(LocalCommandFailed("uncertain")),
                local_read_provider=FakeReadProvider(
                    {"energy_saving.enabled": False}
                ),
            ),
            MyLgWideqSwitch(
                wideq,
                pat,
                _wideq_switch_description("comfortable_power_save"),
                local_control=FakeLocalRouter(LocalCommandFailed("uncertain")),
                local_read_provider=FakeReadProvider(
                    {"comfort_energy_saving.enabled": False}
                ),
            ),
        )

        for entity in cases:
            with self.subTest(key=entity.entity_description.key):
                with self.assertRaises(HomeAssistantError):
                    if isinstance(entity, (MyLgSelect, MyLgWideqSelect)):
                        option = (
                            "50"
                            if entity.entity_description.key == "display_brightness"
                            else "30min"
                        )
                        await entity.async_select_option(option)
                    else:
                        await entity.async_turn_on()

        self.assertEqual(pat.controls, [])
        self.assertEqual(wideq.controls, [])


if __name__ == "__main__":
    unittest.main()
