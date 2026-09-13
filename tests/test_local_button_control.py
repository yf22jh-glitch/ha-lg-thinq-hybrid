"""Local pause promotion keeps one button press to one appliance command."""

from __future__ import annotations

from typing import Any
import unittest

from homeassistant.exceptions import HomeAssistantError

from custom_components.my_lg.button import MyLgButton, WASHTOWER_BUTTONS, STYLER_BUTTONS
from custom_components.my_lg.local_command import LocalCommandFailed, LocalCommandResult
from custom_components.my_lg.local_control_contract import load_local_control_entity_contract


def _description(key: str):
    return next(item for item in (*WASHTOWER_BUTTONS, *STYLER_BUTTONS) if item.key == key)


class Coordinator:
    device_id = "synthetic-pat-device-001"
    device_type = "DEVICE_WASHTOWER"
    alias = "Test WashTower"
    model = "WTL_KPK_BDH_KR_01"

    def __init__(self) -> None:
        self.data: dict[str, Any] = {"washer": {"operation": {}}}
        self.controls: list[dict[str, Any]] = []

    def async_add_listener(self, *_args: Any, **_kwargs: Any):
        return lambda: None

    def get(self, *path: str, default: Any = None) -> Any:
        node: Any = self.data
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    async def async_control(self, payload: dict[str, Any]) -> None:
        self.controls.append(payload)


class Router:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, str, str]] = []

    async def async_execute(
        self, device_id: str, capability: str, value: str = "true"
    ) -> Any:
        self.calls.append((device_id, capability, value))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class LocalPauseButtonTests(unittest.IsolatedAsyncioTestCase):
    def test_power_routes_exist_in_the_unchanged_real_contract_but_start_is_not_promoted(self) -> None:
        contract = load_local_control_entity_contract()
        for model, keys in (
            ("WTL_KPK_BDH_KR_01", ("washer_power_off", "dryer_power_off")),
            ("ST_R_ETH01Y_", ("styler_power_off", "styler_power_on")),
        ):
            for key in keys:
                description = _description(key)
                descriptor = next(
                    item for item in contract.descriptors_by_model[model]
                    if item.capability_id == description.local_capability
                )
                self.assertIn(description.local_value, descriptor.exact_local_request_values)
        self.assertFalse(any(
            item.capability_id.endswith(".operation.start_or_resume")
            for item in contract.descriptors
        ))

    async def test_existing_buttons_route_the_exact_unit_and_value(self) -> None:
        for key, capability, value in (
            ("washer_start", "washer.operation.start_or_resume", "true"),
            ("dryer_start", "dryer.operation.start_or_resume", "true"),
            ("washer_power_off", "washer.power_requested", "false"),
            ("dryer_power_off", "dryer.power_requested", "false"),
            ("styler_power_off", "operation.power_requested", "false"),
            ("styler_power_on", "operation.power_requested", "true"),
        ):
            with self.subTest(key=key):
                coordinator = Coordinator()
                router = Router(LocalCommandResult("confirmed", {}))
                await MyLgButton(coordinator, _description(key), router).async_press()
                self.assertEqual(router.calls, [(coordinator.device_id, capability, value)])
                self.assertEqual(coordinator.controls, [])

    async def test_start_partial_delivery_never_falls_back_to_cloud(self) -> None:
        for key in ("washer_start", "dryer_start"):
            for outcome in (
                LocalCommandFailed("laundry-sequence-delivery-ambiguous"),
                LocalCommandResult("unverifiable", {}),
            ):
                with self.subTest(key=key, outcome=type(outcome).__name__):
                    coordinator = Coordinator()
                    router = Router(outcome)
                    button = MyLgButton(coordinator, _description(key), router)
                    if isinstance(outcome, Exception):
                        with self.assertRaises(HomeAssistantError):
                            await button.async_press()
                    else:
                        await button.async_press()
                    self.assertEqual(len(router.calls), 1)
                    self.assertEqual(coordinator.controls, [])

    async def test_unpromoted_start_preserves_existing_pre_wire_cloud_route(self) -> None:
        # Routing metadata is not permission to transmit. Until the producer and
        # per-binding authority promote a start, the router returns None pre-wire.
        coordinator = Coordinator()
        router = Router(None)
        await MyLgButton(coordinator, _description("washer_start"), router).async_press()
        self.assertEqual(router.calls, [(coordinator.device_id, "washer.operation.start_or_resume", "true")])
        self.assertEqual(coordinator.controls, [{"washer": {"operation": {"washerOperationMode": "START"}}}])

    async def test_cloud_only_installations_and_unknown_styler_start_stay_unchanged(self) -> None:
        for key in ("washer_start", "dryer_start", "styler_start"):
            coordinator = Coordinator()
            description = _description(key)
            await MyLgButton(coordinator, description).async_press()
            self.assertEqual(coordinator.controls, [description.payload])
        self.assertIsNone(_description("styler_start").local_capability)

    async def test_confirmed_pause_is_not_also_sent_to_the_cloud(self) -> None:
        coordinator = Coordinator()
        router = Router(LocalCommandResult("confirmed", {"washer.cycle.state": "pause"}))
        button = MyLgButton(coordinator, _description("washer_stop"), router)  # type: ignore[arg-type]

        await button.async_press()

        self.assertEqual(
            router.calls,
            [(coordinator.device_id, "washer.operation.pause", "true")],
        )
        self.assertEqual(coordinator.controls, [])

    async def test_pre_wire_refusal_falls_back_to_the_cloud_once(self) -> None:
        coordinator = Coordinator()
        router = Router(None)
        button = MyLgButton(coordinator, _description("washer_stop"), router)  # type: ignore[arg-type]

        await button.async_press()

        self.assertEqual(len(router.calls), 1)
        self.assertEqual(
            coordinator.controls,
            [{"washer": {"operation": {"washerOperationMode": "STOP"}}}],
        )

    async def test_unverifiable_pause_is_neither_retried_nor_shown_as_confirmed(self) -> None:
        coordinator = Coordinator()
        router = Router(LocalCommandResult("unverifiable", {}))
        button = MyLgButton(coordinator, _description("washer_stop"), router)  # type: ignore[arg-type]

        await button.async_press()

        self.assertEqual(len(router.calls), 1)
        self.assertEqual(coordinator.controls, [])

    async def test_a_pause_that_may_have_landed_is_never_retried_through_the_cloud(self) -> None:
        coordinator = Coordinator()
        router = Router(LocalCommandFailed("the appliance did not report the change"))
        button = MyLgButton(coordinator, _description("washer_stop"), router)  # type: ignore[arg-type]

        with self.assertRaises(HomeAssistantError):
            await button.async_press()

        self.assertEqual(len(router.calls), 1)
        self.assertEqual(coordinator.controls, [])


if __name__ == "__main__":
    unittest.main()
