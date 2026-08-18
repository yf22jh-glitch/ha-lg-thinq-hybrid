"""Pure contract tests for the read-only Rethink Local shadow provider."""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "my_lg"
    / "local_provider.py"
)
SPEC = importlib.util.spec_from_file_location("my_lg_local_provider_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
local = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = local
SPEC.loader.exec_module(local)


NOW = datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc)
BINDING_ID = "pilot_dhum_provider_001"
BINDING_TWO = "pilot_dhum_provider_002"
SESSION_ONE = "session_dhum_provider_001"
SESSION_TWO = "session_dhum_provider_002"
SERVICE_ONE = "1" * 32
SERVICE_TWO = "2" * 32
PUBLICATION_SESSION_ONE = SERVICE_ONE
PUBLICATION_SESSION_TWO = SERVICE_TWO
PAT_ID_A = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
PAT_ID_B = "11111111-2222-4333-8444-555555555555"
IDENTITY_BINDING = "pilot_identity_binding_0001"
IDENTITY_GENERATION = 4


def state_payload(
    *,
    value: bool = True,
    session_id: str = SESSION_ONE,
    sequence: int = 1,
    binding_id: str = BINDING_ID,
    published_at: str = "2026-08-13T00:59:59.000Z",
) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "semantics_revision": 26,
            "binding_id": binding_id,
            "model_id": "DHUM_056905_WW",
            "platform": "thinq2",
            "session_id": session_id,
            "sequence": sequence,
            "published_at": published_at,
            "fields": {
                "water_tank.full": {
                    "value": value,
                    "value_type": "boolean",
                    "observed_at": "2026-08-13T00:59:58.000Z",
                    "confidence": (
                        "confirmed-exact-device-bidirectional-local-"
                        "interlock-correlation"
                    ),
                    "exposure": "state",
                }
            },
            "diagnostics": {
                "rejected_frames": 0,
                "unresolved_fields": 0,
                "invalid_values": 0,
                "unsupported_frames": 0,
            },
        },
        separators=(",", ":"),
    ).encode()


def availability_payload(
    status: str,
    *,
    session_id: str = SESSION_ONE,
    observed_at: str = "2026-08-13T01:00:00.000Z",
) -> bytes:
    return json.dumps(
        {
            "status": status,
            "session_id": session_id,
            "observed_at": observed_at,
        },
        separators=(",", ":"),
    ).encode()


def runtime_payload(
    status: str,
    *,
    service_instance_id: str = SERVICE_ONE,
    observed_at: str = "2026-08-13T01:00:00.000Z",
) -> bytes:
    return json.dumps(
        {
            "status": status,
            "service_instance_id": service_instance_id,
            "observed_at": observed_at,
        },
        separators=(",", ":"),
    ).encode()


class LocalShadowProviderTests(unittest.TestCase):
    def make_provider(self):
        return local.LocalWaterTankShadowProvider(BINDING_ID, now=lambda: NOW)

    def test_topics_are_exact_and_have_no_wildcards(self) -> None:
        provider = self.make_provider()
        self.assertEqual(
            provider.topics,
            (
                f"lg_rethink_pilot/v1/state/{BINDING_ID}",
                f"lg_rethink_pilot/v1/availability/{BINDING_ID}",
                f"lg_rethink_pilot/v1/runtime/{BINDING_ID}/availability",
            ),
        )
        self.assertNotIn("#", "".join(provider.topics))
        self.assertNotIn("+", "".join(provider.topics))

    def test_accepts_only_the_exact_pinned_dehumidifier_contract(self) -> None:
        provider = self.make_provider()
        provider.ingest(provider.state_topic, state_payload(), qos=1, retained=True)
        self.assertTrue(provider.shadow_value)
        self.assertEqual(provider.session_id, SESSION_ONE)
        self.assertEqual(provider.sequence, 1)

        invalid = json.loads(state_payload())
        invalid["fields"]["water_tank.full"]["value"] = "ON"
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.state_topic,
                json.dumps(invalid).encode(),
                qos=1,
                retained=True,
            )
        self.assertTrue(
            provider.shadow_value, "invalid input must not replace good state"
        )

    def test_rejects_values_outside_javascript_safe_integer_contract(self) -> None:
        provider = self.make_provider()
        invalid_sequence = json.loads(state_payload())
        invalid_sequence["sequence"] = local.MAX_JSON_SAFE_INTEGER + 1
        invalid_diagnostics = json.loads(state_payload())
        invalid_diagnostics["diagnostics"]["rejected_frames"] = (
            local.MAX_JSON_SAFE_INTEGER + 1
        )
        boolean_version = json.loads(state_payload())
        boolean_version["schema_version"] = True

        for payload in (invalid_sequence, invalid_diagnostics, boolean_version):
            with (
                self.subTest(payload=payload),
                self.assertRaises(local.LocalProviderContractError),
            ):
                provider.ingest(
                    provider.state_topic,
                    json.dumps(payload).encode(),
                    qos=1,
                    retained=True,
                )
        self.assertIsNone(provider.shadow_value)

        accepted = json.loads(state_payload())
        accepted["sequence"] = local.MAX_JSON_SAFE_INTEGER
        accepted["diagnostics"]["rejected_frames"] = local.MAX_JSON_SAFE_INTEGER
        provider.ingest(
            provider.state_topic,
            json.dumps(accepted).encode(),
            qos=1,
            retained=True,
        )
        self.assertEqual(provider.sequence, local.MAX_JSON_SAFE_INTEGER)

    def test_rejects_unknown_keys_fields_topics_and_oversized_payloads(self) -> None:
        provider = self.make_provider()
        invalid = json.loads(state_payload())
        invalid["unexpected"] = True
        cases = [
            (provider.state_topic, json.dumps(invalid).encode()),
            (provider.state_topic, b"x" * (local.MAX_PAYLOAD_BYTES + 1)),
            (f"{provider.state_topic}/extra", state_payload()),
        ]
        for topic, payload in cases:
            with (
                self.subTest(topic=topic, length=len(payload)),
                self.assertRaises(local.LocalProviderContractError),
            ):
                provider.ingest(topic, payload, qos=1, retained=True)
        self.assertIsNone(provider.shadow_value)
        self.assertEqual(provider.rejected_messages, 3)

    def test_deep_json_is_normalized_to_a_contract_rejection(self) -> None:
        provider = self.make_provider()
        payload = b'{"x":' + b"[" * 1_000 + b"0" + b"]" * 1_000 + b"}"
        self.assertLess(len(payload), local.MAX_PAYLOAD_BYTES)
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.state_topic,
                payload,
                qos=1,
                retained=True,
            )
        self.assertEqual(provider.rejected_messages, 1)
        self.assertIsNone(provider.shadow_value)

    def test_non_scalar_and_invalid_utf8_json_strings_are_rejected(self) -> None:
        for name, payload in (
            ("escaped lone surrogate", b'{"nested":["\\ud800"]}'),
            ("surrogate UTF-8 bytes", b'{"nested":["\xed\xa0\x80"]}'),
        ):
            with (
                self.subTest(name=name),
                self.assertRaises(local.LocalProviderContractError),
            ):
                local._decode_payload(payload)

    def test_requires_qos_one_but_accepts_live_or_retained_state(self) -> None:
        provider = self.make_provider()
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(provider.state_topic, state_payload(), qos=0, retained=True)
        provider.ingest(provider.state_topic, state_payload(), qos=1, retained=False)
        self.assertTrue(provider.shadow_value)

    def test_state_cursor_is_monotonic_and_exact_replay_is_idempotent(self) -> None:
        provider = self.make_provider()
        payload = state_payload(sequence=2)
        self.assertTrue(
            provider.ingest(provider.state_topic, payload, qos=1, retained=True)
        )
        self.assertFalse(
            provider.ingest(provider.state_topic, payload, qos=1, retained=True)
        )

        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.state_topic,
                state_payload(value=False, sequence=2),
                qos=1,
                retained=True,
            )
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.state_topic,
                state_payload(sequence=1),
                qos=1,
                retained=True,
            )

    def test_session_rotation_requires_matching_offline_and_tombstones_old_session(
        self,
    ) -> None:
        provider = self.make_provider()
        provider.ingest(provider.state_topic, state_payload(), qos=1, retained=True)
        provider.ingest(
            provider.availability_topic,
            availability_payload("online"),
            qos=1,
            retained=True,
        )
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.state_topic,
                state_payload(session_id=SESSION_TWO),
                qos=1,
                retained=True,
            )

        provider.ingest(
            provider.availability_topic,
            availability_payload("offline"),
            qos=1,
            retained=True,
        )
        provider.ingest(
            provider.state_topic,
            state_payload(value=False, session_id=SESSION_TWO),
            qos=1,
            retained=True,
        )
        self.assertFalse(provider.shadow_value)
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.state_topic,
                state_payload(sequence=2),
                qos=1,
                retained=True,
            )

    def test_availability_and_runtime_are_identity_bound_and_fail_closed(self) -> None:
        provider = self.make_provider()
        provider.ingest(provider.state_topic, state_payload(), qos=1, retained=True)
        provider.ingest(
            provider.availability_topic,
            availability_payload("online"),
            qos=1,
            retained=True,
        )
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online"),
            qos=1,
            retained=True,
        )
        provider.set_transport_ready(True)
        self.assertTrue(provider.shadow_healthy)

        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.availability_topic,
                availability_payload("online", session_id=SESSION_TWO),
                qos=1,
                retained=True,
            )
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.runtime_availability_topic,
                runtime_payload("offline", service_instance_id=SERVICE_TWO),
                qos=1,
                retained=True,
            )
        self.assertTrue(provider.shadow_healthy)

        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("offline"),
            qos=1,
            retained=True,
        )
        self.assertFalse(provider.shadow_healthy)

    def test_runtime_lwt_older_than_online_still_fails_closed(self) -> None:
        provider = self.make_provider()
        provider.ingest(provider.state_topic, state_payload(), qos=1, retained=True)
        provider.ingest(
            provider.availability_topic,
            availability_payload("online"),
            qos=1,
            retained=True,
        )
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online", observed_at="2026-08-13T01:00:00.000Z"),
            qos=1,
            retained=True,
        )
        provider.set_transport_ready(True)
        self.assertTrue(provider.shadow_healthy)

        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("offline", observed_at="2026-08-13T00:59:00.000Z"),
            qos=1,
            retained=False,
        )
        self.assertFalse(provider.shadow_healthy)
        self.assertFalse(
            provider.ingest(
                provider.runtime_availability_topic,
                runtime_payload("offline", observed_at="2026-08-13T00:59:00.000Z"),
                qos=1,
                retained=False,
            ),
            "an exact QoS 1 LWT replay must be idempotent",
        )

        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.runtime_availability_topic,
                runtime_payload("online", observed_at="2026-08-13T00:59:30.000Z"),
                qos=1,
                retained=False,
            )
        self.assertFalse(provider.shadow_healthy)

    def test_future_or_noncanonical_timestamps_fail_without_mutation(self) -> None:
        provider = self.make_provider()
        future = state_payload(published_at="2026-08-13T01:05:00.001Z")
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(provider.state_topic, future, qos=1, retained=True)
        noncanonical = availability_payload(
            "online", observed_at="2026-08-13 01:00:00Z"
        )
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.availability_topic,
                noncanonical,
                qos=1,
                retained=True,
            )
        self.assertIsNone(provider.shadow_value)

    def test_retained_final_current_recovers_one_missed_generation_atomically(
        self,
    ) -> None:
        provider = self.make_provider()
        provider.ingest(provider.state_topic, state_payload(), qos=1, retained=True)
        provider.ingest(
            provider.availability_topic,
            availability_payload("online"),
            qos=1,
            retained=True,
        )
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online"),
            qos=1,
            retained=True,
        )
        provider.set_transport_ready(True)
        provider.set_transport_ready(False)

        final_current = {
            provider.state_topic: (
                state_payload(
                    value=False,
                    session_id=SESSION_TWO,
                    sequence=1,
                ),
                1,
                True,
            ),
            provider.availability_topic: (
                availability_payload("online", session_id=SESSION_TWO),
                1,
                True,
            ),
            provider.runtime_availability_topic: (
                runtime_payload("online", service_instance_id=SERVICE_TWO),
                1,
                True,
            ),
        }
        self.assertTrue(provider.ingest_retained_final_current(final_current))
        provider.set_transport_ready(True)
        self.assertEqual(provider.session_id, SESSION_TWO)
        self.assertFalse(provider.shadow_value)
        self.assertTrue(provider.shadow_healthy)

        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.state_topic,
                state_payload(sequence=2),
                qos=1,
                retained=False,
            )

    def test_invalid_final_current_never_partially_rotates_state(self) -> None:
        provider = self.make_provider()
        provider.ingest(provider.state_topic, state_payload(), qos=1, retained=True)
        provider.ingest(
            provider.availability_topic,
            availability_payload("offline"),
            qos=1,
            retained=True,
        )
        invalid = {
            provider.state_topic: (
                state_payload(value=False, session_id=SESSION_TWO),
                1,
                True,
            ),
            provider.availability_topic: (
                availability_payload("online", session_id=SESSION_ONE),
                1,
                True,
            ),
            provider.runtime_availability_topic: (
                runtime_payload("online", service_instance_id=SERVICE_TWO),
                1,
                True,
            ),
        }
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest_retained_final_current(invalid)
        self.assertEqual(provider.session_id, SESSION_ONE)
        self.assertTrue(provider.shadow_value)

    def test_retained_final_current_accepts_older_runtime_lwt_fail_closed(
        self,
    ) -> None:
        provider = self.make_provider()
        provider.ingest(provider.state_topic, state_payload(), qos=1, retained=True)
        provider.ingest(
            provider.availability_topic,
            availability_payload("online"),
            qos=1,
            retained=True,
        )
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online", observed_at="2026-08-13T01:00:00.000Z"),
            qos=1,
            retained=True,
        )
        provider.set_transport_ready(True)
        provider.set_transport_ready(False)

        final_current = {
            provider.state_topic: (state_payload(), 1, True),
            provider.availability_topic: (
                availability_payload("online"),
                1,
                True,
            ),
            provider.runtime_availability_topic: (
                runtime_payload("offline", observed_at="2026-08-13T00:59:00.000Z"),
                1,
                True,
            ),
        }
        self.assertTrue(provider.ingest_retained_final_current(final_current))
        provider.set_transport_ready(True)
        self.assertFalse(provider.shadow_healthy)

        provider.set_transport_ready(False)
        self.assertFalse(
            provider.ingest_retained_final_current(final_current),
            "the same retained crash LWT must remain idempotent after reconnect",
        )
        provider.set_transport_ready(True)
        self.assertFalse(provider.shadow_healthy)

        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.runtime_availability_topic,
                runtime_payload("online", observed_at="2026-08-13T00:59:30.000Z"),
                qos=1,
                retained=False,
            )


class LocalShadowConfigurationTests(unittest.TestCase):
    def test_disabled_is_a_true_noop_even_with_empty_fields(self) -> None:
        self.assertIsNone(
            local.local_shadow_configuration(
                {
                    local.OPT_LOCAL_PROVIDER_MODE: local.LOCAL_PROVIDER_MODE_DISABLED,
                    local.OPT_LOCAL_PAT_DEVICE_ID: "",
                    local.OPT_LOCAL_BINDING_ID: "",
                    local.OPT_LOCAL_MQTT_PASSWORD: "",
                }
            )
        )

    def test_shadow_derives_the_exact_read_only_acl_username(self) -> None:
        config = local.local_shadow_configuration(
            {
                local.OPT_LOCAL_PROVIDER_MODE: local.LOCAL_PROVIDER_MODE_SHADOW,
                local.OPT_LOCAL_PAT_DEVICE_ID: "pat-device-001",
                local.OPT_LOCAL_BINDING_ID: BINDING_ID,
                local.OPT_LOCAL_MQTT_PASSWORD: "private-test-password",
            }
        )
        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.pat_device_id, "pat-device-001")
        self.assertEqual(config.binding_id, BINDING_ID)
        self.assertEqual(config.mqtt_username, f"shadow-{BINDING_ID}")
        self.assertEqual(config.profile_id, "dhum-water-tank-v1")
        self.assertEqual(config.model_id, "DHUM_056905_WW")
        self.assertEqual(config.platform, "thinq2")

    def test_versioned_bindings_accept_json_or_list_and_reject_duplicates(self) -> None:
        bindings = [
            {
                "schema_version": 1,
                "mode": "shadow",
                "profile_id": "dhum-water-tank-v1",
                "model_id": "DHUM_056905_WW",
                "platform": "thinq2",
                "pat_device_id": "pat-device-001",
                "binding_id": BINDING_ID,
                "mqtt_password": "private-test-password-one",
            },
            {
                "schema_version": 1,
                "mode": "shadow",
                "profile_id": "dhum-water-tank-v1",
                "model_id": "DHUM_056905_WW",
                "platform": "thinq2",
                "pat_device_id": "pat-device-002",
                "binding_id": BINDING_TWO,
                "mqtt_password": "private-test-password-two",
            },
        ]
        parsed_list = local.local_shadow_configurations(
            {local.OPT_LOCAL_BINDINGS: bindings}
        )
        parsed_json = local.local_shadow_configurations(
            {local.OPT_LOCAL_BINDINGS: json.dumps(bindings)}
        )
        self.assertEqual(parsed_list, parsed_json)
        self.assertEqual(len(parsed_list), 2)
        self.assertEqual(
            [config.pat_device_id for config in parsed_list],
            ["pat-device-001", "pat-device-002"],
        )

        for duplicate_key in ("pat_device_id", "binding_id"):
            invalid = json.loads(json.dumps(bindings))
            invalid[1][duplicate_key] = invalid[0][duplicate_key]
            with (
                self.subTest(duplicate_key=duplicate_key),
                self.assertRaises(local.LocalProviderConfigurationError),
            ):
                local.local_shadow_configurations({local.OPT_LOCAL_BINDINGS: invalid})

    def test_generic_schema_one_is_transitional_shadow_only(self) -> None:
        binding = {
            "schema_version": 1,
            "mode": "shadow",
            "profile_id": "cst570-core-state-v1",
            "model_id": "CST_570004_WW",
            "platform": "thinq2",
            "pat_device_id": "legacy-pat-anchor-001",
            "binding_id": "pilot_cst570_legacy_001",
            "mqtt_password": "private-test-password",
        }

        config = local.local_shadow_configurations(
            {local.OPT_LOCAL_BINDINGS: [binding]}
        )[0]

        self.assertEqual(config.schema_version, 1)
        self.assertEqual(config.snapshot_schema_version, 1)
        self.assertEqual(config.publication_plan_revision, 1)
        self.assertEqual(config.profile_id, "cst570-core-state-v1")
        self.assertIsNone(config.identity_expectation)

        with self.assertRaises(local.LocalProviderConfigurationError):
            local.local_shadow_configurations(
                {
                    local.OPT_LOCAL_BINDINGS: [
                        {**binding, "mode": "preferred"}
                    ]
                }
            )

    def test_isolated_startup_rejects_bad_and_ambiguous_bindings_only(self) -> None:
        healthy = {
            "schema_version": 1,
            "mode": "shadow",
            "profile_id": "cst570-core-state-v1",
            "model_id": "CST_570004_WW",
            "platform": "thinq2",
            "pat_device_id": "legacy-pat-anchor-001",
            "binding_id": "pilot_cst570_legacy_001",
            "mqtt_password": "private-test-password-one",
        }
        duplicate = {
            **healthy,
            "binding_id": "pilot_cst570_legacy_002",
            "mqtt_password": "private-test-password-two",
        }
        unrelated = {
            **healthy,
            "pat_device_id": "legacy-pat-anchor-002",
            "binding_id": "pilot_cst570_legacy_003",
            "mqtt_password": "private-test-password-three",
        }
        malformed = {**healthy, "schema_version": 99}

        configs, rejected = local.isolated_local_shadow_configurations(
            {
                local.OPT_LOCAL_BINDINGS: [
                    healthy,
                    duplicate,
                    unrelated,
                    malformed,
                ]
            }
        )

        self.assertEqual(
            [(config.pat_device_id, config.binding_id) for config in configs],
            [("legacy-pat-anchor-002", "pilot_cst570_legacy_003")],
        )
        self.assertEqual(rejected, 3)

    def test_fifteen_generic_schema_one_shadows_coexist_with_one_ian_schema_three(
        self,
    ) -> None:
        legacy = [
            {
                "schema_version": 1,
                "mode": "shadow",
                "profile_id": "cst570-core-state-v1",
                "model_id": "CST_570004_WW",
                "platform": "thinq2",
                "pat_device_id": f"legacy-cst570-anchor-{index:02d}",
                "binding_id": f"pilot_cst570_legacy_{index:03d}",
                "mqtt_password": f"private-test-password-{index:02d}",
            }
            for index in range(1, 16)
        ]
        ian = {
            "schema_version": 3,
            "mode": "shadow",
            "profile_id": "cst570-core-state-v1",
            "model_id": "CST_570004_WW",
            "platform": "thinq2",
            "pat_device_id": PAT_ID_A,
            "binding_id": "pilot_cst570_ian_v3_001",
            "binding_generation": 1,
            "mqtt_password": "private-test-password-ian",
        }

        configs = local.local_shadow_configurations(
            {local.OPT_LOCAL_BINDINGS: [*legacy, ian]}
        )

        self.assertEqual(len(configs), 16)
        self.assertTrue(
            all(
                config.schema_version == 1
                and config.identity_expectation is None
                for config in configs[:15]
            )
        )
        self.assertEqual(configs[-1].snapshot_schema_version, 3)
        self.assertIsNotNone(configs[-1].identity_expectation)

    def test_ian_schema_three_and_saved_schema_one_records_both_parse(self) -> None:
        common = {
            "mode": "shadow",
            "profile_id": "cst570-core-state-v1",
            "model_id": "CST_570004_WW",
            "platform": "thinq2",
            "pat_device_id": PAT_ID_A,
            "binding_id": "pilot_cst570_ian_rollback_001",
            "mqtt_password": "private-test-password",
        }
        v3 = {**common, "schema_version": 3, "binding_generation": 1}
        saved_v1 = {**common, "schema_version": 1}

        promoted = local.local_shadow_configurations(
            {local.OPT_LOCAL_BINDINGS: [v3]}
        )[0]
        rolled_back = local.local_shadow_configurations(
            {local.OPT_LOCAL_BINDINGS: [saved_v1]}
        )[0]

        self.assertEqual(promoted.binding_id, rolled_back.binding_id)
        self.assertEqual(promoted.pat_device_id, rolled_back.pat_device_id)
        self.assertEqual(promoted.snapshot_schema_version, 3)
        self.assertEqual(rolled_back.snapshot_schema_version, 1)
        self.assertIsNone(rolled_back.identity_expectation)
        self.assertEqual(rolled_back.publication_plan_revision, 1)

    def test_ian_schema_three_rolls_back_once_with_exact_confirmation(self) -> None:
        common = {
            "mode": "shadow",
            "profile_id": "cst570-core-state-v1",
            "model_id": "CST_570004_WW",
            "platform": "thinq2",
            "pat_device_id": PAT_ID_A,
            "binding_id": "pilot_cst570_ian_rollback_001",
            "mqtt_password": "private-test-password",
        }
        existing = {
            local.OPT_LOCAL_BINDINGS: [
                {**common, "schema_version": 3, "binding_generation": 1}
            ]
        }
        submitted = {
            local.OPT_LOCAL_BINDINGS: [
                {**common, "schema_version": 1, "mqtt_password": ""}
            ],
            local.OPT_LOCAL_SCHEMA_ONE_ROLLBACK_CONFIRMATION: (
                "I CONFIRM V3 SERVICE STOPPED AND RETAINED TOPICS RESET; "
                "ROLLBACK pilot_cst570_ian_rollback_001 FROM SCHEMA 3 TO SCHEMA 1"
            ),
        }

        rolled_back = local.merge_local_shadow_options(submitted, existing)

        self.assertNotIn(
            local.OPT_LOCAL_SCHEMA_ONE_ROLLBACK_CONFIRMATION, rolled_back
        )
        self.assertEqual(
            rolled_back[local.OPT_LOCAL_BINDINGS][0],
            {**common, "schema_version": 1},
        )
        self.assertEqual(
            local.local_shadow_configurations(rolled_back)[0].snapshot_schema_version,
            1,
        )

        with self.assertRaises(local.LocalProviderConfigurationError):
            local.merge_local_shadow_options(
                {
                    **rolled_back,
                    local.OPT_LOCAL_SCHEMA_ONE_ROLLBACK_CONFIRMATION: submitted[
                        local.OPT_LOCAL_SCHEMA_ONE_ROLLBACK_CONFIRMATION
                    ],
                },
                rolled_back,
            )

    def test_schema_three_rollback_rejects_missing_wrong_or_broad_confirmation(
        self,
    ) -> None:
        common = {
            "mode": "shadow",
            "profile_id": "cst570-core-state-v1",
            "model_id": "CST_570004_WW",
            "platform": "thinq2",
            "pat_device_id": PAT_ID_A,
            "binding_id": "pilot_cst570_ian_rollback_001",
            "mqtt_password": "private-test-password",
        }
        existing = {
            local.OPT_LOCAL_BINDINGS: [
                {**common, "schema_version": 3, "binding_generation": 1}
            ]
        }
        rollback = {**common, "schema_version": 1, "mqtt_password": ""}
        for confirmation in (
            "",
            "ROLLBACK pilot_cst570_ian_rollback_001 FROM SCHEMA 3 TO SCHEMA 2",
            "ROLLBACK another_binding_0000001 FROM SCHEMA 3 TO SCHEMA 1",
        ):
            with (
                self.subTest(confirmation=confirmation),
                self.assertRaises(local.LocalProviderConfigurationError),
            ):
                local.merge_local_shadow_options(
                    {
                        local.OPT_LOCAL_BINDINGS: [rollback],
                        local.OPT_LOCAL_SCHEMA_ONE_ROLLBACK_CONFIRMATION: confirmation,
                    },
                    existing,
                )

        changed_anchor = {**rollback, "pat_device_id": PAT_ID_B}
        with self.assertRaises(local.LocalProviderConfigurationError):
            local.merge_local_shadow_options(
                {
                    local.OPT_LOCAL_BINDINGS: [changed_anchor],
                    local.OPT_LOCAL_SCHEMA_ONE_ROLLBACK_CONFIRMATION: (
                        local.local_schema_one_rollback_confirmation(
                            common["binding_id"]
                        )
                    ),
                },
                existing,
            )

    def test_one_confirmation_cannot_rollback_two_schema_three_bindings(self) -> None:
        second = {
            "mode": "shadow",
            "profile_id": "cst570-core-state-v1",
            "model_id": "CST_570004_WW",
            "platform": "thinq2",
            "pat_device_id": PAT_ID_B,
            "binding_id": "pilot_cst570_ian_rollback_002",
            "mqtt_password": "private-test-password-two",
        }
        first = {
            **second,
            "pat_device_id": PAT_ID_A,
            "binding_id": "pilot_cst570_ian_rollback_001",
            "mqtt_password": "private-test-password-one",
        }
        existing = {
            local.OPT_LOCAL_BINDINGS: [
                {**first, "schema_version": 3, "binding_generation": 1},
                {**second, "schema_version": 3, "binding_generation": 1},
            ]
        }
        with self.assertRaises(local.LocalProviderConfigurationError):
            local.merge_local_shadow_options(
                {
                    local.OPT_LOCAL_BINDINGS: [
                        {**first, "schema_version": 1, "mqtt_password": ""},
                        {**second, "schema_version": 1, "mqtt_password": ""},
                    ],
                    local.OPT_LOCAL_SCHEMA_ONE_ROLLBACK_CONFIRMATION: (
                        local.local_schema_one_rollback_confirmation(
                            first["binding_id"]
                        )
                    ),
                },
                existing,
            )

    def test_unrelated_corrupt_stored_binding_cannot_hide_schema_three_rollback(
        self,
    ) -> None:
        common = {
            "mode": "shadow",
            "profile_id": "cst570-core-state-v1",
            "model_id": "CST_570004_WW",
            "platform": "thinq2",
            "pat_device_id": PAT_ID_A,
            "binding_id": "pilot_cst570_ian_rollback_001",
            "mqtt_password": "private-test-password",
        }
        existing = {
            local.OPT_LOCAL_BINDINGS: [
                {**common, "schema_version": 3, "binding_generation": 1},
                {"schema_version": 99},
            ]
        }
        submitted = {
            local.OPT_LOCAL_BINDINGS: [
                {**common, "schema_version": 1, "mqtt_password": "replacement-password"}
            ]
        }

        with self.assertRaises(local.LocalProviderConfigurationError):
            local.merge_local_shadow_options(submitted, existing)

        submitted[local.OPT_LOCAL_SCHEMA_ONE_ROLLBACK_CONFIRMATION] = (
            local.local_schema_one_rollback_confirmation(common["binding_id"])
        )
        rolled_back = local.merge_local_shadow_options(submitted, existing)
        self.assertEqual(
            rolled_back[local.OPT_LOCAL_BINDINGS][0]["schema_version"], 1
        )
        self.assertEqual(
            rolled_back[local.OPT_LOCAL_BINDINGS][0]["mqtt_password"],
            "replacement-password",
        )

    def test_versioned_binding_pins_profile_model_platform_and_exact_keys(self) -> None:
        base = {
            "schema_version": 1,
            "mode": "shadow",
            "profile_id": "dhum-water-tank-v1",
            "model_id": "DHUM_056905_WW",
            "platform": "thinq2",
            "pat_device_id": "pat-device-001",
            "binding_id": BINDING_ID,
            "mqtt_password": "private-test-password",
        }
        cases = []
        for key, value in (
            ("schema_version", 2),
            ("mode", "preferred"),
            ("profile_id", "unknown-profile"),
            ("model_id", "OTHER_MODEL"),
            ("platform", "thinq1"),
        ):
            invalid = dict(base)
            invalid[key] = value
            cases.append(invalid)
        unexpected = dict(base)
        unexpected["unexpected"] = True
        cases.append(unexpected)

        for binding in cases:
            with (
                self.subTest(binding=binding),
                self.assertRaises(local.LocalProviderConfigurationError),
            ):
                local.local_shadow_configurations({local.OPT_LOCAL_BINDINGS: [binding]})

    def test_identity_bound_pat_uuid_duplicates_are_case_insensitive(self) -> None:
        base = {
            "schema_version": 2,
            "mode": "shadow",
            "profile_id": "air-tower-core-state-v1",
            "model_id": "AIR_2C0001_WW",
            "platform": "thinq2",
            "pat_device_id": PAT_ID_A,
            "binding_id": IDENTITY_BINDING,
            "binding_generation": IDENTITY_GENERATION,
            "mqtt_password": "private-test-password-one",
        }
        duplicate = {
            **base,
            "pat_device_id": PAT_ID_A.upper(),
            "binding_id": "pilot_identity_binding_0002",
            "mqtt_password": "private-test-password-two",
        }
        with self.assertRaises(local.LocalProviderConfigurationError):
            local.local_shadow_configurations(
                {local.OPT_LOCAL_BINDINGS: [base, duplicate]}
            )

    def test_identity_bound_pat_uuid_is_stored_canonically(self) -> None:
        binding = {
            "schema_version": 3,
            "mode": "shadow",
            "profile_id": "air-tower-core-state-v1",
            "model_id": "AIR_2C0001_WW",
            "platform": "thinq2",
            "pat_device_id": PAT_ID_A.upper(),
            "binding_id": IDENTITY_BINDING,
            "binding_generation": IDENTITY_GENERATION,
            "mqtt_password": "private-test-password",
        }

        config = local.local_shadow_configurations(
            {local.OPT_LOCAL_BINDINGS: [binding]}
        )[0]

        self.assertEqual(config.pat_device_id, PAT_ID_A)
        self.assertEqual(
            local.migrate_local_shadow_options(
                {local.OPT_LOCAL_BINDINGS: [binding]}
            )[local.OPT_LOCAL_BINDINGS][0]["pat_device_id"],
            PAT_ID_A,
        )

    def test_deep_binding_json_is_normalized_to_configuration_error(self) -> None:
        nested = "[" * 1_000 + "0" + "]" * 1_000
        with self.assertRaises(local.LocalProviderConfigurationError):
            local.local_shadow_configurations(
                {local.OPT_LOCAL_BINDINGS: nested}
            )

    def test_preferred_and_incomplete_shadow_modes_fail_closed(self) -> None:
        cases = [
            {local.OPT_LOCAL_PROVIDER_MODE: "preferred"},
            {
                local.OPT_LOCAL_PROVIDER_MODE: local.LOCAL_PROVIDER_MODE_SHADOW,
                local.OPT_LOCAL_PAT_DEVICE_ID: "pat-device-001",
                local.OPT_LOCAL_BINDING_ID: "too_short",
                local.OPT_LOCAL_MQTT_PASSWORD: "secret",
            },
            {
                local.OPT_LOCAL_PROVIDER_MODE: local.LOCAL_PROVIDER_MODE_SHADOW,
                local.OPT_LOCAL_PAT_DEVICE_ID: "pat-device-001",
                local.OPT_LOCAL_BINDING_ID: BINDING_ID,
                local.OPT_LOCAL_MQTT_PASSWORD: "",
            },
            {
                local.OPT_LOCAL_PROVIDER_MODE: local.LOCAL_PROVIDER_MODE_SHADOW,
                local.OPT_LOCAL_PAT_DEVICE_ID: "pat-device-001",
                local.OPT_LOCAL_BINDING_ID: BINDING_ID,
                local.OPT_LOCAL_MQTT_PASSWORD: "\ud800",
            },
        ]
        for options in cases:
            with (
                self.subTest(options=options),
                self.assertRaises(local.LocalProviderConfigurationError),
            ):
                local.local_shadow_configuration(options)

    def test_options_keep_an_existing_secret_without_redisplaying_it(self) -> None:
        submitted = {
            local.OPT_LOCAL_PROVIDER_MODE: local.LOCAL_PROVIDER_MODE_SHADOW,
            local.OPT_LOCAL_PAT_DEVICE_ID: "pat-device-001",
            local.OPT_LOCAL_BINDING_ID: BINDING_ID,
            local.OPT_LOCAL_MQTT_PASSWORD: "",
            "unrelated_option": 300,
        }
        merged = local.merge_local_shadow_options(
            submitted,
            {local.OPT_LOCAL_MQTT_PASSWORD: "existing-private-test-password"},
        )
        self.assertNotIn(local.OPT_LOCAL_MQTT_PASSWORD, merged)
        self.assertEqual(
            merged[local.OPT_LOCAL_BINDINGS][0]["mqtt_password"],
            "existing-private-test-password",
        )
        self.assertEqual(merged[local.OPT_LOCAL_BINDINGS][0]["schema_version"], 1)
        self.assertEqual(merged["unrelated_option"], 300)

    def test_disabling_local_removes_binding_and_secret_options(self) -> None:
        merged = local.merge_local_shadow_options(
            {
                local.OPT_LOCAL_PROVIDER_MODE: local.LOCAL_PROVIDER_MODE_DISABLED,
                local.OPT_LOCAL_PAT_DEVICE_ID: "pat-device-001",
                local.OPT_LOCAL_BINDING_ID: BINDING_ID,
                local.OPT_LOCAL_MQTT_PASSWORD: "submitted-secret",
                "unrelated_option": 300,
            },
            {local.OPT_LOCAL_MQTT_PASSWORD: "existing-secret"},
        )
        self.assertEqual(
            merged,
            {
                local.OPT_LOCAL_BINDINGS: [],
                "unrelated_option": 300,
            },
        )

    def test_new_binding_form_masks_secrets_and_merges_them_by_binding(self) -> None:
        existing = local.migrate_local_shadow_options(
            {
                local.OPT_LOCAL_PROVIDER_MODE: local.LOCAL_PROVIDER_MODE_SHADOW,
                local.OPT_LOCAL_PAT_DEVICE_ID: "pat-device-001",
                local.OPT_LOCAL_BINDING_ID: BINDING_ID,
                local.OPT_LOCAL_MQTT_PASSWORD: "existing-private-test-password",
            }
        )
        rendered = local.local_bindings_for_form(existing)
        self.assertNotIn("existing-private-test-password", rendered)
        submitted = json.loads(rendered)
        self.assertEqual(submitted[0]["mqtt_password"], "")

        merged = local.merge_local_shadow_options(
            {local.OPT_LOCAL_BINDINGS: rendered, "unrelated_option": 300},
            existing,
        )
        self.assertEqual(
            merged[local.OPT_LOCAL_BINDINGS][0]["mqtt_password"],
            "existing-private-test-password",
        )
        self.assertEqual(merged["unrelated_option"], 300)

    def test_invalid_existing_bindings_can_be_repaired_without_reusing_secrets(
        self,
    ) -> None:
        repaired = {
            "schema_version": 2,
            "mode": "shadow",
            "profile_id": "styler-core-state-v2",
            "model_id": "ST_R_ETH01Y_",
            "platform": "thinq2",
            "pat_device_id": PAT_ID_A,
            "binding_id": "pilot_styler_provider_001",
            "binding_generation": 1,
            "mqtt_password": "new-private-test-password",
        }

        merged = local.merge_local_shadow_options(
            {local.OPT_LOCAL_BINDINGS: [repaired]},
            {local.OPT_LOCAL_BINDINGS: "not-json"},
        )

        self.assertEqual(merged[local.OPT_LOCAL_BINDINGS], [repaired])

    def test_identity_bound_binding_requires_uuid_generation_and_new_binding_on_repair(
        self,
    ) -> None:
        base = {
            "schema_version": 2,
            "mode": "shadow",
            "profile_id": "air-tower-core-state-v1",
            "model_id": "AIR_2C0001_WW",
            "platform": "thinq2",
            "pat_device_id": PAT_ID_A,
            "binding_id": IDENTITY_BINDING,
            "binding_generation": IDENTITY_GENERATION,
            "mqtt_password": "private-test-password",
        }
        config = local.local_shadow_configurations(
            {local.OPT_LOCAL_BINDINGS: [base]}
        )[0]
        self.assertEqual(config.schema_version, 2)
        self.assertIsNotNone(config.identity_expectation)
        assert config.identity_expectation is not None
        self.assertEqual(
            config.identity_expectation.binding_generation,
            IDENTITY_GENERATION,
        )

        for changes in (
            {"pat_device_id": "not-a-uuid"},
            {"binding_generation": 0},
            {"binding_generation": True},
        ):
            invalid = {**base, **changes}
            with (
                self.subTest(changes=changes),
                self.assertRaises(local.LocalProviderConfigurationError),
            ):
                local.local_shadow_configurations(
                    {local.OPT_LOCAL_BINDINGS: [invalid]}
                )

        for leaking_binding in (
            "pilot_identity_bbbb_0001",
            "pilot_aaaabbbb4ccc8dddeeeeeeeeeeee",
        ):
            invalid = {**base, "binding_id": leaking_binding}
            with (
                self.subTest(leaking_binding=leaking_binding),
                self.assertRaises(local.LocalProviderConfigurationError),
            ):
                local.local_shadow_configurations(
                    {local.OPT_LOCAL_BINDINGS: [invalid]}
                )

        for changes in (
            {"pat_device_id": PAT_ID_B},
            {"binding_generation": IDENTITY_GENERATION + 1},
        ):
            submitted = {**base, **changes, "mqtt_password": ""}
            with (
                self.subTest(changes=changes),
                self.assertRaises(local.LocalProviderConfigurationError),
            ):
                local.merge_local_shadow_options(
                    {local.OPT_LOCAL_BINDINGS: [submitted]},
                    {local.OPT_LOCAL_BINDINGS: [base]},
                )

    def test_schema_three_strictly_opts_into_snapshot_three_and_plan_two(self) -> None:
        base = {
            "schema_version": 3,
            "mode": "shadow",
            "profile_id": "air-tower-core-state-v1",
            "model_id": "AIR_2C0001_WW",
            "platform": "thinq2",
            "pat_device_id": PAT_ID_A,
            "binding_id": IDENTITY_BINDING,
            "binding_generation": IDENTITY_GENERATION,
            "mqtt_password": "private-test-password",
        }
        config = local.local_shadow_configurations(
            {local.OPT_LOCAL_BINDINGS: [base]}
        )[0]
        self.assertEqual(config.schema_version, 3)
        self.assertEqual(config.snapshot_schema_version, 3)
        self.assertEqual(config.publication_plan_revision, 2)
        self.assertIsNotNone(config.identity_expectation)

        v2 = {**base, "schema_version": 2}
        v2_config = local.local_shadow_configurations(
            {local.OPT_LOCAL_BINDINGS: [v2]}
        )[0]
        self.assertEqual(v2_config.snapshot_schema_version, 2)
        self.assertEqual(v2_config.publication_plan_revision, 1)

        for invalid in (
            {key: value for key, value in base.items() if key != "binding_generation"},
            {**base, "publication_plan_revision": 2},
            {**base, "schema_version": 4},
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(local.LocalProviderConfigurationError),
            ):
                local.local_shadow_configurations(
                    {local.OPT_LOCAL_BINDINGS: [invalid]}
                )

        with self.assertRaises(local.LocalProviderConfigurationError):
            local.merge_local_shadow_options(
                {local.OPT_LOCAL_BINDINGS: [{**base, "mqtt_password": ""}]},
                {local.OPT_LOCAL_BINDINGS: [v2]},
            )


class IdentityBoundLocalProviderTests(unittest.TestCase):
    def profile(self) -> local.LocalSemanticProfile:
        return local.LocalSemanticProfile(
            profile_id="synthetic-identity-v1",
            model_id="AIR_910604_WW",
            platform="thinq2",
            semantics_revision=30,
            fields={
                "feature.enabled": local.LocalSemanticFieldContract(
                    value_type="boolean",
                    exposure="state",
                    confidence=("confirmed-synthetic",),
                )
            },
        )

    def expectation(
        self, *, pat_device_id: str = PAT_ID_A, generation: int = IDENTITY_GENERATION
    ) -> local.LocalPilotIdentityExpectation:
        profile = self.profile()
        return local.LocalPilotIdentityExpectation(
            binding_id=IDENTITY_BINDING,
            binding_generation=generation,
            model_id=profile.model_id,
            platform=profile.platform,
            pat_device_id_proof_sha256=local.create_local_pat_device_identity_proof(
                binding_id=IDENTITY_BINDING,
                model_id=profile.model_id,
                platform=profile.platform,
                pat_device_id=pat_device_id,
            ),
        )

    def provider(self) -> local.LocalSemanticShadowProvider:
        return local.LocalSemanticShadowProvider(
            IDENTITY_BINDING,
            self.profile(),
            identity_expectation=self.expectation(),
            now=lambda: NOW,
        )

    def state(
        self,
        *,
        proof: str | None = None,
        generation: int = IDENTITY_GENERATION,
        value: bool = True,
        sequence: int = 1,
        session_id: str = SESSION_ONE,
        published_at: str = "2026-08-13T00:59:59.000Z",
    ) -> bytes:
        return json.dumps(
            {
                "schema_version": 2,
                "semantics_revision": 30,
                "binding_id": IDENTITY_BINDING,
                "binding_generation": generation,
                "pat_device_id_proof_sha256": (
                    proof or self.expectation().pat_device_id_proof_sha256
                ),
                "model_id": self.profile().model_id,
                "platform": self.profile().platform,
                "session_id": session_id,
                "sequence": sequence,
                "published_at": published_at,
                "fields": {
                    "feature.enabled": {
                        "value": value,
                        "value_type": "boolean",
                        "observed_at": "2026-08-13T00:59:58.000Z",
                        "confidence": "confirmed-synthetic",
                        "exposure": "state",
                    }
                },
                "diagnostics": {
                    "rejected_frames": 0,
                    "unresolved_fields": 0,
                    "invalid_values": 0,
                    "unsupported_frames": 0,
                },
            },
            separators=(",", ":"),
        ).encode()

    def availability(
        self,
        *,
        proof: str | None = None,
        generation: int = IDENTITY_GENERATION,
        state_sequence: int = 1,
        session_id: str = SESSION_ONE,
        status: str = "online",
        observed_at: str = "2026-08-13T01:00:00.000Z",
    ) -> bytes:
        return json.dumps(
            {
                "status": status,
                "session_id": session_id,
                "observed_at": observed_at,
                "binding_generation": generation,
                "pat_device_id_proof_sha256": (
                    proof or self.expectation().pat_device_id_proof_sha256
                ),
                "state_sequence": state_sequence,
            },
            separators=(",", ":"),
        ).encode()

    def identity(
        self,
        *,
        proof: str | None = None,
        generation: int = IDENTITY_GENERATION,
    ) -> bytes:
        return json.dumps(
            {
                "schema_version": 1,
                "binding_id": IDENTITY_BINDING,
                "binding_generation": generation,
                "model_id": self.profile().model_id,
                "platform": self.profile().platform,
                "pat_device_id_proof_sha256": (
                    proof or self.expectation().pat_device_id_proof_sha256
                ),
            },
            separators=(",", ":"),
        ).encode()

    def final_current(
        self,
        provider: local.LocalSemanticShadowProvider,
        *,
        proof: str | None = None,
        generation: int = IDENTITY_GENERATION,
        value: bool = True,
        sequence: int = 1,
        availability_state_sequence: int | None = None,
    ) -> dict[str, tuple[bytes, int, bool]]:
        return {
            provider.state_topic: (
                self.state(
                    proof=proof,
                    generation=generation,
                    value=value,
                    sequence=sequence,
                ),
                1,
                True,
            ),
            provider.availability_topic: (
                self.availability(
                    proof=proof,
                    generation=generation,
                    state_sequence=(
                        sequence
                        if availability_state_sequence is None
                        else availability_state_sequence
                    ),
                ),
                1,
                True,
            ),
            provider.runtime_availability_topic: (
                runtime_payload("online"),
                1,
                True,
            ),
            provider.identity_topic: (
                self.identity(proof=proof, generation=generation),
                1,
                True,
            ),
        }

    def test_proof_matches_the_rethink_cross_language_fixture(self) -> None:
        self.assertEqual(
            local.create_local_pat_device_identity_proof(
                binding_id="pilot_identity_binding_0001",
                model_id="AIR_910604_WW",
                platform="thinq2",
                pat_device_id="AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE",
            ),
            "13a5c957b4d79ba3c8a4305ccc18bf258e2245e033b3e303b0c8c80a9298488e",
        )

    def test_identity_bound_final_current_requires_all_four_exact_topics(self) -> None:
        provider = self.provider()
        self.assertEqual(
            provider.topics,
            (
                f"lg_rethink_pilot/v1/state/{IDENTITY_BINDING}",
                f"lg_rethink_pilot/v1/availability/{IDENTITY_BINDING}",
                f"lg_rethink_pilot/v1/runtime/{IDENTITY_BINDING}/availability",
                f"lg_rethink_pilot/v1/identity/{IDENTITY_BINDING}",
            ),
        )
        incomplete = self.final_current(provider)
        incomplete.pop(provider.identity_topic)
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest_retained_final_current(incomplete)
        self.assertIsNone(provider.field_value("feature.enabled"))

        self.assertTrue(
            provider.ingest_retained_final_current(self.final_current(provider))
        )
        provider.set_transport_ready(True)
        self.assertTrue(provider.shadow_healthy)
        self.assertIs(provider.field_value("feature.enabled"), True)

    def test_v2_availability_requires_one_positive_safe_state_sequence(self) -> None:
        provider = self.provider()
        base = json.loads(self.availability())
        cases: list[tuple[str, dict[str, object]]] = []

        missing = dict(base)
        missing.pop("state_sequence")
        cases.append(("missing", missing))

        extra = {**base, "unexpected": 1}
        cases.append(("extra", extra))

        for invalid_value in (0, True, local.MAX_JSON_SAFE_INTEGER + 1):
            cases.append(
                (
                    f"invalid-{type(invalid_value).__name__}",
                    {**base, "state_sequence": invalid_value},
                )
            )

        for name, value in cases:
            with (
                self.subTest(name=name),
                self.assertRaises(local.LocalProviderContractError),
            ):
                provider.ingest(
                    provider.availability_topic,
                    json.dumps(value, separators=(",", ":")).encode(),
                    qos=1,
                    retained=True,
                )
        self.assertEqual(provider.rejected_messages, len(cases))
        self.assertFalse(provider.shadow_healthy)

    def test_newer_state_fails_closed_until_matching_availability_arrives(
        self,
    ) -> None:
        provider = self.provider()
        provider.ingest_retained_final_current(self.final_current(provider))
        provider.set_transport_ready(True)
        self.assertTrue(provider.shadow_healthy)

        self.assertTrue(
            provider.ingest(
                provider.state_topic,
                self.state(value=False, sequence=2),
                qos=1,
                retained=False,
            )
        )
        self.assertEqual(provider.sequence, 2)
        self.assertIs(provider.field_value("feature.enabled"), False)
        self.assertFalse(provider.shadow_healthy)

        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.availability_topic,
                self.availability(state_sequence=1),
                qos=1,
                retained=False,
            )
        self.assertFalse(provider.shadow_healthy)

        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.availability_topic,
                self.availability(
                    state_sequence=2,
                    observed_at="2026-08-13T00:59:59.500Z",
                ),
                qos=1,
                retained=False,
            )
        self.assertFalse(provider.shadow_healthy)

        self.assertTrue(
            provider.ingest(
                provider.availability_topic,
                self.availability(
                    state_sequence=2,
                    observed_at="2026-08-13T01:00:01.000Z",
                ),
                qos=1,
                retained=False,
            )
        )
        self.assertTrue(provider.shadow_healthy)
        self.assertFalse(
            provider.ingest(
                provider.availability_topic,
                self.availability(
                    state_sequence=2,
                    observed_at="2026-08-13T01:00:01.000Z",
                ),
                qos=1,
                retained=False,
            )
        )

    def test_final_current_sequence_mismatch_is_atomic(self) -> None:
        provider = self.provider()
        provider.ingest_retained_final_current(self.final_current(provider))
        provider.set_transport_ready(True)
        self.assertTrue(provider.shadow_healthy)
        provider.set_transport_ready(False)

        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest_retained_final_current(
                self.final_current(
                    provider,
                    value=False,
                    sequence=2,
                    availability_state_sequence=1,
                )
            )
        self.assertEqual(provider.sequence, 1)
        self.assertIs(provider.field_value("feature.enabled"), True)
        provider.set_transport_ready(True)
        self.assertTrue(provider.shadow_healthy)

        provider.set_transport_ready(False)
        self.assertTrue(
            provider.ingest_retained_final_current(
                self.final_current(provider, value=False, sequence=2)
            )
        )
        provider.set_transport_ready(True)
        self.assertEqual(provider.sequence, 2)
        self.assertIs(provider.field_value("feature.enabled"), False)
        self.assertTrue(provider.shadow_healthy)

    def test_same_model_other_pat_and_old_generation_fail_atomically(self) -> None:
        other_pat_proof = self.expectation(
            pat_device_id=PAT_ID_B
        ).pat_device_id_proof_sha256
        for proof, generation in (
            (other_pat_proof, IDENTITY_GENERATION),
            (self.expectation().pat_device_id_proof_sha256, IDENTITY_GENERATION - 1),
        ):
            provider = self.provider()
            with (
                self.subTest(proof=proof, generation=generation),
                self.assertRaises(local.LocalProviderContractError),
            ):
                provider.ingest_retained_final_current(
                    self.final_current(
                        provider,
                        proof=proof,
                        generation=generation,
                    )
                )
            provider.set_transport_ready(True)
            self.assertFalse(provider.shadow_healthy)
            self.assertIsNone(provider.field_value("feature.enabled"))

    def test_non_ascii_proofs_are_contract_rejections_on_every_wire_shape(self) -> None:
        bad_proof = "é" * 64
        provider = self.provider()
        for topic, payload in (
            (provider.state_topic, self.state(proof=bad_proof)),
            (
                provider.availability_topic,
                self.availability(proof=bad_proof),
            ),
            (provider.identity_topic, self.identity(proof=bad_proof)),
        ):
            with (
                self.subTest(topic=topic),
                self.assertRaises(local.LocalProviderContractError),
            ):
                provider.ingest(topic, payload, qos=1, retained=True)
        self.assertEqual(provider.rejected_messages, 3)
        self.assertFalse(provider.shadow_healthy)

    def test_identity_tombstone_clears_the_whole_cohort_and_old_state_cannot_revive(
        self,
    ) -> None:
        provider = self.provider()
        provider.ingest_retained_final_current(self.final_current(provider))
        provider.set_transport_ready(True)
        self.assertTrue(provider.shadow_healthy)

        self.assertTrue(
            provider.ingest(
                provider.identity_topic,
                b"",
                qos=1,
                retained=False,
            )
        )
        self.assertFalse(provider.shadow_healthy)
        self.assertIsNone(provider.field_value("feature.enabled"))
        self.assertIsNone(provider.session_id)

        # A new runtime process alone must never revive the deleted identity,
        # state, or device-availability cohort.
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online", service_instance_id=SERVICE_TWO),
            qos=1,
            retained=False,
        )
        self.assertFalse(provider.shadow_healthy)
        self.assertIsNone(provider.field_value("feature.enabled"))

        # Replaying the tombstoned session is fenced even if its proof/model
        # were once valid.
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.state_topic,
                self.state(),
                qos=1,
                retained=False,
            )
        self.assertFalse(provider.shadow_healthy)


class CohortLocalProviderTests(IdentityBoundLocalProviderTests):
    """Identity-bound v3 ordering uses one O(1) cohort high-water."""

    def provider(self) -> local.LocalSemanticShadowProvider:
        return local.LocalSemanticShadowProvider(
            IDENTITY_BINDING,
            self.profile(),
            identity_expectation=self.expectation(),
            snapshot_schema_version=3,
            now=lambda: NOW,
        )

    def state(
        self,
        *,
        cohort_generation: int = 1,
        **kwargs,
    ) -> bytes:
        kwargs.setdefault("session_id", PUBLICATION_SESSION_ONE)
        value = json.loads(super().state(**kwargs))
        value["schema_version"] = 3
        value["cohort_generation"] = cohort_generation
        return json.dumps(value, separators=(",", ":")).encode()

    def availability(
        self,
        *,
        cohort_generation: int = 1,
        **kwargs,
    ) -> bytes:
        kwargs.setdefault("session_id", PUBLICATION_SESSION_ONE)
        value = json.loads(super().availability(**kwargs))
        value["schema_version"] = 3
        value["cohort_generation"] = cohort_generation
        return json.dumps(value, separators=(",", ":")).encode()

    def final_current(
        self,
        provider: local.LocalSemanticShadowProvider,
        *,
        cohort_generation: int = 1,
        session_id: str = PUBLICATION_SESSION_ONE,
        runtime_service_instance_id: str | None = None,
        **kwargs,
    ) -> dict[str, tuple[bytes, int, bool]]:
        publications = super().final_current(provider, **kwargs)
        state = json.loads(publications[provider.state_topic][0])
        state["schema_version"] = 3
        state["cohort_generation"] = cohort_generation
        state["session_id"] = session_id
        availability = json.loads(publications[provider.availability_topic][0])
        availability["schema_version"] = 3
        availability["cohort_generation"] = cohort_generation
        availability["session_id"] = session_id
        publications[provider.state_topic] = (
            json.dumps(state, separators=(",", ":")).encode(),
            1,
            True,
        )
        publications[provider.availability_topic] = (
            json.dumps(availability, separators=(",", ":")).encode(),
            1,
            True,
        )
        publications[provider.runtime_availability_topic] = (
            runtime_payload(
                "online",
                service_instance_id=(
                    session_id
                    if runtime_service_instance_id is None
                    else runtime_service_instance_id
                ),
            ),
            1,
            True,
        )
        return publications

    def test_v3_publication_session_is_exact_lowercase_hex_and_v2_is_unchanged(
        self,
    ) -> None:
        invalid_sessions = (
            SESSION_ONE,
            "A" * 32,
            "a" * 31,
            "g" * 32,
        )
        for session_id in invalid_sessions:
            for topic_kind, payload in (
                ("state", self.state(session_id=session_id)),
                (
                    "availability",
                    self.availability(session_id=session_id),
                ),
            ):
                provider = self.provider()
                topic = (
                    provider.state_topic
                    if topic_kind == "state"
                    else provider.availability_topic
                )
                with (
                    self.subTest(kind=topic_kind, session_id=session_id),
                    self.assertRaisesRegex(
                        local.LocalProviderContractError,
                        (
                            "availability session id is invalid"
                            if topic_kind == "availability"
                            else "session id is invalid"
                        ),
                    ),
                ):
                    provider.ingest(topic, payload, qos=1, retained=True)

        legacy = super().provider()
        legacy.ingest(
            legacy.state_topic,
            super().state(session_id=SESSION_ONE),
            qos=1,
            retained=True,
        )
        self.assertEqual(legacy.session_id, SESSION_ONE)

    def test_v3_final_current_requires_publication_and_runtime_session_match(
        self,
    ) -> None:
        provider = self.provider()
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest_retained_final_current(
                self.final_current(
                    provider,
                    runtime_service_instance_id=PUBLICATION_SESSION_TWO,
                )
            )

        self.assertIsNone(provider.session_id)
        self.assertIsNone(provider.field_value("feature.enabled"))
        self.assertFalse(provider.shadow_healthy)

    def test_v3_incremental_runtime_mismatch_stages_fail_closed_until_repaired(
        self,
    ) -> None:
        provider = self.provider()
        provider.ingest_retained_final_current(self.final_current(provider))
        provider.set_transport_ready(True)
        self.assertTrue(provider.shadow_healthy)

        provider.ingest(
            provider.state_topic,
            self.state(
                cohort_generation=2,
                session_id=PUBLICATION_SESSION_TWO,
                value=False,
            ),
            qos=1,
            retained=False,
        )
        provider.ingest(
            provider.availability_topic,
            self.availability(
                cohort_generation=2,
                session_id=PUBLICATION_SESSION_TWO,
            ),
            qos=1,
            retained=False,
        )
        self.assertFalse(provider.shadow_healthy)

        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("offline", service_instance_id=PUBLICATION_SESSION_ONE),
            qos=1,
            retained=False,
        )
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online", service_instance_id=PUBLICATION_SESSION_TWO),
            qos=1,
            retained=False,
        )
        self.assertTrue(provider.shadow_healthy)

        runtime_first = self.provider()
        runtime_first.ingest(
            runtime_first.identity_topic,
            self.identity(),
            qos=1,
            retained=False,
        )
        runtime_first.ingest(
            runtime_first.runtime_availability_topic,
            runtime_payload(
                "online",
                service_instance_id=PUBLICATION_SESSION_TWO,
            ),
            qos=1,
            retained=False,
        )
        runtime_first.ingest(
            runtime_first.state_topic,
            self.state(session_id=PUBLICATION_SESSION_ONE),
            qos=1,
            retained=False,
        )
        runtime_first.ingest(
            runtime_first.availability_topic,
            self.availability(session_id=PUBLICATION_SESSION_ONE),
            qos=1,
            retained=False,
        )
        runtime_first.set_transport_ready(True)
        self.assertFalse(runtime_first.shadow_healthy)

        runtime_first.ingest(
            runtime_first.runtime_availability_topic,
            runtime_payload(
                "offline",
                service_instance_id=PUBLICATION_SESSION_TWO,
            ),
            qos=1,
            retained=False,
        )
        runtime_first.ingest(
            runtime_first.runtime_availability_topic,
            runtime_payload(
                "online",
                service_instance_id=PUBLICATION_SESSION_ONE,
            ),
            qos=1,
            retained=False,
        )
        self.assertTrue(runtime_first.shadow_healthy)

    def test_v3_availability_cannot_predate_state_publication(self) -> None:
        provider = self.provider()
        publications = self.final_current(provider)
        availability = json.loads(publications[provider.availability_topic][0])
        availability["observed_at"] = "2026-08-13T00:59:58.999Z"
        publications[provider.availability_topic] = (
            json.dumps(availability, separators=(",", ":")).encode(),
            1,
            True,
        )
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest_retained_final_current(publications)
        self.assertIsNone(provider.session_id)

        provider.ingest(
            provider.state_topic,
            self.state(published_at="2026-08-13T00:59:59.500Z"),
            qos=1,
            retained=False,
        )
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.availability_topic,
                self.availability(
                    observed_at="2026-08-13T00:59:59.499Z",
                ),
                qos=1,
                retained=False,
            )
        self.assertFalse(provider.shadow_healthy)

    def test_v3_is_strict_and_v2_remains_v2_only(self) -> None:
        provider = self.provider()
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.state_topic,
                super().state(),
                qos=1,
                retained=True,
            )

        for payload in (
            {
                key: value
                for key, value in json.loads(self.state()).items()
                if key != "cohort_generation"
            },
            {**json.loads(self.state()), "unexpected": True},
            {
                key: value
                for key, value in json.loads(self.availability()).items()
                if key != "schema_version"
            },
            {
                key: value
                for key, value in json.loads(self.availability()).items()
                if key != "cohort_generation"
            },
            {**json.loads(self.availability()), "schema_version": 2},
        ):
            topic = (
                provider.state_topic
                if "fields" in payload
                else provider.availability_topic
            )
            with (
                self.subTest(payload=payload),
                self.assertRaises(local.LocalProviderContractError),
            ):
                provider.ingest(
                    topic,
                    json.dumps(payload, separators=(",", ":")).encode(),
                    qos=1,
                    retained=True,
                )

        v2 = super().provider()
        for topic, payload in (
            (v2.state_topic, self.state()),
            (v2.availability_topic, self.availability()),
        ):
            with (
                self.subTest(v2_topic=topic),
                self.assertRaises(local.LocalProviderContractError),
            ):
                v2.ingest(topic, payload, qos=1, retained=True)

    def test_cohort_generation_has_the_exact_ledger_bound(self) -> None:
        for cohort_generation in (
            0,
            True,
            local.MAX_COHORT_GENERATION + 1,
        ):
            for kind, payload in (
                ("state", self.state(cohort_generation=cohort_generation)),
                (
                    "availability",
                    self.availability(cohort_generation=cohort_generation),
                ),
            ):
                provider = self.provider()
                topic = (
                    provider.state_topic
                    if kind == "state"
                    else provider.availability_topic
                )
                with (
                    self.subTest(kind=kind, cohort=cohort_generation),
                    self.assertRaises(local.LocalProviderContractError),
                ):
                    provider.ingest(topic, payload, qos=1, retained=True)

        provider = self.provider()
        provider.ingest(
            provider.state_topic,
            self.state(cohort_generation=local.MAX_COHORT_GENERATION),
            qos=1,
            retained=True,
        )
        self.assertEqual(provider.cohort_generation, local.MAX_COHORT_GENERATION)

    def test_higher_cohort_supersedes_without_offline_and_fails_closed(self) -> None:
        provider = self.provider()
        provider.ingest_retained_final_current(self.final_current(provider))
        provider.set_transport_ready(True)
        self.assertTrue(provider.shadow_healthy)

        self.assertTrue(
            provider.ingest(
                provider.state_topic,
                self.state(
                    cohort_generation=2,
                    session_id=PUBLICATION_SESSION_ONE,
                    value=False,
                ),
                qos=1,
                retained=False,
            )
        )
        self.assertEqual(provider.cohort_generation, 2)
        self.assertEqual(provider.session_id, PUBLICATION_SESSION_ONE)
        self.assertFalse(provider.shadow_healthy)
        self.assertIs(provider.field_value("feature.enabled"), False)

        for cohort, session, sequence in (
            (1, PUBLICATION_SESSION_ONE, 1),
            (2, PUBLICATION_SESSION_TWO, 1),
            (2, PUBLICATION_SESSION_ONE, 0),
        ):
            with (
                self.subTest(cohort=cohort, session=session, sequence=sequence),
                self.assertRaises(local.LocalProviderContractError),
            ):
                provider.ingest(
                    provider.state_topic,
                    self.state(
                        cohort_generation=cohort,
                        session_id=session,
                        sequence=sequence,
                    ),
                    qos=1,
                    retained=False,
                )

        for cohort, session, sequence in (
            (1, PUBLICATION_SESSION_ONE, 1),
            (2, PUBLICATION_SESSION_TWO, 1),
            (2, PUBLICATION_SESSION_ONE, 2),
        ):
            with (
                self.subTest(cohort=cohort, session=session, sequence=sequence),
                self.assertRaises(local.LocalProviderContractError),
            ):
                provider.ingest(
                    provider.availability_topic,
                    self.availability(
                        cohort_generation=cohort,
                        session_id=session,
                        state_sequence=sequence,
                    ),
                    qos=1,
                    retained=False,
                )

        self.assertTrue(
            provider.ingest(
                provider.availability_topic,
                self.availability(
                    cohort_generation=2,
                    session_id=PUBLICATION_SESSION_ONE,
                    state_sequence=1,
                ),
                qos=1,
                retained=False,
            )
        )
        self.assertTrue(provider.shadow_healthy)

    def test_retained_tombstone_preserves_high_water_and_blocks_revive(self) -> None:
        provider = self.provider()
        provider.ingest_retained_final_current(
            self.final_current(provider, cohort_generation=7)
        )
        provider.set_transport_ready(True)
        self.assertTrue(provider.shadow_healthy)

        provider.ingest(
            provider.state_topic,
            b"",
            qos=1,
            retained=False,
        )
        self.assertFalse(provider.shadow_healthy)
        self.assertIsNone(provider.cohort_generation)

        for cohort in (6, 7):
            with (
                self.subTest(cohort=cohort),
                self.assertRaises(local.LocalProviderContractError),
            ):
                provider.ingest(
                    provider.state_topic,
                    self.state(cohort_generation=cohort),
                    qos=1,
                    retained=False,
                )

        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest_retained_final_current(
                self.final_current(provider, cohort_generation=7)
            )

        self.assertTrue(
            provider.ingest(
                provider.state_topic,
                self.state(
                    cohort_generation=8,
                    session_id=PUBLICATION_SESSION_TWO,
                ),
                qos=1,
                retained=False,
            )
        )

    def test_ten_thousand_and_one_cohorts_use_constant_memory(self) -> None:
        provider = self.provider()
        for cohort in range(1, 10_002):
            session = (
                PUBLICATION_SESSION_ONE
                if cohort % 2
                else PUBLICATION_SESSION_TWO
            )
            provider.ingest(
                provider.state_topic,
                self.state(
                    cohort_generation=cohort,
                    session_id=session,
                    value=bool(cohort % 2),
                ),
                qos=1,
                retained=False,
            )
        self.assertEqual(provider.cohort_generation, 10_001)
        self.assertEqual(provider._cohort_generation_high_water, 10_001)
        self.assertEqual(len(provider._tombstoned_sessions), 0)

    def test_disconnected_final_current_uses_the_same_cohort_high_water(self) -> None:
        provider = self.provider()
        provider.ingest_retained_final_current(
            self.final_current(provider, cohort_generation=2)
        )
        provider.set_transport_ready(True)
        self.assertTrue(provider.shadow_healthy)
        provider.set_transport_ready(False)

        self.assertTrue(
            provider.ingest_retained_final_current(
                self.final_current(
                    provider,
                    cohort_generation=4,
                    session_id=PUBLICATION_SESSION_TWO,
                    value=False,
                )
            )
        )
        self.assertEqual(provider.cohort_generation, 4)
        self.assertEqual(provider.session_id, PUBLICATION_SESSION_TWO)
        self.assertIs(provider.field_value("feature.enabled"), False)

        for cohort, session in (
            (3, PUBLICATION_SESSION_ONE),
            (4, PUBLICATION_SESSION_ONE),
        ):
            with (
                self.subTest(cohort=cohort, session=session),
                self.assertRaises(local.LocalProviderContractError),
            ):
                provider.ingest_retained_final_current(
                    self.final_current(
                        provider,
                        cohort_generation=cohort,
                        session_id=session,
                    )
                )
        self.assertEqual(provider.cohort_generation, 4)
        self.assertIs(provider.field_value("feature.enabled"), False)


class GenericLocalSemanticProviderTests(unittest.TestCase):
    def profile(self):
        return local.LocalSemanticProfile(
            profile_id="synthetic-two-field-v1",
            model_id="SYNTHETIC_MODEL",
            platform="thinq2",
            semantics_revision=26,
            fields={
                "temperature.current_c": local.LocalSemanticFieldContract(
                    value_type="number",
                    exposure="state",
                    confidence=("confirmed-synthetic",),
                    unit="°C",
                ),
                "door.open": local.LocalSemanticFieldContract(
                    value_type="boolean",
                    exposure="state",
                    confidence=("confirmed-synthetic",),
                ),
                "mode.current": local.LocalSemanticFieldContract(
                    value_type="string",
                    exposure="state",
                    confidence=("confirmed-synthetic",),
                    allowed_values=("normal", "turbo"),
                ),
            },
        )

    def payload(self, **overrides) -> bytes:
        value = {
            "schema_version": 1,
            "semantics_revision": 26,
            "binding_id": BINDING_ID,
            "model_id": "SYNTHETIC_MODEL",
            "platform": "thinq2",
            "session_id": SESSION_ONE,
            "sequence": 1,
            "published_at": "2026-08-13T00:59:59.000Z",
            "fields": {
                "temperature.current_c": {
                    "value": 23.5,
                    "value_type": "number",
                    "unit": "°C",
                    "observed_at": "2026-08-13T00:59:58.000Z",
                    "confidence": "confirmed-synthetic",
                    "exposure": "state",
                },
                "door.open": {
                    "value": False,
                    "value_type": "boolean",
                    "observed_at": "2026-08-13T00:59:58.000Z",
                    "confidence": "confirmed-synthetic",
                    "exposure": "state",
                },
            },
            "diagnostics": {
                "rejected_frames": 0,
                "unresolved_fields": 0,
                "invalid_values": 0,
                "unsupported_frames": 0,
            },
        }
        value.update(overrides)
        return json.dumps(value, separators=(",", ":")).encode()

    def test_stores_typed_allowlisted_fields_for_one_exact_profile(self) -> None:
        provider = local.LocalSemanticShadowProvider(
            BINDING_ID, self.profile(), now=lambda: NOW
        )
        provider.ingest(provider.state_topic, self.payload(), qos=1, retained=True)
        self.assertEqual(provider.profile_id, "synthetic-two-field-v1")
        self.assertEqual(provider.model_id, "SYNTHETIC_MODEL")
        self.assertEqual(provider.platform, "thinq2")
        self.assertEqual(provider.field_value("temperature.current_c"), 23.5)
        self.assertIs(provider.field_value("door.open"), False)
        self.assertIsNone(provider.field_value("unknown.field"))
        self.assertEqual(
            set(provider.shadow_fields),
            {"temperature.current_c", "door.open"},
        )

    def test_rejects_unknown_or_contract_mismatched_fields_atomically(self) -> None:
        provider = local.LocalSemanticShadowProvider(
            BINDING_ID, self.profile(), now=lambda: NOW
        )
        accepted = self.payload()
        provider.ingest(provider.state_topic, accepted, qos=1, retained=True)

        cases = []
        unknown = json.loads(accepted)
        unknown["sequence"] = 2
        unknown["fields"]["unknown.field"] = unknown["fields"]["door.open"]
        cases.append(unknown)
        wrong_model = json.loads(accepted)
        wrong_model["sequence"] = 2
        wrong_model["model_id"] = "OTHER_MODEL"
        cases.append(wrong_model)
        wrong_unit = json.loads(accepted)
        wrong_unit["sequence"] = 2
        wrong_unit["fields"]["temperature.current_c"]["unit"] = "°F"
        cases.append(wrong_unit)
        wrong_type = json.loads(accepted)
        wrong_type["sequence"] = 2
        wrong_type["fields"]["door.open"]["value"] = 1
        cases.append(wrong_type)

        for payload in cases:
            with (
                self.subTest(payload=payload),
                self.assertRaises(local.LocalProviderContractError),
            ):
                provider.ingest(
                    provider.state_topic,
                    json.dumps(payload).encode(),
                    qos=1,
                    retained=True,
                )
        self.assertEqual(provider.sequence, 1)
        self.assertEqual(provider.field_value("temperature.current_c"), 23.5)

    def test_huge_json_integer_is_a_bounded_contract_rejection(self) -> None:
        provider = local.LocalSemanticShadowProvider(
            BINDING_ID, self.profile(), now=lambda: NOW
        )
        invalid = json.loads(self.payload())
        invalid["fields"]["temperature.current_c"]["value"] = 10**400
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.state_topic,
                json.dumps(invalid).encode(),
                qos=1,
                retained=True,
            )
        self.assertEqual(provider.rejected_messages, 1)
        self.assertIsNone(provider.field_value("temperature.current_c"))

    def test_string_enum_rejects_values_outside_the_catalogue_domain(self) -> None:
        provider = local.LocalSemanticShadowProvider(
            BINDING_ID, self.profile(), now=lambda: NOW
        )
        accepted = json.loads(self.payload())
        accepted["fields"] = {
            "mode.current": {
                "value": "normal",
                "value_type": "string",
                "observed_at": "2026-08-13T00:59:58.000Z",
                "confidence": "confirmed-synthetic",
                "exposure": "state",
            }
        }
        provider.ingest(
            provider.state_topic,
            json.dumps(accepted).encode(),
            qos=1,
            retained=True,
        )
        self.assertEqual(provider.field_value("mode.current"), "normal")

        invalid = json.loads(json.dumps(accepted))
        invalid["sequence"] = 2
        invalid["fields"]["mode.current"]["value"] = "future_mode"
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.state_topic,
                json.dumps(invalid).encode(),
                qos=1,
                retained=True,
            )
        self.assertEqual(provider.sequence, 1)
        self.assertEqual(provider.field_value("mode.current"), "normal")

    def test_unrestricted_semantic_strings_require_unicode_scalars(self) -> None:
        profile = local.LocalSemanticProfile(
            profile_id="synthetic-string-v1",
            model_id="SYNTHETIC_MODEL",
            platform="thinq2",
            semantics_revision=26,
            fields={
                "mode.label": local.LocalSemanticFieldContract(
                    value_type="string",
                    exposure="state",
                    confidence=("confirmed-synthetic",),
                )
            },
        )

        def payload(value: str) -> bytes:
            snapshot = json.loads(self.payload())
            snapshot["fields"] = {
                "mode.label": {
                    "value": value,
                    "value_type": "string",
                    "observed_at": "2026-08-13T00:59:58.000Z",
                    "confidence": "confirmed-synthetic",
                    "exposure": "state",
                }
            }
            return json.dumps(snapshot, separators=(",", ":")).encode()

        valid_provider = local.LocalSemanticShadowProvider(
            BINDING_ID, profile, now=lambda: NOW
        )
        valid_provider.ingest(
            valid_provider.state_topic,
            payload("쾌적🌿"),
            qos=1,
            retained=True,
        )
        self.assertEqual(valid_provider.field_value("mode.label"), "쾌적🌿")

        for name, invalid_value in (
            ("high surrogate", "\ud800"),
            ("low surrogate", "\udfff"),
        ):
            provider = local.LocalSemanticShadowProvider(
                BINDING_ID, profile, now=lambda: NOW
            )
            with (
                self.subTest(name=name),
                self.assertRaises(local.LocalProviderContractError),
            ):
                provider.ingest(
                    provider.state_topic,
                    payload(invalid_value),
                    qos=1,
                    retained=True,
                )
            self.assertIsNone(provider.field_value("mode.label"))

    def test_profile_contract_strings_require_unicode_scalars(self) -> None:
        surrogate = "\ud800"
        for name, overrides in (
            ("confidence", {"confidence": (surrogate,)}),
            ("unit", {"unit": surrogate}),
            ("allowed_values", {"allowed_values": (surrogate,)}),
        ):
            values = {
                "value_type": "string",
                "exposure": "state",
                "confidence": ("confirmed-synthetic",),
                **overrides,
            }
            with self.subTest(name=name), self.assertRaises(ValueError):
                local.LocalSemanticFieldContract(**values)

    def test_authoritative_invalidation_clears_a_prior_value_and_is_healthy(
        self,
    ) -> None:
        profile = local.LocalSemanticProfile(
            profile_id="synthetic-authoritative-v1",
            model_id="SYNTHETIC_MODEL",
            platform="thinq2",
            semantics_revision=26,
            fields={
                "mode.current": local.LocalSemanticFieldContract(
                    value_type="string",
                    exposure="state",
                    confidence=("confirmed-synthetic",),
                    allowed_values=("normal", "turbo"),
                )
            },
            authoritative_invalidations=True,
        )
        provider = local.LocalSemanticShadowProvider(
            BINDING_ID, profile, now=lambda: NOW
        )
        state = json.loads(self.payload())
        state["fields"] = {
            "mode.current": {
                "value": "normal",
                "value_type": "string",
                "observed_at": "2026-08-13T00:59:58.000Z",
                "confidence": "confirmed-synthetic",
                "exposure": "state",
            }
        }
        provider.ingest(
            provider.state_topic,
            json.dumps(state).encode(),
            qos=1,
            retained=True,
        )
        provider.ingest(
            provider.availability_topic,
            availability_payload("online"),
            qos=1,
            retained=True,
        )
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online"),
            qos=1,
            retained=True,
        )
        provider.set_transport_ready(True)
        self.assertTrue(provider.shadow_healthy)
        self.assertEqual(provider.field_value("mode.current"), "normal")

        invalidated = json.loads(json.dumps(state))
        invalidated["sequence"] = 2
        invalidated["fields"] = {}
        invalidated["invalidated_fields"] = {
            "mode.current": {
                "observed_at": "2026-08-13T00:59:58.500Z",
                "confidence": "confirmed-synthetic",
            }
        }
        provider.ingest(
            provider.state_topic,
            json.dumps(invalidated).encode(),
            qos=1,
            retained=True,
        )
        self.assertTrue(provider.shadow_healthy)
        self.assertIsNone(provider.field_value("mode.current"))

        partial_profile = local.LocalSemanticProfile(
            profile_id="synthetic-partial-v1",
            model_id="SYNTHETIC_MODEL",
            platform="thinq2",
            semantics_revision=26,
            fields=profile.fields,
        )
        partial_provider = local.LocalSemanticShadowProvider(
            BINDING_TWO, partial_profile, now=lambda: NOW
        )
        forged = json.loads(json.dumps(invalidated))
        forged["binding_id"] = BINDING_TWO
        with self.assertRaises(local.LocalProviderContractError):
            partial_provider.ingest(
                partial_provider.state_topic,
                json.dumps(forged).encode(),
                qos=1,
                retained=True,
            )

    def test_exact_profiles_accept_only_their_published_semantics_revisions(
        self,
    ) -> None:
        profiles = local.load_local_semantic_profile_catalogue()[1]
        cases = (
            (
                "dhum-core-state-v2",
                "pilot_dhum_display_provider_001",
                "display.enabled",
                "confirmed-exact-device-6-writes+6-acks+6-immediate-readbacks",
                (30,),
                (29,),
            ),
            (
                "air-tower-core-state-v1",
                "pilot_air_tower_provider_001",
                "energy_saving.ai_enabled",
                "confirmed-exact-device-7-writes+7-immediate-readbacks",
                (27, 28, 29, 30),
                (26,),
            ),
            (
                "styler-core-state-v2",
                "pilot_styler_provider_001",
                "display.current_time_enabled",
                "confirmed-exact-modeljson+4-on-off-local-cloud-cycles",
                (28, 29, 30),
                (27,),
            ),
        )
        for (
            profile_id,
            binding_id,
            semantic_id,
            confidence,
            accepted_revisions,
            rejected_revisions,
        ) in cases:
            profile = profiles[profile_id]

            def payload(revision: int) -> bytes:
                return json.dumps(
                    {
                        "schema_version": 1,
                        "semantics_revision": revision,
                        "binding_id": binding_id,
                        "model_id": profile.model_id,
                        "platform": profile.platform,
                        "session_id": SESSION_ONE,
                        "sequence": 1,
                        "published_at": "2026-08-13T00:59:59.000Z",
                        "fields": {
                            semantic_id: {
                                "value": True,
                                "value_type": "boolean",
                                "observed_at": "2026-08-13T00:59:58.000Z",
                                "confidence": confidence,
                                "exposure": "state",
                            }
                        },
                        "diagnostics": {
                            "rejected_frames": 0,
                            "unresolved_fields": 0,
                            "invalid_values": 0,
                            "unsupported_frames": 0,
                        },
                    },
                    separators=(",", ":"),
                ).encode()

            for revision in accepted_revisions:
                with self.subTest(profile=profile_id, revision=revision):
                    provider = local.LocalSemanticShadowProvider(
                        binding_id, profile, now=lambda: NOW
                    )
                    provider.ingest(
                        provider.state_topic,
                        payload(revision),
                        qos=1,
                        retained=True,
                    )
                    self.assertIs(provider.field_value(semantic_id), True)

            for revision in rejected_revisions:
                with (
                    self.subTest(profile=profile_id, revision=revision),
                    self.assertRaises(local.LocalProviderContractError),
                ):
                    provider = local.LocalSemanticShadowProvider(
                        binding_id, profile, now=lambda: NOW
                    )
                    provider.ingest(
                        provider.state_topic,
                        payload(revision),
                        qos=1,
                        retained=True,
                    )

    def test_provider_listeners_observe_only_accepted_state_and_health_changes(
        self,
    ) -> None:
        provider = local.LocalSemanticShadowProvider(
            BINDING_ID, self.profile(), now=lambda: NOW
        )
        notifications: list[tuple[bool, object]] = []
        remove = provider.async_add_listener(
            lambda: notifications.append(
                (
                    provider.shadow_healthy,
                    provider.field_value("door.open"),
                )
            )
        )

        provider.ingest(provider.state_topic, self.payload(), qos=1, retained=True)
        provider.ingest(
            provider.availability_topic,
            availability_payload("online"),
            qos=1,
            retained=True,
        )
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online"),
            qos=1,
            retained=True,
        )
        provider.set_transport_ready(True)
        self.assertEqual(len(notifications), 4)
        self.assertEqual(notifications[-1], (True, False))

        provider.ingest(provider.state_topic, self.payload(), qos=1, retained=True)
        provider.set_transport_ready(True)
        self.assertEqual(len(notifications), 4, "exact replays must not notify")

        invalid = json.loads(self.payload())
        invalid["sequence"] = 2
        invalid["fields"]["door.open"]["value"] = 1
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.state_topic,
                json.dumps(invalid).encode(),
                qos=1,
                retained=True,
            )
        self.assertEqual(len(notifications), 4)

        remove()
        provider.set_transport_ready(False)
        self.assertEqual(len(notifications), 4)


class WaterTankResolverTests(unittest.TestCase):
    def test_shadow_mode_never_changes_operational_wideq_result(self) -> None:
        provider = local.LocalWaterTankShadowProvider(BINDING_ID, now=lambda: NOW)
        provider.ingest(
            provider.state_topic, state_payload(value=True), qos=1, retained=True
        )
        provider.ingest(
            provider.availability_topic,
            availability_payload("online"),
            qos=1,
            retained=True,
        )
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online"),
            qos=1,
            retained=True,
        )
        resolver = local.WaterTankProviderResolver(provider)

        self.assertFalse(resolver.resolve({local.WIDEQ_WATER_TANK_KEY: 0}))
        self.assertTrue(resolver.available(True))
        self.assertTrue(provider.shadow_value)
        self.assertEqual(resolver.mode, local.LOCAL_PROVIDER_MODE_SHADOW)

    def test_wideq_parser_never_guesses_unknown_values(self) -> None:
        resolver = local.WaterTankProviderResolver()
        accepted = (
            (0, False),
            (0.0, False),
            ("0", False),
            ("0.0", False),
            (1, True),
            (1.0, True),
            ("1", True),
            ("1.0", True),
        )
        for value, expected in accepted:
            with self.subTest(value=value):
                self.assertIs(
                    resolver.resolve({local.WIDEQ_WATER_TANK_KEY: value}),
                    expected,
                )
        with self.assertLogs(local._LOGGER.name, level="WARNING") as logs:
            for value in ("ON", "OFF", 2, -1, True, False, object()):
                with self.subTest(value=value):
                    self.assertIsNone(
                        resolver.resolve({local.WIDEQ_WATER_TANK_KEY: value})
                    )
            self.assertIsNone(resolver.resolve([]))
        self.assertEqual(resolver.invalid_wideq_values, 8)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("count=1", logs.output[0])
        self.assertIsNone(
            resolver.resolve({}), "missing data is absence, not an invalid value"
        )
        self.assertEqual(resolver.invalid_wideq_values, 8)


if __name__ == "__main__":
    unittest.main()
