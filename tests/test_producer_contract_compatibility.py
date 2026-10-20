"""Intentional legacy differences must never hide additional contract drift."""
import json
from pathlib import Path
import unittest

from tests.producer_contract_compatibility import (
    differences,
    verify_composite_domain_sidecar,
    verify_reviewed_pair,
    verify_sidecar,
)

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "my_lg"


class ProducerCompatibilityTests(unittest.TestCase):
    def test_adding_a_new_authority_is_detected(self):
        before = (COMPONENT / "pilot-profiles.v1.json").read_bytes()
        changed = json.loads(before)
        changed["profiles"][0]["authoritative_invalidations"] = True
        with self.assertRaisesRegex(AssertionError, "review required"):
            verify_reviewed_pair("pilot_profiles", json.dumps(changed).encode(), before)

    def test_producer_drift_is_detected_even_if_consumer_is_unchanged(self):
        before = (COMPONENT / "pilot-profiles.v1.json").read_bytes()
        with self.assertRaisesRegex(AssertionError, "producer changed"):
            verify_reviewed_pair("pilot_profiles", before, before)

    def test_sidecar_cannot_authorize_different_bytes(self):
        before = (COMPONENT / "pilot-profiles.v1.json").read_bytes()
        sidecar = (COMPONENT / "pilot-profiles.v1.sha256").read_bytes()
        with self.assertRaisesRegex(AssertionError, "does not match"):
            verify_sidecar(before + b"\n", sidecar)

    def test_absence_null_false_and_number_have_distinct_meanings(self):
        cases = [({}, {"a": None}), ({"a": False}, {"a": 0}), ([], [None])]
        for before, after in cases:
            with self.subTest(before=before, after=after):
                self.assertTrue(differences(before, after))

    def test_composite_sidecar_uses_the_format_defined_digest(self):
        artifact = COMPONENT / "home-assistant-local-control-composite-domain.v1.json"
        verify_composite_domain_sidecar(
            artifact.read_bytes(), artifact.with_suffix(".sha256").read_bytes()
        )

    def test_composite_sidecar_rejects_changed_body_or_digest(self):
        artifact = COMPONENT / "home-assistant-local-control-composite-domain.v1.json"
        raw = artifact.read_bytes()
        sidecar = artifact.with_suffix(".sha256").read_bytes()
        changed = json.loads(raw)
        changed["sourceTargetAuthoritySha256"] = "0" * 64
        for body, digest in (
            (json.dumps(changed).encode(), sidecar),
            (raw, b"0" * 64 + b"\n"),
        ):
            with self.subTest(body_changed=body != raw), self.assertRaisesRegex(
                AssertionError, "does not match"
            ):
                verify_composite_domain_sidecar(body, digest)
