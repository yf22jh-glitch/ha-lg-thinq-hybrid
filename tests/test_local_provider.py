"""Pure contract tests for the read-only Rethink Local shadow provider."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import re
import unittest
from datetime import datetime, timedelta, timezone
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

    def test_change_listener_reports_only_committed_provider_updates(self) -> None:
        provider = self.make_provider()
        updates: list[tuple[int, bool]] = []
        remove = provider.async_add_listener(
            lambda: updates.append((provider.sequence, provider.shadow_healthy))
        )

        self.assertTrue(
            provider.ingest(
                provider.state_topic, state_payload(), qos=1, retained=True
            )
        )
        self.assertEqual(updates, [(1, False)])
        self.assertFalse(
            provider.ingest(
                provider.state_topic, state_payload(), qos=1, retained=True
            )
        )
        self.assertEqual(updates, [(1, False)], "idempotent replay must not notify")

        invalid = json.loads(state_payload(sequence=2))
        invalid["fields"]["water_tank.full"]["value"] = "ON"
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.state_topic,
                json.dumps(invalid).encode(),
                qos=1,
                retained=True,
            )
        self.assertEqual(updates, [(1, False)], "rejected input must not notify")

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
        self.assertEqual(len(updates), 4)
        self.assertTrue(updates[-1][1])

        remove()
        provider.set_transport_ready(False)
        self.assertEqual(len(updates), 4, "removed listener must stay detached")

    def test_listener_exception_isolated_from_provider_and_other_entities(self) -> None:
        provider = self.make_provider()
        updates: list[int] = []

        def broken_listener() -> None:
            raise RuntimeError("synthetic entity failure")

        provider.async_add_listener(broken_listener)
        provider.async_add_listener(lambda: updates.append(provider.sequence))

        with self.assertLogs(local._LOGGER, level="ERROR") as captured:
            self.assertTrue(
                provider.ingest(
                    provider.state_topic, state_payload(), qos=1, retained=True
                )
            )
        self.assertEqual(updates, [1])
        self.assertIn("update listener failed", captured.output[0])

    def test_full_bootstrap_notifies_once_and_exact_replay_does_not(self) -> None:
        provider = self.make_provider()
        updates: list[tuple[int, bool]] = []
        provider.async_add_listener(
            lambda: updates.append((provider.sequence, provider.shadow_healthy))
        )
        publications = {
            provider.state_topic: (state_payload(), 1, True),
            provider.availability_topic: (
                availability_payload("online"),
                1,
                True,
            ),
            provider.runtime_availability_topic: (
                runtime_payload("online"),
                1,
                True,
            ),
        }

        self.assertTrue(provider.ingest_bootstrap_final_current(publications))
        self.assertEqual(updates, [(1, False)])
        self.assertFalse(provider.ingest_bootstrap_final_current(publications))
        self.assertEqual(updates, [(1, False)])

    def test_field_availability_applies_profile_freshness_sla(self) -> None:
        clock = [NOW]
        profile = local.load_local_semantic_profile_catalogue()[1][
            "kimchi-thinq1-core-state-v1"
        ]
        provider = local.LocalSemanticShadowProvider(
            BINDING_ID, profile, now=lambda: clock[0]
        )
        contract = profile.fields["lock.enabled"]
        snapshot = json.loads(state_payload())
        snapshot.update(
            {
                "semantics_revision": profile.semantics_revision,
                "model_id": profile.model_id,
                "platform": profile.platform,
                "fields": {
                    "lock.enabled": {
                        "value": True,
                        "value_type": contract.value_type,
                        "observed_at": "2026-08-13T00:59:58.000Z",
                        "confidence": contract.confidence[0],
                        "exposure": contract.exposure,
                    }
                },
            }
        )
        provider.ingest(
            provider.state_topic,
            json.dumps(snapshot, separators=(",", ":")).encode(),
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

        self.assertTrue(provider.semantic_field_fresh("lock.enabled"))
        self.assertTrue(provider.semantic_field_available("lock.enabled"))
        self.assertEqual(
            provider.semantic_field_fresh_until("lock.enabled"),
            datetime(2026, 8, 13, 1, 44, 58, tzinfo=timezone.utc),
        )

        clock[0] = datetime(2026, 8, 13, 1, 44, 58, 1_000, tzinfo=timezone.utc)
        self.assertFalse(provider.semantic_field_fresh("lock.enabled"))
        self.assertFalse(provider.semantic_field_available("lock.enabled"))
        self.assertFalse(provider.semantic_field_available("unknown.field"))

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
    def test_local_provider_is_the_operational_owner_when_configured(self) -> None:
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
        provider.set_transport_ready(True)
        resolver = local.WaterTankProviderResolver(provider)

        self.assertTrue(resolver.resolve({local.WIDEQ_WATER_TANK_KEY: 0}))
        self.assertTrue(resolver.available(True))
        self.assertTrue(provider.shadow_value)
        self.assertEqual(resolver.mode, local.LOCAL_PROVIDER_MODE_SHADOW)

        provider.set_transport_ready(False)
        self.assertFalse(resolver.available(True))
        self.assertTrue(
            resolver.resolve({local.WIDEQ_WATER_TANK_KEY: 0}),
            "a configured Local owner must never fail open to WideQ",
        )

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
            (2, True),
            (2.0, True),
            ("2", True),
            ("2.0", True),
        )
        for value, expected in accepted:
            with self.subTest(value=value):
                self.assertIs(
                    resolver.resolve({local.WIDEQ_WATER_TANK_KEY: value}),
                    expected,
                )
        with self.assertLogs(local._LOGGER.name, level="WARNING") as logs:
            for value in ("ON", "OFF", 3, -1, True, False, object()):
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

    def test_old_wideq_tank_light_setting_is_not_a_tank_full_fallback(self) -> None:
        resolver = local.WaterTankProviderResolver()

        self.assertIsNone(
            resolver.resolve({"airState.miscFuncState.watertankLight": 1})
        )

    def test_wideq_fallback_key_is_in_the_dehumidifier_snapshot_contract(self) -> None:
        raw_paths = json.loads(
            (MODULE_PATH.parent / "feature_catalog" / "raw_paths.json").read_text(
                encoding="utf8"
            )
        )

        self.assertIn(
            [local.WIDEQ_WATER_TANK_KEY],
            raw_paths["wideq"]["DHUM_056905_WW"],
        )


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
        session_id: str = SESSION_ONE,
        state_sequence: int = 1,
        binding_generation: int = 1,
        cohort_generation: int = 2,
        observed_at: str = "2026-08-13T00:59:59.000Z",
    ) -> bytes:
        value = {
            "status": status,
            "session_id": session_id,
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

    def healthy_v3_provider(
        self,
        *,
        cohort_generation: int,
        sequence: int,
        session_id: str = SESSION_ONE,
        binding_generation: int = 1,
    ):
        provider = self.provider(
            pat_device_id=self.PAT_DEVICE_ID, require_identity=True
        )
        provider.ingest(
            provider.state_topic,
            self.payload(
                3,
                cohort_generation=cohort_generation,
                sequence=sequence,
                session_id=session_id,
                binding_generation=binding_generation,
            ),
            qos=1,
            retained=False,
        )
        provider.ingest(
            provider.availability_topic,
            self.availability(
                3,
                cohort_generation=cohort_generation,
                state_sequence=sequence,
                session_id=session_id,
                binding_generation=binding_generation,
            ),
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
        return provider

    def final_current(
        self,
        provider,
        *,
        cohort_generation: int,
        sequence: int,
        retained: bool,
        session_id: str = SESSION_ONE,
        binding_generation: int = 1,
        published_at: str = "2026-08-13T00:59:59.000Z",
        observed_at: str = "2026-08-13T00:59:59.000Z",
    ):
        return {
            provider.state_topic: (
                self.payload(
                    3,
                    cohort_generation=cohort_generation,
                    sequence=sequence,
                    session_id=session_id,
                    binding_generation=binding_generation,
                    published_at=published_at,
                ),
                1,
                retained,
            ),
            provider.availability_topic: (
                self.availability(
                    3,
                    cohort_generation=cohort_generation,
                    state_sequence=sequence,
                    session_id=session_id,
                    binding_generation=binding_generation,
                    observed_at=observed_at,
                ),
                1,
                retained,
            ),
            provider.runtime_availability_topic: (
                runtime_payload("online"),
                1,
                retained,
            ),
        }

    @staticmethod
    def operational_state(provider) -> tuple[object, ...]:
        """Snapshot every mutable fence except the intentional rejection count."""
        return (
            provider._transport_ready,
            provider._binding_generation,
            provider._cohort_generation,
            provider._session_id,
            provider._sequence,
            provider._state_payload,
            provider._state_published_at,
            provider._state_availability_coordinate,
            dict(provider._shadow_fields),
            provider._device_status,
            provider._device_availability_payload,
            provider._device_availability_at,
            provider._device_availability_coordinate,
            frozenset(provider._tombstoned_sessions),
            provider._service_instance_id,
            provider._runtime_status,
            provider._runtime_payload,
            provider._runtime_availability_at,
            frozenset(provider._tombstoned_service_instances),
        )

    def test_accepts_a_schema_two_snapshot_whose_proof_matches(self) -> None:
        provider = self.provider(pat_device_id=self.PAT_DEVICE_ID)
        provider.ingest(provider.state_topic, self.payload(2), qos=1, retained=True)
        self.assertIs(provider.field_value("door.open"), True)

    def test_accepts_a_schema_three_snapshot_with_its_cohort(self) -> None:
        provider = self.provider(pat_device_id=self.PAT_DEVICE_ID)
        provider.ingest(provider.state_topic, self.payload(3), qos=1, retained=True)
        self.assertIs(provider.field_value("door.open"), True)

    def test_live_state_and_availability_retained_deletes_fail_closed_in_either_order(
        self,
    ) -> None:
        for first_topic in ("state", "availability"):
            with self.subTest(first_topic=first_topic):
                provider = self.provider(
                    pat_device_id=self.PAT_DEVICE_ID, require_identity=True
                )
                provider.ingest(
                    provider.state_topic, self.payload(3), qos=1, retained=False
                )
                provider.ingest(
                    provider.availability_topic,
                    self.availability(3, status="online"),
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

                topics = {
                    "state": provider.state_topic,
                    "availability": provider.availability_topic,
                }
                second_topic = (
                    "availability" if first_topic == "state" else "state"
                )
                self.assertTrue(
                    provider.ingest(
                        topics[first_topic], b"", qos=1, retained=False
                    )
                )
                self.assertFalse(provider.shadow_healthy)
                self.assertIsNone(provider._device_availability_payload)
                self.assertEqual(provider._device_status, "offline")

                for replay_topic, replay_payload in (
                    (provider.state_topic, self.payload(3)),
                    (provider.availability_topic, self.availability(3)),
                ):
                    with self.assertRaises(local.LocalProviderContractError):
                        provider.ingest(
                            replay_topic,
                            replay_payload,
                            qos=1,
                            retained=False,
                        )

                provider.ingest(
                    topics[second_topic], b"", qos=1, retained=False
                )
                self.assertIsNone(provider.session_id)
                self.assertEqual(dict(provider.shadow_fields), {})
                self.assertIsNone(provider._state_payload)
                self.assertIn(SESSION_ONE, provider._tombstoned_sessions)

                with self.assertRaises(local.LocalProviderContractError):
                    provider.ingest(
                        provider.state_topic,
                        self.payload(3),
                        qos=1,
                        retained=False,
                    )

    def test_runtime_retained_delete_fails_closed_and_tombstones_its_service(self) -> None:
        provider = self.provider(
            pat_device_id=self.PAT_DEVICE_ID, require_identity=True
        )
        online = runtime_payload("online")
        provider.ingest(
            provider.runtime_availability_topic,
            online,
            qos=1,
            retained=False,
        )

        self.assertTrue(
            provider.ingest(
                provider.runtime_availability_topic,
                b"",
                qos=1,
                retained=False,
            )
        )
        self.assertEqual(provider._runtime_status, "offline")
        self.assertIsNone(provider._runtime_payload)
        self.assertIn(SERVICE_ONE, provider._tombstoned_service_instances)

        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.runtime_availability_topic,
                online,
                qos=1,
                retained=False,
            )

        self.assertTrue(
            provider.ingest(
                provider.runtime_availability_topic,
                runtime_payload("online", service_instance_id=SERVICE_TWO),
                qos=1,
                retained=False,
            )
        )
        self.assertEqual(provider._service_instance_id, SERVICE_TWO)
        self.assertEqual(provider._runtime_status, "online")

    def test_live_v2_reset_rotates_to_one_healthy_v3_session(self) -> None:
        provider = self.provider(
            pat_device_id=self.PAT_DEVICE_ID, require_identity=True
        )
        provider.ingest(provider.state_topic, self.payload(2), qos=1, retained=False)
        provider.ingest(
            provider.availability_topic,
            self.availability(2, status="offline"),
            qos=1,
            retained=False,
        )
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("offline"),
            qos=1,
            retained=False,
        )
        provider.set_transport_ready(True)

        provider.ingest(provider.state_topic, b"", qos=1, retained=False)
        provider.ingest(provider.availability_topic, b"", qos=1, retained=False)
        self.assertFalse(provider.shadow_healthy)

        state = json.loads(self.payload(3, session_id=SESSION_TWO))
        availability = json.loads(self.availability(3, status="online"))
        availability["session_id"] = SESSION_TWO
        provider.ingest(
            provider.state_topic,
            json.dumps(state).encode(),
            qos=1,
            retained=False,
        )
        provider.ingest(
            provider.availability_topic,
            json.dumps(availability).encode(),
            qos=1,
            retained=False,
        )
        self.assertFalse(
            provider.shadow_healthy,
            "the old offline runtime cannot make the fresh V3 current usable",
        )

        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online", service_instance_id=SERVICE_TWO),
            qos=1,
            retained=False,
        )
        self.assertTrue(provider.shadow_healthy)
        self.assertEqual(provider.session_id, SESSION_TWO)
        self.assertIn(SESSION_ONE, provider._tombstoned_sessions)
        self.assertIn(SERVICE_ONE, provider._tombstoned_service_instances)

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

    def test_live_atomic_pair_retains_the_previous_committed_snapshot(self) -> None:
        provider = self.healthy_v3_provider(cohort_generation=2, sequence=1)
        notifications = []
        provider.async_add_listener(lambda: notifications.append((provider.sequence, provider.shadow_healthy)))
        next_state = self.payload(3, sequence=2)
        self.assertFalse(provider.ingest_live_semantic_publication(provider.state_topic, next_state, qos=1, retained=False))
        self.assertEqual(provider.sequence, 1)
        self.assertTrue(provider.shadow_healthy)
        self.assertEqual(notifications, [])
        provider.ingest_live_semantic_publication(provider.availability_topic, self.availability(3, state_sequence=2), qos=1, retained=False)
        self.assertEqual(notifications, [(2, True)])
        self.assertEqual(provider.shadow_fields['door.open'].observed_at.isoformat(), '2026-08-13T00:59:58+00:00')
        provider.ingest_live_semantic_publication(provider.state_topic, self.payload(3, cohort_generation=3, sequence=1), qos=1, retained=False)
        self.assertEqual(provider.cohort_generation, 2)
        provider.ingest_live_semantic_publication(provider.availability_topic, self.availability(3, cohort_generation=3), qos=1, retained=False)
        self.assertEqual(provider.cohort_generation, 3)
        self.assertTrue(all(healthy for _, healthy in notifications))

    def test_live_atomic_pair_never_defers_real_offline_or_transport_loss(self) -> None:
        for topic_kind in ('device', 'runtime', 'transport', 'delete'):
            with self.subTest(topic_kind=topic_kind):
                provider = self.healthy_v3_provider(cohort_generation=2, sequence=1)
                provider.ingest_live_semantic_publication(provider.state_topic, self.payload(3, sequence=2), qos=1, retained=False)
                if topic_kind == 'transport':
                    provider.set_transport_ready(False)
                elif topic_kind == 'delete':
                    provider.ingest_live_semantic_publication(provider.state_topic, b'', qos=1, retained=False)
                elif topic_kind == 'runtime':
                    provider.ingest_live_semantic_publication(provider.runtime_availability_topic, runtime_payload('offline'), qos=1, retained=False)
                else:
                    provider.ingest_live_semantic_publication(provider.availability_topic, self.availability(3, status='offline'), qos=1, retained=False)
                self.assertFalse(provider.shadow_healthy)

    def test_live_atomic_pair_rejects_pending_regression_and_wrong_ack(self) -> None:
        provider = self.healthy_v3_provider(cohort_generation=2, sequence=1)
        provider.ingest_live_semantic_publication(provider.state_topic, self.payload(3, sequence=3), qos=1, retained=False)
        for topic, payload in (
            (provider.state_topic, self.payload(3, sequence=2)),
            (provider.availability_topic, self.availability(3, state_sequence=2)),
            (provider.state_topic, self.payload(3, sequence=3, session_id=SESSION_TWO)),
            (provider.state_topic, self.payload(3, sequence=4, pat_device_id_proof_sha256='0'*64)),
        ):
            with self.assertRaises(local.LocalProviderContractError):
                provider.ingest_live_semantic_publication(topic, payload, qos=1, retained=False)
            self.assertEqual(provider.sequence, 1)
        provider.ingest_live_semantic_publication(provider.availability_topic, self.availability(3, state_sequence=3), qos=1, retained=False)
        self.assertEqual(provider.sequence, 3)
        self.assertTrue(provider.shadow_healthy)

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

    def test_live_v3_higher_cohort_resets_sequence_and_invalidates_old_health(
        self,
    ) -> None:
        provider = self.healthy_v3_provider(cohort_generation=2, sequence=9)

        self.assertTrue(
            provider.ingest(
                provider.state_topic,
                self.payload(3, cohort_generation=3, sequence=1),
                qos=1,
                retained=False,
            )
        )
        self.assertEqual(provider.sequence, 1)
        self.assertEqual(provider._device_status, "unknown")
        self.assertIsNone(provider._device_availability_payload)
        self.assertIsNone(provider._device_availability_at)
        self.assertIsNone(provider._device_availability_coordinate)
        self.assertFalse(provider.shadow_healthy)

        self.assertTrue(
            provider.ingest(
                provider.availability_topic,
                self.availability(3, cohort_generation=3, state_sequence=1),
                qos=1,
                retained=False,
            )
        )
        self.assertTrue(provider.shadow_healthy)

    def test_live_v3_orders_cohort_before_session_and_sequence(self) -> None:
        cases = (
            (
                "cohort regression",
                {"cohort_generation": 2, "sequence": 99},
            ),
            (
                "same-cohort sequence regression",
                {"cohort_generation": 3, "sequence": 4},
            ),
            (
                "same-cohort cursor collision",
                {
                    "cohort_generation": 3,
                    "sequence": 5,
                    "published_at": "2026-08-13T00:59:59.500Z",
                },
            ),
            (
                "same-cohort session collision",
                {
                    "cohort_generation": 3,
                    "sequence": 6,
                    "session_id": SESSION_TWO,
                },
            ),
        )
        for name, overrides in cases:
            with self.subTest(name=name):
                provider = self.healthy_v3_provider(
                    cohort_generation=3, sequence=5
                )
                before = self.operational_state(provider)
                rejected_before = provider.rejected_messages

                with self.assertRaises(local.LocalProviderContractError):
                    provider.ingest(
                        provider.state_topic,
                        self.payload(3, **overrides),
                        qos=1,
                        retained=False,
                    )

                self.assertEqual(self.operational_state(provider), before)
                self.assertEqual(provider.rejected_messages, rejected_before + 1)
                self.assertTrue(provider.shadow_healthy)

        provider = self.provider(
            pat_device_id=self.PAT_DEVICE_ID, require_identity=True
        )
        provider.ingest(
            provider.state_topic,
            self.payload(3, cohort_generation=3, sequence=5),
            qos=1,
            retained=False,
        )
        self.assertTrue(
            provider.ingest(
                provider.state_topic,
                self.payload(
                    3,
                    cohort_generation=4,
                    sequence=1,
                    session_id=SESSION_TWO,
                ),
                qos=1,
                retained=False,
            )
        )
        self.assertEqual(provider.session_id, SESSION_TWO)
        self.assertEqual(provider.sequence, 1)
        self.assertNotIn(SESSION_ONE, provider._tombstoned_sessions)

    def test_v3_retained_state_delete_preserves_and_enforces_cohort_high_water(
        self,
    ) -> None:
        provider = self.healthy_v3_provider(cohort_generation=3, sequence=5)

        self.assertTrue(
            provider.ingest(
                provider.state_topic,
                b"",
                qos=1,
                retained=False,
            )
        )
        self.assertEqual(provider._cohort_generation, 3)
        self.assertIsNone(provider.session_id)
        self.assertIn(SESSION_ONE, provider._tombstoned_sessions)

        before = self.operational_state(provider)
        rejected_before = provider.rejected_messages
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.state_topic,
                self.payload(
                    3,
                    cohort_generation=2,
                    sequence=99,
                    session_id=SESSION_TWO,
                ),
                qos=1,
                retained=False,
            )
        self.assertEqual(self.operational_state(provider), before)
        self.assertEqual(provider.rejected_messages, rejected_before + 1)

        self.assertTrue(
            provider.ingest(
                provider.state_topic,
                self.payload(3, cohort_generation=4, sequence=1),
                qos=1,
                retained=False,
            )
        )
        self.assertEqual(provider._cohort_generation, 4)
        self.assertEqual(provider.session_id, SESSION_ONE)
        self.assertNotIn(SESSION_ONE, provider._tombstoned_sessions)
        self.assertEqual(provider._device_status, "unknown")
        self.assertFalse(provider.shadow_healthy)

        provider.ingest(
            provider.availability_topic,
            self.availability(3, cohort_generation=4, state_sequence=1),
            qos=1,
            retained=False,
        )
        self.assertTrue(provider.shadow_healthy)

    def test_schema_two_same_session_sequence_reset_remains_rejected(self) -> None:
        provider = self.provider(
            pat_device_id=self.PAT_DEVICE_ID, require_identity=True
        )
        provider.ingest(
            provider.state_topic,
            self.payload(2, sequence=9),
            qos=1,
            retained=False,
        )
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.state_topic,
                self.payload(2, sequence=1),
                qos=1,
                retained=False,
            )
        self.assertEqual(provider.sequence, 9)

        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.state_topic,
                self.payload(3, cohort_generation=1, sequence=1),
                qos=1,
                retained=False,
            )
        self.assertEqual(provider.sequence, 9)
        self.assertIsNone(provider._cohort_generation)

    def test_v3_final_current_paths_order_cohort_before_sequence(self) -> None:
        for method_name, retained in (
            ("ingest_retained_final_current", True),
            ("ingest_bootstrap_final_current", False),
        ):
            with self.subTest(method=method_name):
                provider = self.healthy_v3_provider(
                    cohort_generation=2, sequence=9
                )
                provider.set_transport_ready(False)

                publications = self.final_current(
                    provider,
                    cohort_generation=3,
                    sequence=4,
                    retained=retained,
                )
                self.assertTrue(getattr(provider, method_name)(publications))
                provider.set_transport_ready(True)
                self.assertEqual(provider.sequence, 4)
                self.assertTrue(provider.shadow_healthy)

                provider.set_transport_ready(False)
                before = self.operational_state(provider)
                rejected_candidates = (
                    (
                        "cohort regression",
                        self.final_current(
                            provider,
                            cohort_generation=2,
                            sequence=99,
                            retained=retained,
                        ),
                    ),
                    (
                        "same-cohort sequence regression",
                        self.final_current(
                            provider,
                            cohort_generation=3,
                            sequence=3,
                            retained=retained,
                        ),
                    ),
                    (
                        "same-cohort cursor collision",
                        self.final_current(
                            provider,
                            cohort_generation=3,
                            sequence=4,
                            retained=retained,
                            published_at="2026-08-13T00:59:59.500Z",
                            observed_at="2026-08-13T00:59:59.500Z",
                        ),
                    ),
                )
                for name, invalid in rejected_candidates:
                    with self.subTest(method=method_name, rejection=name):
                        rejected_before = provider.rejected_messages
                        with self.assertRaises(local.LocalProviderContractError):
                            getattr(provider, method_name)(invalid)
                        self.assertEqual(self.operational_state(provider), before)
                        self.assertEqual(
                            provider.rejected_messages,
                            rejected_before + 1,
                        )

    def test_v3_final_current_paths_reject_same_cohort_foreign_session_atomically(
        self,
    ) -> None:
        for method_name, retained in (
            ("ingest_retained_final_current", True),
            ("ingest_bootstrap_final_current", False),
        ):
            with self.subTest(method=method_name):
                provider = self.healthy_v3_provider(
                    cohort_generation=3, sequence=5
                )
                provider.set_transport_ready(False)
                before = self.operational_state(provider)
                rejected_before = provider.rejected_messages
                foreign = self.final_current(
                    provider,
                    cohort_generation=3,
                    sequence=6,
                    session_id=SESSION_TWO,
                    retained=retained,
                )

                with self.assertRaises(local.LocalProviderContractError):
                    getattr(provider, method_name)(foreign)

                self.assertEqual(self.operational_state(provider), before)
                self.assertEqual(provider.rejected_messages, rejected_before + 1)

    def test_v3_new_binding_generation_can_reset_cohort_via_final_current(
        self,
    ) -> None:
        provider = self.healthy_v3_provider(cohort_generation=9, sequence=9)
        provider.set_transport_ready(False)
        final_current = self.final_current(
            provider,
            binding_generation=2,
            cohort_generation=1,
            sequence=1,
            session_id=SESSION_TWO,
            retained=True,
        )

        self.assertTrue(provider.ingest_retained_final_current(final_current))
        provider.set_transport_ready(True)
        self.assertEqual(provider._binding_generation, 2)
        self.assertEqual(provider._cohort_generation, 1)
        self.assertEqual(provider.session_id, SESSION_TWO)
        self.assertEqual(provider.sequence, 1)
        self.assertTrue(provider.shadow_healthy)

        before = self.operational_state(provider)
        rejected_before = provider.rejected_messages
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.state_topic,
                self.payload(
                    3,
                    binding_generation=1,
                    cohort_generation=99,
                    sequence=99,
                ),
                qos=1,
                retained=False,
            )
        self.assertEqual(self.operational_state(provider), before)
        self.assertEqual(provider.rejected_messages, rejected_before + 1)

    def test_v3_live_binding_generation_reset_after_retained_delete_fences_old_generation(
        self,
    ) -> None:
        provider = self.healthy_v3_provider(cohort_generation=9, sequence=9)
        provider.ingest(provider.state_topic, b"", qos=1, retained=False)
        provider.ingest(provider.availability_topic, b"", qos=1, retained=False)

        provider.ingest(
            provider.state_topic,
            self.payload(
                3,
                binding_generation=2,
                cohort_generation=1,
                sequence=1,
                session_id=SESSION_TWO,
            ),
            qos=1,
            retained=False,
        )
        provider.ingest(
            provider.availability_topic,
            self.availability(
                3,
                binding_generation=2,
                cohort_generation=1,
                state_sequence=1,
                session_id=SESSION_TWO,
            ),
            qos=1,
            retained=False,
        )
        self.assertEqual(provider._binding_generation, 2)
        self.assertEqual(provider._cohort_generation, 1)
        self.assertTrue(provider.shadow_healthy)

        before = self.operational_state(provider)
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.state_topic,
                self.payload(
                    3,
                    binding_generation=1,
                    cohort_generation=99,
                    sequence=99,
                ),
                qos=1,
                retained=False,
            )
        self.assertEqual(self.operational_state(provider), before)

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
                provider._cohort_generation,
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


class ControlPresenceTests(unittest.TestCase):
    """Authenticated device presence is a control fence, not a state snapshot."""

    PAT_DEVICE_ID = (
        "1111111122222222333333334444444455555555666666667777777788888888"
    )
    PROFILE_ID = "synthetic-control-presence-v1"
    MODEL_ID = "SYNTHETIC_CONTROL_MODEL"

    def profile(
        self,
        *,
        platform: str = "thinq2",
        availability_policy: str = "attested-session",
        freshness_max_age_ms: int | None = None,
    ):
        return local.LocalSemanticProfile(
            profile_id=self.PROFILE_ID,
            model_id=self.MODEL_ID,
            platform=platform,
            semantics_revision=31,
            fields={
                "door.open": local.LocalSemanticFieldContract(
                    value_type="boolean",
                    exposure="state",
                    confidence=("confirmed-synthetic",),
                ),
                "operation.mode": local.LocalSemanticFieldContract(
                    value_type="string",
                    exposure="state",
                    confidence=("confirmed-synthetic",),
                ),
                "fan.mode": local.LocalSemanticFieldContract(
                    value_type="string",
                    exposure="state",
                    confidence=("confirmed-synthetic",),
                ),
                "temperature.target_c": local.LocalSemanticFieldContract(
                    value_type="number",
                    exposure="state",
                    confidence=("confirmed-synthetic",),
                ),
            },
            availability_policy=availability_policy,
            freshness_max_age_ms=freshness_max_age_ms,
        )

    def proof(self, *, platform: str = "thinq2") -> str:
        return local.local_pat_device_identity_proof(
            BINDING_ID,
            self.MODEL_ID,
            platform,
            self.PAT_DEVICE_ID,
        )

    def provider(self, *, clock=None, platform: str = "thinq2"):
        profile = (
            self.profile()
            if platform == "thinq2"
            else self.profile(
                platform="thinq1",
                availability_policy="device-report",
                freshness_max_age_ms=45 * 60 * 1000,
            )
        )
        return local.LocalSemanticShadowProvider(
            BINDING_ID,
            profile,
            pat_device_id=self.PAT_DEVICE_ID,
            require_identity=True,
            now=clock or (lambda: NOW),
        )

    def presence(
        self,
        *,
        status: str = "online",
        evidence: str = "attested-session",
        binding_generation: int = 1,
        sequence: int = 1,
        proof: str | None = None,
        profile_id: str | None = None,
        service_instance_id: str = SERVICE_ONE,
        observed_at: str = "2026-08-13T00:59:57.000Z",
        valid_until=None,
        platform: str = "thinq2",
    ) -> bytes:
        return json.dumps(
            {
                "schema_version": 1,
                "status": status,
                "evidence": evidence,
                "binding_generation": binding_generation,
                "sequence": sequence,
                "pat_device_id_proof_sha256": proof or self.proof(platform=platform),
                "profile_id": profile_id or self.PROFILE_ID,
                "service_instance_id": service_instance_id,
                "observed_at": observed_at,
                "valid_until": valid_until,
            },
            separators=(",", ":"),
        ).encode()

    def state(
        self,
        *,
        sequence: int = 1,
        cohort_generation: int = 1,
        binding_generation: int = 1,
        published_at: str = "2026-08-13T00:59:59.000Z",
        tuple_observed_at: dict[str, str] | None = None,
    ) -> bytes:
        tuple_clocks = tuple_observed_at or {}
        default_observed_at = "2026-08-13T00:59:58.000Z"
        return json.dumps(
            {
                "schema_version": 3,
                "semantics_revision": 31,
                "binding_id": BINDING_ID,
                "model_id": self.MODEL_ID,
                "platform": "thinq2",
                "session_id": SESSION_ONE,
                "sequence": sequence,
                "binding_generation": binding_generation,
                "cohort_generation": cohort_generation,
                "pat_device_id_proof_sha256": self.proof(),
                "published_at": published_at,
                "fields": {
                    "door.open": {
                        "value": True,
                        "value_type": "boolean",
                        "observed_at": default_observed_at,
                        "confidence": "confirmed-synthetic",
                        "exposure": "state",
                    },
                    "operation.mode": {
                        "value": "cool",
                        "value_type": "string",
                        "observed_at": tuple_clocks.get(
                            "operation.mode", default_observed_at
                        ),
                        "confidence": "confirmed-synthetic",
                        "exposure": "state",
                    },
                    "fan.mode": {
                        "value": "high",
                        "value_type": "string",
                        "observed_at": tuple_clocks.get(
                            "fan.mode", default_observed_at
                        ),
                        "confidence": "confirmed-synthetic",
                        "exposure": "state",
                    },
                    "temperature.target_c": {
                        "value": 24,
                        "value_type": "number",
                        "observed_at": tuple_clocks.get(
                            "temperature.target_c", default_observed_at
                        ),
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
            },
            separators=(",", ":"),
        ).encode()

    def availability(
        self,
        *,
        state_sequence: int = 1,
        cohort_generation: int = 1,
        binding_generation: int = 1,
        status: str = "online",
        observed_at: str = "2026-08-13T01:00:00.000Z",
    ) -> bytes:
        return json.dumps(
            {
                "schema_version": 3,
                "status": status,
                "session_id": SESSION_ONE,
                "observed_at": observed_at,
                "binding_generation": binding_generation,
                "cohort_generation": cohort_generation,
                "pat_device_id_proof_sha256": self.proof(),
                "state_sequence": state_sequence,
            },
            separators=(",", ":"),
        ).encode()

    @staticmethod
    def presence_state(provider) -> tuple[object, ...]:
        return (
            provider._presence_binding_generation,
            provider._presence_sequence,
            provider._presence_status,
            provider._presence_payload,
            provider._presence_service_instance_id,
            provider._presence_observed_at,
            provider._presence_valid_until,
            provider._presence_live_received_at,
            frozenset(provider._tombstoned_presence_service_instances),
            provider._control_state_current,
            provider._semantic_transport_current,
        )

    @staticmethod
    def full_operational_state(provider) -> tuple[object, ...]:
        """Snapshot every mutable fence except the intentional rejection count."""
        return (
            provider._transport_ready,
            provider._binding_generation,
            provider._cohort_generation,
            provider._session_id,
            provider._sequence,
            provider._state_payload,
            provider._state_published_at,
            provider._state_availability_coordinate,
            dict(provider._shadow_fields),
            provider._device_status,
            provider._device_availability_payload,
            provider._device_availability_at,
            provider._device_availability_coordinate,
            frozenset(provider._tombstoned_sessions),
            provider._service_instance_id,
            provider._runtime_status,
            provider._runtime_payload,
            provider._runtime_availability_at,
            frozenset(provider._tombstoned_service_instances),
            provider._presence_binding_generation,
            provider._presence_sequence,
            provider._presence_status,
            provider._presence_payload,
            provider._presence_service_instance_id,
            provider._presence_observed_at,
            provider._presence_valid_until,
            provider._presence_live_received_at,
            frozenset(provider._tombstoned_presence_service_instances),
            provider._control_state_current,
            provider._semantic_transport_current,
        )

    def test_presence_topic_is_exact_and_control_alive_does_not_need_state(self) -> None:
        provider = self.provider()
        self.assertEqual(
            provider.presence_topic,
            f"{local.LOCAL_PILOT_PREFIX}/presence/{BINDING_ID}",
        )
        self.assertEqual(provider.topics[-1], provider.presence_topic)

        provider.ingest(
            provider.presence_topic, self.presence(), qos=1, retained=True
        )
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online"),
            qos=1,
            retained=True,
        )
        provider.set_transport_ready(True)

        self.assertFalse(
            provider.control_alive,
            "a retained marker alone does not prove a publisher on this MQTT connection",
        )
        self.assertIsNone(provider.read_publication_authority)
        self.assertTrue(
            provider.ingest(
                provider.presence_topic,
                self.presence(),
                qos=1,
                retained=False,
            )
        )
        self.assertTrue(provider.control_alive)
        self.assertFalse(provider.shadow_healthy)
        self.assertIsNone(provider.session_id)
        self.assertIsNone(provider.cohort_generation)
        self.assertEqual(
            provider.read_publication_authority,
            (1, SERVICE_ONE),
        )
        provider.set_transport_ready(False)
        self.assertFalse(provider.control_alive)
        self.assertIsNone(provider.read_publication_authority)

    def test_live_presence_receipt_expires_and_resets_on_every_connection(self) -> None:
        clock = [NOW]
        provider = self.provider(clock=lambda: clock[0])
        payload = self.presence()
        provider.ingest(provider.presence_topic, payload, qos=1, retained=True)
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online"),
            qos=1,
            retained=True,
        )
        provider.set_transport_ready(True)
        self.assertFalse(provider.control_alive)

        self.assertTrue(
            provider.ingest(
                provider.presence_topic, payload, qos=1, retained=False
            )
        )
        self.assertTrue(provider.control_alive)
        clock[0] += timedelta(seconds=240)
        self.assertTrue(provider.control_alive)
        clock[0] += timedelta(milliseconds=1)
        self.assertFalse(provider.control_alive)

        clock[0] += timedelta(seconds=1)
        self.assertTrue(
            provider.ingest(
                provider.presence_topic, payload, qos=1, retained=False
            )
        )
        self.assertTrue(provider.control_alive)
        provider.set_transport_ready(False)
        provider.set_transport_ready(True)
        self.assertFalse(provider.control_alive)

    def test_read_authority_expiry_notifies_once_and_heartbeat_reschedules(
        self,
    ) -> None:
        clock = [NOW]
        provider = self.provider(clock=lambda: clock[0])
        payload = self.presence()
        provider.ingest(provider.presence_topic, payload, qos=1, retained=True)
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online"),
            qos=1,
            retained=True,
        )
        provider.set_transport_ready(True)
        provider.ingest(provider.presence_topic, payload, qos=1, retained=False)
        first_expiry = provider.read_publication_authority_expiry
        self.assertIsNotNone(first_expiry)

        clock[0] += timedelta(seconds=120)
        provider.ingest(provider.presence_topic, payload, qos=1, retained=False)
        second_expiry = provider.read_publication_authority_expiry
        self.assertIsNotNone(second_expiry)
        self.assertNotEqual(first_expiry, second_expiry)

        updates = []
        remove = provider.async_add_listener(lambda: updates.append(clock[0]))
        try:
            assert first_expiry is not None and second_expiry is not None
            clock[0] = first_expiry[1] + timedelta(milliseconds=1)
            self.assertFalse(
                provider.expire_read_publication_authority(first_expiry)
            )
            self.assertTrue(provider.control_alive)
            self.assertEqual(updates, [])

            clock[0] = second_expiry[1] + timedelta(milliseconds=1)
            self.assertTrue(
                provider.expire_read_publication_authority(second_expiry)
            )
            self.assertFalse(provider.control_alive)
            self.assertEqual(len(updates), 1)
            self.assertFalse(
                provider.expire_read_publication_authority(second_expiry)
            )
            self.assertEqual(len(updates), 1)
        finally:
            remove()

    def test_same_physical_presence_heartbeat_never_closes_current_tuple(self) -> None:
        clock = [NOW]
        provider = self.provider(clock=lambda: clock[0])
        payload = self.presence()
        provider.ingest(provider.presence_topic, payload, qos=1, retained=False)
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online"),
            qos=1,
            retained=False,
        )
        provider.ingest(provider.state_topic, self.state(), qos=1, retained=False)
        provider.ingest(
            provider.availability_topic,
            self.availability(),
            qos=1,
            retained=False,
        )
        provider.set_transport_ready(True)
        required = (
            "operation.mode",
            "fan.mode",
            "temperature.target_c",
        )
        self.assertTrue(provider.control_fields_ready(required))

        clock[0] += timedelta(seconds=60)
        self.assertTrue(
            provider.ingest(
                provider.presence_topic, payload, qos=1, retained=False
            )
        )
        self.assertTrue(provider.control_fields_ready(required))

        provider.ingest(
            provider.presence_topic,
            self.presence(sequence=2),
            qos=1,
            retained=False,
        )
        self.assertTrue(provider.control_alive)
        self.assertFalse(provider.control_fields_ready(required))

    def test_semantic_health_and_control_presence_remain_independent(self) -> None:
        provider = self.provider()
        provider.ingest(provider.state_topic, self.state(), qos=1, retained=False)
        provider.ingest(
            provider.availability_topic,
            self.availability(),
            qos=1,
            retained=False,
        )
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online"),
            qos=1,
            retained=False,
        )
        provider.ingest(
            provider.presence_topic,
            self.presence(status="offline"),
            qos=1,
            retained=False,
        )
        provider.set_transport_ready(True)
        self.assertTrue(provider.shadow_healthy)
        self.assertFalse(provider.control_alive)

        provider.ingest(
            provider.presence_topic,
            self.presence(
                sequence=2, observed_at="2026-08-13T01:00:01.000Z"
            ),
            qos=1,
            retained=False,
        )
        self.assertTrue(provider.control_alive)

        provider.ingest(
            provider.state_topic,
            self.state(sequence=1, cohort_generation=2),
            qos=1,
            retained=False,
        )
        self.assertFalse(provider.shadow_healthy)
        self.assertTrue(
            provider.control_alive,
            "a state cohort transition must not erase authenticated presence",
        )

    def test_control_alive_requires_the_current_online_runtime_instance(self) -> None:
        provider = self.provider()
        provider.ingest(
            provider.presence_topic, self.presence(), qos=1, retained=False
        )
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online", service_instance_id=SERVICE_TWO),
            qos=1,
            retained=False,
        )
        provider.set_transport_ready(True)
        self.assertFalse(provider.control_alive)

        provider.ingest(
            provider.presence_topic,
            self.presence(
                service_instance_id=SERVICE_TWO,
                observed_at="2026-08-13T01:00:01.000Z",
            ),
            qos=1,
            retained=False,
        )
        self.assertTrue(provider.control_alive)

        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload(
                "offline",
                service_instance_id=SERVICE_TWO,
                observed_at="2026-08-13T01:00:01.000Z",
            ),
            qos=1,
            retained=False,
        )
        self.assertFalse(provider.control_alive)

    def test_presence_contract_rejects_mismatches_without_mutation(self) -> None:
        provider = self.provider()
        provider.ingest(
            provider.presence_topic, self.presence(), qos=1, retained=False
        )
        before = self.presence_state(provider)
        rejected_before = provider.rejected_messages
        invalid_payloads = []
        for overrides in (
            {"evidence": "device-report"},
            {"profile_id": "wrong-profile"},
            {"proof": "0" * 64},
            {"valid_until": "2026-08-13T01:45:00.000Z"},
            {"observed_at": "2026-08-13T01:05:00.001Z"},
            {"sequence": 0},
        ):
            invalid_payloads.append(self.presence(**overrides))
        extra_key = json.loads(self.presence())
        extra_key["unexpected"] = True
        invalid_payloads.append(json.dumps(extra_key).encode())

        for payload in invalid_payloads:
            with self.assertRaises(local.LocalProviderContractError):
                provider.ingest(
                    provider.presence_topic, payload, qos=1, retained=False
                )
        self.assertEqual(self.presence_state(provider), before)
        self.assertEqual(
            provider.rejected_messages,
            rejected_before + len(invalid_payloads),
        )

    def test_presence_cursor_rejects_regression_collision_and_old_service_replay(
        self,
    ) -> None:
        provider = self.provider()
        first = self.presence(binding_generation=2, sequence=3)
        self.assertTrue(
            provider.ingest(provider.presence_topic, first, qos=1, retained=False)
        )
        self.assertFalse(
            provider.ingest(provider.presence_topic, first, qos=1, retained=False)
        )

        rejected = (
            self.presence(
                binding_generation=1,
                sequence=99,
                observed_at="2026-08-13T01:00:02.000Z",
            ),
            self.presence(binding_generation=2, sequence=2),
            self.presence(binding_generation=2, sequence=3, status="offline"),
        )
        for payload in rejected:
            before = self.presence_state(provider)
            with self.assertRaises(local.LocalProviderContractError):
                provider.ingest(
                    provider.presence_topic, payload, qos=1, retained=False
                )
            self.assertEqual(self.presence_state(provider), before)

        self.assertTrue(
            provider.ingest(
                provider.presence_topic,
                self.presence(
                    binding_generation=2,
                    sequence=1,
                    service_instance_id=SERVICE_TWO,
                    observed_at="2026-08-13T01:00:01.000Z",
                ),
                qos=1,
                retained=False,
            )
        )
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.presence_topic,
                self.presence(
                    binding_generation=2,
                    sequence=99,
                    service_instance_id=SERVICE_ONE,
                    observed_at="2026-08-13T01:00:02.000Z",
                ),
                qos=1,
                retained=False,
            )

    def test_presence_retained_delete_fails_closed_and_blocks_stale_replay(self) -> None:
        provider = self.provider()
        payload = self.presence()
        provider.ingest(provider.presence_topic, payload, qos=1, retained=True)
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online"),
            qos=1,
            retained=True,
        )
        provider.set_transport_ready(True)
        self.assertFalse(provider.control_alive)
        provider.ingest(
            provider.presence_topic, payload, qos=1, retained=False
        )
        self.assertTrue(provider.control_alive)

        self.assertTrue(
            provider.ingest(provider.presence_topic, b"", qos=1, retained=False)
        )
        self.assertFalse(provider.control_alive)
        self.assertIsNone(provider._presence_live_received_at)
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.presence_topic, payload, qos=1, retained=False
            )

    def test_runtime_retained_delete_closes_presence_until_a_new_service_pairs(
        self,
    ) -> None:
        provider = self.provider()
        provider.ingest(
            provider.presence_topic, self.presence(), qos=1, retained=False
        )
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online"),
            qos=1,
            retained=True,
        )
        provider.set_transport_ready(True)
        self.assertTrue(provider.control_alive)

        self.assertTrue(
            provider.ingest(
                provider.runtime_availability_topic,
                b"",
                qos=1,
                retained=False,
            )
        )
        self.assertFalse(provider.control_alive)
        self.assertIsNone(provider._presence_live_received_at)
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.runtime_availability_topic,
                runtime_payload("online"),
                qos=1,
                retained=False,
            )

        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload(
                "online",
                service_instance_id=SERVICE_TWO,
                observed_at="2026-08-13T01:00:01.000Z",
            ),
            qos=1,
            retained=False,
        )
        self.assertFalse(provider.control_alive)
        provider.ingest(
            provider.presence_topic,
            self.presence(
                service_instance_id=SERVICE_TWO,
                sequence=1,
                observed_at="2026-08-13T01:00:01.000Z",
            ),
            qos=1,
            retained=False,
        )
        self.assertTrue(provider.control_alive)

    def test_live_presence_hands_over_a_stale_retained_runtime_service(self) -> None:
        provider = self.provider()
        stale_presence = self.presence()
        provider.ingest_control_bootstrap_final_current(
            {
                provider.runtime_availability_topic: (
                    runtime_payload("online"),
                    1,
                    True,
                ),
                provider.presence_topic: (stale_presence, 1, True),
            }
        )
        provider.set_transport_ready(True)
        self.assertFalse(provider.control_alive)

        replacement_offline = self.presence(
            status="offline",
            service_instance_id=SERVICE_TWO,
            sequence=1,
            observed_at="2026-08-13T01:00:00.000Z",
        )
        provider.ingest(
            provider.presence_topic,
            replacement_offline,
            qos=1,
            retained=True,
        )
        self.assertEqual(provider._service_instance_id, SERVICE_ONE)
        self.assertEqual(provider._runtime_status, "online")
        self.assertNotIn(SERVICE_ONE, provider._tombstoned_service_instances)
        self.assertFalse(provider.control_alive)

        provider.ingest(
            provider.presence_topic,
            replacement_offline,
            qos=1,
            retained=False,
        )
        self.assertEqual(provider._runtime_status, "offline")
        self.assertIsNone(provider._runtime_payload)
        self.assertIn(SERVICE_ONE, provider._tombstoned_service_instances)
        self.assertIsNotNone(provider._presence_live_received_at)

        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload(
                "online",
                service_instance_id=SERVICE_TWO,
                observed_at="2026-08-13T01:00:01.000Z",
            ),
            qos=1,
            retained=False,
        )
        self.assertFalse(provider.control_alive)
        provider.ingest(
            provider.presence_topic,
            self.presence(
                service_instance_id=SERVICE_TWO,
                sequence=2,
                observed_at="2026-08-13T01:00:01.000Z",
            ),
            qos=1,
            retained=False,
        )
        self.assertTrue(provider.control_alive)

        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.runtime_availability_topic,
                runtime_payload("online"),
                qos=1,
                retained=False,
            )

    def test_live_runtime_matching_retained_presence_hands_over_stale_service(
        self,
    ) -> None:
        provider = self.provider()
        provider.ingest_control_bootstrap_final_current(
            {
                provider.runtime_availability_topic: (
                    runtime_payload("online"),
                    1,
                    True,
                ),
                provider.presence_topic: (self.presence(), 1, True),
            }
        )
        replacement_online = self.presence(
            service_instance_id=SERVICE_TWO,
            observed_at="2026-08-13T01:00:00.000Z",
        )
        provider.ingest_control_bootstrap_final_current(
            {
                provider.runtime_availability_topic: (
                    runtime_payload("online"),
                    1,
                    True,
                ),
                provider.presence_topic: (replacement_online, 1, True),
            }
        )
        provider.set_transport_ready(True)
        self.assertEqual(provider._service_instance_id, SERVICE_ONE)
        self.assertEqual(provider._runtime_status, "online")
        self.assertFalse(provider.control_alive)

        replacement_runtime = runtime_payload(
            "online",
            service_instance_id=SERVICE_TWO,
            observed_at="2026-08-13T01:00:01.000Z",
        )
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.runtime_availability_topic,
                replacement_runtime,
                qos=1,
                retained=True,
            )
        self.assertEqual(provider._service_instance_id, SERVICE_ONE)
        self.assertNotIn(SERVICE_ONE, provider._tombstoned_service_instances)

        provider.ingest(
            provider.runtime_availability_topic,
            replacement_runtime,
            qos=1,
            retained=False,
        )
        self.assertEqual(provider._service_instance_id, SERVICE_TWO)
        self.assertEqual(provider._runtime_status, "online")
        self.assertIn(SERVICE_ONE, provider._tombstoned_service_instances)
        self.assertFalse(provider.control_alive)

        provider.ingest(
            provider.presence_topic,
            replacement_online,
            qos=1,
            retained=False,
        )
        self.assertTrue(provider.control_alive)

        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest(
                provider.runtime_availability_topic,
                runtime_payload("online"),
                qos=1,
                retained=False,
            )

    def test_buffered_mismatched_live_presence_is_rejected_atomically(self) -> None:
        provider = self.provider()
        before = self.full_operational_state(provider)
        rejected_before = provider.rejected_messages
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest_control_bootstrap_final_current(
                {
                    provider.runtime_availability_topic: (
                        runtime_payload("online"),
                        1,
                        True,
                    ),
                    provider.presence_topic: (
                        self.presence(
                            status="offline",
                            service_instance_id=SERVICE_TWO,
                            observed_at="2026-08-13T01:00:00.000Z",
                        ),
                        1,
                        False,
                    ),
                }
            )
        self.assertEqual(self.full_operational_state(provider), before)
        self.assertEqual(provider.rejected_messages, rejected_before + 1)

    def test_buffered_live_presence_rejects_a_mismatched_retained_runtime(
        self,
    ) -> None:
        provider = self.provider()
        provider.ingest_control_bootstrap_final_current(
            {
                provider.runtime_availability_topic: (
                    runtime_payload("online"),
                    1,
                    True,
                ),
                provider.presence_topic: (self.presence(), 1, True),
            }
        )

        before = self.full_operational_state(provider)
        rejected_before = provider.rejected_messages
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest_control_bootstrap_final_current(
                {
                    provider.runtime_availability_topic: (
                        runtime_payload(
                            "online",
                            service_instance_id=SERVICE_TWO,
                            observed_at="2026-08-13T01:00:01.000Z",
                        ),
                        1,
                        True,
                    ),
                    provider.presence_topic: (self.presence(), 1, False),
                }
            )
        self.assertEqual(self.full_operational_state(provider), before)
        self.assertEqual(provider.rejected_messages, rejected_before + 1)

    def test_full_buffered_mismatched_live_presence_is_rejected_atomically(
        self,
    ) -> None:
        provider = self.provider()
        initial = {
            provider.state_topic: (self.state(), 1, True),
            provider.availability_topic: (self.availability(), 1, True),
            provider.runtime_availability_topic: (
                runtime_payload("online"),
                1,
                True,
            ),
            provider.presence_topic: (self.presence(), 1, True),
        }
        provider.ingest_retained_final_current(initial)
        repaired = dict(initial)
        repaired[provider.runtime_availability_topic] = (
            runtime_payload(
                "online",
                service_instance_id=SERVICE_TWO,
                observed_at="2026-08-13T01:00:01.000Z",
            ),
            1,
            True,
        )
        repaired[provider.presence_topic] = (self.presence(), 1, False)

        before = self.full_operational_state(provider)
        rejected_before = provider.rejected_messages
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest_bootstrap_final_current(repaired)
        self.assertEqual(self.full_operational_state(provider), before)
        self.assertEqual(provider.rejected_messages, rejected_before + 1)

    def test_thinq1_valid_until_expires_dynamically_and_is_exact(self) -> None:
        clock = [NOW]
        provider = self.provider(clock=lambda: clock[0], platform="thinq1")
        provider.ingest(
            provider.presence_topic,
            self.presence(
                status="offline",
                evidence="device-report",
                platform="thinq1",
                sequence=1,
                observed_at="2026-08-13T01:00:00.000Z",
                valid_until="2026-08-13T01:00:00.000Z",
            ),
            qos=1,
            retained=True,
        )
        # A restarted publisher first emits offline at startup, then adopts a still-fresh durable
        # report whose observation predates that startup. Sequence, not the semantic timestamp,
        # orders those two publications without opening offline -> stale-online replay.
        valid_until = "2026-08-13T01:44:00.000Z"
        online = self.presence(
            evidence="device-report",
            platform="thinq1",
            sequence=2,
            observed_at="2026-08-13T00:59:00.000Z",
            valid_until=valid_until,
        )
        provider.ingest(
            provider.presence_topic,
            online,
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
        self.assertFalse(provider.control_alive)
        provider.ingest(
            provider.presence_topic, online, qos=1, retained=False
        )
        self.assertTrue(provider.control_alive)
        clock[0] = NOW + timedelta(minutes=44)
        provider.ingest(
            provider.presence_topic, online, qos=1, retained=False
        )
        self.assertTrue(provider.control_alive)
        clock[0] += timedelta(milliseconds=1)
        self.assertFalse(provider.control_alive)

        other = self.provider(platform="thinq1")
        with self.assertRaises(local.LocalProviderContractError):
            other.ingest(
                other.presence_topic,
                self.presence(
                    evidence="device-report",
                    platform="thinq1",
                    valid_until="2026-08-13T01:43:59.999Z",
                    observed_at="2026-08-13T00:59:00.000Z",
                ),
                qos=1,
                retained=True,
            )

    def test_bootstrap_requires_and_atomically_applies_all_four_topics(self) -> None:
        provider = self.provider()
        publications = {
            provider.state_topic: (self.state(), 1, True),
            provider.availability_topic: (self.availability(), 1, True),
            provider.runtime_availability_topic: (
                runtime_payload("online"),
                1,
                True,
            ),
            provider.presence_topic: (self.presence(), 1, True),
        }
        incomplete = dict(publications)
        incomplete.pop(provider.presence_topic)
        before = self.presence_state(provider)
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest_retained_final_current(incomplete)
        self.assertEqual(self.presence_state(provider), before)
        self.assertIsNone(provider.session_id)

        self.assertTrue(provider.ingest_retained_final_current(publications))
        provider.set_transport_ready(True)
        self.assertTrue(provider.shadow_healthy)
        self.assertFalse(provider.control_alive)
        provider.ingest(
            provider.presence_topic,
            publications[provider.presence_topic][0],
            qos=1,
            retained=False,
        )
        self.assertTrue(provider.control_alive)

    def test_no_state_bootstrap_opens_control_before_optional_semantics_arrive(
        self,
    ) -> None:
        provider = self.provider()
        updates: list[tuple[int, bool]] = []
        provider.async_add_listener(
            lambda: updates.append((provider.sequence, provider.shadow_healthy))
        )
        presence = self.presence()
        self.assertTrue(
            provider.ingest_control_bootstrap_final_current(
                {
                    provider.runtime_availability_topic: (
                        runtime_payload("online"),
                        1,
                        True,
                    ),
                    provider.presence_topic: (presence, 1, True),
                }
            )
        )
        self.assertEqual(updates, [(0, False)])
        provider.set_transport_ready(True)
        self.assertEqual(updates, [(0, False), (0, False)])
        self.assertFalse(provider.control_alive)
        provider.ingest(
            provider.presence_topic, presence, qos=1, retained=False
        )
        self.assertEqual(len(updates), 3)
        self.assertTrue(provider.control_alive)
        self.assertFalse(provider.control_state_ready)
        self.assertFalse(provider.shadow_healthy)

        self.assertTrue(
            provider.ingest_semantic_bootstrap_final_current(
                {
                    provider.availability_topic: (self.availability(), 1, True),
                    provider.state_topic: (self.state(), 1, True),
                }
            )
        )
        self.assertEqual(len(updates), 4)
        self.assertEqual(updates[-1], (1, True))
        self.assertTrue(provider.control_state_ready)
        self.assertTrue(provider.shadow_healthy)

    def test_buffered_live_presence_can_open_the_atomic_control_bootstrap(self) -> None:
        provider = self.provider()
        self.assertTrue(
            provider.ingest_control_bootstrap_final_current(
                {
                    provider.runtime_availability_topic: (
                        runtime_payload("online"),
                        1,
                        True,
                    ),
                    # A retained publish is delivered with retain=False to an already subscribed
                    # client. The subscriber buffers that flag with this connection generation.
                    provider.presence_topic: (self.presence(), 1, False),
                }
            )
        )
        provider.set_transport_ready(True)
        self.assertTrue(provider.control_alive)
        self.assertFalse(provider.control_state_ready)

    def test_reconnect_liveness_pair_cannot_reauthorize_old_semantic_fields(
        self,
    ) -> None:
        provider = self.provider()
        publications = {
            provider.state_topic: (self.state(), 1, True),
            provider.availability_topic: (self.availability(), 1, True),
            provider.runtime_availability_topic: (
                runtime_payload("online"),
                1,
                True,
            ),
            provider.presence_topic: (self.presence(), 1, True),
        }
        provider.ingest_retained_final_current(publications)
        provider.set_transport_ready(True)
        self.assertTrue(provider.control_state_ready)
        provider.ingest(
            provider.presence_topic,
            publications[provider.presence_topic][0],
            qos=1,
            retained=False,
        )
        self.assertTrue(provider.control_alive)

        provider.set_transport_ready(False)
        provider.ingest_control_bootstrap_final_current(
            {
                provider.runtime_availability_topic: publications[
                    provider.runtime_availability_topic
                ],
                provider.presence_topic: publications[provider.presence_topic],
            }
        )
        provider.set_transport_ready(True)
        self.assertFalse(provider.control_alive)
        provider.ingest(
            provider.presence_topic,
            publications[provider.presence_topic][0],
            qos=1,
            retained=False,
        )
        self.assertTrue(provider.control_alive)
        self.assertFalse(provider.control_state_ready)
        self.assertFalse(provider.shadow_healthy)

        provider.ingest_semantic_bootstrap_final_current(
            {
                provider.state_topic: publications[provider.state_topic],
                provider.availability_topic: publications[
                    provider.availability_topic
                ],
            }
        )
        self.assertTrue(provider.control_state_ready)

    def test_full_bootstrap_does_not_authorize_state_from_before_presence(self) -> None:
        provider = self.provider()
        presence = self.presence(observed_at="2026-08-13T01:00:00.000Z")
        provider.ingest_retained_final_current(
            {
                provider.state_topic: (self.state(), 1, True),
                provider.availability_topic: (self.availability(), 1, True),
                provider.runtime_availability_topic: (
                    runtime_payload("online"),
                    1,
                    True,
                ),
                provider.presence_topic: (
                    presence,
                    1,
                    True,
                ),
            }
        )
        provider.set_transport_ready(True)
        self.assertFalse(provider.control_alive)
        provider.ingest(
            provider.presence_topic, presence, qos=1, retained=False
        )
        self.assertTrue(provider.control_alive)
        self.assertFalse(provider.control_state_ready)

        provider.ingest(
            provider.state_topic,
            self.state(
                sequence=2,
                published_at="2026-08-13T01:00:01.000Z",
            ),
            qos=1,
            retained=False,
        )
        provider.ingest(
            provider.availability_topic,
            self.availability(
                state_sequence=2,
                observed_at="2026-08-13T01:00:01.000Z",
            ),
            qos=1,
            retained=False,
        )
        self.assertTrue(provider.control_state_ready)

    def test_liveness_first_then_old_optional_pair_stays_control_ineligible(
        self,
    ) -> None:
        provider = self.provider()
        presence = self.presence(observed_at="2026-08-13T01:00:00.000Z")
        provider.ingest_control_bootstrap_final_current(
            {
                provider.runtime_availability_topic: (
                    runtime_payload("online"),
                    1,
                    True,
                ),
                provider.presence_topic: (
                    presence,
                    1,
                    True,
                ),
            }
        )
        provider.set_transport_ready(True)
        self.assertFalse(provider.control_alive)
        provider.ingest(
            provider.presence_topic, presence, qos=1, retained=False
        )
        provider.ingest_semantic_bootstrap_final_current(
            {
                provider.state_topic: (self.state(), 1, True),
                provider.availability_topic: (self.availability(), 1, True),
            }
        )
        self.assertTrue(provider.control_alive)
        self.assertFalse(provider.control_state_ready)

    def test_presence_reconnect_edge_requires_a_post_edge_semantic_pair(self) -> None:
        provider = self.provider()
        provider.ingest(
            provider.presence_topic, self.presence(), qos=1, retained=False
        )
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online"),
            qos=1,
            retained=False,
        )
        provider.ingest(provider.state_topic, self.state(), qos=1, retained=False)
        provider.ingest(
            provider.availability_topic,
            self.availability(),
            qos=1,
            retained=False,
        )
        provider.set_transport_ready(True)
        self.assertTrue(provider.control_state_ready)

        provider.ingest(
            provider.presence_topic,
            self.presence(
                status="offline",
                sequence=2,
                observed_at="2026-08-13T01:00:00.000Z",
            ),
            qos=1,
            retained=False,
        )
        self.assertFalse(provider.control_alive)
        self.assertFalse(provider.control_state_ready)
        provider.ingest(
            provider.presence_topic,
            self.presence(
                sequence=3,
                observed_at="2026-08-13T01:00:01.000Z",
            ),
            qos=1,
            retained=False,
        )
        self.assertTrue(provider.control_alive)
        self.assertFalse(provider.control_state_ready)

        provider.ingest(
            provider.state_topic,
            self.state(
                sequence=2,
                published_at="2026-08-13T01:00:02.000Z",
            ),
            qos=1,
            retained=False,
        )
        provider.ingest(
            provider.availability_topic,
            self.availability(
                state_sequence=2,
                observed_at="2026-08-13T01:00:02.000Z",
            ),
            qos=1,
            retained=False,
        )
        self.assertTrue(provider.control_state_ready)

    def test_partial_state_after_presence_edge_does_not_reauthorize_old_tuple_fields(
        self,
    ) -> None:
        provider = self.provider()
        required = (
            "operation.mode",
            "fan.mode",
            "temperature.target_c",
        )
        provider.ingest(
            provider.presence_topic, self.presence(), qos=1, retained=False
        )
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online"),
            qos=1,
            retained=False,
        )
        provider.ingest(provider.state_topic, self.state(), qos=1, retained=False)
        provider.ingest(
            provider.availability_topic,
            self.availability(),
            qos=1,
            retained=False,
        )
        provider.set_transport_ready(True)
        self.assertTrue(provider.control_fields_ready(required))

        provider.ingest(
            provider.presence_topic,
            self.presence(
                status="offline",
                sequence=2,
                observed_at="2026-08-13T01:00:00.000Z",
            ),
            qos=1,
            retained=False,
        )
        provider.ingest(
            provider.presence_topic,
            self.presence(
                sequence=3,
                observed_at="2026-08-13T01:00:01.000Z",
            ),
            qos=1,
            retained=False,
        )
        provider.ingest(
            provider.state_topic,
            self.state(
                sequence=2,
                published_at="2026-08-13T01:00:02.000Z",
                tuple_observed_at={
                    "operation.mode": "2026-08-13T01:00:02.000Z",
                },
            ),
            qos=1,
            retained=False,
        )
        provider.ingest(
            provider.availability_topic,
            self.availability(
                state_sequence=2,
                observed_at="2026-08-13T01:00:02.000Z",
            ),
            qos=1,
            retained=False,
        )
        self.assertTrue(provider.control_alive)
        self.assertTrue(provider.control_state_ready)
        self.assertFalse(provider.control_fields_ready(required))

        post_edge = {
            semantic_id: "2026-08-13T01:00:02.000Z"
            for semantic_id in required
        }
        provider.ingest(
            provider.state_topic,
            self.state(
                sequence=3,
                published_at="2026-08-13T01:00:03.000Z",
                tuple_observed_at=post_edge,
            ),
            qos=1,
            retained=False,
        )
        provider.ingest(
            provider.availability_topic,
            self.availability(
                state_sequence=3,
                observed_at="2026-08-13T01:00:03.000Z",
            ),
            qos=1,
            retained=False,
        )
        self.assertTrue(provider.control_fields_ready(required))

    def test_runtime_edge_also_closes_composite_state(self) -> None:
        provider = self.provider()
        provider.ingest(
            provider.presence_topic, self.presence(), qos=1, retained=False
        )
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online"),
            qos=1,
            retained=False,
        )
        provider.ingest(provider.state_topic, self.state(), qos=1, retained=False)
        provider.ingest(
            provider.availability_topic,
            self.availability(),
            qos=1,
            retained=False,
        )
        provider.set_transport_ready(True)
        self.assertTrue(provider.control_state_ready)

        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload(
                "offline", observed_at="2026-08-13T01:00:01.000Z"
            ),
            qos=1,
            retained=False,
        )
        self.assertFalse(provider.control_alive)
        self.assertFalse(provider.control_state_ready)

    def test_semantic_bootstrap_generation_mismatch_is_atomic(self) -> None:
        provider = self.provider()
        provider.ingest_control_bootstrap_final_current(
            {
                provider.runtime_availability_topic: (
                    runtime_payload("online"),
                    1,
                    True,
                ),
                provider.presence_topic: (
                    self.presence(binding_generation=2),
                    1,
                    True,
                ),
            }
        )
        before = self.presence_state(provider)
        with self.assertRaises(local.LocalProviderContractError):
            provider.ingest_semantic_bootstrap_final_current(
                {
                    provider.state_topic: (self.state(), 1, True),
                    provider.availability_topic: (self.availability(), 1, True),
                }
            )
        self.assertEqual(self.presence_state(provider), before)
        self.assertIsNone(provider.session_id)

    def test_live_state_generation_mismatch_blocks_only_composite_state(self) -> None:
        provider = self.provider()
        provider.ingest(
            provider.presence_topic, self.presence(), qos=1, retained=False
        )
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online"),
            qos=1,
            retained=False,
        )
        provider.ingest(
            provider.state_topic,
            self.state(binding_generation=2),
            qos=1,
            retained=False,
        )
        provider.ingest(
            provider.availability_topic,
            self.availability(binding_generation=2),
            qos=1,
            retained=False,
        )
        provider.set_transport_ready(True)
        self.assertTrue(provider.control_alive)
        self.assertFalse(provider.control_state_ready)

        provider.ingest(
            provider.presence_topic,
            self.presence(binding_generation=2),
            qos=1,
            retained=False,
        )
        self.assertFalse(provider.control_state_ready)
        provider.ingest(
            provider.state_topic,
            self.state(sequence=2, binding_generation=2),
            qos=1,
            retained=False,
        )
        provider.ingest(
            provider.availability_topic,
            self.availability(state_sequence=2, binding_generation=2),
            qos=1,
            retained=False,
        )
        self.assertTrue(provider.control_state_ready)

    def test_exact_offline_state_marker_does_not_own_control_liveness(self) -> None:
        provider = self.provider()
        provider.ingest(
            provider.presence_topic, self.presence(), qos=1, retained=False
        )
        provider.ingest(
            provider.runtime_availability_topic,
            runtime_payload("online"),
            qos=1,
            retained=False,
        )
        provider.ingest(provider.state_topic, self.state(), qos=1, retained=False)
        provider.ingest(
            provider.availability_topic,
            self.availability(status="offline"),
            qos=1,
            retained=False,
        )
        provider.set_transport_ready(True)
        self.assertTrue(provider.control_alive)
        self.assertTrue(provider.control_state_ready)
        self.assertFalse(provider.shadow_healthy)


class CstLiveDecoderContractTests(unittest.TestCase):
    def test_auto_comfort_and_temperature_retraction_are_accepted_together(self):
        profiles = local.load_local_semantic_profile_catalogue()[1]
        for name in ('cst170-core-state-v1', 'cst570-core-state-v1'):
            with self.subTest(profile=name):
                profile = profiles[name]
                provider = local.LocalSemanticShadowProvider(BINDING_ID, profile, now=lambda: NOW)
                payload = {
                    'schema_version': 1, 'semantics_revision': 33,
                    'binding_id': BINDING_ID, 'model_id': profile.model_id,
                    'platform': 'thinq2', 'session_id': SESSION_ONE,
                    'sequence': 1, 'published_at': '2026-08-13T00:59:59.000Z',
                    'fields': {'comfort.preference_step': {
                        'value': 0, 'value_type': 'number', 'exposure': 'state',
                        'confidence': 'confirmed-exact-device-five-step-auto-comfort-preference-sweep',
                        'observed_at': '2026-08-13T00:59:58.000Z',
                    }},
                    'invalidated_fields': {'temperature.target_c': {
                        'observed_at': '2026-08-13T00:59:58.000Z',
                        'confidence': profile.fields['temperature.target_c'].confidence[0],
                    }},
                    'diagnostics': {'rejected_frames': 0,
                                    'unresolved_fields': 0, 'invalid_values': 0, 'unsupported_frames': 0},
                }
                provider.ingest(provider.state_topic, json.dumps(payload).encode(), qos=1, retained=False)
                self.assertEqual(provider.shadow_fields['comfort.preference_step'].value, 0)
                self.assertNotIn('temperature.target_c', provider.shadow_fields)


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
