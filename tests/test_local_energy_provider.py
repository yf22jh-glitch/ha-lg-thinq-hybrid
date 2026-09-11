"""Contract tests for the independent retained cumulative-energy feed."""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType


COMPONENT_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "my_lg"
PACKAGE_NAME = "my_lg_local_energy_provider_test"
PACKAGE = ModuleType(PACKAGE_NAME)
PACKAGE.__path__ = [str(COMPONENT_PATH)]
sys.modules[PACKAGE_NAME] = PACKAGE


def _load(name: str):
    path = COMPONENT_PATH / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{PACKAGE_NAME}.{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


energy = _load("local_energy_provider")
BINDING = "local_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
PROOF = "b" * 64


def envelope(
    *,
    model: str = "WBEF3",
    binding_generation: int = 1,
    baseline: int = 10,
    last_counted: int = 20,
    cursor: int = 20,
    total: int = 5000,
) -> dict[str, object]:
    semantic_ids = energy._MODEL_FIELDS[model]
    fields = [
        {
            "semantic_id": semantic_id,
            "value_type": "number",
            "unit": "Wh",
            "device_class": "energy",
            "state_class": "total_increasing",
        }
        for semantic_id in semantic_ids
    ]
    totals = {semantic_id: total for semantic_id in semantic_ids}
    return {
        "schema_version": 1,
        "publication_plan_revision": 1,
        "policy_revision": "cumulative-energy-materialization-v1",
        "feed_contract_sha256": energy._feed_contract_sha256(model),
        "fields": fields,
        "binding_id": BINDING,
        "model_id": model,
        "binding_generation": binding_generation,
        "pat_device_id_proof_sha256": PROOF,
        "publication_session_id": "c" * 32,
        "ledger_schema_version": 1,
        "ledger_record_sha256": "d" * 64,
        "baseline_generation": baseline,
        "last_counted_generation": last_counted,
        "cursor_generation_before_publish": cursor,
        "ledger_ahead_by_generations": last_counted - cursor,
        "published_at": "2026-09-07T12:00:00.000Z",
        "totals_wh": totals,
    }


class CumulativeEnergyProviderTests(unittest.TestCase):
    def provider(self, model: str = "WBEF3"):
        return energy.CumulativeEnergyShadowProvider(BINDING, model, PROOF)

    @staticmethod
    def ingest(provider, value: dict[str, object]) -> None:
        provider.ingest(
            provider.current_topic,
            json.dumps(value, separators=(",", ":")).encode(),
            qos=1,
            retained=True,
        )

    def test_contract_hashes_match_producer(self) -> None:
        self.assertEqual(
            energy._feed_contract_sha256("WBEF3"),
            "a84175ec56ef418184c6a1d7b9142e3851fb81f581e4b6188749ddf92172a18a",
        )
        self.assertEqual(
            energy._feed_contract_sha256("WTL_KPK_BDH_KR_01"),
            "eda28553c0199dffad48b85e2cfb4a4abe175685f6165aacf22d25285fd1a09c",
        )

    def test_accepts_exact_monotonic_level(self) -> None:
        provider = self.provider()
        provider.set_transport_ready(True)
        self.ingest(provider, envelope())
        self.assertTrue(provider.field_available("energy.total_wh"))
        self.assertEqual(provider.total_wh("energy.total_wh"), 5000)

    def test_accepts_proven_baseline_reset_and_continues(self) -> None:
        provider = self.provider()
        provider.set_transport_ready(True)
        self.ingest(provider, envelope())
        self.ingest(
            provider,
            envelope(baseline=21, last_counted=21, cursor=21, total=0),
        )
        self.ingest(
            provider,
            envelope(baseline=21, last_counted=22, cursor=22, total=8),
        )
        self.assertEqual(provider.total_wh("energy.total_wh"), 8)

    def test_rejects_total_regression_without_reset_evidence(self) -> None:
        provider = self.provider()
        provider.set_transport_ready(True)
        self.ingest(provider, envelope())
        with self.assertRaises(energy.CumulativeEnergyProviderContractError):
            self.ingest(
                provider,
                envelope(baseline=10, last_counted=21, cursor=21, total=4999),
            )

    def test_rejected_exact_topic_can_invalidate_stale_state(self) -> None:
        provider = self.provider()
        provider.set_transport_ready(True)
        self.ingest(provider, envelope())
        provider.reject_current()
        self.assertFalse(provider.field_available("energy.total_wh"))
        self.assertEqual(provider.total_wh("energy.total_wh"), 5000)

    def test_transport_reconnect_requires_fresh_retained_level(self) -> None:
        provider = self.provider()
        provider.set_transport_ready(True)
        self.ingest(provider, envelope())
        provider.set_transport_ready(False)
        provider.set_transport_ready(True)
        self.assertFalse(provider.field_available("energy.total_wh"))

    def test_washtower_requires_two_exact_totals(self) -> None:
        provider = self.provider("WTL_KPK_BDH_KR_01")
        provider.set_transport_ready(True)
        value = envelope(model="WTL_KPK_BDH_KR_01", total=87)
        self.ingest(provider, value)
        self.assertEqual(provider.total_wh("washer.energy.total_wh"), 87)
        self.assertEqual(provider.total_wh("dryer.energy.total_wh"), 87)


if __name__ == "__main__":
    unittest.main()
