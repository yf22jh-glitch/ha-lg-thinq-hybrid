"""What the bridge's answer means for whether the same request may be sent again.

The whole reason a local write can fall back to the cloud is that some answers prove nothing
reached the appliance. Getting that reading wrong in either direction is the expensive kind of
bug: too strict and the local path is never used, too loose and one request becomes two
commands on a machine that heats, cools or spins.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import types
import unittest

_PACKAGE_NAME = "my_lg_local_command_test"
_DIRECTORY = Path(__file__).resolve().parents[1] / "custom_components" / "my_lg"
_package = types.ModuleType(_PACKAGE_NAME)
_package.__path__ = [str(_DIRECTORY)]
sys.modules[_PACKAGE_NAME] = _package
_spec = importlib.util.spec_from_file_location(
    f"{_PACKAGE_NAME}.local_command", _DIRECTORY / "local_command.py"
)
assert _spec is not None and _spec.loader is not None
local_command = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = local_command
_spec.loader.exec_module(local_command)

DEVICE = "synthetic-bridge-device-001"


class Response:
    def __init__(self, status: int, body: object) -> None:
        self.status = status
        self._body = body

    async def json(self, content_type: object = None) -> object:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    async def __aenter__(self) -> "Response":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class Session:
    def __init__(self, response: object) -> None:
        self._response = response
        self.posts: list[tuple[str, dict]] = []

    def post(self, url: str, **kwargs: object) -> object:
        self.posts.append((url, dict(kwargs)))
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def send(response: object):
    session = Session(response)
    client = local_command.LocalCommandClient(session, "http://bridge:44401")
    return session, asyncio.run(client.async_send(DEVICE, "operation.power_requested", "false"))


def send_expecting(response: object, error: type[Exception]):
    session = Session(response)
    client = local_command.LocalCommandClient(session, "http://bridge:44401")
    try:
        asyncio.run(client.async_send(DEVICE, "operation.power_requested", "false"))
    except error as err:
        return session, err
    raise AssertionError(f"expected {error.__name__}")


class LocalCommandClientTest(unittest.TestCase):
    def test_the_appliance_confirming_is_the_only_success(self) -> None:
        session, result = send(Response(200, {"verdict": "confirmed", "state": {"power": False}}))
        self.assertTrue(result.confirmed)
        self.assertEqual(result.state, {"power": False})
        url, kwargs = session.posts[0]
        self.assertEqual(
            url,
            f"http://bridge:44401/control/home-assistant/{DEVICE}",
        )
        self.assertEqual(kwargs["json"], {"capability": "operation.power_requested", "value": "false"})
        # A redirect could replay the POST at another endpoint. One Home Assistant request must
        # remain exactly one bridge request, even when a proxy is misconfigured.
        self.assertIs(kwargs["allow_redirects"], False)

    def test_both_climate_commands_carry_the_exact_pre_command_state(self) -> None:
        expected_state = {
            "operation.mode": "cool",
            "fan.mode": "high",
            "temperature.target_c": 24,
        }
        for capability, value in (
            ("climate.mode_fan_setpoint", "cool|low|24C"),
            ("climate.power_on_with_setpoint", "cool|high|24C"),
        ):
            with self.subTest(capability=capability):
                session = Session(
                    Response(200, {"verdict": "confirmed", "state": {}})
                )
                client = local_command.LocalCommandClient(
                    session, "http://bridge:44401"
                )
                asyncio.run(
                    client.async_send(
                        DEVICE,
                        capability,
                        value,
                        expected_state=expected_state,
                    )
                )
                self.assertEqual(
                    session.posts[0][1]["json"],
                    {
                        "capability": capability,
                        "value": value,
                        "expected_state": expected_state,
                    },
                )

        auto_state = {
            "operation.mode": "auto",
            "fan.mode": "medium",
            "comfort.preference_step": 0,
        }
        session = Session(Response(200, {"verdict": "confirmed", "state": {}}))
        client = local_command.LocalCommandClient(session, "http://bridge:44401")
        asyncio.run(
            client.async_send(
                DEVICE,
                "climate.mode_fan_setpoint",
                "auto|medium|comfort:1",
                expected_state=auto_state,
            )
        )
        self.assertEqual(session.posts[0][1]["json"]["expected_state"], auto_state)

    def test_a_climate_tuple_without_an_exact_expected_state_is_refused_before_http(
        self,
    ) -> None:
        for expected_state in (
            None,
            [
                "operation.mode",
                "fan.mode",
                "temperature.target_c",
            ],
            {"operation.mode": "cool", "fan.mode": "high"},
            {
                "operation.mode": "cool",
                "fan.mode": "high",
                "temperature.target_c": float("inf"),
            },
            {
                "operation.mode": "cool",
                "fan.mode": "high",
                "temperature.target_c": 24,
                "extra": True,
            },
            {
                "operation.mode": "auto",
                "fan.mode": "high",
                "temperature.target_c": 17,
            },
            {
                "operation.mode": "cool",
                "fan.mode": "high",
                "comfort.preference_step": 0,
            },
        ):
            with self.subTest(expected_state=expected_state):
                session = Session(Response(200, {"verdict": "confirmed"}))
                client = local_command.LocalCommandClient(
                    session, "http://bridge:44401"
                )
                with self.assertRaises(local_command.LocalCommandUnavailable):
                    asyncio.run(
                        client.async_send(
                            DEVICE,
                            "climate.mode_fan_setpoint",
                            "cool|low|24C",
                            expected_state=expected_state,
                        )
                    )
                self.assertEqual(session.posts, [])

    def test_already_in_that_state_is_a_confirmation_too(self) -> None:
        _session, result = send(Response(200, {"verdict": "already", "state": {}}))
        self.assertTrue(result.confirmed)

    def test_a_refusal_before_the_write_leaves_the_request_for_the_cloud(self) -> None:
        for status in (400, 404, 409):
            with self.subTest(status=status):
                _session, err = send_expecting(
                    Response(status, {"error": "refused"}), local_command.LocalCommandUnavailable
                )
                self.assertIn("refused", str(err))

    def test_a_change_the_appliance_never_reported_is_pending_not_a_fallback(self) -> None:
        # 202: the bridge sent it. Offering this to the cloud would be a second command.
        send_expecting(
            Response(202, {"verdict": "unreported", "state": {}}), local_command.LocalCommandPending
        )

    def test_a_write_nothing_could_confirm_is_returned_rather_than_raised(self) -> None:
        _session, result = send(Response(202, {"verdict": "unverifiable", "state": {}}))
        # Restating a setting the unit is already on lands here. Raising made that look like an
        # error, and the obvious response to an error is to press it again - the second frame.
        self.assertFalse(result.confirmed)
        self.assertEqual(result.verdict, "unverifiable")

    def test_a_verdict_this_does_not_know_is_never_read_as_success(self) -> None:
        send_expecting(Response(202, {"verdict": "who knows"}), local_command.LocalCommandFailed)

    def test_an_error_that_says_nothing_about_the_wire_is_pending_not_retried(self) -> None:
        send_expecting(Response(500, {"error": "boom"}), local_command.LocalCommandPending)

    def test_only_the_gateway_answer_that_forwarded_nothing_may_be_retried(self) -> None:
        # 503: nothing in front of the bridge had anywhere to send it, so the endpoint never ran.
        send_expecting(Response(503, {"error": "no upstream"}), local_command.LocalCommandUnavailable)
        # 502 and 504 do not say that. nginx answers 502 when the upstream closed after receiving
        # the request, and 504 when it forwarded and gave up waiting - which is exactly the window
        # where the bridge has already sent the frame and is waiting for the appliance.
        for status in (502, 504):
            with self.subTest(status=status):
                send_expecting(Response(status, {"error": "bad gateway"}), local_command.LocalCommandPending)

    def test_a_bridge_that_could_not_be_connected_to_did_not_send_anything(self) -> None:
        import aiohttp
        from unittest.mock import Mock

        failure = aiohttp.ClientConnectorError(Mock(), OSError("connection refused"))
        send_expecting(failure, local_command.LocalCommandUnavailable)

    def test_a_request_that_went_out_and_then_failed_is_assumed_to_have_landed(self) -> None:
        import aiohttp

        # The bridge records and sends the frame BEFORE it waits up to twenty seconds for the
        # appliance to report, so a timeout is exactly when the write most likely did happen.
        send_expecting(TimeoutError("too slow"), local_command.LocalCommandPending)
        send_expecting(aiohttp.ServerDisconnectedError(), local_command.LocalCommandPending)

    def test_a_connect_phase_failure_leaves_the_request_for_the_cloud(self) -> None:
        import aiohttp

        # A host that is wedged or blackholed times out while still connecting. Nothing was sent,
        # and reading it as sent would make every command a hard error with the cloud never tried.
        send_expecting(aiohttp.ConnectionTimeoutError("connect timed out"), local_command.LocalCommandUnavailable)
        # And a URL that was never usable is a configuration fault, not an appliance one.
        send_expecting(aiohttp.InvalidURL("http://:bad"), local_command.LocalCommandUnavailable)

    def test_a_body_that_is_not_the_endpoint_s_own_json_is_read_from_the_status(self) -> None:
        # A proxy answering with a page. The status still says whether anything was sent.
        send_expecting(Response(500, ValueError("not json")), local_command.LocalCommandPending)
        send_expecting(Response(404, ValueError("not json")), local_command.LocalCommandUnavailable)

    def test_a_body_that_is_json_but_not_an_object_does_not_escape_as_an_attribute_error(self) -> None:
        # Neither exception the callers handle: it would reach neither the appliance nor the cloud.
        for body in ("ok", [1, 2], 7):
            with self.subTest(body=body):
                send_expecting(Response(409, body), local_command.LocalCommandUnavailable)
        _session, result = send(Response(200, {"verdict": "confirmed"}))
        self.assertTrue(result.confirmed)


class ReflectsTheApplianceTest(unittest.TestCase):
    """Whether Home Assistant may show the requested state as though it took effect."""

    def test_a_request_the_cloud_took_is_reflected_as_it_always_was(self) -> None:
        self.assertTrue(local_command.reflects_the_appliance(None))

    def test_a_confirmed_local_write_is_reflected(self) -> None:
        for verdict in ("confirmed", "already"):
            with self.subTest(verdict=verdict):
                self.assertTrue(
                    local_command.reflects_the_appliance(local_command.LocalCommandResult(verdict, {}))
                )

    def test_a_write_nothing_could_confirm_is_not_reflected(self) -> None:
        # Showing it would be a guess wearing the appliance's own numbers, and the next report is
        # a second away.
        self.assertFalse(
            local_command.reflects_the_appliance(local_command.LocalCommandResult("unverifiable", {}))
        )


class SwingWritesTest(unittest.TestCase):
    """Each direction's two names stay attached to each other and to the right selection."""

    def test_each_direction_keeps_its_own_field_and_capability(self) -> None:
        horizontal, vertical = local_command.swing_writes(horizontal=True, vertical=False)
        # A swap here would send a horizontal selection to the vertical vane, with nothing in the
        # rest of the flow able to notice it.
        self.assertEqual(
            (horizontal.field, horizontal.capability, horizontal.enabled),
            ("rotateLeftRight", "swing.horizontal_enabled", True),
        )
        self.assertEqual(
            (vertical.field, vertical.capability, vertical.enabled),
            ("rotateUpDown", "swing.vertical_enabled", False),
        )

    def test_a_direction_the_appliance_does_not_have_is_not_written(self) -> None:
        self.assertEqual(
            [write.field for write in local_command.swing_writes(horizontal=None, vertical=True)],
            ["rotateUpDown"],
        )
        self.assertEqual(local_command.swing_writes(horizontal=None, vertical=None), ())

    def test_off_is_a_write_of_false_rather_than_no_write(self) -> None:
        self.assertEqual(
            [write.enabled for write in local_command.swing_writes(horizontal=False, vertical=False)],
            [False, False],
        )


class ClimateTupleTest(unittest.TestCase):
    """The frame carries mode, fan and setpoint together, so all three have to be true."""

    def setUp(self) -> None:
        self.now = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)
        at = self.now - timedelta(seconds=1)
        self.shadow = {
            "operation.mode": SimpleNamespace(value="cool", observed_at=at),
            "fan.mode": SimpleNamespace(value="high", observed_at=at),
            "temperature.target_c": SimpleNamespace(value=24.0, observed_at=at),
        }

    def test_a_whole_degree_is_written_the_way_the_appliance_writes_it(self) -> None:
        self.assertEqual(
            local_command.climate_tuple(self.shadow, now=self.now),
            "cool|high|24C",
        )

    def test_a_half_degree_keeps_its_half(self) -> None:
        self.shadow["temperature.target_c"] = SimpleNamespace(
            value=24.5, observed_at=self.now - timedelta(seconds=1)
        )
        self.assertEqual(local_command.climate_tuple(self.shadow, now=self.now), "cool|high|24.5C")

    def test_a_field_the_appliance_has_not_reported_recently_is_named_rather_than_guessed(self) -> None:
        self.shadow["fan.mode"] = SimpleNamespace(value="high", observed_at=self.now - timedelta(minutes=11))
        with self.assertRaises(local_command.LocalCommandUnavailable) as caught:
            local_command.climate_tuple(self.shadow, now=self.now)
        # Naming it matters: the alternative is writing a guess into a field nobody asked to change.
        self.assertIn("fan.mode", str(caught.exception))

    def test_a_setpoint_cannot_be_reused_without_a_fresh_mode_to_classify_it(self) -> None:
        # COOL and AUTO use different numeric grids. Per-field observations can age independently,
        # so a fresh-looking 16 must not be assigned to either grid after its mode reading expired.
        self.shadow["operation.mode"] = SimpleNamespace(
            value="auto", observed_at=self.now - timedelta(minutes=11)
        )
        self.shadow["temperature.target_c"] = SimpleNamespace(
            value=16, observed_at=self.now - timedelta(seconds=1)
        )
        with self.assertRaises(local_command.LocalCommandUnavailable) as caught:
            local_command.climate_tuple(self.shadow, mode="cool", now=self.now)
        self.assertIn("operation.mode", str(caught.exception))

        # An explicit target is the request's own temperature, so it does not depend on what the
        # stale source-mode field made 0x1fe mean.
        self.assertEqual(
            local_command.climate_tuple(
                self.shadow, mode="cool", target_c=24, now=self.now
            ),
            "cool|high|24C",
        )

    def test_auto_uses_its_reported_unitless_preference_for_an_in_mode_change(self) -> None:
        at = self.now - timedelta(seconds=1)
        self.shadow["operation.mode"] = SimpleNamespace(value="auto", observed_at=at)
        self.shadow.pop("temperature.target_c")
        self.shadow["comfort.preference_step"] = SimpleNamespace(value=0, observed_at=at)

        self.assertEqual(
            local_command.climate_tuple(self.shadow, fan="low", now=self.now),
            "auto|low|comfort:0",
        )

    def test_crossing_auto_requires_the_destination_variant_argument(self) -> None:
        with self.assertRaisesRegex(
            local_command.LocalCommandUnavailable, "comfort.preference_step"
        ):
            local_command.climate_tuple(self.shadow, mode="auto", now=self.now)
        self.assertEqual(
            local_command.climate_tuple(
                self.shadow,
                mode="auto",
                retained_comfort_preference=0,
                now=self.now,
            ),
            "auto|high|comfort:0",
        )
        self.assertEqual(
            local_command.climate_tuple(
                self.shadow,
                mode="auto",
                comfort_preference=1,
                now=self.now,
            ),
            "auto|high|comfort:1",
        )
        with self.assertRaisesRegex(
            local_command.LocalCommandUnavailable, "not a Celsius target"
        ):
            local_command.climate_tuple(
                self.shadow, mode="auto", target_c=18, now=self.now
            )

    def test_the_placeholder_setpoint_reported_under_the_power_fan_is_never_written(self) -> None:
        at = self.now - timedelta(seconds=1)
        self.shadow["fan.mode"] = SimpleNamespace(value="power", observed_at=at)
        self.shadow["temperature.target_c"] = SimpleNamespace(value=18, observed_at=at)
        # While the fan is on power the appliance reports 18 and gives the real target back as soon
        # as the fan leaves it. Changing the fan is how anyone leaves 파워 냉방풍, and that request
        # would have written the 18 as the genuine setpoint - the case that matters most, and the
        # one a guard on the requested fan rather than the reported one leaves open.
        with self.assertRaises(local_command.LocalCommandUnavailable):
            local_command.climate_tuple(self.shadow, fan="low", now=self.now)
        with self.assertRaises(local_command.LocalCommandUnavailable):
            local_command.climate_tuple(self.shadow, mode="cool", now=self.now)
        # A previously observed non-power target may carry the appliance back
        # out of POWER without turning the temporary 18 into a setting.
        self.assertEqual(
            local_command.climate_tuple(
                self.shadow,
                fan="low",
                retained_target_c=25,
                now=self.now,
            ),
            "cool|low|25C",
        )

    def test_a_setpoint_cannot_be_filled_in_without_a_fresh_fan_reading_to_classify_it(self) -> None:
        stale = self.now - timedelta(minutes=11)
        self.shadow["fan.mode"] = SimpleNamespace(value="power", observed_at=stale)
        self.shadow["temperature.target_c"] = SimpleNamespace(
            value=18, observed_at=self.now - timedelta(seconds=1)
        )
        # Fields are timestamped one by one, so the fan reading can go stale while the setpoint
        # stays fresh - and an 18 nobody can classify is exactly the one that must not be written.
        with self.assertRaises(local_command.LocalCommandUnavailable):
            local_command.climate_tuple(self.shadow, fan="low", now=self.now)
        del self.shadow["fan.mode"]
        with self.assertRaises(local_command.LocalCommandUnavailable):
            local_command.climate_tuple(self.shadow, fan="low", now=self.now)

    def test_power_fan_uses_the_current_target_structurally_but_cannot_change_it(self) -> None:
        self.assertEqual(
            local_command.climate_tuple(
                self.shadow, fan="power", now=self.now
            ),
            "cool|power|24C",
        )
        with self.assertRaises(local_command.LocalCommandUnavailable):
            local_command.climate_tuple(self.shadow, fan="power", target_c=25, now=self.now)

    def test_power_fan_is_rejected_outside_cooling_without_coercion(self) -> None:
        with self.assertRaisesRegex(
            local_command.LocalCommandUnavailable, "cooling mode"
        ):
            local_command.climate_tuple(
                self.shadow, mode="dry", fan="power", now=self.now
            )

    def test_a_setpoint_that_is_not_a_number_leaves_the_request_for_the_cloud(self) -> None:
        self.shadow["temperature.target_c"] = SimpleNamespace(
            value="warm", observed_at=self.now - timedelta(seconds=1)
        )
        # Raised rather than escaping as a ValueError, which would reach neither path.
        with self.assertRaises(local_command.LocalCommandUnavailable):
            local_command.climate_tuple(self.shadow, fan="low", now=self.now)

    def test_what_the_request_names_wins_over_what_the_appliance_reports(self) -> None:
        self.assertEqual(
            local_command.climate_tuple(self.shadow, fan="low", now=self.now),
            "cool|low|24C",
        )


if __name__ == "__main__":
    unittest.main()
