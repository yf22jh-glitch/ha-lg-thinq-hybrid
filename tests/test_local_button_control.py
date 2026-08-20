"""Local pause promotion keeps one button press to one appliance command."""

from __future__ import annotations

from typing import Any
import unittest

from homeassistant.exceptions import HomeAssistantError

from custom_components.my_lg.button import MyLgButton, WASHTOWER_BUTTONS
from custom_components.my_lg.local_command import LocalCommandFailed, LocalCommandResult


def _description(key: str):
    return next(item for item in WASHTOWER_BUTTONS if item.key == key)


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
