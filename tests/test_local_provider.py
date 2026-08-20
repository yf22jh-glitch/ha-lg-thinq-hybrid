"""Pure contract tests for the read-only Rethink Local shadow provider."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import re
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
# Read from the bundled catalogue rather than pinned here: a publisher revision
# bump should fail on a real contract mismatch, not on a stale test constant.
CATALOGUE_REVISION = local.load_local_semantic_profile_catalogue()[0]
BINDING_ID = "pilot_dhum_provider_001"
BINDING_TWO = "pilot_dhum_provider_002"
SESSION_ONE = "session_dhum_provider_001"
SESSION_TWO = "session_dhum_provider_002"
SERVICE_ONE = "1" * 32
SERVICE_TWO = "2" * 32


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
            "semantics_revision": CATALOGUE_REVISION,
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
                f"{local.LOCAL_PILOT_PREFIX}/state/{BINDING_ID}",
                f"{local.LOCAL_PILOT_PREFIX}/availability/{BINDING_ID}",
                f"{local.LOCAL_PILOT_PREFIX}/runtime/{BINDING_ID}/availability",
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

    def test_schema_one_availability_cannot_predate_the_state_it_names(self) -> None:
        # Causality predates identity coordinates: keeping the legacy schema compatible does not
        # mean accepting an availability observation from before its state was published.
        provider = self.make_provider()
        provider.ingest(provider.state_topic, state_payload(), qos=1, retained=True)
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.availability_topic,
                availability_payload(
                    "online", observed_at="2026-08-13T00:59:58.999Z"
                ),
                qos=1,
                retained=True,
            )
        self.assertEqual(provider._device_status, "unknown")

    def test_schema_one_state_advance_waits_for_a_causally_new_online_refresh(self) -> None:
        provider = self.make_provider()
        provider.ingest(provider.state_topic, state_payload(), qos=1, retained=False)
        provider.ingest(
            provider.availability_topic,
            availability_payload("online"),
            qos=1,
            retained=False,
        )
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online"),
            qos=1,
            retained=False,
        )
        provider.set_transport_ready(True)
        self.assertTrue(provider.shadow_healthy)

        # Schema 1 has no state_sequence in availability. The old online marker therefore has to
        # be fenced by causality when a newer same-session state arrives; None == None is not proof
        # that it attested sequence 2.
        provider.ingest(
            provider.state_topic,
            state_payload(
                sequence=2, published_at="2026-08-13T01:00:00.500Z"
            ),
            qos=1,
            retained=False,
        )
        self.assertFalse(provider.shadow_healthy)

        self.assertTrue(
            provider.ingest(
                provider.availability_topic,
                availability_payload(
                    "online", observed_at="2026-08-13T01:00:01.000Z"
                ),
                qos=1,
                retained=False,
            )
        )
        self.assertTrue(provider.shadow_healthy)

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

    def test_identity_requirement_survives_every_options_round_trip(self) -> None:
        binding = {
            "schema_version": 1,
            "mode": "shadow",
            "profile_id": "dhum-water-tank-v1",
            "model_id": "DHUM_056905_WW",
            "platform": "thinq2",
            "pat_device_id": "pat-device-identity-round-trip",
            "binding_id": "pilot_identity_round_trip_001",
            "mqtt_password": "existing-private-test-password",
            "require_identity": True,
        }
        options = {local.OPT_LOCAL_BINDINGS: [binding]}
        self.assertTrue(local.local_shadow_configurations(options)[0].require_identity)

        migrated = local.migrate_local_shadow_options(options)
        self.assertIs(
            migrated[local.OPT_LOCAL_BINDINGS][0]["require_identity"], True
        )

        rendered = local.local_bindings_for_form(migrated)
        submitted = json.loads(rendered)
        self.assertIs(submitted[0]["require_identity"], True)
        self.assertEqual(submitted[0]["mqtt_password"], "")

        merged = local.merge_local_shadow_options(
            {local.OPT_LOCAL_BINDINGS: rendered}, migrated
        )
        self.assertIs(merged[local.OPT_LOCAL_BINDINGS][0]["require_identity"], True)
        self.assertTrue(
            local.local_shadow_configurations(merged)[0].require_identity
        )

    def test_invalid_existing_bindings_can_be_repaired_without_reusing_secrets(
        self,
    ) -> None:
        repaired = {
            "schema_version": 1,
            "mode": "shadow",
            "profile_id": "styler-core-state-v1",
            "model_id": "ST_R_ETH01Y_",
            "platform": "thinq2",
            "pat_device_id": "pat-styler-001",
            "binding_id": "pilot_styler_provider_001",
            "mqtt_password": "new-private-test-password",
        }

        merged = local.merge_local_shadow_options(
            {local.OPT_LOCAL_BINDINGS: [repaired]},
            {local.OPT_LOCAL_BINDINGS: "not-json"},
        )

        self.assertEqual(merged[local.OPT_LOCAL_BINDINGS], [repaired])


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


class LocalPrefixMirrorTests(unittest.TestCase):
    """The publisher lives in a sibling repository and owns the namespace.

    The prefix is one value held in two processes. If they disagree the
    publisher still publishes and the subscriber still subscribes - to
    different topics - so nothing errors and the appliance simply goes quiet.
    """

    SIBLING = (
        Path(__file__).resolve().parents[2]
        / "lg_rethink_local"
        / "local"
        / "semantic"
        / "pilot-topics.ts"
    )

    def test_prefix_matches_the_publisher(self) -> None:
        if not self.SIBLING.exists():
            self.skipTest("sibling lg_rethink_local checkout is not present")
        source = self.SIBLING.read_text(encoding="utf8")
        match = re.search(r"LOCAL_PILOT_TOPIC_PREFIX\s*=\s*'([^']+)'", source)
        self.assertIsNotNone(match, "could not read the publisher's topic prefix")
        assert match is not None
        self.assertEqual(local.LOCAL_PILOT_PREFIX, match.group(1))


class IdentityBoundPublicationTests(unittest.TestCase):
    """Schema 2 and 3 publications name the appliance and prove it.

    A binding that only checks the topic trusts whoever publishes there. The
    proof is the difference between "arrived on our topic" and "came from this
    exact appliance", so it is verified against the PAT identity the binding was
    configured with, and a binding that can verify must never accept a
    publication that carries no proof at all.
    """

    PAT_DEVICE_ID = "aaaaaaaabbbbbbbbccccccccddddddddeeeeeeeeffffffff0000000011111111"

    def profile(self, **overrides):
        base = dict(
            profile_id="synthetic-identity-v1",
            model_id="SYNTHETIC_MODEL",
            platform="thinq2",
            semantics_revision=31,
            fields={
                "door.open": local.LocalSemanticFieldContract(
                    value_type="boolean",
                    exposure="state",
                    confidence=("confirmed-synthetic",),
                ),
            },
        )
        base.update(overrides)
        return local.LocalSemanticProfile(**base)

    def proof(self, pat_device_id: str | None = None) -> str:
        return local.local_pat_device_identity_proof(
            BINDING_ID,
            "SYNTHETIC_MODEL",
            "thinq2",
            pat_device_id or self.PAT_DEVICE_ID,
        )

    def provider(self, *, pat_device_id: str | None = None, profile=None, require_identity=False):
        return local.LocalSemanticShadowProvider(
            BINDING_ID,
            profile or self.profile(),
            pat_device_id=pat_device_id,
            require_identity=require_identity,
            now=lambda: NOW,
        )

    def payload(self, schema_version: int = 2, **overrides) -> bytes:
        value = {
            "schema_version": schema_version,
            "semantics_revision": 31,
            "binding_id": BINDING_ID,
            "model_id": "SYNTHETIC_MODEL",
            "platform": "thinq2",
            "session_id": SESSION_ONE,
            "sequence": 1,
            "published_at": "2026-08-13T00:59:59.000Z",
            "fields": {
                "door.open": {
                    "value": True,
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
        }
        if schema_version != 1:
            value["binding_generation"] = 1
            value["pat_device_id_proof_sha256"] = self.proof()
        if schema_version == 3:
            value["cohort_generation"] = 2
        value.update(overrides)
        return json.dumps(value, separators=(",", ":")).encode()

    def availability(
        self,
        schema_version: int = 2,
        *,
        status: str = "online",
        state_sequence: int = 1,
        binding_generation: int = 1,
        cohort_generation: int = 2,
        observed_at: str = "2026-08-13T00:59:59.000Z",
    ) -> bytes:
        value = {
            "status": status,
            "session_id": SESSION_ONE,
            "observed_at": observed_at,
        }
        if schema_version != 1:
            value.update(
                {
                    "binding_generation": binding_generation,
                    "pat_device_id_proof_sha256": self.proof(),
                    "state_sequence": state_sequence,
                }
            )
        if schema_version == 3:
            value.update(
                {
                    "schema_version": 3,
                    "cohort_generation": cohort_generation,
                }
            )
        return json.dumps(value, separators=(",", ":")).encode()

    def test_accepts_a_schema_two_snapshot_whose_proof_matches(self) -> None:
        provider = self.provider(pat_device_id=self.PAT_DEVICE_ID)
        provider.ingest(provider.state_topic, self.payload(2), qos=1, retained=True)
        self.assertIs(provider.field_value("door.open"), True)

    def test_accepts_a_schema_three_snapshot_with_its_cohort(self) -> None:
        provider = self.provider(pat_device_id=self.PAT_DEVICE_ID)
        provider.ingest(provider.state_topic, self.payload(3), qos=1, retained=True)
        self.assertIs(provider.field_value("door.open"), True)

    def test_refuses_a_snapshot_proving_a_different_appliance(self) -> None:
        provider = self.provider(pat_device_id=self.PAT_DEVICE_ID)
        other = self.proof("1111111111111111222222222222222233333333333333334444444444444444")
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.state_topic,
                self.payload(2, pat_device_id_proof_sha256=other),
                qos=1,
                retained=True,
            )
        self.assertIsNone(provider.field_value("door.open"))

    def test_refuses_an_unproven_snapshot_once_the_binding_requires_identity(self) -> None:
        provider = self.provider(pat_device_id=self.PAT_DEVICE_ID, require_identity=True)
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(provider.state_topic, self.payload(1), qos=1, retained=True)

    def test_takes_an_unproven_snapshot_from_a_publisher_not_yet_migrated(self) -> None:
        # Demanding a proof from a publisher that has none to give rejects every
        # message, which is worse than leaving it unverified until the migration.
        provider = self.provider(pat_device_id=self.PAT_DEVICE_ID)
        provider.ingest(provider.state_topic, self.payload(1), qos=1, retained=True)
        self.assertIs(provider.field_value("door.open"), True)

    def test_a_wrong_proof_is_refused_even_before_identity_is_required(self) -> None:
        provider = self.provider(pat_device_id=self.PAT_DEVICE_ID)
        other = self.proof("1111111111111111222222222222222233333333333333334444444444444444")
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.state_topic,
                self.payload(2, pat_device_id_proof_sha256=other),
                qos=1,
                retained=True,
            )

    def test_requiring_identity_without_a_pat_device_id_is_refused_at_construction(self) -> None:
        with self.assertRaises(local.LocalProviderConfigurationError):
            self.provider(require_identity=True)

    def test_a_binding_without_a_pat_identity_still_takes_schema_one(self) -> None:
        provider = self.provider()
        provider.ingest(provider.state_topic, self.payload(1), qos=1, retained=True)
        self.assertIs(provider.field_value("door.open"), True)

    def test_refuses_a_malformed_proof_or_generation(self) -> None:
        for override in (
            {"pat_device_id_proof_sha256": "not-a-proof"},
            {"binding_generation": 0},
            {"binding_generation": "1"},
        ):
            provider = self.provider(pat_device_id=self.PAT_DEVICE_ID)
            with self.assertRaises(local.LocalProviderContractError):
                provider.ingest(
                    provider.state_topic, self.payload(2, **override), qos=1, retained=True
                )

    def test_live_state_advance_waits_for_its_exact_same_status_availability(self) -> None:
        provider = self.provider(
            pat_device_id=self.PAT_DEVICE_ID, require_identity=True
        )
        provider.ingest(provider.state_topic, self.payload(3), qos=1, retained=False)
        provider.ingest(
            provider.availability_topic,
            self.availability(3),
            qos=1,
            retained=False,
        )
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online"),
            qos=1,
            retained=False,
        )
        provider.set_transport_ready(True)
        self.assertTrue(provider.shadow_healthy)

        provider.ingest(
            provider.state_topic,
            self.payload(3, sequence=2),
            qos=1,
            retained=False,
        )
        self.assertFalse(
            provider.shadow_healthy,
            "an availability marker for sequence 1 must not attest sequence 2",
        )
        self.assertTrue(
            provider.ingest(
                provider.availability_topic,
                self.availability(3, state_sequence=2),
                qos=1,
                retained=False,
            )
        )
        self.assertTrue(provider.shadow_healthy)

    def test_schema_three_availability_matches_every_state_identity_coordinate(self) -> None:
        for mismatch in (
            {"state_sequence": 2},
            {"binding_generation": 2},
            {"cohort_generation": 3},
        ):
            with self.subTest(mismatch=mismatch):
                provider = self.provider(
                    pat_device_id=self.PAT_DEVICE_ID, require_identity=True
                )
                provider.ingest(
                    provider.state_topic, self.payload(3), qos=1, retained=False
                )
                with self.assertRaises(local.LocalProviderContractError):
                    provider.ingest(
                        provider.availability_topic,
                        self.availability(3, **mismatch),
                        qos=1,
                        retained=False,
                    )
                self.assertTrue(
                    provider.ingest(
                        provider.availability_topic,
                        self.availability(3),
                        qos=1,
                        retained=False,
                    )
                )

    def test_identity_bound_availability_cannot_predate_the_state_it_names(self) -> None:
        provider = self.provider(
            pat_device_id=self.PAT_DEVICE_ID, require_identity=True
        )
        provider.ingest(provider.state_topic, self.payload(3), qos=1, retained=False)
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.availability_topic,
                self.availability(3, observed_at="2026-08-13T00:59:58.999Z"),
                qos=1,
                retained=False,
            )
        self.assertEqual(provider._device_status, "unknown")

    def test_final_current_rejects_availability_that_predates_its_exact_state_atomically(self) -> None:
        provider = self.provider(
            pat_device_id=self.PAT_DEVICE_ID, require_identity=True
        )
        invalid = {
            provider.state_topic: (self.payload(3), 1, True),
            provider.availability_topic: (
                self.availability(3, observed_at="2026-08-13T00:59:58.999Z"),
                1,
                True,
            ),
            provider.runtime_availability_topic: (
                runtime_payload("online"),
                1,
                True,
            ),
        }
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest_retained_final_current(invalid)
        self.assertIsNone(provider.session_id)
        self.assertIsNone(provider.field_value("door.open"))

    def test_rejected_higher_generation_state_mutates_no_contract_state(self) -> None:
        provider = self.provider(
            pat_device_id=self.PAT_DEVICE_ID, require_identity=True
        )
        provider.ingest(provider.state_topic, self.payload(2), qos=1, retained=False)
        before = (
            provider.session_id,
            provider.sequence,
            dict(provider.shadow_fields),
            provider._binding_generation,
        )

        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.state_topic,
                self.payload(2, binding_generation=2),
                qos=1,
                retained=False,
            )
        self.assertEqual(
            (
                provider.session_id,
                provider.sequence,
                dict(provider.shadow_fields),
                provider._binding_generation,
            ),
            before,
        )

        self.assertTrue(
            provider.ingest(
                provider.state_topic,
                self.payload(2, sequence=2),
                qos=1,
                retained=False,
            )
        )
        self.assertEqual(provider.sequence, 2)

    def test_rejected_final_current_mutates_no_contract_state(self) -> None:
        provider = self.provider(
            pat_device_id=self.PAT_DEVICE_ID, require_identity=True
        )
        provider.ingest(provider.state_topic, self.payload(3), qos=1, retained=True)
        provider.ingest(
            provider.availability_topic,
            self.availability(3),
            qos=1,
            retained=True,
        )
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online"),
            qos=1,
            retained=True,
        )

        def payload_fingerprint(value: str | None) -> bytes | None:
            return None if value is None else hashlib.sha256(value.encode()).digest()

        def contract_state() -> tuple[object, ...]:
            return (
                provider._transport_ready,
                provider._binding_generation,
                provider._session_id,
                provider._sequence,
                payload_fingerprint(provider._state_payload),
                provider._state_published_at,
                provider._state_availability_coordinate,
                dict(provider._shadow_fields),
                provider._device_status,
                payload_fingerprint(provider._device_availability_payload),
                provider._device_availability_at,
                provider._device_availability_coordinate,
                frozenset(provider._tombstoned_sessions),
                provider._service_instance_id,
                provider._runtime_status,
                payload_fingerprint(provider._runtime_payload),
                provider._runtime_availability_at,
                frozenset(provider._tombstoned_service_instances),
            )

        before = contract_state()
        rejected_before = provider.rejected_messages
        invalid = {
            provider.state_topic: (
                self.payload(3, binding_generation=2),
                1,
                True,
            ),
            provider.availability_topic: (
                self.availability(3, binding_generation=2),
                1,
                True,
            ),
            provider.runtime_availability_topic: (
                runtime_payload("online"),
                1,
                True,
            ),
        }
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest_retained_final_current(invalid)
        self.assertEqual(contract_state(), before)
        self.assertEqual(provider.rejected_messages, rejected_before + 1)

        self.assertTrue(
            provider.ingest(
                provider.state_topic,
                self.payload(3, sequence=2),
                qos=1,
                retained=False,
            )
        )

    def test_the_proof_matches_the_publisher_for_a_synthetic_binding(self) -> None:
        # Fixed cross-language vector with no household identifier in source control.
        self.assertEqual(
            local.local_pat_device_identity_proof(
                BINDING_ID,
                "SYNTHETIC_MODEL",
                "thinq2",
                self.PAT_DEVICE_ID,
            ),
            "d5b929818f4499907b08f60238f047af653e50d1630ec4fbc6bf695601ae6f31",
        )


class AuthoritativeInvalidationTests(unittest.TestCase):
    """Only a profile that declares it may retract a retained value."""

    def profile(self, *, authoritative: bool):
        return local.LocalSemanticProfile(
            profile_id="synthetic-invalidation-v1",
            model_id="SYNTHETIC_MODEL",
            platform="thinq2",
            semantics_revision=31,
            fields={
                "door.open": local.LocalSemanticFieldContract(
                    value_type="boolean",
                    exposure="state",
                    confidence=("confirmed-synthetic",),
                ),
                "battery.level_pct": local.LocalSemanticFieldContract(
                    value_type="number",
                    exposure="state",
                    confidence=("confirmed-synthetic",),
                    unit="%",
                ),
            },
            authoritative_invalidations=authoritative,
        )

    def payload(self, **overrides) -> bytes:
        value = {
            "schema_version": 1,
            "semantics_revision": 31,
            "binding_id": BINDING_ID,
            "model_id": "SYNTHETIC_MODEL",
            "platform": "thinq2",
            "session_id": SESSION_ONE,
            "sequence": 1,
            "published_at": "2026-08-13T00:59:59.000Z",
            "fields": {
                "door.open": {
                    "value": True,
                    "value_type": "boolean",
                    "observed_at": "2026-08-13T00:59:58.000Z",
                    "confidence": "confirmed-synthetic",
                    "exposure": "state",
                }
            },
            "invalidated_fields": {
                "battery.level_pct": {
                    "observed_at": "2026-08-13T00:59:58.000Z",
                    "confidence": "confirmed-synthetic",
                }
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

    def published_payload(self, **overrides) -> bytes:
        return self.payload(
            fields={
                "door.open": {
                    "value": True,
                    "value_type": "boolean",
                    "observed_at": "2026-08-13T00:59:58.000Z",
                    "confidence": "confirmed-synthetic",
                    "exposure": "state",
                },
                "battery.level_pct": {
                    "value": 55,
                    "value_type": "number",
                    "unit": "%",
                    "observed_at": "2026-08-13T00:59:58.000Z",
                    "confidence": "confirmed-synthetic",
                    "exposure": "state",
                },
            },
            invalidated_fields=None,
            **overrides,
        )

    def test_a_tombstone_retracts_a_value_this_binding_was_showing(self) -> None:
        provider = local.LocalSemanticShadowProvider(
            BINDING_ID, self.profile(authoritative=True), now=lambda: NOW
        )
        published = json.loads(self.published_payload().decode())
        del published["invalidated_fields"]
        provider.ingest(
            provider.state_topic,
            json.dumps(published, separators=(",", ":")).encode(),
            qos=1,
            retained=True,
        )
        self.assertEqual(provider.field_value("battery.level_pct"), 55)

        provider.ingest(provider.state_topic, self.payload(sequence=2), qos=1, retained=True)
        self.assertIs(provider.field_value("door.open"), True)
        self.assertIsNone(provider.field_value("battery.level_pct"))

    def test_a_snapshot_that_retracts_everything_carries_no_fields_at_all(self) -> None:
        # What the publisher actually emits when a whole record becomes unknown.
        provider = local.LocalSemanticShadowProvider(
            BINDING_ID, self.profile(authoritative=True), now=lambda: NOW
        )
        provider.ingest(
            provider.state_topic,
            self.payload(
                fields={},
                invalidated_fields={
                    "door.open": {
                        "observed_at": "2026-08-13T00:59:58.000Z",
                        "confidence": "confirmed-synthetic",
                    },
                    "battery.level_pct": {
                        "observed_at": "2026-08-13T00:59:58.000Z",
                        "confidence": "confirmed-synthetic",
                    },
                },
            ),
            qos=1,
            retained=True,
        )
        self.assertEqual(dict(provider.shadow_fields), {})

    def test_a_snapshot_with_neither_fields_nor_tombstones_is_refused(self) -> None:
        provider = local.LocalSemanticShadowProvider(
            BINDING_ID, self.profile(authoritative=True), now=lambda: NOW
        )
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.state_topic,
                self.payload(fields={}, invalidated_fields={}),
                qos=1,
                retained=True,
            )

    def test_an_unauthorized_profile_refuses_them(self) -> None:
        provider = local.LocalSemanticShadowProvider(
            BINDING_ID, self.profile(authoritative=False), now=lambda: NOW
        )
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(provider.state_topic, self.payload(), qos=1, retained=True)

    def test_a_field_cannot_be_published_and_retracted_at_once(self) -> None:
        provider = local.LocalSemanticShadowProvider(
            BINDING_ID, self.profile(authoritative=True), now=lambda: NOW
        )
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.state_topic,
                self.payload(
                    invalidated_fields={
                        "door.open": {
                            "observed_at": "2026-08-13T00:59:58.000Z",
                            "confidence": "confirmed-synthetic",
                        }
                    }
                ),
                qos=1,
                retained=True,
            )


class AllowedValueTests(unittest.TestCase):
    def profile(self):
        return local.LocalSemanticProfile(
            profile_id="synthetic-allowlist-v1",
            model_id="SYNTHETIC_MODEL",
            platform="thinq2",
            semantics_revision=31,
            fields={
                "operation.mode": local.LocalSemanticFieldContract(
                    value_type="string",
                    exposure="state",
                    confidence=("confirmed-synthetic",),
                    allowed_values=("idle", "cleaning"),
                ),
            },
        )

    def payload(self, mode: str) -> bytes:
        return json.dumps(
            {
                "schema_version": 1,
                "semantics_revision": 31,
                "binding_id": BINDING_ID,
                "model_id": "SYNTHETIC_MODEL",
                "platform": "thinq2",
                "session_id": SESSION_ONE,
                "sequence": 1,
                "published_at": "2026-08-13T00:59:59.000Z",
                "fields": {
                    "operation.mode": {
                        "value": mode,
                        "value_type": "string",
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

    def test_a_value_inside_the_allowlist_is_stored(self) -> None:
        provider = local.LocalSemanticShadowProvider(
            BINDING_ID, self.profile(), now=lambda: NOW
        )
        provider.ingest(provider.state_topic, self.payload("cleaning"), qos=1, retained=True)
        self.assertEqual(provider.field_value("operation.mode"), "cleaning")

    def test_a_value_outside_it_is_refused(self) -> None:
        provider = local.LocalSemanticShadowProvider(
            BINDING_ID, self.profile(), now=lambda: NOW
        )
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.state_topic, self.payload("turbo"), qos=1, retained=True
            )


class CatalogueBackedContractTests(unittest.TestCase):
    """Exercise the real catalogue, not a hand-built profile.

    Every earlier test for these keys constructed its profile by hand, so the
    loader could have dropped `allowed_values`, `availability_policy` and
    `authoritative_invalidations` entirely and stayed green - which is how a
    publisher-side rename could silently disable them.
    """

    def setUp(self) -> None:
        _revision, self.profiles, _digest = local.load_local_semantic_profile_catalogue()

    def test_the_vacuum_profile_carries_its_allowlists_and_tombstone_authority(self) -> None:
        profile = self.profiles["wireless-vacuum-core-state-v1"]
        self.assertTrue(profile.authoritative_invalidations)
        allowlisted = {
            semantic_id: contract.allowed_values
            for semantic_id, contract in profile.fields.items()
            if contract.allowed_values
        }
        self.assertTrue(allowlisted, "the catalogue declares enum allowlists for this profile")
        for values in allowlisted.values():
            self.assertTrue(all(isinstance(item, str) for item in values))

    def test_the_tower_profile_carries_its_availability_policy(self) -> None:
        self.assertEqual(
            self.profiles["air-tower-core-state-v1"].availability_policy,
            "attested-session",
        )

    def test_a_profile_without_them_leaves_them_unset(self) -> None:
        profile = self.profiles["dhum-water-tank-v1"]
        self.assertIsNone(profile.availability_policy)
        self.assertFalse(profile.authoritative_invalidations)

    def test_a_catalogue_value_outside_a_declared_allowlist_is_refused(self) -> None:
        profile = self.profiles["wireless-vacuum-core-state-v1"]
        semantic_id, contract = next(
            (key, value) for key, value in profile.fields.items() if value.allowed_values
        )
        provider = local.LocalSemanticShadowProvider(BINDING_ID, profile, now=lambda: NOW)
        payload = {
            "schema_version": 1,
            "semantics_revision": profile.semantics_revision,
            "binding_id": BINDING_ID,
            "model_id": profile.model_id,
            "platform": profile.platform,
            "session_id": SESSION_ONE,
            "sequence": 1,
            "published_at": "2026-08-13T00:59:59.000Z",
            "fields": {
                semantic_id: {
                    "value": "definitely-not-in-the-allowlist",
                    "value_type": contract.value_type,
                    "observed_at": "2026-08-13T00:59:58.000Z",
                    "confidence": contract.confidence[0],
                    "exposure": contract.exposure,
                    **({"unit": contract.unit} if contract.unit else {}),
                }
            },
            "diagnostics": {
                "rejected_frames": 0,
                "unresolved_fields": 0,
                "invalid_values": 0,
                "unsupported_frames": 0,
            },
        }
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.state_topic,
                json.dumps(payload, separators=(",", ":")).encode(),
                qos=1,
                retained=True,
            )


class SupportedRevisionTests(unittest.TestCase):
    """A publisher one revision behind must still be understood.

    The catalogue carries a single current revision and, per profile, the set its
    contract holds across. Comparing against the current one alone means rolling
    the publisher back - the one moment this side has to keep working - takes
    Home Assistant down with it.
    """

    def setUp(self) -> None:
        _revision, self.profiles, _digest = local.load_local_semantic_profile_catalogue()
        self.profile = self.profiles["dhum-water-tank-v1"]

    def payload(self, revision: int) -> bytes:
        semantic_id, contract = next(iter(self.profile.fields.items()))
        return json.dumps(
            {
                "schema_version": 1,
                "semantics_revision": revision,
                "binding_id": BINDING_ID,
                "model_id": self.profile.model_id,
                "platform": self.profile.platform,
                "session_id": SESSION_ONE,
                "sequence": 1,
                "published_at": "2026-08-13T00:59:59.000Z",
                "fields": {
                    semantic_id: {
                        "value": True if contract.value_type == "boolean" else 1,
                        "value_type": contract.value_type,
                        "observed_at": "2026-08-13T00:59:58.000Z",
                        "confidence": contract.confidence[0],
                        "exposure": contract.exposure,
                        **({"unit": contract.unit} if contract.unit else {}),
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

    def test_every_revision_the_profile_declares_is_accepted(self) -> None:
        self.assertIn(26, self.profile.supported_semantics_revisions)
        for revision in self.profile.supported_semantics_revisions:
            with self.subTest(revision=revision):
                provider = local.LocalSemanticShadowProvider(
                    BINDING_ID, self.profile, now=lambda: NOW
                )
                provider.ingest(
                    provider.state_topic, self.payload(revision), qos=1, retained=True
                )
                self.assertTrue(provider.shadow_fields)

    def test_a_revision_the_profile_does_not_declare_is_refused(self) -> None:
        provider = local.LocalSemanticShadowProvider(BINDING_ID, self.profile, now=lambda: NOW)
        unsupported = max(self.profile.supported_semantics_revisions) + 1
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(provider.state_topic, self.payload(unsupported), qos=1, retained=True)


class DeviceReportPolicyTests(unittest.TestCase):
    """The publisher can judge availability by the appliance's own report.

    One thinq1 appliance changed nothing it reports as state for nine days while
    reporting to its own endpoint every fifteen minutes. Judged by the age of its
    state it is offline; judged by the capture session it is online with the
    appliance unplugged. The catalogue names which rule applies, and the window it
    is judged by, so this side has to carry both or reject both.
    """

    def setUp(self) -> None:
        _revision, self.profiles, _digest = local.load_local_semantic_profile_catalogue()

    def test_the_catalogue_carries_the_policy_and_its_window(self) -> None:
        profile = self.profiles["kimchi-thinq1-core-state-v1"]
        self.assertEqual(profile.availability_policy, "device-report")
        self.assertEqual(profile.freshness_max_age_ms, 45 * 60_000)

    def test_a_profile_on_no_policy_carries_no_window(self) -> None:
        profile = self.profiles["dhum-water-tank-v1"]
        self.assertIsNone(profile.availability_policy)
        self.assertIsNone(profile.freshness_max_age_ms)

    def test_no_other_profile_was_moved_onto_the_policy_by_accident(self) -> None:
        on_policy = sorted(
            profile_id
            for profile_id, profile in self.profiles.items()
            if profile.availability_policy == "device-report"
        )
        self.assertEqual(on_policy, ["kimchi-thinq1-core-state-v1"])
