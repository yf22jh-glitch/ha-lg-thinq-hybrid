"""Fresh-state acknowledgement tests for PAT controls."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.my_lg.coordinator import PatDeviceCoordinator
from custom_components.my_lg.pat_control import (
    PatStateRequirement,
    build_pat_control_request,
)


class FakePatApi:
    def __init__(self, statuses: list[object]) -> None:
        self.statuses = list(statuses)
        self.phases: list[str] = []
        self.payloads: list[dict] = []
        self.control_error: Exception | None = None

    async def async_get_device_status(self, _device_id: str):
        self.phases.append("poll")
        if not self.statuses:
            raise RuntimeError("no queued status")
        return deepcopy(self.statuses.pop(0))

    async def async_post_device_control(self, _device_id: str, payload: dict):
        self.phases.append("ack")
        self.payloads.append(deepcopy(payload))
        if self.control_error is not None:
            raise self.control_error


class PatCoordinatorControlTests(unittest.IsolatedAsyncioTestCase):
    def _coordinator(self, api: FakePatApi) -> PatDeviceCoordinator:
        hass = HomeAssistant(str(Path("/tmp/lg-ha-pat-coordinator-test")))
        coordinator = PatDeviceCoordinator(
            hass,
            None,
            api,
            {
                "deviceId": "synthetic-device",
                "deviceInfo": {
                    "alias": "Synthetic",
                    "deviceType": "DEVICE_AIR_CONDITIONER",
                    "modelName": "SYNTHETIC",
                },
            },
        )
        coordinator.profile = {
            "property": {
                "setting": {
                    "preserved": {"type": "number", "mode": ["r", "w"]},
                    "value": {"type": "number", "mode": ["r", "w"]},
                },
                "mode": {
                    "current": {"type": "enum", "mode": ["r", "w"]}
                },
            }
        }
        return coordinator

    async def test_raw_payload_is_blocked_before_any_network_call(self) -> None:
        api = FakePatApi([])
        coordinator = self._coordinator(api)

        with self.assertRaisesRegex(HomeAssistantError, "echo contract"):
            await coordinator.async_control(  # type: ignore[arg-type]
                {"setting": {"value": 1}}
            )
        self.assertEqual(api.phases, [])

        with self.assertRaisesRegex(HomeAssistantError, "echo contract"):
            await coordinator.async_unverified_control(
                {"operation": {"stylerOperationMode": "START"}}
            )
        self.assertEqual(api.phases, [])

    async def test_fresh_prestate_ack_and_fresh_poststate_are_ordered(self) -> None:
        api = FakePatApi(
            [
                {"setting": {"value": 0}},
                {"setting": {"value": 1}},
            ]
        )
        coordinator = self._coordinator(api)
        request = build_pat_control_request({"setting": {"value": 1}})

        with patch("asyncio.sleep", new=AsyncMock()):
            await coordinator.async_control(request)

        self.assertEqual(api.phases, ["poll", "ack", "poll"])
        self.assertEqual(api.payloads, [{"setting": {"value": 1}}])
        self.assertEqual(coordinator.get("setting", "value"), 1)

    async def test_already_satisfied_setting_is_a_network_safe_noop(self) -> None:
        api = FakePatApi([{"setting": {"value": 1}}])
        coordinator = self._coordinator(api)

        await coordinator.async_control(
            build_pat_control_request({"setting": {"value": 1}})
        )

        self.assertEqual(api.phases, ["poll"])
        self.assertEqual(api.payloads, [])

    async def test_fresh_requirement_failure_blocks_before_ack(self) -> None:
        api = FakePatApi(
            [{"mode": {"current": "AUTO"}, "setting": {"value": 0}}]
        )
        coordinator = self._coordinator(api)
        request = build_pat_control_request(
            {"setting": {"value": 1}},
            requirements=(
                PatStateRequirement(
                    ("mode", "current"), ("COOL",), "mode changed"
                ),
            ),
        )

        with self.assertRaisesRegex(HomeAssistantError, "mode changed"):
            await coordinator.async_control(request)
        self.assertEqual(api.phases, ["poll"])

    async def test_ack_without_matching_readback_fails_closed(self) -> None:
        api = FakePatApi(
            [
                {"setting": {"value": 0}},
                {"setting": {"value": 0}},
                {"setting": {"value": 0}},
            ]
        )
        coordinator = self._coordinator(api)
        request = build_pat_control_request({"setting": {"value": 1}})

        with (
            patch("asyncio.sleep", new=AsyncMock()),
            self.assertRaisesRegex(HomeAssistantError, "did not match"),
        ):
            await coordinator.async_control(request)
        self.assertEqual(api.phases, ["poll", "ack", "poll", "poll"])

    async def test_factory_runs_after_fresh_state_under_control_path(self) -> None:
        api = FakePatApi(
            [
                {"setting": {"preserved": 7, "value": 0}},
                {"setting": {"preserved": 7, "value": 1}},
            ]
        )
        coordinator = self._coordinator(api)

        def request_factory(state):
            return build_pat_control_request(
                {
                    "setting": {
                        "preserved": state["setting"]["preserved"],
                        "value": 1,
                    }
                }
            )

        with patch("asyncio.sleep", new=AsyncMock()):
            await coordinator.async_control(request_factory)
        self.assertEqual(
            api.payloads,
            [{"setting": {"preserved": 7, "value": 1}}],
        )


if __name__ == "__main__":
    unittest.main()
