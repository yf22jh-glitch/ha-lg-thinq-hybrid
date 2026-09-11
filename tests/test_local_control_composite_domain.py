"""Pinned, non-Cartesian Local climate command-domain contract."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from custom_components.my_lg.local_control_composite_domain import (
    EXPECTED_LOCAL_CONTROL_COMPOSITE_DOMAIN_ROOT_SHA256,
    LocalControlCompositeDomainError,
    _load_local_control_composite_domain,
    load_local_control_composite_domain_contract,
)


COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "my_lg"
ARTIFACT = COMPONENT / "home-assistant-local-control-composite-domain.v1.json"
SIDECAR = COMPONENT / "home-assistant-local-control-composite-domain.v1.sha256"
BRIDGE_ARTIFACT = (
    Path(__file__).resolve().parents[2]
    / "lg_rethink_local"
    / "local"
    / "model-contract"
    / ARTIFACT.name
)
BRIDGE_SIDECAR = BRIDGE_ARTIFACT.with_suffix(".sha256")


class LocalControlCompositeDomainTests(unittest.TestCase):
    def test_bundled_authority_is_byte_identical_to_the_bridge_artifact(self) -> None:
        if not BRIDGE_ARTIFACT.is_file() or not BRIDGE_SIDECAR.is_file():
            self.skipTest("sibling lg_rethink_local checkout is not present")
        self.assertEqual(ARTIFACT.read_bytes(), BRIDGE_ARTIFACT.read_bytes())
        self.assertEqual(SIDECAR.read_bytes(), BRIDGE_SIDECAR.read_bytes())

    def test_exact_artifact_exposes_decoder_domain_without_cartesian_values(self) -> None:
        contract = load_local_control_composite_domain_contract()

        self.assertEqual(
            contract.root_sha256,
            EXPECTED_LOCAL_CONTROL_COMPOSITE_DOMAIN_ROOT_SHA256,
        )
        self.assertEqual(len(contract.capabilities), 4)
        for model_id in ("CST_170004_WW", "CST_570004_WW"):
            for capability_id in (
                "climate.mode_fan_setpoint",
                "climate.power_on_with_setpoint",
            ):
                capability = contract.capability(model_id, capability_id)
                self.assertIsNotNone(capability)
                assert capability is not None
                domain = capability.input_domain
                self.assertEqual(
                    domain.modes,
                    ("auto", "cool")
                    if model_id == "CST_170004_WW"
                    else ("auto", "cool", "dry", "fan_only"),
                )
                self.assertEqual(
                    domain.fans,
                    ("auto", "high", "low", "medium", "power", "very low"),
                )
                cool = domain.target_range("cool")
                self.assertIsNotNone(cool)
                assert cool is not None
                self.assertEqual((cool.min_c, cool.max_c, cool.step_c), (16, 30, 0.5))
                auto = domain.target_range("auto")
                self.assertIsNone(auto)
                self.assertEqual(
                    (
                        domain.comfort_preference.min_step,
                        domain.comfort_preference.max_step,
                        domain.comfort_preference.step,
                        domain.comfort_preference.applies_to_modes,
                    ),
                    (-2, 2, 1, ("auto",)),
                )

    def test_unseen_valid_compositions_are_authorized_component_by_component(self) -> None:
        contract = load_local_control_composite_domain_contract()

        for value in (
            "cool|very low|16C",
            "cool|high|26C",
            "cool|power|25.5C",
        ):
            with self.subTest(value=value):
                self.assertTrue(
                    contract.authorizes(
                        "CST_170004_WW", "climate.mode_fan_setpoint", value
                    )
                )

        for value in ("dry|auto|30C", "fan_only|medium|20.5C"):
            with self.subTest(value=value):
                self.assertTrue(
                    contract.authorizes(
                        "CST_570004_WW", "climate.mode_fan_setpoint", value
                    )
                )
                self.assertFalse(
                    contract.authorizes(
                        "CST_170004_WW", "climate.mode_fan_setpoint", value
                    )
                )

        for model_id in ("CST_170004_WW", "CST_570004_WW"):
            for value in (
                "auto|very low|comfort:-2",
                "auto|medium|comfort:0",
                "auto|high|comfort:2",
            ):
                with self.subTest(model_id=model_id, value=value):
                    self.assertTrue(
                        contract.authorizes(
                            model_id, "climate.mode_fan_setpoint", value
                        )
                    )

    def test_invalid_meaning_or_noncanonical_value_is_refused(self) -> None:
        contract = load_local_control_composite_domain_contract()

        for value in (
            "auto|high|24C",
            "dry|power|24C",
            "cool|high|15.5C",
            "cool|high|30.5C",
            "cool|high|24.25C",
            "cool|HIGH|24C",
            "cool|high|24.0C",
            "cool|high|comfort:0",
        ):
            with self.subTest(value=value):
                self.assertFalse(
                    contract.authorizes(
                        "CST_170004_WW", "climate.mode_fan_setpoint", value
                    )
                )

        for value in (
            "auto|high|15C",
            "auto|high|comfort:-3",
            "auto|high|comfort:3",
            "auto|power|comfort:0",
        ):
            with self.subTest(value=value):
                self.assertFalse(
                    contract.authorizes(
                        "CST_570004_WW", "climate.mode_fan_setpoint", value
                    )
                )

    def test_bytes_sidecar_duplicates_and_private_markers_fail_closed(self) -> None:
        raw = ARTIFACT.read_bytes()
        digest = SIDECAR.read_bytes()

        for name, mutated_raw, mutated_digest in (
            ("trailing-byte", raw + b" ", digest),
            ("sidecar", raw, b"0" * 64 + b"\n"),
            (
                "duplicate-key",
                raw.replace(
                    b'"authority": "decoder-model-contract-observed-layout",',
                    b'"authority": "decoder-model-contract-observed-layout", '
                    b'"authority": "decoder-model-contract-observed-layout",',
                    1,
                ),
                digest,
            ),
            (
                "private-marker",
                raw.replace(b'"stats": {', b'"binding_id": "private", "stats": {', 1),
                digest,
            ),
        ):
            with self.subTest(name=name), self.assertRaises(
                LocalControlCompositeDomainError
            ):
                _load_local_control_composite_domain(mutated_raw, mutated_digest)

        decoded = json.loads(raw)
        decoded["sourceTargetAuthoritySha256"] = "0" * 64
        mutated = (
            json.dumps(decoded, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode()
        with self.assertRaises(LocalControlCompositeDomainError):
            _load_local_control_composite_domain(mutated, digest)


if __name__ == "__main__":
    unittest.main()
