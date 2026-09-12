"""Pinned model controls and scoped per-device registration eligibility."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from custom_components.my_lg.local_control_contract import (
    EXPECTED_LOCAL_CONTROL_CHECKPOINT_REVISION,
    EXPECTED_LOCAL_CONTROL_CHECKPOINT_SHA256,
    EXPECTED_LOCAL_CONTROL_ENTITY_CONTRACT_ROOT_SHA256,
    EXPECTED_LOCAL_CONTROL_TARGET_AUTHORITY_REVISION,
    EXPECTED_LOCAL_CONTROL_TARGET_AUTHORITY_SHA256,
    LOCAL_CONTROL_ELIGIBILITY_OPTION,
    LocalControlEligibilityError,
    _load_local_control_entity_contract,
    eligible_factory_descriptors,
    load_local_control_entity_contract,
    local_control_capability_authorized,
    local_control_value_authorized,
    resolve_local_control_binding_eligibility,
)

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "my_lg"
ARTIFACT = COMPONENT / "home-assistant-local-control-entity-contract.v1.json"
SIDECAR = COMPONENT / "home-assistant-local-control-entity-contract.v1.sha256"


def _binding_id(model_index: int, unit_index: int) -> str:
    return f"test_control_binding_{model_index:02d}_{unit_index:02d}"


def _fleet_payload():
    """Build a role-free exact-18 synthetic form of the private v3 input."""
    contract = load_local_control_entity_contract()
    binding_models: dict[str, str] = {}
    rows: list[dict[str, object]] = []
    for model_index, (model_id, unit_count) in enumerate(
        contract.model_fleet_counts.items(), start=1
    ):
        for unit_index in range(1, unit_count + 1):
            binding_id = _binding_id(model_index, unit_index)
            binding_models[binding_id] = model_id
            rows.append(
                {
                    "binding_id": binding_id,
                    "entries": [
                        {
                            "capability_id": descriptor.capability_id,
                            "exact_values": list(descriptor.exact_local_request_values),
                        }
                        for descriptor in contract.descriptors_by_model.get(
                            model_id, ()
                        )
                    ],
                }
            )
    return (
        contract,
        binding_models,
        {
            LOCAL_CONTROL_ELIGIBILITY_OPTION: {
                "schema_version": 3,
                "contract_sha256": contract.root_sha256,
                "checkpoint_revision": EXPECTED_LOCAL_CONTROL_CHECKPOINT_REVISION,
                "checkpoint_sha256": EXPECTED_LOCAL_CONTROL_CHECKPOINT_SHA256,
                "target_authority_revision": (
                    EXPECTED_LOCAL_CONTROL_TARGET_AUTHORITY_REVISION
                ),
                "target_authority_sha256": (
                    EXPECTED_LOCAL_CONTROL_TARGET_AUTHORITY_SHA256
                ),
                "bindings": rows,
            }
        },
    )


class LocalControlEntityContractTests(unittest.TestCase):
    def test_selected_devices_do_not_require_a_fixed_whole_house_count(self) -> None:
        contract, binding_models, options = _fleet_payload()
        payload = options[LOCAL_CONTROL_ELIGIBILITY_OPTION]
        selected = next(
            row for row in payload["bindings"]
            if binding_models[row["binding_id"]] == "HUM_056905_WW"
        )
        scoped = {LOCAL_CONTROL_ELIGIBILITY_OPTION: {**payload, "bindings": [selected]}}
        result = resolve_local_control_binding_eligibility(scoped, contract, binding_models)
        self.assertEqual(set(result), {selected["binding_id"]})
        single_model = {selected["binding_id"]: "HUM_056905_WW"}
        self.assertEqual(
            set(resolve_local_control_binding_eligibility(scoped, contract, single_model)),
            set(single_model),
        )

    def test_additional_device_of_a_supported_model_does_not_change_other_devices(self) -> None:
        contract, binding_models, options = _fleet_payload()
        payload = options[LOCAL_CONTROL_ELIGIBILITY_OPTION]
        selected = next(
            row for row in payload["bindings"]
            if binding_models[row["binding_id"]] == "HUM_056905_WW"
        )
        extra_id = "test_control_new_humidifier"
        rows = sorted(
            [*payload["bindings"], {**selected, "binding_id": extra_id}],
            key=lambda row: row["binding_id"],
        )
        result = resolve_local_control_binding_eligibility(
            {LOCAL_CONTROL_ELIGIBILITY_OPTION: {**payload, "bindings": rows}},
            contract,
            {**binding_models, extra_id: "HUM_056905_WW"},
        )
        self.assertEqual(len(result), len(binding_models) + 1)
        self.assertEqual(
            result[extra_id].values_by_capability,
            result[selected["binding_id"]].values_by_capability,
        )

    def test_composite_capability_evidence_is_exact_binding_model_and_capability(self) -> None:
        contract, binding_models, options = _fleet_payload()
        eligibility = resolve_local_control_binding_eligibility(
            options, contract, binding_models
        )
        binding_id, model_id = next(
            (binding_id, model_id)
            for binding_id, model_id in binding_models.items()
            if model_id == "CST_170004_WW"
        )

        self.assertTrue(
            local_control_capability_authorized(
                contract,
                eligibility,
                binding_id=binding_id,
                model_id=model_id,
                capability_id="climate.mode_fan_setpoint",
            )
        )
        for wrong_binding, wrong_model, wrong_capability in (
            ("missing_binding", model_id, "climate.mode_fan_setpoint"),
            (binding_id, "CST_570004_WW", "climate.mode_fan_setpoint"),
            (binding_id, model_id, "climate.not_real"),
        ):
            with self.subTest(
                binding_id=wrong_binding,
                model_id=wrong_model,
                capability_id=wrong_capability,
            ):
                self.assertFalse(
                    local_control_capability_authorized(
                        contract,
                        eligibility,
                        binding_id=wrong_binding,
                        model_id=wrong_model,
                        capability_id=wrong_capability,
                    )
                )

    def test_exact_current_contract_is_canonical_pinned_and_closed(self) -> None:
        contract = load_local_control_entity_contract()

        self.assertEqual(
            EXPECTED_LOCAL_CONTROL_ENTITY_CONTRACT_ROOT_SHA256,
            "0c424972c46f489e8e353e2743790cf6cd3c4f44666830b37aebc09a80554a70",
        )
        self.assertEqual(
            EXPECTED_LOCAL_CONTROL_CHECKPOINT_REVISION,
            "ha-local-control-authority-checkpoint-v3:98695b8d3012d057",
        )
        self.assertEqual(
            EXPECTED_LOCAL_CONTROL_CHECKPOINT_SHA256,
            "98695b8d3012d05719d776360f31521454bfcdebb22df4ad90405b27dd51df06",
        )
        self.assertEqual(
            EXPECTED_LOCAL_CONTROL_TARGET_AUTHORITY_REVISION,
            "ha-local-control-target-authority-v3:46d0b93596df5a1b",
        )
        self.assertEqual(
            EXPECTED_LOCAL_CONTROL_TARGET_AUTHORITY_SHA256,
            "46d0b93596df5a1b94525f127c9c7eb204b6b1e32b8261f95359be1efef50f4d",
        )
        self.assertEqual(
            contract.root_sha256,
            EXPECTED_LOCAL_CONTROL_ENTITY_CONTRACT_ROOT_SHA256,
        )
        self.assertEqual(
            contract.revision, "ha-local-control-entity-v1:0c424972c46f489e"
        )
        self.assertEqual(len(contract.descriptors), 107)
        self.assertEqual(
            sum(item.factory_eligible for item in contract.descriptors), 78
        )
        self.assertEqual(sum(item.existing_owner for item in contract.descriptors), 29)
        self.assertEqual(contract.stats["supportedValueCount"], 318)
        self.assertEqual(
            (
                contract.stats["naivePhysicalEntityInstanceCount"],
                contract.stats["bindingEligiblePhysicalEntityInstanceCount"],
                contract.stats["bindingBlockedPhysicalEntityInstanceCount"],
                contract.stats["naivePhysicalValueInstanceCount"],
                contract.stats["bindingEligiblePhysicalValueInstanceCount"],
                contract.stats["bindingBlockedPhysicalValueInstanceCount"],
                contract.stats["excludedValueCount"],
            ),
            (139, 139, 0, 395, 395, 0, 76),
        )
        self.assertEqual(sum(contract.model_fleet_counts.values()), 18)
        self.assertTrue(
            all(
                item.exact_state_semantic
                for item in contract.descriptors
                if item.factory_eligible
            )
        )
        self.assertFalse(
            any(
                "|power|" in value
                for item in contract.descriptors
                for value in item.exact_local_request_values
            )
        )
        forbidden_zero_values = {
            "cool|high|21C",
            "cool|high|22C",
            "cool|low|22C",
            "cool|medium|22C",
        }
        self.assertTrue(
            forbidden_zero_values.isdisjoint(
                value
                for item in contract.descriptors
                if item.model_id == "CST_170004_WW"
                and item.capability_id == "climate.mode_fan_setpoint"
                for value in item.exact_local_request_values
            )
        )

    def test_artifact_bytes_and_sidecar_are_both_fail_closed(self) -> None:
        raw = ARTIFACT.read_bytes()
        digest = SIDECAR.read_bytes()
        with self.assertRaises(RuntimeError):
            _load_local_control_entity_contract(raw + b" ", digest)
        with self.assertRaises(RuntimeError):
            _load_local_control_entity_contract(raw, b"0" * 64 + b"\n")

        for name, mutate in (
            (
                "stats",
                lambda value: value["stats"].__setitem__("factoryEntityCount", 79),
            ),
            (
                "revision",
                lambda value: value.__setitem__(
                    "revision", "ha-local-control-entity-v1:" + "0" * 16
                ),
            ),
            ("root", lambda value: value.__setitem__("rootSha256", "0" * 64)),
            (
                "authority",
                lambda value: value.__setitem__(
                    "authority", "exact-codec-and-send-gate"
                ),
            ),
        ):
            decoded = json.loads(raw)
            mutate(decoded)
            mutated = (
                json.dumps(decoded, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode()
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                _load_local_control_entity_contract(mutated, digest)

    def test_missing_or_root_mismatched_private_input_materializes_zero(self) -> None:
        contract = load_local_control_entity_contract()
        binding_models = {_binding_id(1, 1): "AIR_2C0001_WW"}

        automatic = resolve_local_control_binding_eligibility({}, contract, binding_models)
        self.assertEqual(
            set(automatic),
            {binding_id for binding_id, model_id in binding_models.items()
             if contract.descriptors_by_model.get(model_id)},
        )
        for binding_id in automatic:
            self.assertEqual(
                automatic[binding_id].values_by_capability,
                {descriptor.capability_id: descriptor.exact_local_request_values
                 for descriptor in contract.descriptors_by_model[binding_models[binding_id]]},
            )

    def test_automatic_registration_skips_unknown_and_unsupported_models(self) -> None:
        contract = load_local_control_entity_contract()
        result = resolve_local_control_binding_eligibility({}, contract, {
            "known_humidifier_binding": "HUM_056905_WW",
            "unknown_device_binding": "UNKNOWN_MODEL",
            "excluded_thinq1_binding": "2REK1D04AR170",
        })
        self.assertEqual(set(result), {"known_humidifier_binding"})

    def test_target_authority_pin_is_closed_and_globally_fail_closed(self) -> None:
        contract, binding_models, valid = _fleet_payload()
        valid_payload = valid[LOCAL_CONTROL_ELIGIBILITY_OPTION]

        pin_fields = (
            "contract_sha256",
            "checkpoint_revision",
            "checkpoint_sha256",
            "target_authority_revision",
            "target_authority_sha256",
        )
        for field in pin_fields:
            missing = json.loads(json.dumps(valid))
            missing[LOCAL_CONTROL_ELIGIBILITY_OPTION].pop(field)
            with (
                self.subTest(field=field, case="missing"),
                self.assertRaises(LocalControlEligibilityError),
            ):
                resolve_local_control_binding_eligibility(
                    missing, contract, binding_models
                )

        for field, stale_value in (
            ("contract_sha256", "0" * 64),
            (
                "checkpoint_revision",
                "ha-local-control-authority-checkpoint-v3:" + "0" * 16,
            ),
            ("checkpoint_sha256", "0" * 64),
            ("target_authority_revision", "stale-target-authority"),
            ("target_authority_sha256", "0" * 64),
            (
                "target_authority_revision",
                "ha-local-control-target-authority-v2:29d685e16110beec",
            ),
            (
                "target_authority_sha256",
                "29d685e16110beec40c11b0983d53832371866b8850b547ef0920083ddd43ba1",
            ),
        ):
            stale = json.loads(json.dumps(valid))
            stale[LOCAL_CONTROL_ELIGIBILITY_OPTION][field] = stale_value
            eligibility = resolve_local_control_binding_eligibility(
                stale, contract, binding_models
            )
            binding_id, model_id = next(
                (binding_id, model_id)
                for binding_id, model_id in binding_models.items()
                if contract.descriptors_by_model.get(model_id)
            )
            descriptor = contract.descriptors_by_model[model_id][0]
            with self.subTest(field=field, case="stale"):
                self.assertEqual(dict(eligibility), {})
                self.assertEqual(
                    eligible_factory_descriptors(
                        contract,
                        eligibility,
                        binding_id=binding_id,
                        model_id=model_id,
                    ),
                    (),
                )
                self.assertFalse(
                    local_control_value_authorized(
                        contract,
                        eligibility,
                        binding_id=binding_id,
                        model_id=model_id,
                        capability_id=descriptor.capability_id,
                        local_request_value=descriptor.exact_local_request_values[0],
                    )
                )

        old_v2 = json.loads(json.dumps(valid))
        old_v2[LOCAL_CONTROL_ELIGIBILITY_OPTION].update(
            {
                "schema_version": 2,
                "target_authority_revision": (
                    "ha-local-control-target-authority-v2:29d685e16110beec"
                ),
                "target_authority_sha256": (
                    "29d685e16110beec40c11b0983d53832371866b8850b547ef0920083ddd43ba1"
                ),
            }
        )
        with self.assertRaises(LocalControlEligibilityError):
            resolve_local_control_binding_eligibility(old_v2, contract, binding_models)

        unknown = json.loads(json.dumps(valid))
        unknown[LOCAL_CONTROL_ELIGIBILITY_OPTION]["unknown"] = True
        with self.assertRaises(LocalControlEligibilityError):
            resolve_local_control_binding_eligibility(unknown, contract, binding_models)

        self.assertEqual(
            valid_payload["checkpoint_revision"],
            EXPECTED_LOCAL_CONTROL_CHECKPOINT_REVISION,
        )
        self.assertEqual(
            valid_payload["checkpoint_sha256"],
            EXPECTED_LOCAL_CONTROL_CHECKPOINT_SHA256,
        )
        self.assertEqual(
            valid_payload["target_authority_revision"],
            EXPECTED_LOCAL_CONTROL_TARGET_AUTHORITY_REVISION,
        )
        self.assertEqual(
            valid_payload["target_authority_sha256"],
            EXPECTED_LOCAL_CONTROL_TARGET_AUTHORITY_SHA256,
        )
        mismatch = {
            LOCAL_CONTROL_ELIGIBILITY_OPTION: {
                "schema_version": 3,
                "contract_sha256": (
                    "0b2664497d40c86555bcf2f5de8eaee46d926eb169346589906c42e0755cb836"
                ),
                "checkpoint_revision": EXPECTED_LOCAL_CONTROL_CHECKPOINT_REVISION,
                "checkpoint_sha256": EXPECTED_LOCAL_CONTROL_CHECKPOINT_SHA256,
                "target_authority_revision": (
                    EXPECTED_LOCAL_CONTROL_TARGET_AUTHORITY_REVISION
                ),
                "target_authority_sha256": (
                    EXPECTED_LOCAL_CONTROL_TARGET_AUTHORITY_SHA256
                ),
                "bindings": [],
            }
        }
        self.assertEqual(
            dict(
                resolve_local_control_binding_eligibility(
                    mismatch, contract, binding_models
                )
            ),
            {},
        )

    def test_missing_extra_duplicate_and_reordered_bindings_entries_values_fail(
        self,
    ) -> None:
        contract, binding_models, valid = _fleet_payload()
        mutations: list[tuple[str, object]] = []

        def mutate(name, callback):
            candidate = json.loads(json.dumps(valid))
            callback(candidate[LOCAL_CONTROL_ELIGIBILITY_OPTION]["bindings"])
            mutations.append((name, candidate))

        mutate(
            "extra binding",
            lambda rows: rows.append(dict(rows[-1], binding_id=_binding_id(99, 1))),
        )
        mutate(
            "duplicate binding",
            lambda rows: rows.__setitem__(
                1, dict(rows[1], binding_id=rows[0]["binding_id"])
            ),
        )
        mutate(
            "reordered bindings",
            lambda rows: rows.__setitem__(slice(None), reversed(rows)),
        )

        controlled_index = next(
            index
            for index, row in enumerate(
                valid[LOCAL_CONTROL_ELIGIBILITY_OPTION]["bindings"]
            )
            if len(row["entries"]) > 1
            and any(len(entry["exact_values"]) > 1 for entry in row["entries"])
        )

        def entries(rows):
            return rows[controlled_index]["entries"]

        mutate("missing entry", lambda rows: entries(rows).pop())
        mutate(
            "extra entry",
            lambda rows: entries(rows).append(
                {"capability_id": "unknown.capability", "exact_values": ["true"]}
            ),
        )
        mutate(
            "duplicate entry",
            lambda rows: entries(rows).__setitem__(
                1, json.loads(json.dumps(entries(rows)[0]))
            ),
        )
        mutate(
            "reordered entries",
            lambda rows: entries(rows).__setitem__(
                slice(None), reversed(entries(rows))
            ),
        )

        value_index = next(
            index
            for index, entry in enumerate(
                valid[LOCAL_CONTROL_ELIGIBILITY_OPTION]["bindings"][controlled_index][
                    "entries"
                ]
            )
            if len(entry["exact_values"]) > 1
        )

        def values(rows):
            return entries(rows)[value_index]["exact_values"]

        mutate("missing value", lambda rows: values(rows).pop())
        mutate("extra value", lambda rows: values(rows).append("unknown-private-value"))
        mutate(
            "duplicate value", lambda rows: values(rows).__setitem__(1, values(rows)[0])
        )
        mutate(
            "reordered values",
            lambda rows: values(rows).__setitem__(slice(None), reversed(values(rows))),
        )

        for name, candidate in mutations:
            with (
                self.subTest(name=name),
                self.assertRaises(LocalControlEligibilityError),
            ):
                resolve_local_control_binding_eligibility(
                    candidate, contract, binding_models
                )

    def test_role_free_18_binding_fixture_reproduces_exact_physical_totals(
        self,
    ) -> None:
        contract, binding_models, options = _fleet_payload()
        eligibility = resolve_local_control_binding_eligibility(
            options, contract, binding_models
        )
        physical_entities = 0
        physical_values = 0
        full_domain_entities = 0
        full_domain_values = 0
        generic_keys: set[tuple[str, str]] = set()
        existing_keys: set[tuple[str, str]] = set()
        physical_owner_keys: list[tuple[str, str]] = []
        ha_surfaces: list[tuple[str, str, str]] = []

        for binding_id, model_id in binding_models.items():
            binding = eligibility[binding_id]
            descriptors = {
                item.capability_id: item
                for item in contract.descriptors_by_model.get(model_id, ())
            }
            for capability_id, allowed in binding.values_by_capability.items():
                descriptor = descriptors[capability_id]
                physical_entities += 1
                physical_values += len(allowed)
                self.assertEqual(allowed, descriptor.exact_local_request_values)
                full_domain_entities += 1
                full_domain_values += len(allowed)
                owner_key = (binding_id, capability_id)
                physical_owner_keys.append(owner_key)
                ha_surfaces.append(
                    (
                        binding_id,
                        descriptor.entity_domain,
                        descriptor.home_assistant_entity_key,
                    )
                )
                if (
                    descriptor.factory_eligible
                    and allowed == descriptor.exact_local_request_values
                ):
                    generic_keys.add(owner_key)
                elif descriptor.existing_owner:
                    existing_keys.add(owner_key)

        self.assertEqual(len(binding_models), 18)
        self.assertEqual(len(contract.model_fleet_counts), 16)
        self.assertEqual(
            sum(
                not contract.descriptors_by_model.get(model_id)
                for model_id in binding_models.values()
            ),
            4,
        )
        self.assertEqual(physical_entities, 139)
        self.assertEqual(physical_values, 395)
        self.assertEqual((full_domain_entities, full_domain_values), (139, 395))
        self.assertEqual((len(generic_keys), len(existing_keys)), (83, 56))
        self.assertEqual(len(physical_owner_keys), 139)
        self.assertEqual(len(set(physical_owner_keys)), 139)
        self.assertEqual(len(set(ha_surfaces)), 139)
        self.assertEqual(generic_keys | existing_keys, set(physical_owner_keys))
        self.assertTrue(generic_keys.isdisjoint(existing_keys))
        self.assertTrue(
            all(
                descriptor.factory_eligible is not descriptor.existing_owner
                for descriptor in contract.descriptors
            )
        )

    def test_empty_models_and_foreign_capabilities_fail_but_omitted_devices_are_skipped(
        self,
    ) -> None:
        contract, binding_models, options = _fleet_payload()
        payload = options[LOCAL_CONTROL_ELIGIBILITY_OPTION]
        rows = payload["bindings"]
        empty_rows = [row for row in rows if not row["entries"]]
        self.assertEqual(len(empty_rows), 4)
        self.assertEqual(
            {binding_models[row["binding_id"]] for row in empty_rows},
            {"2REK1D04AR170", "D121110", "WBEF3", "WMLJ32RS"},
        )

        missing = {
            LOCAL_CONTROL_ELIGIBILITY_OPTION: {
                **payload,
                "bindings": rows[:-1],
            }
        }
        selected = resolve_local_control_binding_eligibility(missing, contract, binding_models)
        self.assertEqual(set(selected), {row["binding_id"] for row in rows[:-1]})
        self.assertNotIn(rows[-1]["binding_id"], selected)

        unknown_models = dict(binding_models)
        unknown_models[next(iter(unknown_models))] = "UNKNOWN_MODEL"
        with self.assertRaises(LocalControlEligibilityError):
            resolve_local_control_binding_eligibility(options, contract, unknown_models)

        empty_model_row = empty_rows[0]
        illegal_empty_entry = {
            LOCAL_CONTROL_ELIGIBILITY_OPTION: {
                **payload,
                "bindings": [
                    {
                        **row,
                        "entries": (
                            [
                                {
                                    "capability_id": "unknown.capability",
                                    "exact_values": ["true"],
                                }
                            ]
                            if row["binding_id"] == empty_model_row["binding_id"]
                            else row["entries"]
                        ),
                    }
                    for row in rows
                ],
            }
        }
        with self.assertRaises(LocalControlEligibilityError):
            resolve_local_control_binding_eligibility(
                illegal_empty_entry, contract, binding_models
            )

    def test_public_artifact_contains_no_private_identity_or_raw_material(self) -> None:
        artifact = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        forbidden_keys = {
            "device_id",
            "deviceid",
            "binding_id",
            "bindingid",
            "evidence_role",
            "evidencerole",
            "target_role",
            "targetrole",
            "source_locator",
            "sourcelocator",
            "certificate",
            "oauth",
            "oauth_token",
            "token",
            "raw_hex",
            "rawhex",
            "frame_hex",
            "framehex",
        }

        def assert_public_only(value: object) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    self.assertNotIn(key.lower(), forbidden_keys)
                    assert_public_only(child)
            elif isinstance(value, list):
                for child in value:
                    assert_public_only(child)

        assert_public_only(artifact)


if __name__ == "__main__":
    unittest.main()
