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
