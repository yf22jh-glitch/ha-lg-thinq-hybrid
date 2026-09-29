"""Runtime behavior for private-gated, Local-only Home Assistant controls."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory

from custom_components.my_lg.local_command import (
    LocalCommandBusy,
    LocalCommandFailed,
    LocalCommandNotReady,
    LocalCommandResult,
)
from custom_components.my_lg.local_control_contract import (
    EXPECTED_LOCAL_CONTROL_CHECKPOINT_REVISION,
    EXPECTED_LOCAL_CONTROL_CHECKPOINT_SHA256,
    EXPECTED_LOCAL_CONTROL_TARGET_AUTHORITY_REVISION,
    EXPECTED_LOCAL_CONTROL_TARGET_AUTHORITY_SHA256,
    LOCAL_CONTROL_ELIGIBILITY_OPTION,
    LocalControlEligibilityError,
    LocalControlEntityDescriptor,
    _load_bundled_local_control_entity_contract,
    load_local_control_entity_contract,
    resolve_local_control_binding_eligibility,
)
from custom_components.my_lg.local_control_entity import (
    MyLgLocalContractButton,
    MyLgLocalContractNumber,
    MyLgLocalContractSelect,
    MyLgLocalContractSwitch,
    local_control_entities_for_domain,
    local_control_unique_id,
)

MODEL = "AIR_2C0001_WW"
BINDING_ID = "test_generic_control_binding_05_01"
DEVICE_ID = "test-generic-control-device-001"


class Coordinator:
    alias = "Synthetic appliance"
    device_type = "TEST_DEVICE"

    def __init__(self, model: str = MODEL, device_id: str = DEVICE_ID) -> None:
        self.model = model
        self.device_id = device_id
        self.data: dict[str, Any] = {}
        self.listeners: list[Any] = []

    def async_add_listener(self, callback, *_args):
        self.listeners.append(callback)

        def remove():
            if callback in self.listeners:
                self.listeners.remove(callback)

        return remove


class PrimaryProvider:
    def __init__(self, values=None, *, alive: bool = True, model: str = MODEL) -> None:
        self.binding_id = BINDING_ID
        self.model_id = model
        self.control_alive = alive
        self.values = dict(values or {})
        self.available = set(self.values)
        self.listeners: list[Any] = []

    def semantic_field_available(self, semantic_id: str) -> bool:
        return semantic_id in self.available

    def field_value(self, semantic_id: str):
        return self.values.get(semantic_id)

    def async_add_listener(self, callback, *_args):
        self.listeners.append(callback)

        def remove():
            if callback in self.listeners:
                self.listeners.remove(callback)

        return remove

    def emit(self) -> None:
        for callback in tuple(self.listeners):
            callback()


class ReadProvider:
    def __init__(self, values=None) -> None:
        self.values = dict(values or {})
        self.available = set(self.values)
        self.listeners: list[Any] = []

    def field_available(self, semantic_id: str) -> bool:
        return semantic_id in self.available

    def field_value(self, semantic_id: str):
        return self.values.get(semantic_id)

    def async_add_listener(self, callback, *_args):
        self.listeners.append(callback)

        def remove():
            if callback in self.listeners:
                self.listeners.remove(callback)

        return remove

    def emit(self) -> None:
        for callback in tuple(self.listeners):
            callback()


class Router:
    def __init__(self, outcome=None, *, target_available: bool = True) -> None:
        self.outcome = outcome or LocalCommandResult("confirmed", {})
        self.target_available = target_available
        self.calls: list[tuple[str, str, str]] = []
        self.methods: list[str] = []

    def control_target_available(self, _device_id: str) -> bool:
        return self.target_available

    async def async_set_value(self, device_id: str, capability: str, value: str):
        self.methods.append("set_value")
        self.calls.append((device_id, capability, value))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def async_set_value_strict(self, device_id: str, capability: str, value: str):
        self.methods.append("set_value_strict")
        self.calls.append((device_id, capability, value))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def async_execute(self, device_id: str, capability: str, value: str):
        self.methods.append("execute")
        self.calls.append((device_id, capability, value))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def async_execute_strict(self, device_id: str, capability: str, value: str):
        self.methods.append("execute_strict")
        self.calls.append((device_id, capability, value))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def descriptor(domain: str) -> LocalControlEntityDescriptor:
    contract = load_local_control_entity_contract()
    return next(
        item
        for item in contract.descriptors_by_model[MODEL]
        if item.factory_eligible and item.entity_domain == domain
    )


def entity_for(
    domain: str,
    *,
    state: object | None = None,
    read_state: object | None = None,
    router: Router | None = None,
):
    desc = descriptor(domain)
    values = {} if state is None else {desc.exact_state_semantic: state}
    read_values = {} if read_state is None else {desc.exact_state_semantic: read_state}
    coordinator = Coordinator()
    primary = PrimaryProvider(values)
    read = ReadProvider(read_values)
    route = router or Router()
    cls = {
        "switch": MyLgLocalContractSwitch,
        "select": MyLgLocalContractSelect,
        "number": MyLgLocalContractNumber,
    }[domain]
    return cls(coordinator, desc, route, primary, read), primary, read, route


def _full_fleet_options(contract):
    binding_models = {}
    bindings = []
    for model_index, (model_id, unit_count) in enumerate(
        contract.model_fleet_counts.items(), start=1
    ):
        for unit_index in range(1, unit_count + 1):
            binding_id = (
                f"test_generic_control_binding_{model_index:02d}_{unit_index:02d}"
            )
            binding_models[binding_id] = model_id
            bindings.append(
                {
                    "binding_id": binding_id,
                    "entries": [
                        {
                            "capability_id": item.capability_id,
                            "exact_values": list(item.exact_local_request_values),
                        }
                        for item in contract.descriptors_by_model.get(model_id, ())
                    ],
                }
            )
    options = {
        LOCAL_CONTROL_ELIGIBILITY_OPTION: {
            "schema_version": 3,
            "contract_sha256": contract.root_sha256,
            "checkpoint_revision": EXPECTED_LOCAL_CONTROL_CHECKPOINT_REVISION,
            "checkpoint_sha256": EXPECTED_LOCAL_CONTROL_CHECKPOINT_SHA256,
            "target_authority_revision": (
                EXPECTED_LOCAL_CONTROL_TARGET_AUTHORITY_REVISION
            ),
            "target_authority_sha256": (EXPECTED_LOCAL_CONTROL_TARGET_AUTHORITY_SHA256),
            "bindings": bindings,
        }
    }
    return options, binding_models


def full_model_eligibility():
    # Factory cardinality is a bundled-contract unit test.  The production
    # feature DB intentionally enables a smaller, editable control menu.
    contract = _load_bundled_local_control_entity_contract()
    options, binding_models = _full_fleet_options(contract)
    return contract, resolve_local_control_binding_eligibility(
        options, contract, binding_models
    )


class LocalControlGenericFactoryTests(unittest.TestCase):
    def make_entry(self, *, eligibility=True, model=MODEL, provider_model=MODEL):
        contract, resolved = full_model_eligibility()
        coordinator = Coordinator(model=model)
        primary = PrimaryProvider(model=provider_model)
        data = SimpleNamespace(
            local_control_entity_contract=contract,
            local_control_binding_eligibility=resolved if eligibility else {},
            local_control=Router(),
            coordinators={coordinator.device_id: coordinator},
            local_providers={coordinator.device_id: primary},
            local_read_providers={},
        )
        return SimpleNamespace(runtime_data=data)

    def test_missing_private_gate_or_model_mismatch_creates_zero(self) -> None:
        for entry in (
            self.make_entry(eligibility=False),
            self.make_entry(model="OTHER_MODEL"),
            self.make_entry(provider_model="OTHER_MODEL"),
        ):
            with self.subTest(entry=entry):
                self.assertEqual(local_control_entities_for_domain(entry, "switch"), [])
                self.assertEqual(local_control_entities_for_domain(entry, "select"), [])
                self.assertEqual(local_control_entities_for_domain(entry, "number"), [])
                self.assertEqual(local_control_entities_for_domain(entry, "button"), [])

    def test_factory_creates_only_full_exact_new_owners_on_existing_device(
        self,
    ) -> None:
        entry = self.make_entry()
        contract = entry.runtime_data.local_control_entity_contract
        created = {
            domain: local_control_entities_for_domain(entry, domain)
            for domain in ("switch", "select", "number", "button")
        }
        expected = {
            domain: sum(
                item.model_id == MODEL
                and item.factory_eligible
                and item.entity_domain == domain
                for item in contract.descriptors
            )
            for domain in created
        }

        self.assertEqual({key: len(value) for key, value in created.items()}, expected)
        all_entities = [entity for entities in created.values() for entity in entities]
        self.assertTrue(all_entities)
        self.assertEqual(
            len({entity.unique_id for entity in all_entities}), len(all_entities)
        )
        self.assertTrue(
            all(
                entity.entity_category == EntityCategory.CONFIG
                for entity in all_entities
            )
        )
        self.assertTrue(
            all(not entity.entity_registry_enabled_default for entity in all_entities)
        )
        self.assertTrue(
            all(
                any("가" <= char <= "힣" for char in entity.name)
                for entity in all_entities
            )
        )
        self.assertTrue(
            all(not entity._descriptor.existing_owner for entity in all_entities)
        )

    def test_partial_exact_subset_never_creates_a_generic_surface(self) -> None:
        contract = _load_bundled_local_control_entity_contract()
        desc = next(
            item
            for item in contract.descriptors_by_model[MODEL]
            if item.factory_eligible and len(item.exact_local_request_values) > 1
        )
        options, binding_models = _full_fleet_options(contract)
        row = next(
            item
            for item in options[LOCAL_CONTROL_ELIGIBILITY_OPTION]["bindings"]
            if item["binding_id"] == BINDING_ID
        )
        entry = next(
            item
            for item in row["entries"]
            if item["capability_id"] == desc.capability_id
        )
        entry["exact_values"] = [desc.exact_local_request_values[0]]
        with self.assertRaises(LocalControlEligibilityError):
            resolve_local_control_binding_eligibility(options, contract, binding_models)
        coordinator = Coordinator()
        data = SimpleNamespace(
            local_control_entity_contract=contract,
            local_control_binding_eligibility={},
            local_control=Router(),
            coordinators={DEVICE_ID: coordinator},
            local_providers={DEVICE_ID: PrimaryProvider()},
            local_read_providers={},
        )

        self.assertEqual(
            local_control_entities_for_domain(
                SimpleNamespace(runtime_data=data), desc.entity_domain
            ),
            [],
        )

    def test_unique_ids_are_stable_bounded_and_collision_resistant(self) -> None:
        first = local_control_unique_id("p" * 300, "local_" + "x" * 200)
        second = local_control_unique_id("p" * 300, "local_" + "x" * 199 + "y")
        self.assertEqual(len(first), 128)
        self.assertEqual(len(second), 128)
        self.assertNotEqual(first, second)


class LocalControlGenericEntityTests(unittest.IsolatedAsyncioTestCase):
    def test_exact_cycle_power_readback_uses_only_verified_power_states(self) -> None:
        contract = load_local_control_entity_contract()
        cases = (
            ("ST_R_ETH01Y_", "operation.power_requested", "power_off", False),
            ("ST_R_ETH01Y_", "operation.power_requested", "initial", True),
            ("WTL_KPK_BDH_KR_01", "washer.power_requested", "power off", False),
            ("WTL_KPK_BDH_KR_01", "washer.power_requested", "initial", True),
            ("WTL_KPK_BDH_KR_01", "dryer.power_requested", "power off", False),
            ("WTL_KPK_BDH_KR_01", "dryer.power_requested", "initial", True),
        )
        for model, capability, state, expected in cases:
            with self.subTest(model=model, capability=capability, state=state):
                desc = next(
                    item for item in contract.descriptors_by_model[model]
                    if item.capability_id == capability and item.entity_domain == "switch"
                )
                entity = MyLgLocalContractSwitch(
                    Coordinator(model=model), desc, Router(),
                    PrimaryProvider({desc.exact_state_semantic: state}, model=model),
                    ReadProvider(),
                )
                self.assertIs(entity.is_on, expected)

        # A paused/other cycle status is not automatically treated as ON.
        desc = next(
            item for item in contract.descriptors_by_model["WTL_KPK_BDH_KR_01"]
            if item.capability_id == "washer.power_requested"
        )
        entity = MyLgLocalContractSwitch(
            Coordinator(model="WTL_KPK_BDH_KR_01"), desc, Router(),
            PrimaryProvider({desc.exact_state_semantic: "pause"}, model="WTL_KPK_BDH_KR_01"),
            ReadProvider(),
        )
        with self.assertLogs("custom_components.my_lg.local_control_entity", level="WARNING"):
            self.assertIsNone(entity.is_on)

    def test_aabb_boolean_and_sparse_numeric_readback_use_only_existing_options(self) -> None:
        contract = load_local_control_entity_contract()
        cases = (
            ("1WPD4CMIDR__3", "auto_care.enabled", False, "OFF"),
            ("1WPD4CMIDR__3", "auto_care.enabled", True, "ON"),
            ("1WPD4CMIDR__3", "sterilization.schedule.hour", 18, "18"),
            ("1WPD4CMIDR__3", "water.default_selection", "last used", "RECENT_WATER"),
            ("1WPD4CMIDR__3", "water.default_selection", "purified water", "NORMAL_WATER"),
            ("1WPD4CMIDR__3", "water.default_selection", "cold water", "COLD_WATER"),
            ("2REFO1DBN3K_U", "smart_care.enabled", False, "OFF"),
            ("2REFO1DBN3K_U", "sound.button_enabled", True, "ON"),
            ("3REK2G03VI230D_2", "filter.one_touch_enabled", False, "OFF"),
        )
        for model, capability, state, expected in cases:
            with self.subTest(model=model, capability=capability, state=state):
                desc = next(
                    item for item in contract.descriptors_by_model[model]
                    if item.capability_id == capability and item.entity_domain == "select"
                )
                primary = PrimaryProvider({desc.exact_state_semantic: state}, model=model)
                entity = MyLgLocalContractSelect(
                    Coordinator(model=model), desc, Router(), primary, ReadProvider()
                )
                self.assertEqual(entity.current_option, expected)
                self.assertIn(entity.current_option, entity.options)

        # Out-of-domain values remain unknown; this conversion never expands
        # the write domain or guesses a new state.
        desc = next(
            item for item in contract.descriptors_by_model["1WPD4CMIDR__3"]
            if item.capability_id == "auto_care.enabled"
        )
        entity = MyLgLocalContractSelect(
            Coordinator(model="1WPD4CMIDR__3"), desc, Router(),
            PrimaryProvider({desc.exact_state_semantic: 2}, model="1WPD4CMIDR__3"),
            ReadProvider(),
        )
        with self.assertLogs("custom_components.my_lg.local_control_entity", level="WARNING"):
            self.assertIsNone(entity.current_option)

    def test_exact_local_state_is_used_without_cloud_or_guessing(self) -> None:
        for domain in ("switch", "select", "number"):
            desc = descriptor(domain)
            state = desc.supported_values[0]
            entity, _primary, _read, _router = entity_for(domain, state=state)
            with self.subTest(domain=domain):
                if domain == "switch":
                    self.assertEqual(
                        entity.is_on,
                        desc.value_mappings[0].home_assistant_value == "on",
                    )
                elif domain == "select":
                    self.assertEqual(
                        entity.current_option,
                        desc.value_mappings[0].home_assistant_value,
                    )
                else:
                    self.assertEqual(
                        entity.native_value,
                        float(desc.value_mappings[0].home_assistant_value),
                    )

    def test_reviewed_aabb_setting_reads_drive_existing_local_selects(self) -> None:
        contract = load_local_control_entity_contract()
        cases = (
            ("2REFO1DBN3K_U", "compartment.fridge.setpoint_raw", 5, "5"),
            ("2REFO1DBN3K_U", "compartment.freezer.setpoint_raw", 3, "3"),
            ("1WPD4CMIDR__3", "water.default_amount_mode_raw", 2, "2"),
            ("1WPD4CMIDR__3", "water.default_amount_1_raw", 12, "12"),
            ("1WPD4CMIDR__3", "water.custom_recipe_1.temperature_raw", 40, "40"),
        )
        for model, capability, value, expected in cases:
            with self.subTest(model=model, capability=capability):
                desc = next(
                    item for item in contract.descriptors_by_model[model]
                    if item.capability_id == capability and item.entity_domain == "select"
                )
                entity = MyLgLocalContractSelect(
                    Coordinator(model=model), desc, Router(),
                    PrimaryProvider(model=model),
                    ReadProvider({desc.exact_state_semantic: value}),
                )
                self.assertEqual(entity.current_option, expected)
                self.assertIn(entity.current_option, entity.options)

    def test_inconsistent_or_unknown_state_logs_semantic_once_without_private_data(
        self,
    ) -> None:
        desc = descriptor("select")
        private_raw = "private-raw-value-must-not-appear"
        entity, _primary, _read, _router = entity_for(
            "select",
            state=desc.supported_values[0],
            read_state=private_raw,
        )
        with self.assertLogs(
            "custom_components.my_lg.local_control_entity", level="WARNING"
        ) as caught:
            self.assertIsNone(entity.current_option)
            self.assertIsNone(entity.current_option)

        self.assertEqual(len(caught.records), 1)
        rendered = caught.output[0]
        self.assertIn(desc.exact_state_semantic, rendered)
        self.assertNotIn(private_raw, rendered)
        self.assertNotIn(DEVICE_ID, rendered)

    def test_availability_requires_authenticated_presence_pairing_and_model(
        self,
    ) -> None:
        entity, primary, _read, router = entity_for("switch")
        self.assertTrue(entity.available)
        primary.control_alive = False
        self.assertFalse(entity.available)
        primary.control_alive = True
        router.target_available = False
        self.assertFalse(entity.available)
        router.target_available = True
        entity.coordinator.model = "OTHER_MODEL"
        self.assertFalse(entity.available)

    async def test_listeners_update_and_detach_cleanly(self) -> None:
        desc = descriptor("select")
        first, second = desc.supported_values[:2]
        entity, primary, read, _router = entity_for("select", state=first)
        writes: list[str | None] = []
        entity.async_write_ha_state = lambda: writes.append(entity.current_option)

        await entity.async_added_to_hass()
        primary.values[desc.exact_state_semantic] = second
        primary.emit()
        self.assertEqual(writes, [desc.value_mappings[1].home_assistant_value])

        await entity.async_will_remove_from_hass()
        primary.values[desc.exact_state_semantic] = first
        primary.emit()
        read.emit()
        self.assertEqual(writes, [desc.value_mappings[1].home_assistant_value])
        self.assertEqual(primary.listeners, [])
        self.assertEqual(read.listeners, [])

    async def test_select_rejects_unknown_option_before_router(self) -> None:
        entity, _primary, _read, router = entity_for("select")
        self.assertEqual(
            entity.options,
            [
                mapping.home_assistant_value
                for mapping in entity._descriptor.value_mappings
            ],
        )
        with self.assertRaises(HomeAssistantError):
            await entity.async_select_option("not-in-exact-contract")
        self.assertEqual(router.calls, [])

    async def test_number_rejects_every_off_grid_value_before_router(self) -> None:
        entity, _primary, _read, router = entity_for("number")
        exact_grid = [
            float(mapping.home_assistant_value)
            for mapping in entity._descriptor.value_mappings
        ]
        self.assertEqual(exact_grid[0], entity.native_min_value)
        self.assertEqual(exact_grid[-1], entity.native_max_value)
        adjacent_pairs = [
            (exact_grid[index], exact_grid[index + 1])
            for index in range(len(exact_grid) - 1)
        ]
        self.assertTrue(
            all(right - left == entity.native_step for left, right in adjacent_pairs)
        )
        off_grid = [
            entity.native_min_value - entity.native_step,
            entity.native_max_value + entity.native_step,
            *(left + (right - left) / 2 for left, right in adjacent_pairs),
            float("nan"),
            float("inf"),
            float("-inf"),
        ]
        for value in off_grid:
            with self.subTest(value=value), self.assertRaises(HomeAssistantError):
                await entity.async_set_native_value(value)
        self.assertEqual(router.calls, [])

    async def test_switch_routes_both_exact_polarities_through_set_value(self) -> None:
        desc = descriptor("switch")
        entity, _primary, _read, router = entity_for("switch")

        await entity.async_turn_off()
        await entity.async_turn_on()

        self.assertEqual(router.methods, ["set_value_strict", "set_value_strict"])
        self.assertEqual(
            router.calls,
            [
                (DEVICE_ID, desc.capability_id, "false"),
                (DEVICE_ID, desc.capability_id, "true"),
            ],
        )

    async def test_reviewed_one_shot_uses_execute_endpoint_once(self) -> None:
        contract = load_local_control_entity_contract()
        desc = next(
            item for item in contract.descriptors if item.entity_domain == "button"
        )
        self.assertTrue(desc.one_shot)
        self.assertTrue(desc.existing_owner)
        self.assertFalse(desc.factory_eligible)
        coordinator = Coordinator(model=desc.model_id)
        primary = PrimaryProvider(model=desc.model_id)
        router = Router()
        entity = MyLgLocalContractButton(coordinator, desc, router, primary, None)

        await entity.async_press()

        self.assertEqual(router.methods, ["execute_strict"])
        self.assertEqual(
            router.calls,
            [
                (
                    DEVICE_ID,
                    desc.capability_id,
                    desc.exact_local_request_values[0],
                )
            ],
        )

    async def test_confirmed_write_is_single_local_command_and_never_optimistic(
        self,
    ) -> None:
        desc = descriptor("switch")
        entity, primary, _read, router = entity_for(
            "switch", state=False, router=Router(LocalCommandResult("confirmed", {}))
        )

        await entity.async_turn_on()

        self.assertEqual(
            router.calls,
            [(DEVICE_ID, desc.capability_id, "true")],
        )
        self.assertEqual(router.methods, ["set_value_strict"])
        self.assertFalse(entity.is_on)
        self.assertFalse(primary.values[desc.exact_state_semantic])

    async def test_prewire_refusal_errors_marks_unavailable_and_never_retries(
        self,
    ) -> None:
        router = Router(LocalCommandResult("confirmed", {}))
        router.outcome = None
        entity, _primary, _read, _router = entity_for("switch", router=router)
        entity.async_write_ha_state = lambda: None

        with self.assertRaises(HomeAssistantError):
            await entity.async_turn_on()

        self.assertEqual(len(router.calls), 1)
        self.assertFalse(entity.available)
        with self.assertRaises(HomeAssistantError):
            await entity.async_turn_on()
        self.assertEqual(len(router.calls), 1)

    async def test_transient_busy_errors_without_latching_and_later_succeeds(
        self,
    ) -> None:
        router = Router(LocalCommandBusy("another appliance command is still being confirmed"))
        entity, _primary, _read, _router = entity_for("switch", router=router)

        with self.assertRaises(HomeAssistantError):
            await entity.async_turn_on()

        self.assertTrue(entity.available)
        self.assertFalse(entity._prewire_refused)
        router.outcome = LocalCommandResult("confirmed", {})
        await entity.async_turn_on()
        self.assertEqual(len(router.calls), 2)
        self.assertTrue(entity.available)

    async def test_dynamic_precondition_errors_without_latching_and_later_succeeds(
        self,
    ) -> None:
        router = Router(LocalCommandNotReady("sensor monitoring requires a current own prestate"))
        entity, _primary, _read, _router = entity_for("select", router=router)

        with self.assertRaises(HomeAssistantError):
            await entity.async_select_option(entity.options[0])

        self.assertTrue(entity.available)
        self.assertFalse(entity._prewire_refused)
        router.outcome = LocalCommandResult("confirmed", {})
        await entity.async_select_option(entity.options[0])
        self.assertEqual(len(router.calls), 2)
        self.assertTrue(entity.available)

    async def test_postwire_failure_errors_without_retry_or_guess(self) -> None:
        router = Router(LocalCommandFailed("synthetic postwire uncertainty"))
        entity, _primary, _read, _router = entity_for("select", router=router)

        with self.assertRaises(HomeAssistantError):
            await entity.async_select_option(entity.options[0])

        self.assertEqual(len(router.calls), 1)

    async def test_postwire_unverifiable_waits_for_readback_without_retry(self) -> None:
        router = Router(LocalCommandResult("unverifiable", {}))
        entity, primary, _read, _router = entity_for("select", router=router)
        before = entity.current_option

        await entity.async_select_option(entity.options[0])

        self.assertEqual(len(router.calls), 1)
        self.assertEqual(entity.current_option, before)
        self.assertNotIn(entity._descriptor.exact_state_semantic, primary.values)


if __name__ == "__main__":
    unittest.main()
