"""The local path serves what it can and hands back what it cannot, without sending twice."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
import types
from typing import Any
import unittest

# Loaded without the integration package, whose __init__ needs a running Home Assistant.
# The router is deliberately free of Home Assistant imports so this is possible; the relative
# import inside it needs a parent package, so one is stood up here holding just these two.
_PACKAGE_NAME = "my_lg_local_control_router_test"
_DIRECTORY = Path(__file__).resolve().parents[1] / "custom_components" / "my_lg"
_package = types.ModuleType(_PACKAGE_NAME)
_package.__path__ = [str(_DIRECTORY)]
sys.modules[_PACKAGE_NAME] = _package


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"{_PACKAGE_NAME}.{name}", _DIRECTORY / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_local_command = _load("local_command")
LocalCommandFailed = _local_command.LocalCommandFailed
LocalCommandResult = _local_command.LocalCommandResult
LocalCommandUnavailable = _local_command.LocalCommandUnavailable
_router_module = _load("local_control_router")
LocalControlRouter = _router_module.LocalControlRouter
# The module is loaded under a stub package, so its logger is not the one it has in Home Assistant.
ROUTER_LOGGER = _router_module._LOGGER.name

PAT = "pat-study-ac"
BRIDGE = "synthetic-bridge-device-001"
NOW = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Field:
    value: Any
    observed_at: datetime


def shadow(age: timedelta = timedelta(seconds=1)) -> dict[str, Field]:
    at = NOW - age
    return {
        "operation.mode": Field("cool", at),
        "fan.mode": Field("high", at),
        "temperature.target_c": Field(24, at),
    }


class Provider:
    def __init__(
        self,
        fields: dict[str, Field],
        healthy: bool = True,
        alive: bool = True,
        state_ready: bool = True,
        fields_ready: bool = True,
    ) -> None:
        self.shadow_fields = fields
        self.shadow_healthy = healthy
        self.control_alive = alive
        self.control_state_ready = state_ready
        self._fields_ready = fields_ready

    def control_fields_ready(self, semantic_ids: tuple[str, ...]) -> bool:
        self.requested_control_fields = semantic_ids
        return self.control_state_ready and self._fields_ready


class Sender:
    def __init__(self, outcome: Any = None) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.expected_states: list[dict[str, Any] | None] = []
        self._outcome = outcome or LocalCommandResult("confirmed", {})

    async def async_send(
        self,
        device_id: str,
        capability: str,
        value: str,
        *,
        expected_state: dict[str, Any] | None = None,
    ) -> LocalCommandResult:
        self.sent.append((device_id, capability, value))
        self.expected_states.append(expected_state)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class SwingRefusingSender(Sender):
    """Confirm climate tuples but refuse the unsupported swing before wire."""

    async def async_send(
        self,
        device_id: str,
        capability: str,
        value: str,
        *,
        expected_state: dict[str, Any] | None = None,
    ) -> LocalCommandResult:
        self.sent.append((device_id, capability, value))
        self.expected_states.append(expected_state)
        if capability.startswith("swing."):
            raise LocalCommandUnavailable("no codec for this model's swing direction")
        return LocalCommandResult("confirmed", {})


class PowerOffRefusingSender(Sender):
    """Confirm climate tuples but refuse power-off before wire."""

    async def async_send(
        self,
        device_id: str,
        capability: str,
        value: str,
        *,
        expected_state: dict[str, Any] | None = None,
    ) -> LocalCommandResult:
        self.sent.append((device_id, capability, value))
        self.expected_states.append(expected_state)
        if capability == "operation.power_requested":
            raise LocalCommandUnavailable("power-off codec unavailable")
        return LocalCommandResult("confirmed", {})


def router(sender: Sender, provider: Provider | None = None, bridge_id: str | None = BRIDGE):
    return LocalControlRouter(
        sender,
        {PAT: provider} if provider is not None else {},
        lambda pat: bridge_id if pat == PAT else None,
    )


def run(coro):
    return asyncio.run(coro)


class LocalControlRouterTest(unittest.TestCase):
    def test_changes_one_field_and_keeps_the_rest_as_the_appliance_reported_them(self) -> None:
        sender = Sender()
        result = run(router(sender, Provider(shadow())).async_set_climate(PAT, fan="low", now=NOW))
        self.assertTrue(result and result.confirmed)
        # The appliance writes mode, fan and setpoint together, so changing the fan still has to
        # state the other two - and stating a guess would silently move them.
        self.assertEqual(sender.sent, [(BRIDGE, "climate.mode_fan_setpoint", "cool|low|24C")])
        self.assertEqual(
            sender.expected_states,
            [
                {
                    "operation.mode": "cool",
                    "fan.mode": "high",
                    "temperature.target_c": 24,
                }
            ],
        )

    def test_an_appliance_with_no_local_shadow_is_not_addressed_at_all(self) -> None:
        sender = Sender()
        with self.assertLogs(ROUTER_LOGGER, level="INFO") as caught:
            self.assertIsNone(run(router(sender).async_set_climate(PAT, fan="low", now=NOW)))
        self.assertEqual(sender.sent, [])
        # Silent here is what made an install where the local path never serves anything look
        # exactly like one where it works.
        self.assertIn("no local shadow", caught.output[0])

    def test_semantic_health_does_not_replace_authenticated_control_presence(self) -> None:
        sender = Sender()
        provider = Provider(shadow(), healthy=False)
        result = run(
            router(sender, provider).async_set_climate(PAT, fan="low", now=NOW)
        )
        self.assertTrue(result and result.confirmed)
        self.assertEqual(
            sender.sent,
            [(BRIDGE, "climate.mode_fan_setpoint", "cool|low|24C")],
        )

    def test_an_offline_control_presence_is_not_addressed(self) -> None:
        sender = Sender()
        provider = Provider(shadow(), alive=False)
        with self.assertLogs(ROUTER_LOGGER, level="INFO") as caught:
            self.assertIsNone(
                run(
                    router(sender, provider).async_set_climate(
                        PAT, fan="low", now=NOW
                    )
                )
            )
        self.assertEqual(sender.sent, [])
        self.assertIn("authenticated presence", caught.output[0])

    def test_a_normal_sparse_report_is_still_recent_enough_to_compose_from(self) -> None:
        sender = Sender()
        provider = Provider(shadow(age=timedelta(minutes=5)))
        result = run(router(sender, provider).async_set_climate(PAT, fan="low", now=NOW))
        self.assertTrue(result and result.confirmed)
        self.assertEqual(
            sender.sent,
            [(BRIDGE, "climate.mode_fan_setpoint", "cool|low|24C")],
        )

    def test_a_reading_beyond_two_sparse_report_windows_is_handed_back_not_guessed(self) -> None:
        sender = Sender()
        provider = Provider(shadow(age=timedelta(minutes=11)))
        self.assertIsNone(run(router(sender, provider).async_set_climate(PAT, fan="low", now=NOW)))
        self.assertEqual(sender.sent, [])

    def test_without_the_bridge_pairing_there_is_nothing_to_send_to(self) -> None:
        sender = Sender()
        with self.assertLogs(ROUTER_LOGGER, level="INFO") as caught:
            result = run(
                router(sender, Provider(shadow()), bridge_id=None).async_set_climate(PAT, fan="low", now=NOW)
            )
        self.assertIsNone(result)
        self.assertEqual(sender.sent, [])
        self.assertIn("not resolved", caught.output[0])

    def test_a_refusal_made_before_the_write_is_offered_to_the_cloud(self) -> None:
        sender = Sender(LocalCommandUnavailable("no codec for that value"))
        result = run(router(sender, Provider(shadow())).async_set_climate(PAT, target_c=26, now=NOW))
        # Refused because 26C was never observed. The frame did not go out, so the caller
        # sending it to the cloud is one command, not two.
        self.assertIsNone(result)
        self.assertEqual(len(sender.sent), 1)

    def test_a_tuple_refusal_arms_the_cloud_barrier_before_returning_to_its_caller(self) -> None:
        sender = Sender(LocalCommandUnavailable("no codec for that value"))
        r = router(sender, Provider(shadow()))

        async def commands() -> None:
            self.assertIsNone(
                await r.async_set_climate(PAT, target_c=26, now=NOW)
            )
            # The caller is about to dispatch that intent to the cloud. A concurrent/later tuple
            # request must already be fenced; relying on a second callback after this return leaves
            # a window where retained pre-cloud state can be sent locally.
            self.assertIsNone(
                await r.async_set_climate(
                    PAT, fan="low", now=NOW + timedelta(seconds=1)
                )
            )

        run(commands())
        self.assertEqual(len(sender.sent), 1)

    def test_a_frame_that_went_out_unconfirmed_is_never_handed_back_for_a_second_try(self) -> None:
        sender = Sender(LocalCommandFailed("the appliance did not report the change"))
        with self.assertRaises(LocalCommandFailed):
            run(router(sender, Provider(shadow())).async_set_climate(PAT, fan="low", now=NOW))

    def test_a_request_naming_no_field_is_not_a_request(self) -> None:
        sender = Sender()
        # An unmapped fan speed (LG's POWER) reaches here as None. Sent anyway it would write
        # the appliance's current tuple back to it - a frame on the wire that changes nothing.
        self.assertIsNone(run(router(sender, Provider(shadow())).async_set_climate(PAT, now=NOW)))
        self.assertEqual(sender.sent, [])

    def test_power_off_is_a_write_to_the_power_field(self) -> None:
        sender = Sender()
        result = run(router(sender, Provider(shadow())).async_turn_off(PAT))
        self.assertTrue(result and result.confirmed)
        self.assertEqual(sender.sent, [(BRIDGE, "operation.power_requested", "false")])

    def test_power_on_restates_the_settings_the_appliance_kept_while_off(self) -> None:
        sender = Sender()
        result = run(router(sender, Provider(shadow())).async_turn_on(PAT, now=NOW))
        self.assertTrue(result and result.confirmed)
        # Not a power write: the observed frames all carry the mode, fan and temperature the
        # unit already had, so composing the tuple IS how it is turned on.
        self.assertEqual(sender.sent, [(BRIDGE, "climate.power_on_with_setpoint", "cool|high|24C")])
        self.assertEqual(
            sender.expected_states,
            [
                {
                    "operation.mode": "cool",
                    "fan.mode": "high",
                    "temperature.target_c": 24,
                }
            ],
        )

    def test_powering_on_in_a_mode_is_one_frame_rather_than_two_saying_the_same_thing(self) -> None:
        sender = Sender()
        run(router(sender, Provider(shadow())).async_turn_on(PAT, mode="dry", now=NOW))
        # The power-on frame carries mode, fan and setpoint whatever happens, so following it with
        # a mode write would put two frames on the wire for one press.
        self.assertEqual(sender.sent, [(BRIDGE, "climate.power_on_with_setpoint", "dry|high|24C")])

    def test_a_boolean_setting_is_named_by_the_caller_and_judged_by_the_bridge(self) -> None:
        sender = Sender()
        result = run(router(sender, Provider(shadow())).async_set_flag(PAT, "swing.vertical_enabled", True))
        self.assertTrue(result and result.confirmed)
        self.assertEqual(sender.sent, [(BRIDGE, "swing.vertical_enabled", "true")])

    def test_a_reviewed_parameterless_capability_uses_the_same_single_send_contract(self) -> None:
        sender = Sender()
        result = run(
            router(sender, Provider(shadow())).async_execute(
                PAT, "washer.operation.pause"
            )
        )
        self.assertTrue(result and result.confirmed)
        self.assertEqual(
            sender.sent,
            [(BRIDGE, "washer.operation.pause", "true")],
        )

    def test_a_stateless_exact_command_only_needs_authenticated_presence(self) -> None:
        sender = Sender()
        provider = Provider({}, healthy=False, alive=True)
        result = run(
            router(sender, provider).async_execute(
                PAT, "washer.operation.pause"
            )
        )
        self.assertTrue(result and result.confirmed)
        self.assertEqual(
            sender.sent,
            [(BRIDGE, "washer.operation.pause", "true")],
        )
        self.assertEqual(sender.expected_states, [None])

    def test_a_state_dependent_tuple_without_state_falls_back_before_wire(self) -> None:
        sender = Sender()
        provider = Provider({}, healthy=False, alive=True)
        self.assertIsNone(
            run(router(sender, provider).async_set_climate(PAT, fan="low", now=NOW))
        )
        self.assertEqual(sender.sent, [])

    def test_a_state_generation_mismatch_blocks_only_composite_commands(self) -> None:
        sender = Sender()
        provider = Provider(
            shadow(), healthy=True, alive=True, state_ready=False
        )
        r = router(sender, provider)
        self.assertIsNone(run(r.async_set_climate(PAT, fan="low", now=NOW)))
        result = run(r.async_execute(PAT, "washer.operation.pause"))
        self.assertTrue(result and result.confirmed)
        self.assertEqual(
            sender.sent,
            [(BRIDGE, "washer.operation.pause", "true")],
        )

    def test_partial_state_after_presence_edge_blocks_only_composite_commands(self) -> None:
        sender = Sender()
        provider = Provider(
            shadow(), healthy=True, alive=True, state_ready=True, fields_ready=False
        )
        r = router(sender, provider)

        self.assertIsNone(run(r.async_set_climate(PAT, fan="low", now=NOW)))
        result = run(r.async_execute(PAT, "washer.operation.pause"))

        self.assertTrue(result and result.confirmed)
        self.assertEqual(
            provider.requested_control_fields,
            ("operation.mode", "fan.mode", "temperature.target_c"),
        )
        self.assertEqual(
            sender.sent,
            [(BRIDGE, "washer.operation.pause", "true")],
        )

    def test_a_refusal_is_reported_once_per_cause_and_names_the_appliance(self) -> None:
        sender = Sender(LocalCommandUnavailable("no codec for that value"))
        r = router(sender, Provider(shadow()))
        with self.assertLogs(ROUTER_LOGGER, level="INFO") as caught:
            for target in (24, 24.5, 25, 25.5, 26):
                run(r.async_set_climate(PAT, target_c=target, now=NOW))
        # Five refused setpoints produce two structural causes, not five values: the first codec
        # refusal arms the cloud fence, and the next request reports that it is awaiting a fresh
        # post-cloud tuple. Repeats of either cause stay at debug.
        self.assertEqual(len(caught.records), 2)
        # And it says which appliance, or a household with three of them cannot tell one refusing
        # from all three.
        self.assertIn(PAT, caught.output[0])

        with self.assertLogs(ROUTER_LOGGER, level="INFO") as caught:
            run(r.async_turn_off(PAT))
        # A different capability is a different thing to know about, so it is not demoted.
        self.assertEqual(len(caught.records), 1)

    def test_a_structurally_different_cause_is_not_hidden_behind_the_first_one(self) -> None:
        # The providers mapping is the router's live view of the shadows, so changing what this
        # appliance reports is done through it rather than by reaching into the router.
        providers = {PAT: Provider(shadow(age=timedelta(minutes=11)))}
        r = LocalControlRouter(Sender(), providers, lambda pat: BRIDGE)
        with self.assertLogs(ROUTER_LOGGER, level="INFO") as caught:
            run(r.async_set_climate(PAT, fan="low", now=NOW))
        self.assertEqual(len(caught.records), 1)

        # A shadow that has not filled in yet is the usual first refusal; the permanent causes
        # under the same capability come later, and must not be demoted to a level nobody has on.
        providers[PAT] = Provider({**shadow(), "operation.mode": Field("auto", NOW)})
        with self.assertLogs(ROUTER_LOGGER, level="INFO") as caught:
            run(r.async_set_climate(PAT, fan="low", now=NOW))
        self.assertEqual(len(caught.records), 1)
        self.assertIn("auto", caught.output[0])

    def test_auto_mode_has_no_tuple_to_write_however_it_arrives(self) -> None:
        sender = Sender()
        fields = shadow()
        fields["operation.mode"] = Field("auto", NOW - timedelta(seconds=1))
        # A request to change only the fan would otherwise take `auto` from the shadow and state
        # a setpoint in a field that, in auto, carries something else entirely.
        self.assertIsNone(run(router(sender, Provider(fields)).async_set_climate(PAT, fan="low", now=NOW)))
        self.assertEqual(sender.sent, [])

    def test_leaving_auto_without_an_explicit_target_never_reuses_its_non_temperature_field(self) -> None:
        sender = Sender()
        fields = shadow()
        fields["operation.mode"] = Field("auto", NOW - timedelta(seconds=1))

        # Naming COOL does not turn AUTO's 0x1fe into a temperature. The cloud can express the
        # mode-only intent without restating that field, while the local tuple cannot.
        self.assertIsNone(
            run(router(sender, Provider(fields)).async_set_climate(PAT, mode="cool", now=NOW))
        )
        self.assertIsNone(
            run(router(sender, Provider(fields)).async_turn_on(PAT, mode="cool", now=NOW))
        )
        self.assertEqual(sender.sent, [])

    def test_leaving_auto_is_local_only_when_the_request_states_a_real_target(self) -> None:
        sender = Sender()
        fields = shadow()
        fields["operation.mode"] = Field("auto", NOW - timedelta(seconds=1))
        result = run(
            router(sender, Provider(fields)).async_set_climate(
                PAT, mode="cool", target_c=24, now=NOW
            )
        )
        self.assertTrue(result and result.confirmed)
        self.assertEqual(
            sender.sent,
            [(BRIDGE, "climate.mode_fan_setpoint", "cool|high|24C")],
        )

    def test_back_to_back_confirmed_tuples_compose_from_the_appliance_confirmation(self) -> None:
        sender = Sender()
        r = router(sender, Provider(shadow()))

        async def commands() -> None:
            first = await r.async_set_climate(PAT, mode="dry", now=NOW)
            second = await r.async_set_climate(PAT, fan="low", now=NOW + timedelta(seconds=1))
            self.assertTrue(first and first.confirmed)
            self.assertTrue(second and second.confirmed)

        run(commands())
        self.assertEqual(
            sender.sent,
            [
                (BRIDGE, "climate.mode_fan_setpoint", "dry|high|24C"),
                (BRIDGE, "climate.mode_fan_setpoint", "dry|low|24C"),
            ],
        )
        self.assertEqual(
            sender.expected_states,
            [
                {
                    "operation.mode": "cool",
                    "fan.mode": "high",
                    "temperature.target_c": 24,
                },
                {
                    "operation.mode": "dry",
                    "fan.mode": "high",
                    "temperature.target_c": 24,
                },
            ],
        )

    def test_a_newer_conflicting_appliance_field_supersedes_its_confirmed_overlay_field(self) -> None:
        sender = Sender()
        provider = Provider(shadow())
        r = router(sender, provider)

        async def commands() -> None:
            await r.async_set_climate(PAT, mode="dry", now=NOW)
            # The app/remote changed the mode after the local confirmation. Its newer field must
            # win even though it conflicts; only the two tuple fields not newly observed retain
            # their confirmed overlay values.
            provider.shadow_fields["operation.mode"] = Field(
                "cool", NOW + timedelta(seconds=1)
            )
            await r.async_set_climate(PAT, fan="low", now=NOW + timedelta(seconds=2))

        run(commands())
        self.assertEqual(sender.sent[-1], (BRIDGE, "climate.mode_fan_setpoint", "cool|low|24C"))

    def test_an_unsupported_swing_cloud_fallback_preserves_the_confirmed_climate_tuple(self) -> None:
        sender = SwingRefusingSender()
        r = router(sender, Provider(shadow()))

        async def commands() -> None:
            await r.async_set_climate(PAT, mode="dry", now=NOW)
            self.assertIsNone(
                await r.async_set_flag(PAT, "swing.horizontal_enabled", True)
            )
            await r.async_set_climate(PAT, fan="low", now=NOW + timedelta(seconds=1))

        run(commands())
        self.assertEqual(
            sender.sent[-1],
            (BRIDGE, "climate.mode_fan_setpoint", "dry|low|24C"),
        )

    def test_a_cloud_power_off_preserves_the_settings_the_appliance_keeps(self) -> None:
        sender = PowerOffRefusingSender()
        r = router(sender, Provider(shadow()))

        async def commands() -> None:
            await r.async_set_climate(PAT, mode="dry", now=NOW)
            self.assertIsNone(await r.async_turn_off(PAT))
            await r.async_set_climate(PAT, fan="low", now=NOW + timedelta(seconds=1))

        run(commands())
        self.assertEqual(
            sender.sent[-1],
            (BRIDGE, "climate.mode_fan_setpoint", "dry|low|24C"),
        )

    def test_a_cloud_tuple_barrier_blocks_stale_composition_without_a_wall_clock_escape(self) -> None:
        sender = Sender()
        provider = Provider(shadow())
        r = router(sender, provider)

        async def commands() -> None:
            await r.async_set_climate(PAT, mode="dry", now=NOW)
            await r.async_mark_cloud_tuple_dispatch(PAT)
            sent_before = len(sender.sent)
            # This represents an immediate fan write after cloud AUTO/POWER. Advancing the wall
            # clock far beyond the normal overlay window must not make an unchanged retained tuple
            # eligible again.
            self.assertIsNone(
                await r.async_set_climate(
                    PAT, fan="low", now=NOW + timedelta(minutes=30)
                )
            )
            self.assertEqual(len(sender.sent), sent_before)

        run(commands())

    def test_a_cloud_tuple_barrier_clears_only_after_every_tuple_input_is_newer(self) -> None:
        sender = Sender()
        provider = Provider(shadow())
        r = router(sender, provider)

        async def commands() -> None:
            await r.async_set_climate(PAT, mode="dry", now=NOW)
            await r.async_mark_cloud_tuple_dispatch(PAT)

            # A delayed delivery of the local confirmation is newer than the retained shadow but
            # older than the confirmed overlay used as the dispatch baseline. It must not be
            # mistaken for the cloud command's report.
            for field, value in (
                ("operation.mode", "dry"),
                ("fan.mode", "high"),
                ("temperature.target_c", 24),
            ):
                provider.shadow_fields[field] = Field(
                    value, NOW - timedelta(milliseconds=500)
                )
            self.assertIsNone(
                await r.async_set_climate(PAT, fan="low", now=NOW + timedelta(seconds=1))
            )

            # A partial report is insufficient: composing a three-field frame from two pre-cloud
            # inputs would still restate stale state.
            provider.shadow_fields["operation.mode"] = Field(
                "cool", NOW + timedelta(seconds=1)
            )
            self.assertIsNone(
                await r.async_set_climate(PAT, fan="low", now=NOW + timedelta(seconds=2))
            )

            for field, value in (
                ("operation.mode", "cool"),
                ("fan.mode", "high"),
                ("temperature.target_c", 24),
            ):
                provider.shadow_fields[field] = Field(
                    value, NOW + timedelta(seconds=3)
                )
            result = await r.async_set_climate(
                PAT, fan="low", now=NOW + timedelta(seconds=4)
            )
            self.assertTrue(result and result.confirmed)

        run(commands())
        self.assertEqual(
            sender.sent[-1],
            (BRIDGE, "climate.mode_fan_setpoint", "cool|low|24C"),
        )


if __name__ == "__main__":
    unittest.main()
