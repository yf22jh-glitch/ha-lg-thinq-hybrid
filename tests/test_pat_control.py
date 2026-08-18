"""Encode/decode contract tests for public ThinQ Connect controls."""

from __future__ import annotations

import unittest

from custom_components.my_lg.pat_control import (
    PatStateExpectation,
    PatStateRequirement,
    build_cooktop_pat_request,
    build_pat_control_request,
    failed_pat_requirement,
    pat_state_verified,
)


class PatControlContractTests(unittest.TestCase):
    def test_direct_payload_requires_all_exact_echoes(self) -> None:
        request = build_pat_control_request(
            {"operation": {"airConOperationMode": "POWER_ON"}}
        )

        self.assertEqual(
            dict(request.payload),
            {"operation": {"airConOperationMode": "POWER_ON"}},
        )
        self.assertTrue(
            pat_state_verified(
                {"operation": {"airConOperationMode": "POWER_ON"}},
                request.expectations,
            )
        )
        self.assertFalse(
            pat_state_verified(
                {"operation": {"airConOperationMode": "POWER_OFF"}},
                request.expectations,
            )
        )

    def test_mapped_contract_covers_every_encoded_leaf(self) -> None:
        payload = {
            "temperatureInUnits": {
                "locationName": "FRIDGE",
                "targetTemperatureC": 3,
            }
        }
        request = build_pat_control_request(
            payload,
            echo_contract={
                ("temperatureInUnits", "locationName"): (
                    "temperature",
                    "@location=FRIDGE",
                    "locationName",
                ),
                ("temperatureInUnits", "targetTemperatureC"): (
                    "temperature",
                    "@location=FRIDGE",
                    "targetTemperature",
                ),
            },
        )
        self.assertTrue(
            pat_state_verified(
                {
                    "temperature": [
                        {"locationName": "FRIDGE", "targetTemperature": 3}
                    ]
                },
                request.expectations,
            )
        )

        with self.assertRaisesRegex(ValueError, "every payload leaf"):
            build_pat_control_request(
                payload,
                echo_contract={
                    ("temperatureInUnits", "targetTemperatureC"): (
                        "temperature",
                        "@location=FRIDGE",
                        "targetTemperature",
                    )
                },
            )

    def test_command_only_value_can_map_to_authoritative_state_value(self) -> None:
        request = build_pat_control_request(
            {"operation": {"stylerOperationMode": "POWER_OFF"}},
            echo_contract={
                ("operation", "stylerOperationMode"): PatStateExpectation(
                    ("runState", "currentState"), "POWER_OFF"
                )
            },
        )
        self.assertTrue(
            pat_state_verified(
                {"runState": {"currentState": "POWER_OFF"}},
                request.expectations,
            )
        )
        self.assertFalse(
            pat_state_verified(
                {"runState": {"currentState": "INITIAL"}},
                request.expectations,
            )
        )

    def test_bool_is_not_accepted_as_numeric_echo(self) -> None:
        expectation = (PatStateExpectation(("value",), 1),)
        self.assertTrue(pat_state_verified({"value": 1.0}, expectation))
        self.assertFalse(pat_state_verified({"value": True}, expectation))

    def test_requirements_are_exact_and_missing_is_opt_in(self) -> None:
        strict = PatStateRequirement(
            ("mode",), ("COOL",), "mode changed"
        )
        optional = PatStateRequirement(
            ("remote",), (True,), "remote denied", allow_missing=True
        )
        self.assertIsNone(
            failed_pat_requirement({"mode": "COOL"}, (strict, optional))
        )
        self.assertEqual(
            failed_pat_requirement(
                {"mode": "AUTO", "remote": True}, (strict, optional)
            ),
            "mode changed",
        )
        self.assertEqual(
            failed_pat_requirement(
                {"mode": "COOL", "remote": False}, (strict, optional)
            ),
            "remote denied",
        )

    def test_cooktop_factory_preserves_fresh_fields_and_verifies_all(self) -> None:
        state = [
            {
                "location": {"locationName": "LEFT_FRONT"},
                "power": {"powerLevel": 2},
                "timer": {"remainHour": 0, "remainMinute": 15},
                "remoteControlEnable": {"remoteControlEnabled": True},
                "control": {"controlEnabled": True},
            }
        ]
        request = build_cooktop_pat_request(
            state, "LEFT_FRONT", power_level=3
        )
        self.assertEqual(
            dict(request.payload),
            {
                "power": {"powerLevel": 3},
                "timer": {"remainHour": 0, "remainMinute": 15},
                "location": {"locationName": "LEFT_FRONT"},
            },
        )
        post = [
            {
                **state[0],
                "power": {"powerLevel": 3},
            }
        ]
        self.assertTrue(pat_state_verified(post, request.expectations))
        self.assertIsNone(failed_pat_requirement(state, request.requirements))

        nested_group_state = [
            {
                "location": {"locationName": "LEFT_FRONT"},
                "power": [
                    {"locationName": "LEFT_FRONT", "powerLevel": 2}
                ],
                "timer": [
                    {
                        "locationName": "LEFT_FRONT",
                        "remainHour": 0,
                        "remainMinute": 15,
                    }
                ],
                "control": [
                    {"locationName": "LEFT_FRONT", "controlEnabled": True}
                ],
            }
        ]
        nested_request = build_cooktop_pat_request(
            nested_group_state, "LEFT_FRONT", power_level=3
        )
        nested_post = [
            {
                **nested_group_state[0],
                "power": [
                    {"locationName": "LEFT_FRONT", "powerLevel": 3}
                ],
                "timer": [
                    {
                        "locationName": "LEFT_FRONT",
                        "remainHour": 0,
                        "remainMinute": 15,
                    }
                ],
            }
        ]
        self.assertTrue(
            pat_state_verified(nested_post, nested_request.expectations)
        )
        self.assertIsNone(
            failed_pat_requirement(
                nested_group_state, nested_request.requirements
            )
        )

        with self.assertRaisesRegex(ValueError, "preservation field"):
            build_cooktop_pat_request(
                [
                    {
                        "location": {"locationName": "LEFT_FRONT"},
                        "control": {"controlEnabled": True},
                    }
                ],
                "LEFT_FRONT",
                power_level=3,
            )


if __name__ == "__main__":
    unittest.main()
