"""Contract and cursor tests for the complete TLV read-only feed."""

from __future__ import annotations

import asyncio
import hashlib
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
    / "local_read_provider.py"
)
SPEC = importlib.util.spec_from_file_location(
    "my_lg_local_read_provider_test", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
read = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = read
SPEC.loader.exec_module(read)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
BINDING_ID = "pilot_full_read_provider_001"
PAT_DEVICE_ID = "pat-full-read-device-001"
MODEL_ID = "DHUM_056905_WW"
PUBLICATION_SESSION_ID = "1" * 32
SOURCE_SESSION_ID = "source_session_001"
PROOF = "a" * 64


class FakePrimaryProvider:
    def __init__(self) -> None:
        self.binding_id = BINDING_ID
        self.model_id = MODEL_ID
        self.platform = "thinq2"
        self.expected_proof = PROOF
        self.binding_generation = 7
        self.cohort_generation = 11
        self.session_id = PUBLICATION_SESSION_ID
        self.authority_binding_generation = 7
        self.authority_session_id = PUBLICATION_SESSION_ID
        self.shadow_healthy = True
        self.transport_ready = True
        self.control_alive = True
        self._listeners = []

    @property
    def read_publication_authority(self):
        if not self.transport_ready or not self.control_alive:
            return None
        return self.authority_binding_generation, self.authority_session_id

    def async_add_listener(self, callback):
        self._listeners.append(callback)

        def remove():
            self._listeners.remove(callback)

        return remove

    def notify(self) -> None:
        for callback in tuple(self._listeners):
            callback()


def field_contract(
    semantic_id: str,
    domain: str,
    value_types: tuple[str, ...],
    exposure: str = "state",
    *,
    unit: str | None = None,
    publication_mode: str = "retained-current",
    enabled_by_default: bool = True,
    event_type: str | None = None,
):
    return read.TlvReadFieldContract(
        descriptor_key=f"{MODEL_ID}|{semantic_id}",
        semantic_id=semantic_id,
        domain=domain,
        value_types=value_types,
        exposure=exposure,
        unit=unit,
        label_ko=f"테스트 {semantic_id}",
        entity_category="diagnostic" if exposure == "diagnostic" else None,
        enabled_by_default=enabled_by_default,
        publication_mode=publication_mode,
        event_type=event_type,
    )


def profile(*, semantics_revision=31):
    fields = (
        field_contract("operation.power_requested", "binary_sensor", ("boolean",)),
        field_contract("humidity.current_pct", "sensor", ("number",), unit="%"),
        field_contract("fan.mode", "sensor", ("number", "string")),
        field_contract(
            "diagnostic.raw",
            "sensor",
            ("string",),
            "diagnostic",
            enabled_by_default=False,
        ),
        field_contract(
            "event.water_tank.changed",
            "event",
            ("string",),
            "event",
            publication_mode="transient-event",
            enabled_by_default=False,
            event_type="water-tank state changed",
        ),
        field_contract(
            "energy.interval.delta_wh",
            "event",
            ("number",),
            "event",
            unit="Wh",
            publication_mode="transient-event",
            enabled_by_default=False,
            event_type="observed",
        ),
    )
    return read.TlvReadProfile(
        profile_id=f"{MODEL_ID}:read-sensors-v1",
        contract_revision=1,
        profile_revision="tlv-read-sensor-profiles-v1:test",
        profile_sha256="b" * 64,
        read_entity_contract_revision="tlv-read-entities-v1:test",
        read_entity_contract_sha256="c" * 64,
        catalog_sha256="d" * 64,
        semantics_revision=semantics_revision,
        model_id=MODEL_ID,
        platform="thinq2",
        fields=fields,
    )


def snapshot_field(value, value_type: str, exposure: str = "state", *, unit=None):
    result = {
        "value": value,
        "value_type": value_type,
        "observed_at": "2026-08-23T11:59:58.000Z",
        "confidence": "confirmed-test",
        "exposure": exposure,
    }
    if unit is not None:
        result["unit"] = unit
    return result


def envelope(
    *,
    sequence=1,
    source_session_id=SOURCE_SESSION_ID,
    fields=None,
    contract=None,
    **overrides,
):
    contract = profile() if contract is None else contract
    value = {
        "schema_version": 2,
        "publication_plan_revision": 2,
        "profile_id": contract.profile_id,
        "profile_contract_revision": contract.contract_revision,
        "profile_revision": contract.profile_revision,
        "profile_sha256": contract.profile_sha256,
        "read_entity_contract_revision": contract.read_entity_contract_revision,
        "read_entity_contract_sha256": contract.read_entity_contract_sha256,
        "catalog_sha256": contract.catalog_sha256,
        "semantics_revision": contract.semantics_revision,
        "binding_id": BINDING_ID,
        "model_id": MODEL_ID,
        "platform": "thinq2",
        "binding_generation": 7,
        "pat_device_id_proof_sha256": PROOF,
        "publication_session_id": PUBLICATION_SESSION_ID,
        "cohort_generation": 11,
        "source_session_id": source_session_id,
        "sequence": sequence,
        "published_at": "2026-08-23T11:59:59.000Z",
        "fields": fields
        if fields is not None
        else {
            "operation.power_requested": snapshot_field(True, "boolean"),
            "humidity.current_pct": snapshot_field(55, "number", unit="%"),
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


def event_envelope(
    *,
    sequence=1,
    semantic_id="event.water_tank.changed",
    value="water-tank state changed",
    value_type="string",
    unit=None,
    **overrides,
):
    current = json.loads(envelope(sequence=sequence))
    for key in ("fields", "invalidated_fields", "diagnostics"):
        current.pop(key, None)
    current.update(
        {
            "descriptor_key": f"{MODEL_ID}|{semantic_id}",
            "semantic_id": semantic_id,
            "event_type": (
                "observed"
                if semantic_id == "energy.interval.delta_wh"
                else "water-tank state changed"
            ),
            "field": snapshot_field(value, value_type, "event", unit=unit),
        }
    )
    current.update(overrides)
    return json.dumps(current, separators=(",", ":")).encode()


def per_model_authority(*, contract=None, semantics_revision=None):
    contract = profile() if contract is None else contract
    return read.TlvReadPerModelAuthority(
        profile_id=contract.profile_id,
        model_id=contract.model_id,
        platform=contract.platform,
        semantics_revision=(
            contract.semantics_revision
            if semantics_revision is None
            else semantics_revision
        ),
        model_contract_sha256="e" * 64,
        feed_schema_version=read.PER_MODEL_TLV_READ_SCHEMA_VERSION,
        publication_plan_revision=read.TLV_READ_PUBLICATION_PLAN_REVISION,
        static_contract_projection_version=(
            read.READ_STATIC_CONTRACT_PROJECTION_VERSION
        ),
    )


def v2_envelope(
    *,
    sequence=1,
    source_session_id=SOURCE_SESSION_ID,
    fields=None,
    authority=None,
    **overrides,
):
    authority = per_model_authority() if authority is None else authority
    value = {
        "schema_version": authority.feed_schema_version,
        "publication_plan_revision": authority.publication_plan_revision,
        "static_contract_projection_version": (
            authority.static_contract_projection_version
        ),
        "profile_id": authority.profile_id,
        "model_contract_sha256": authority.model_contract_sha256,
        "semantics_revision": authority.semantics_revision,
        "binding_id": BINDING_ID,
        "model_id": MODEL_ID,
        "platform": "thinq2",
        "binding_generation": 7,
        "pat_device_id_proof_sha256": PROOF,
        "publication_session_id": PUBLICATION_SESSION_ID,
        "cohort_generation": 11,
        "source_session_id": source_session_id,
        "sequence": sequence,
        "published_at": "2026-08-23T11:59:59.000Z",
        "fields": fields
        if fields is not None
        else {
            "operation.power_requested": snapshot_field(True, "boolean"),
            "humidity.current_pct": snapshot_field(55, "number", unit="%"),
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


def v2_event_envelope(*, sequence=1, **overrides):
    current = json.loads(v2_envelope(sequence=sequence))
    current.pop("fields")
    current.pop("diagnostics")
    current.update(
        {
            "descriptor_key": f"{MODEL_ID}|event.water_tank.changed",
            "semantic_id": "event.water_tank.changed",
            "event_type": "water-tank state changed",
            "field": snapshot_field(
                "water-tank state changed", "string", "event"
            ),
        }
    )
    current.update(overrides)
    return json.dumps(current, separators=(",", ":")).encode()


def consumer_pin(payload: bytes, projection_version: int):
    value = json.loads(payload)
    return read.TlvReadConsumerPin(
        projection_version=projection_version,
        static_read_contract_sha256=(
            read.tlv_read_publication_static_contract_sha256(
                value, projection_version
            )
        ),
        model_contract_sha256=(
            None if projection_version == 1 else value["model_contract_sha256"]
        ),
    )


def consumer_state(*pins, adopted_pin=None):
    accepted = tuple(pins)
    adopted = accepted[0] if adopted_pin is None else adopted_pin
    pin_set = read.build_tlv_read_consumer_pin_set(BINDING_ID, accepted)
    return read.build_tlv_read_consumer_binding_state(
        binding_id=BINDING_ID,
        pat_device_id_proof_sha256=PROOF,
        adopted_pin=adopted,
        consumer_pin_set=pin_set,
    )


class TlvReadFieldContractTests(unittest.TestCase):
    def test_domain_type_exposure_matrix_is_exact(self) -> None:
        bad = (
            ("binary_sensor", ("number",), "state", "retained-current"),
            ("sensor", ("boolean",), "state", "retained-current"),
            ("event", ("boolean",), "event", "transient-event"),
            ("event", ("string",), "state", "transient-event"),
            ("sensor", ("string",), "event", "retained-current"),
            ("sensor", ("number", "number"), "state", "retained-current"),
        )
        for domain, value_types, exposure, mode in bad:
            with self.subTest(
                domain=domain, value_types=value_types, exposure=exposure
            ):
                with self.assertRaises(ValueError):
                    field_contract(
                        "bad.field",
                        domain,
                        value_types,
                        exposure,
                        publication_mode=mode,
                    )

        number_event = field_contract(
            "energy.interval.delta_wh",
            "event",
            ("number",),
            "event",
            unit="Wh",
            publication_mode="transient-event",
            enabled_by_default=False,
            event_type="observed",
        )
        self.assertEqual(number_event.event_type, "observed")
        for event_type in (None, "changed", "water-tank state changed"):
            with self.subTest(event_type=event_type), self.assertRaises(ValueError):
                field_contract(
                    "energy.interval.delta_wh",
                    "event",
                    ("number",),
                    "event",
                    unit="Wh",
                    publication_mode="transient-event",
                    enabled_by_default=False,
                    event_type=event_type,
                )

    def test_profile_rejects_duplicate_descriptor_and_semantic_ids(self) -> None:
        duplicate = field_contract("humidity.current_pct", "sensor", ("number",))
        with self.assertRaises(ValueError):
            read.TlvReadProfile(
                profile_id=f"{MODEL_ID}:read-sensors-v1",
                contract_revision=1,
                profile_revision="tlv-read-sensor-profiles-v1:test",
                profile_sha256="b" * 64,
                read_entity_contract_revision="tlv-read-entities-v1:test",
                read_entity_contract_sha256="c" * 64,
                catalog_sha256="d" * 64,
                semantics_revision=31,
                model_id=MODEL_ID,
                platform="thinq2",
                fields=(duplicate, duplicate),
            )


class TlvReadShadowProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.primary = FakePrimaryProvider()
        self.provider = read.TlvReadShadowProvider(
            BINDING_ID,
            PAT_DEVICE_ID,
            profile(),
            self.primary,
            allow_legacy_v1_fallback=True,
            now=lambda: NOW,
        )
        self.provider.set_transport_ready(True)

    def tearDown(self) -> None:
        self.provider.close()

    def ingest_current(self, payload=None, *, retained=True):
        return self.provider.ingest(
            self.provider.current_topic,
            envelope() if payload is None else payload,
            qos=1,
            retained=retained,
        )

    def test_primary_binding_model_and_platform_must_match_exactly(self) -> None:
        for attribute, value in (
            ("binding_id", "pilot_foreign_provider_001"),
            ("model_id", "FOREIGN_MODEL"),
            ("platform", "thinq1"),
        ):
            primary = FakePrimaryProvider()
            setattr(primary, attribute, value)
            with self.subTest(attribute=attribute), self.assertRaises(ValueError):
                read.TlvReadShadowProvider(
                    BINDING_ID,
                    PAT_DEVICE_ID,
                    profile(),
                    primary,
                    now=lambda: NOW,
                )
        with self.assertRaises(ValueError):
            read.TlvReadShadowProvider(
                "shadow-forbidden_binding_001",
                PAT_DEVICE_ID,
                profile(),
                self.primary,
                now=lambda: NOW,
            )

    def test_current_is_exactly_cross_fenced_and_replaces_absent_fields(self) -> None:
        self.assertTrue(self.ingest_current())
        self.assertEqual(self.provider.field_value("humidity.current_pct"), 55)
        self.assertTrue(self.provider.field_available("humidity.current_pct"))

        replacement = envelope(
            sequence=2,
            fields={"operation.power_requested": snapshot_field(False, "boolean")},
        )
        self.assertTrue(self.ingest_current(replacement, retained=True))
        self.assertIsNone(self.provider.field_value("humidity.current_pct"))
        self.assertFalse(self.provider.field_available("humidity.current_pct"))

        self.primary.control_alive = False
        self.primary.notify()
        self.assertFalse(self.provider.field_available("operation.power_requested"))

    def test_presence_only_authority_accepts_an_independent_read_cohort(self) -> None:
        self.primary.binding_generation = None
        self.primary.cohort_generation = None
        self.primary.session_id = None

        self.assertTrue(self.ingest_current(envelope(cohort_generation=11)))
        self.assertTrue(self.provider.field_available("humidity.current_pct"))

        # A pilot state can legitimately lag sensor-only reducer cohorts.
        self.primary.binding_generation = 7
        self.primary.cohort_generation = 10
        self.primary.session_id = PUBLICATION_SESSION_ID
        self.primary.notify()
        self.assertTrue(self.provider.field_available("humidity.current_pct"))

        # Once semantic state advances beyond the read cursor, that cursor is
        # unavailable until the next independently monotonic read snapshot.
        self.primary.cohort_generation = 12
        self.primary.notify()
        self.assertFalse(self.provider.field_available("humidity.current_pct"))
        with self.assertRaises(read.TlvReadProviderContractError):
            self.ingest_current(envelope(sequence=2, cohort_generation=11))
        self.assertTrue(self.ingest_current(envelope(sequence=2, cohort_generation=12)))
        self.assertTrue(self.provider.field_available("humidity.current_pct"))

    def test_live_presence_rejects_a_foreign_generation(self) -> None:
        with self.assertRaises(read.TlvReadProviderContractError):
            self.ingest_current(envelope(binding_generation=8))
        self.assertEqual(dict(self.provider.fields), {})

    def test_current_cannot_commit_before_subscription_transport_is_ready(self) -> None:
        self.provider.set_transport_ready(False)
        with self.assertRaises(read.TlvReadProviderContractError):
            self.ingest_current(envelope(), retained=True)
        self.assertEqual(dict(self.provider.fields), {})
        self.provider.set_transport_ready(True)
        self.assertTrue(self.ingest_current(envelope(), retained=False))

    def test_exact_retained_current_commits_without_live_presence_but_stays_unavailable(
        self,
    ) -> None:
        self.primary.control_alive = False
        self.primary.binding_generation = None
        self.primary.cohort_generation = None
        self.primary.session_id = None
        current = envelope(cohort_generation=1, sequence=1)
        self.assertTrue(self.ingest_current(current))
        self.assertEqual(self.provider.field_value("humidity.current_pct"), 55)
        self.assertFalse(self.provider.field_available("humidity.current_pct"))

        self.primary.binding_generation = 7
        self.primary.cohort_generation = 1
        self.primary.session_id = PUBLICATION_SESSION_ID
        self.primary.control_alive = True
        self.primary.notify()
        self.assertTrue(self.provider.field_available("humidity.current_pct"))

    def test_retained_current_before_presence_does_not_fence_fresh_recovery(
        self,
    ) -> None:
        self.primary.control_alive = False
        self.primary.binding_generation = None
        self.primary.cohort_generation = None
        self.primary.session_id = None
        old = envelope(cohort_generation=99, sequence=1)
        self.assertTrue(self.ingest_current(old))
        self.assertEqual(self.provider._outer_high_water, None)
        self.assertFalse(self.provider.field_available("humidity.current_pct"))

        recovered_session = "2" * 32
        self.primary.authority_session_id = recovered_session
        self.primary.binding_generation = 7
        self.primary.cohort_generation = 2
        self.primary.session_id = recovered_session
        self.primary.control_alive = True
        self.primary.notify()
        recovered = envelope(
            cohort_generation=2,
            sequence=1,
            publication_session_id=recovered_session,
            source_session_id="source_session_recovered_001",
        )
        self.assertTrue(self.ingest_current(recovered, retained=False))
        self.assertEqual(self.provider._outer_high_water, (7, 2))
        self.assertTrue(self.provider.field_available("humidity.current_pct"))

    def test_live_authority_still_fences_a_regressed_current(self) -> None:
        self.primary.cohort_generation = 99
        self.assertTrue(self.ingest_current(envelope(cohort_generation=99)))
        self.assertEqual(self.provider._outer_high_water, (7, 99))
        with self.assertRaises(read.TlvReadProviderContractError):
            self.ingest_current(
                envelope(cohort_generation=2, sequence=2), retained=False
            )

    def test_prior_session_retained_current_is_visible_but_not_live(self) -> None:
        prior = envelope(cohort_generation=11, sequence=1)
        self.primary.authority_session_id = "2" * 32
        self.primary.session_id = "2" * 32
        self.assertTrue(self.ingest_current(prior))
        self.assertEqual(self.provider.field_value("humidity.current_pct"), 55)
        self.assertFalse(self.provider.field_available("humidity.current_pct"))

        current = envelope(
            cohort_generation=12,
            sequence=1,
            publication_session_id="2" * 32,
            source_session_id="source_session_002",
        )
        self.primary.cohort_generation = 12
        self.assertTrue(self.ingest_current(current))
        self.assertTrue(self.provider.field_available("humidity.current_pct"))

    def test_prior_session_retained_current_hands_off_within_the_same_cohort(
        self,
    ) -> None:
        prior = envelope(cohort_generation=11, sequence=1)
        self.primary.control_alive = False
        self.assertTrue(self.ingest_current(prior))
        self.assertEqual(self.provider._outer_high_water, None)

        current_session = "2" * 32
        self.primary.authority_session_id = current_session
        self.primary.session_id = current_session
        self.primary.control_alive = True
        self.primary.notify()
        current = envelope(
            cohort_generation=11,
            sequence=1,
            publication_session_id=current_session,
            source_session_id="source_session_same_cohort_002",
        )
        self.assertTrue(self.ingest_current(current, retained=False))
        self.assertEqual(self.provider._outer_high_water, (7, 11))
        self.assertTrue(self.provider.field_available("humidity.current_pct"))

    def test_prior_session_retained_current_cannot_regress_live_primary_cohort(
        self,
    ) -> None:
        self.primary.authority_session_id = "2" * 32
        self.primary.session_id = "2" * 32
        self.primary.cohort_generation = 12
        with self.assertRaises(read.TlvReadProviderContractError):
            self.ingest_current(envelope(cohort_generation=11, sequence=1))

    def test_tombstone_retires_an_offline_cursor_before_presence_arrives(
        self,
    ) -> None:
        self.primary.control_alive = False
        future = envelope(binding_generation=8, cohort_generation=1, sequence=1)
        self.assertTrue(self.ingest_current(future, retained=False))
        self.assertFalse(self.provider.field_available("humidity.current_pct"))
        self.assertTrue(
            self.provider.ingest(
                self.provider.current_topic, b"", qos=1, retained=False
            )
        )
        self.primary.authority_binding_generation = 8
        self.primary.binding_generation = 8
        self.primary.cohort_generation = 1
        self.primary.control_alive = True
        self.primary.notify()
        with self.assertRaises(read.TlvReadProviderContractError):
            self.ingest_current(future, retained=True)
        self.assertIsNone(self.provider.field_value("humidity.current_pct"))

    def test_publication_service_identity_is_not_the_source_session(self) -> None:
        self.assertNotEqual(self.primary.session_id, SOURCE_SESSION_ID)
        self.assertTrue(self.ingest_current())
        self.primary.authority_session_id = "2" * 32
        self.primary.notify()
        self.assertFalse(self.provider.field_available("humidity.current_pct"))
        self.assertTrue(self.ingest_current(envelope(sequence=2)))
        self.assertEqual(self.provider.field_value("humidity.current_pct"), 55)
        self.assertFalse(self.provider.field_available("humidity.current_pct"))

    def test_every_pin_and_field_contract_is_fail_closed(self) -> None:
        mutations = {
            "schema_version": 1,
            "publication_plan_revision": 1,
            "profile_revision": "wrong",
            "profile_sha256": "0" * 64,
            "read_entity_contract_revision": "wrong",
            "read_entity_contract_sha256": "0" * 64,
            "catalog_sha256": "0" * 64,
            "semantics_revision": 32,
            "binding_id": "foreign_binding_0001",
            "model_id": "OTHER_MODEL",
            "platform": "thinq1",
            "pat_device_id_proof_sha256": "0" * 64,
        }
        for key, value in mutations.items():
            with self.subTest(key=key):
                with self.assertRaises(read.TlvReadProviderContractError):
                    self.ingest_current(envelope(**{key: value}))

        bad_fields = (
            {"humidity.current_pct": snapshot_field(True, "boolean", unit="%")},
            {"humidity.current_pct": snapshot_field(55, "number")},
            {
                "operation.power_requested": {
                    **snapshot_field(True, "boolean"),
                    "unit": None,
                }
            },
            {"unknown.field": snapshot_field("x", "string")},
            {"event.water_tank.changed": snapshot_field("full", "string", "event")},
        )
        for fields in bad_fields:
            with self.assertRaises(read.TlvReadProviderContractError):
                self.ingest_current(envelope(fields=fields))

    def test_cursor_collision_regression_new_session_and_tombstone(self) -> None:
        payload = envelope()
        self.assertTrue(self.ingest_current(payload))
        self.assertFalse(self.ingest_current(payload))
        with self.assertRaises(read.TlvReadProviderContractError):
            self.ingest_current(envelope(fields={}))
        with self.assertRaises(read.TlvReadProviderContractError):
            self.ingest_current(envelope(sequence=0))

        with self.assertRaises(read.TlvReadProviderContractError):
            self.ingest_current(
                envelope(sequence=1, source_session_id="source_session_002")
            )

        self.assertTrue(
            self.provider.ingest(
                self.provider.current_topic, b"", qos=1, retained=False
            )
        )
        self.assertEqual(dict(self.provider.fields), {})
        with self.assertRaises(read.TlvReadProviderContractError):
            self.ingest_current(payload)

        # A physical reconnect can continue the same source monotonically after
        # the tombstone; only the removed cursor and older replays stay fenced.
        self.assertTrue(self.ingest_current(envelope(sequence=2)))
        self.assertTrue(
            self.provider.ingest(self.provider.current_topic, b"", qos=1, retained=True)
        )
        self.assertTrue(
            self.ingest_current(
                envelope(sequence=1, source_session_id="source_session_002")
            )
        )
        with self.assertRaises(read.TlvReadProviderContractError):
            self.ingest_current(envelope(sequence=3))

        # A restarted V3 publisher can also move service identity without a
        # cohort bump once the old retained current has been removed.
        self.assertTrue(
            self.provider.ingest(
                self.provider.current_topic, b"", qos=1, retained=False
            )
        )
        self.primary.authority_session_id = "3" * 32
        self.primary.session_id = "3" * 32
        self.primary.notify()
        self.assertTrue(
            self.ingest_current(
                envelope(
                    sequence=1,
                    source_session_id="source_session_003",
                    publication_session_id="3" * 32,
                )
            )
        )

    def test_more_than_ten_thousand_cohorts_prune_only_superseded_history(
        self,
    ) -> None:
        final_cohort = 11
        final_sequence = 1
        for offset in range(read.MAX_CURSOR_HISTORY + 1):
            final_cohort = 11 + offset
            final_sequence = offset + 1
            self.assertTrue(
                self.ingest_current(
                    envelope(
                        cohort_generation=final_cohort,
                        sequence=final_sequence,
                    )
                )
            )

        self.assertLessEqual(len(self.provider._publication_sources), 1)
        self.assertLessEqual(len(self.provider._source_sequence_high_water), 1)
        self.assertEqual(len(self.provider._event_high_water), 0)
        self.assertEqual(len(self.provider._tombstoned_current_cursors), 0)

        with self.assertRaises(read.TlvReadProviderContractError):
            self.ingest_current(envelope(cohort_generation=11, sequence=99_999))
        with self.assertRaises(read.TlvReadProviderContractError):
            self.ingest_current(
                envelope(
                    cohort_generation=final_cohort,
                    sequence=final_sequence,
                    fields={
                        "operation.power_requested": snapshot_field(False, "boolean")
                    },
                )
            )
        with self.assertRaises(read.TlvReadProviderContractError):
            self.ingest_current(
                envelope(
                    cohort_generation=final_cohort,
                    sequence=final_sequence + 1,
                    source_session_id="source_session_002",
                )
            )

    def test_event_is_nonretained_exactly_once_and_cannot_advance_current(self) -> None:
        events = []
        remove = self.provider.async_add_event_listener(events.append)
        try:
            self.assertTrue(self.ingest_current())
            self.assertTrue(
                self.provider.ingest(
                    self.provider.event_topic,
                    event_envelope(),
                    qos=1,
                    retained=False,
                )
            )
            self.assertFalse(
                self.provider.ingest(
                    self.provider.event_topic,
                    event_envelope(),
                    qos=1,
                    retained=False,
                )
            )
            self.assertTrue(
                self.provider.ingest(
                    self.provider.event_topic,
                    event_envelope(
                        semantic_id="energy.interval.delta_wh",
                        value=12.5,
                        value_type="number",
                        unit="Wh",
                    ),
                    qos=1,
                    retained=False,
                )
            )
            self.assertEqual(
                [event.event_type for event in events],
                ["water-tank state changed", "observed"],
            )
            self.assertEqual(
                [(event.value, event.value_type, event.unit) for event in events],
                [
                    ("water-tank state changed", "string", None),
                    (12.5, "number", "Wh"),
                ],
            )
            with self.assertRaises(read.TlvReadProviderContractError):
                self.provider.ingest(
                    self.provider.event_topic,
                    event_envelope(value="not-authorized"),
                    qos=1,
                    retained=False,
                )
            with self.assertRaises(read.TlvReadProviderContractError):
                self.provider.ingest(
                    self.provider.event_topic,
                    event_envelope(sequence=2),
                    qos=1,
                    retained=True,
                )
            self.assertEqual(self.provider.current_sequence, 1)
        finally:
            remove()

    def test_numeric_event_value_and_envelope_shape_are_fail_closed(self) -> None:
        self.assertTrue(self.ingest_current())
        valid = json.loads(
            event_envelope(
                semantic_id="energy.interval.delta_wh",
                value=12,
                value_type="number",
                unit="Wh",
            )
        )
        invalid = []
        for bad_value, bad_type in (
            (True, "boolean"),
            ("12", "string"),
            (None, "number"),
        ):
            candidate = json.loads(json.dumps(valid))
            candidate["field"]["value"] = bad_value
            candidate["field"]["value_type"] = bad_type
            invalid.append(candidate)
        candidate = json.loads(json.dumps(valid))
        candidate["eventType"] = "observed"
        invalid.append(candidate)
        candidate = json.loads(json.dumps(valid))
        del candidate["event_type"]
        invalid.append(candidate)
        candidate = json.loads(json.dumps(valid))
        candidate["event_type"] = "water-tank state changed"
        invalid.append(candidate)

        for index, candidate in enumerate(invalid):
            with (
                self.subTest(index=index),
                self.assertRaises(read.TlvReadProviderContractError),
            ):
                self.provider.ingest(
                    self.provider.event_topic,
                    json.dumps(candidate, separators=(",", ":")).encode(),
                    qos=1,
                    retained=False,
                )

        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            payload = event_envelope(
                semantic_id="energy.interval.delta_wh",
                value=12,
                value_type="number",
                unit="Wh",
            ).replace(b'"value":12', b'"value":' + constant, 1)
            with (
                self.subTest(constant=constant),
                self.assertRaises(read.TlvReadProviderContractError),
            ):
                self.provider.ingest(
                    self.provider.event_topic,
                    payload,
                    qos=1,
                    retained=False,
                )

    def test_event_only_plan_is_rejected_without_an_exact_accepted_current(
        self,
    ) -> None:
        events = []
        remove = self.provider.async_add_event_listener(events.append)
        try:
            self.assertFalse(self.provider.event_available)
            with self.assertRaises(read.TlvReadProviderContractError):
                self.provider.ingest(
                    self.provider.event_topic,
                    event_envelope(sequence=4),
                    qos=1,
                    retained=False,
                )
            self.assertTrue(self.ingest_current(envelope(sequence=4)))
            self.assertTrue(self.provider.event_available)
            self.assertTrue(
                self.provider.ingest(
                    self.provider.event_topic,
                    event_envelope(sequence=4),
                    qos=1,
                    retained=False,
                )
            )
            self.assertEqual(
                set(self.provider.fields),
                {"operation.power_requested", "humidity.current_pct"},
            )
            self.assertEqual(self.provider.current_sequence, 4)
            with self.assertRaises(read.TlvReadProviderContractError):
                self.provider.ingest(
                    self.provider.event_topic,
                    event_envelope(sequence=3),
                    qos=1,
                    retained=False,
                )
            with self.assertRaises(read.TlvReadProviderContractError):
                self.provider.ingest(
                    self.provider.event_topic,
                    event_envelope(sequence=5, source_session_id="source_session_002"),
                    qos=1,
                    retained=False,
                )
            self.assertEqual([event.sequence for event in events], [4])
        finally:
            remove()

    def test_64_kib_boundary_and_duplicate_json_keys_are_rejected(self) -> None:
        base = envelope()
        exact = (
            base[:-1] + b" " * (read.MAX_TLV_READ_PAYLOAD_BYTES - len(base)) + base[-1:]
        )
        self.assertEqual(len(exact), read.MAX_TLV_READ_PAYLOAD_BYTES)
        self.assertTrue(self.ingest_current(exact))
        with self.assertRaises(read.TlvReadProviderContractError):
            self.ingest_current(b"{" + b" " * read.MAX_TLV_READ_PAYLOAD_BYTES + b"}")
        duplicate = envelope().replace(
            b'"schema_version":2', b'"schema_version":2,"schema_version":2', 1
        )
        with self.assertRaises(read.TlvReadProviderContractError):
            self.ingest_current(duplicate)
        deeply_nested = b'{"nested":' + b"[" * 1500 + b"0" + b"]" * 1500 + b"}"
        with self.assertRaises(read.TlvReadProviderContractError):
            self.ingest_current(deeply_nested)

    def test_event_payload_has_its_stricter_16_kib_exact_boundary(self) -> None:
        self.assertTrue(self.ingest_current())
        base = event_envelope()
        exact = (
            base[:-1]
            + b" " * (read.MAX_TLV_READ_EVENT_PAYLOAD_BYTES - len(base))
            + base[-1:]
        )
        self.assertEqual(len(exact), read.MAX_TLV_READ_EVENT_PAYLOAD_BYTES)
        self.assertTrue(
            self.provider.ingest(
                self.provider.event_topic,
                exact,
                qos=1,
                retained=False,
            )
        )
        with self.assertRaises(read.TlvReadProviderContractError):
            self.provider.ingest(
                self.provider.event_topic,
                exact[:-1] + b" " + exact[-1:],
                qos=1,
                retained=False,
            )

    def test_diagnostics_and_current_delivery_flags_are_exact(self) -> None:
        value = json.loads(envelope())
        value.pop("diagnostics")
        with self.assertRaises(read.TlvReadProviderContractError):
            self.ingest_current(json.dumps(value).encode())
        value = json.loads(envelope())
        value["diagnostics"] = None
        with self.assertRaises(read.TlvReadProviderContractError):
            self.ingest_current(json.dumps(value).encode())
        self.assertTrue(self.ingest_current(envelope(), retained=False))
        self.assertFalse(self.ingest_current(envelope(), retained=True))
        self.assertTrue(
            self.provider.ingest(
                self.provider.current_topic, b"", qos=1, retained=False
            )
        )
        self.assertFalse(
            self.provider.ingest(self.provider.current_topic, b"", qos=1, retained=True)
        )
        with self.assertRaises(read.TlvReadProviderContractError):
            self.provider.ingest(
                self.provider.current_topic,
                envelope(sequence=2),
                qos=0,
                retained=False,
            )
        with self.assertRaises(read.TlvReadProviderContractError):
            self.provider.ingest(
                self.provider.current_topic,
                envelope(sequence=2),
                qos=True,
                retained=False,
            )

    def test_schema_bounds_and_observation_times_mirror_the_source_validator(
        self,
    ) -> None:
        bad_values = []

        value = json.loads(envelope())
        value["source_session_id"] = "not.allowed"
        bad_values.append(value)

        value = json.loads(
            envelope(fields={"fan.mode": snapshot_field("x" * 129, "string")})
        )
        bad_values.append(value)

        value = json.loads(
            envelope(fields={"fan.mode": snapshot_field("😀" * 65, "string")})
        )
        bad_values.append(value)

        value = json.loads(
            envelope(fields={"fan.mode": snapshot_field(10**1000, "number")})
        )
        bad_values.append(value)

        value = json.loads(envelope())
        value["fields"]["humidity.current_pct"]["observed_at"] = (
            "2026-08-23T12:00:00.000Z"
        )
        bad_values.append(value)

        value = json.loads(envelope(fields={}))
        bad_values.append(value)

        value = json.loads(envelope())
        value["invalidated_fields"] = {}
        bad_values.append(value)

        value = json.loads(envelope(fields={}))
        value["invalidated_fields"] = {
            "humidity.current_pct": {
                "observed_at": "2026-08-23T12:00:00.000Z",
                "confidence": "confirmed-test",
            }
        }
        bad_values.append(value)

        for index, value in enumerate(bad_values):
            with self.subTest(index=index):
                with self.assertRaises(read.TlvReadProviderContractError):
                    self.ingest_current(
                        json.dumps(value, separators=(",", ":")).encode()
                    )


class TlvReadProjectionTransitionTests(unittest.TestCase):
    def provider(self, *pins, adopted_pin=None):
        primary = FakePrimaryProvider()
        provider = read.TlvReadShadowProvider(
            BINDING_ID,
            PAT_DEVICE_ID,
            profile(),
            primary,
            model_authority=per_model_authority(),
            consumer_state=consumer_state(
                *pins, adopted_pin=adopted_pin
            ),
            now=lambda: NOW,
        )
        provider.set_transport_ready(True)
        return provider

    def ingest_current(self, provider, payload):
        return provider.ingest(
            provider.current_topic, payload, qos=1, retained=False
        )

    def test_v1_profile_and_v2_model_revisions_are_independently_fenced(
        self,
    ) -> None:
        contract = profile(semantics_revision=33)
        model_authority = per_model_authority(
            contract=contract, semantics_revision=32
        )
        binding_authority = read.TlvReadConsumerBindingAuthority(
            binding_id=BINDING_ID,
            pat_device_id_proof_sha256=PROOF,
            profile=contract,
            model_authority=model_authority,
        )
        v1 = envelope(contract=contract)
        v2 = v2_envelope(authority=model_authority)
        v1_pin = consumer_pin(v1, 1)
        v2_pin = consumer_pin(v2, 2)
        self.assertEqual(binding_authority.pin_for_projection(1, 7), v1_pin)
        self.assertEqual(binding_authority.pin_for_projection(2, 7), v2_pin)

        primary = FakePrimaryProvider()
        provider = read.TlvReadShadowProvider(
            BINDING_ID,
            PAT_DEVICE_ID,
            contract,
            primary,
            model_authority=model_authority,
            consumer_state=consumer_state(v2_pin),
            now=lambda: NOW,
        )
        provider.set_transport_ready(True)
        try:
            self.assertTrue(self.ingest_current(provider, v2))
            global_revision_v2 = json.loads(v2)
            global_revision_v2["semantics_revision"] = 33
            with self.assertRaises(read.TlvReadProviderContractError):
                self.ingest_current(
                    provider,
                    json.dumps(
                        global_revision_v2, separators=(",", ":")
                    ).encode(),
                )
        finally:
            provider.close()

        primary = FakePrimaryProvider()
        provider = read.TlvReadShadowProvider(
            BINDING_ID,
            PAT_DEVICE_ID,
            contract,
            primary,
            model_authority=model_authority,
            consumer_state=consumer_state(v1_pin),
            now=lambda: NOW,
        )
        provider.set_transport_ready(True)
        try:
            self.assertTrue(self.ingest_current(provider, v1))
        finally:
            provider.close()

    def test_per_model_authority_cannot_claim_a_future_global_revision(self) -> None:
        contract = profile(semantics_revision=33)
        future = per_model_authority(contract=contract, semantics_revision=34)
        with self.assertRaises(ValueError):
            read.TlvReadConsumerBindingAuthority(
                binding_id=BINDING_ID,
                pat_device_id_proof_sha256=PROOF,
                profile=contract,
                model_authority=future,
            )
        with self.assertRaises(ValueError):
            read.TlvReadShadowProvider(
                BINDING_ID,
                PAT_DEVICE_ID,
                contract,
                FakePrimaryProvider(),
                model_authority=future,
                now=lambda: NOW,
            )

    def test_v2_shape_requires_its_exact_staged_pin_and_accepts_events(self) -> None:
        current = v2_envelope()
        v2_pin = consumer_pin(current, 2)
        provider = self.provider(v2_pin)
        events = []
        remove = provider.async_add_event_listener(events.append)
        try:
            self.assertTrue(self.ingest_current(provider, current))
            self.assertTrue(
                provider.ingest(
                    provider.event_topic,
                    v2_event_envelope(),
                    qos=1,
                    retained=False,
                )
            )
            self.assertEqual(
                [event.semantic_id for event in events],
                ["event.water_tank.changed"],
            )
            self.assertEqual(
                provider.current_contract_observation(),
                {
                    "schema_version": 1,
                    "binding_id": BINDING_ID,
                    "model_id": MODEL_ID,
                    "platform": "thinq2",
                    "projection_version": 2,
                    "static_read_contract_sha256": (
                        v2_pin.static_read_contract_sha256
                    ),
                    "model_contract_sha256": "e" * 64,
                },
            )
            provider._primary.authority_session_id = "2" * 32
            self.assertIsNone(provider.current_contract_observation())
        finally:
            remove()
            provider.close()

        v1_pin = consumer_pin(envelope(), 1)
        provider = self.provider(v1_pin)
        try:
            with self.assertRaises(read.TlvReadProviderContractError):
                self.ingest_current(provider, current)
        finally:
            provider.close()

    def test_overlap_accepts_v1_and_v2_then_v1_retirement_is_exact(self) -> None:
        v1 = envelope(sequence=1)
        v2 = v2_envelope(sequence=2)
        v1_pin = consumer_pin(v1, 1)
        v2_pin = consumer_pin(v2, 2)
        provider = self.provider(v1_pin, v2_pin)
        try:
            self.assertTrue(self.ingest_current(provider, v1))
            self.assertTrue(self.ingest_current(provider, v2))
        finally:
            provider.close()

        provider = self.provider(v2_pin)
        try:
            with self.assertRaises(read.TlvReadProviderContractError):
                self.ingest_current(provider, v1)
            self.assertTrue(self.ingest_current(provider, v2_envelope(sequence=1)))
        finally:
            provider.close()

    def test_hybrid_and_declared_projection_shape_mismatches_are_rejected(
        self,
    ) -> None:
        clean_v2 = v2_envelope()
        provider = self.provider(consumer_pin(clean_v2, 2))
        try:
            hybrid_v2 = json.loads(clean_v2)
            hybrid_v2["profile_sha256"] = "b" * 64
            hybrid_v1 = json.loads(envelope())
            hybrid_v1["model_contract_sha256"] = "e" * 64
            v1_declaring_v2 = json.loads(envelope())
            v1_declaring_v2["static_contract_projection_version"] = 2
            for value in (hybrid_v2, hybrid_v1, v1_declaring_v2):
                with self.subTest(keys=sorted(value)):
                    with self.assertRaises(read.TlvReadProviderContractError):
                        self.ingest_current(
                            provider,
                            json.dumps(value, separators=(",", ":")).encode(),
                        )
        finally:
            provider.close()

    def test_more_than_two_consumer_pins_is_rejected(self) -> None:
        pins = tuple(
            read.TlvReadConsumerPin(
                projection_version=1,
                static_read_contract_sha256=f"{index:064x}",
                model_contract_sha256=None,
            )
            for index in range(1, 4)
        )
        with self.assertRaises(ValueError):
            read.build_tlv_read_consumer_pin_set(BINDING_ID, pins)
        with self.assertRaises(ValueError):
            read.build_tlv_read_consumer_pin_set(BINDING_ID, pins[:2])

    def test_store_state_stage_adopt_retire_is_monotonic_and_roundtrips(
        self,
    ) -> None:
        v1_pin = consumer_pin(envelope(), 1)
        v2_pin = consumer_pin(v2_envelope(), 2)
        source = consumer_state(v1_pin)
        staged = read.stage_tlv_read_consumer_target_pin(source, v2_pin)
        self.assertEqual(staged.consumer_pin_set.accepted, (v1_pin, v2_pin))
        self.assertEqual(staged.adopted_projection_version, 1)

        stored = read.tlv_read_consumer_state_inventory_json(
            {BINDING_ID: staged}
        )
        restored = read.parse_tlv_read_consumer_state_inventory(stored)
        self.assertEqual(restored.bindings[BINDING_ID], staged)

        adopted = read.adopt_tlv_read_consumer_projection(staged, v2_pin)
        self.assertEqual(adopted.adopted_projection_version, 2)
        with self.assertRaises(ValueError):
            read.adopt_tlv_read_consumer_projection(adopted, v1_pin)
        retired = read.retire_tlv_read_consumer_source_pin(adopted)
        self.assertEqual(retired.consumer_pin_set.accepted, (v2_pin,))
        self.assertIs(read.retire_tlv_read_consumer_source_pin(retired), retired)

    def test_v2_successor_adopt_restore_and_retire_are_exact(self) -> None:
        predecessor_pin = read.TlvReadConsumerPin(
            projection_version=2,
            static_read_contract_sha256="1" * 64,
            model_contract_sha256="2" * 64,
        )
        successor_pin = read.TlvReadConsumerPin(
            projection_version=2,
            static_read_contract_sha256="3" * 64,
            model_contract_sha256="4" * 64,
        )
        predecessor = consumer_state(predecessor_pin)
        predecessor_json = read.tlv_read_consumer_binding_state_json(predecessor)

        adopted = read.adopt_tlv_read_consumer_successor_v2(
            predecessor, successor_pin
        )
        self.assertEqual(adopted.schema_version, 2)
        self.assertEqual(adopted.consumer_pin_set.accepted, (successor_pin,))
        self.assertEqual(adopted.predecessor_pin, predecessor_pin)
        self.assertEqual(
            adopted.predecessor_record_sha256, predecessor.record_sha256
        )
        restored_inventory = read.parse_tlv_read_consumer_state_inventory(
            read.tlv_read_consumer_state_inventory_json({BINDING_ID: adopted})
        )
        self.assertEqual(restored_inventory.bindings[BINDING_ID], adopted)

        restored = read.restore_tlv_read_consumer_predecessor_v2(adopted)
        self.assertEqual(restored, predecessor)
        self.assertEqual(
            read.tlv_read_consumer_binding_state_json(restored),
            predecessor_json,
        )
        self.assertEqual(
            read.validate_tlv_read_consumer_state_replacement(
                adopted, restored
            ),
            restored,
        )

        retired = read.retire_tlv_read_consumer_predecessor_v2(adopted)
        self.assertEqual(retired.schema_version, 1)
        self.assertEqual(retired.consumer_pin_set.accepted, (successor_pin,))
        self.assertIsNone(retired.predecessor_pin)
        self.assertIsNone(retired.predecessor_record_sha256)
        with self.assertRaisesRegex(ValueError, "predecessor"):
            read.restore_tlv_read_consumer_predecessor_v2(retired)
        with self.assertRaises(ValueError):
            read.validate_tlv_read_consumer_state_replacement(
                retired, predecessor
            )

    def test_accepts_the_exact_schema2_state_serialized_and_hashed_by_typescript(
        self,
    ) -> None:
        fixture = json.loads(
            Path(__file__)
            .with_name(
                "read-contract-consumer-successor-state.typescript.v1.json"
            )
            .read_text(encoding="utf-8")
        )
        parsed = read.parse_tlv_read_consumer_binding_state(fixture)

        self.assertEqual(
            parsed.record_sha256,
            "bd9b94efb4243190bd7fce88830be32a8c84a70747f09f1536def4c5ec8fd06d",
        )
        self.assertEqual(
            parsed.predecessor_record_sha256,
            "c95426c478f1222f7ad8f8a80dcee2a25cbea57c5bed9dcd652e9ce695ce28b3",
        )

    def test_v2_successor_requires_an_exact_singleton_predecessor(self) -> None:
        predecessor_pin = read.TlvReadConsumerPin(2, "1" * 64, "2" * 64)
        successor_pin = read.TlvReadConsumerPin(2, "3" * 64, "4" * 64)
        v1_pin = read.TlvReadConsumerPin(1, "5" * 64, None)
        overlap = consumer_state(v1_pin, predecessor_pin, adopted_pin=predecessor_pin)
        with self.assertRaises(ValueError):
            read.adopt_tlv_read_consumer_successor_v2(overlap, successor_pin)
        with self.assertRaises(ValueError):
            read.build_tlv_read_consumer_binding_state(
                binding_id=BINDING_ID,
                pat_device_id_proof_sha256=PROOF,
                adopted_pin=successor_pin,
                consumer_pin_set=read.build_tlv_read_consumer_pin_set(
                    BINDING_ID, (successor_pin,)
                ),
                predecessor_pin=predecessor_pin,
                predecessor_record_sha256="6" * 64,
            )

    def test_projection_advance_rejects_a_schema2_predecessor_body(self) -> None:
        v1_pin = read.TlvReadConsumerPin(1, "1" * 64, None)
        v2_pin = read.TlvReadConsumerPin(2, "2" * 64, "3" * 64)
        unrelated_v2_pin = read.TlvReadConsumerPin(2, "4" * 64, "5" * 64)
        source = consumer_state(v1_pin)
        unrelated = consumer_state(unrelated_v2_pin)
        forged = read.build_tlv_read_consumer_binding_state(
            binding_id=BINDING_ID,
            pat_device_id_proof_sha256=PROOF,
            adopted_pin=v2_pin,
            consumer_pin_set=read.build_tlv_read_consumer_pin_set(
                BINDING_ID, (v2_pin,)
            ),
            predecessor_pin=unrelated_v2_pin,
            predecessor_record_sha256=unrelated.record_sha256,
        )
        with self.assertRaisesRegex(ValueError, "replacement refused"):
            read.validate_tlv_read_consumer_state_replacement(source, forged)

    def test_provider_derives_both_pins_while_the_appliance_is_offline(self) -> None:
        primary = FakePrimaryProvider()
        primary.binding_generation = None
        provider = read.TlvReadShadowProvider(
            BINDING_ID,
            PAT_DEVICE_ID,
            profile(),
            primary,
            model_authority=per_model_authority(),
            now=lambda: NOW,
        )
        try:
            self.assertEqual(
                provider.consumer_pin_for_projection(1, 7),
                consumer_pin(envelope(), 1),
            )
            self.assertEqual(
                provider.consumer_pin_for_projection(2, 7),
                consumer_pin(v2_envelope(), 2),
            )
        finally:
            provider.close()

    def test_known_binding_generation_must_match_the_jit_manifest(self) -> None:
        provider = self.provider(consumer_pin(envelope(), 1))
        try:
            with self.assertRaisesRegex(ValueError, "generation changed"):
                provider.consumer_pin_for_projection(2, 8)
        finally:
            provider.close()

    def test_named_transitions_are_cas_protected_and_idempotent(self) -> None:
        v1_pin = consumer_pin(envelope(), 1)
        v2_pin = consumer_pin(v2_envelope(), 2)
        common = {
            "binding_id": BINDING_ID,
            "pat_device_id_proof_sha256": PROOF,
            "v1_pin": v1_pin,
            "v2_pin": v2_pin,
        }
        source = read.transition_tlv_read_consumer_binding_state(
            **common,
            operation="bootstrap-v1",
            current=None,
            expected_current_record_sha256=None,
        )
        self.assertEqual(
            read.transition_tlv_read_consumer_binding_state(
                **common,
                operation="bootstrap-v1",
                current=source,
                # A retry after save does not consume the stale absent token.
                expected_current_record_sha256=None,
            ),
            source,
        )
        with self.assertRaisesRegex(ValueError, "CAS changed"):
            read.transition_tlv_read_consumer_binding_state(
                **common,
                operation="stage-v2",
                current=source,
                expected_current_record_sha256="f" * 64,
            )

        staged = read.transition_tlv_read_consumer_binding_state(
            **common,
            operation="stage-v2",
            current=source,
            expected_current_record_sha256=source.record_sha256,
        )
        self.assertEqual(
            read.transition_tlv_read_consumer_binding_state(
                **common,
                operation="stage-v2",
                current=staged,
                expected_current_record_sha256=source.record_sha256,
            ),
            staged,
        )
        restored = read.transition_tlv_read_consumer_binding_state(
            **common,
            operation="restore-staged-v1",
            current=staged,
            expected_current_record_sha256=staged.record_sha256,
        )
        self.assertEqual(restored, source)

        adopted = read.transition_tlv_read_consumer_binding_state(
            **common,
            operation="adopt-v2",
            current=staged,
            expected_current_record_sha256=staged.record_sha256,
        )
        reconciled = read.transition_tlv_read_consumer_binding_state(
            **common,
            operation="stage-v2",
            current=staged,
            observed_adopted_pin=v2_pin,
            expected_current_record_sha256=staged.record_sha256,
        )
        self.assertEqual(reconciled, adopted)
        self.assertEqual(reconciled.adopted_projection_version, 2)
        self.assertEqual(
            tuple(
                pin.projection_version
                for pin in reconciled.consumer_pin_set.accepted
            ),
            (1, 2),
        )
        # Reissuing stage after durable adoption is a no-op and never lowers
        # the latch, even when the caller carries the pre-adoption CAS token.
        self.assertEqual(
            read.transition_tlv_read_consumer_binding_state(
                **common,
                operation="stage-v2",
                current=reconciled,
                observed_adopted_pin=v2_pin,
                expected_current_record_sha256=staged.record_sha256,
            ),
            reconciled,
        )
        self.assertEqual(
            read.transition_tlv_read_consumer_binding_state(
                **common,
                operation="adopt-v2",
                current=adopted,
                expected_current_record_sha256=staged.record_sha256,
            ),
            adopted,
        )
        with self.assertRaisesRegex(ValueError, "cannot roll back"):
            read.transition_tlv_read_consumer_binding_state(
                **common,
                operation="restore-staged-v1",
                current=adopted,
                expected_current_record_sha256=adopted.record_sha256,
            )
        retired = read.transition_tlv_read_consumer_binding_state(
            **common,
            operation="retire-v1",
            current=adopted,
            expected_current_record_sha256=adopted.record_sha256,
        )
        self.assertEqual(
            read.transition_tlv_read_consumer_binding_state(
                **common,
                operation="retire-v1",
                current=retired,
                expected_current_record_sha256=adopted.record_sha256,
            ),
            retired,
        )

    def test_named_v2_successor_transition_is_cas_protected_and_reversible(
        self,
    ) -> None:
        predecessor_pin = read.TlvReadConsumerPin(2, "1" * 64, "2" * 64)
        successor_pin = read.TlvReadConsumerPin(2, "3" * 64, "4" * 64)
        source = consumer_state(predecessor_pin)
        common = {
            "binding_id": BINDING_ID,
            "pat_device_id_proof_sha256": PROOF,
            "v2_pin": successor_pin,
            "predecessor_v2_pin": predecessor_pin,
        }
        with self.assertRaisesRegex(ValueError, "CAS changed"):
            read.transition_tlv_read_consumer_binding_state(
                **common,
                operation="adopt-successor-v2",
                current=source,
                expected_current_record_sha256="f" * 64,
            )
        adopted = read.transition_tlv_read_consumer_binding_state(
            **common,
            operation="adopt-successor-v2",
            current=source,
            expected_current_record_sha256=source.record_sha256,
        )
        self.assertEqual(
            read.transition_tlv_read_consumer_binding_state(
                **common,
                operation="adopt-successor-v2",
                current=adopted,
                expected_current_record_sha256=source.record_sha256,
            ),
            adopted,
        )
        restored = read.transition_tlv_read_consumer_binding_state(
            **common,
            operation="restore-predecessor-v2",
            current=adopted,
            expected_current_record_sha256=adopted.record_sha256,
        )
        self.assertEqual(restored, source)
        self.assertEqual(
            read.transition_tlv_read_consumer_binding_state(
                **common,
                operation="restore-predecessor-v2",
                current=restored,
                expected_current_record_sha256=adopted.record_sha256,
            ),
            source,
        )

        adopted = read.transition_tlv_read_consumer_binding_state(
            **common,
            operation="adopt-successor-v2",
            current=source,
            expected_current_record_sha256=source.record_sha256,
        )
        retired = read.transition_tlv_read_consumer_binding_state(
            **common,
            operation="retire-predecessor-v2",
            current=adopted,
            expected_current_record_sha256=adopted.record_sha256,
        )
        self.assertEqual(retired.schema_version, 1)
        self.assertEqual(
            read.transition_tlv_read_consumer_binding_state(
                **common,
                operation="retire-predecessor-v2",
                current=retired,
                expected_current_record_sha256=adopted.record_sha256,
            ),
            retired,
        )
        with self.assertRaisesRegex(ValueError, "predecessor"):
            read.transition_tlv_read_consumer_binding_state(
                **common,
                operation="restore-predecessor-v2",
                current=retired,
                expected_current_record_sha256=retired.record_sha256,
            )

    def test_consumer_state_digest_matches_the_typescript_switch_golden(
        self,
    ) -> None:
        binding_id = "pilot_jit_switch_template"
        pin = read.TlvReadConsumerPin(1, "b" * 64, None)
        pin_set = read.build_tlv_read_consumer_pin_set(binding_id, (pin,))
        state = read.build_tlv_read_consumer_binding_state(
            binding_id=binding_id,
            pat_device_id_proof_sha256="a" * 64,
            adopted_pin=pin,
            consumer_pin_set=pin_set,
        )
        self.assertEqual(
            pin_set.record_sha256,
            "5219134900bcf93291e6252624b9428f842fa2e46aa9721c826a7e21a4dce7f9",
        )
        self.assertEqual(
            state.record_sha256,
            "f98b56fb3dc82409e1ce389fff4486f43c3c65f4477870c3f1a251f53e96b493",
        )

    def test_missing_or_damaged_store_does_not_reauthorize_v1(self) -> None:
        provider = read.TlvReadShadowProvider(
            BINDING_ID,
            PAT_DEVICE_ID,
            profile(),
            FakePrimaryProvider(),
            model_authority=per_model_authority(),
            now=lambda: NOW,
        )
        provider.set_transport_ready(True)
        try:
            with self.assertRaises(read.TlvReadProviderContractError):
                self.ingest_current(provider, envelope())
        finally:
            provider.close()

        state = consumer_state(consumer_pin(envelope(), 1))
        stored = read.tlv_read_consumer_state_inventory_json(
            {BINDING_ID: state}
        )
        stored["bindings"][0]["pat_device_id_proof_sha256"] = "f" * 64
        with self.assertRaises(ValueError):
            read.parse_tlv_read_consumer_state_inventory(stored)

    def test_v2_acceptance_latches_out_v1_before_source_pin_retirement(self) -> None:
        v1 = envelope(sequence=1)
        v2 = v2_envelope(sequence=2)
        v1_pin = consumer_pin(v1, 1)
        v2_pin = consumer_pin(v2, 2)
        durable_source = consumer_state(v1_pin, v2_pin)
        provider = read.TlvReadShadowProvider(
            BINDING_ID,
            PAT_DEVICE_ID,
            profile(),
            FakePrimaryProvider(),
            model_authority=per_model_authority(),
            consumer_state=durable_source,
            now=lambda: NOW,
        )
        provider.set_transport_ready(True)
        try:
            self.assertTrue(self.ingest_current(provider, v1))
            self.assertEqual(provider.adopted_projection_version, 1)
            self.assertTrue(self.ingest_current(provider, v2))
            self.assertEqual(provider.adopted_projection_version, 2)
            # Publication ingest tightens only the process-local latch. The
            # installation transaction remains the sole durable Store writer.
            self.assertEqual(durable_source.adopted_projection_version, 1)
            self.assertEqual(durable_source.consumer_pin_set.accepted, (v1_pin, v2_pin))
            with self.assertRaises(read.TlvReadProviderContractError):
                self.ingest_current(provider, envelope(sequence=3))
        finally:
            provider.close()

    def test_live_latch_rejects_stale_durable_state_before_store_write(self) -> None:
        v1 = envelope(sequence=1)
        v2 = v2_envelope(sequence=2)
        v1_pin = consumer_pin(v1, 1)
        v2_pin = consumer_pin(v2, 2)
        durable_source = consumer_state(v1_pin, v2_pin)
        provider = read.TlvReadShadowProvider(
            BINDING_ID,
            PAT_DEVICE_ID,
            profile(),
            FakePrimaryProvider(),
            model_authority=per_model_authority(),
            consumer_state=durable_source,
            now=lambda: NOW,
        )
        provider.set_transport_ready(True)
        try:
            self.assertTrue(self.ingest_current(provider, v2))
            self.assertEqual(provider.adopted_projection_version, 2)

            # The installation adapter runs this predicate before Store.save.
            # A delayed or out-of-order v1 stage must not make the durable
            # state looser than the process-local latch.
            with self.assertRaises(ValueError):
                provider.validate_consumer_state_replacement(durable_source)
        finally:
            provider.close()

    def test_same_projection_cannot_replace_the_adopted_contract_identity(
        self,
    ) -> None:
        v1_pin = consumer_pin(envelope(), 1)
        provider = self.provider(v1_pin)
        changed_pin = read.TlvReadConsumerPin(
            projection_version=1,
            static_read_contract_sha256="f" * 64,
            model_contract_sha256=None,
        )
        changed_state = consumer_state(changed_pin)
        try:
            with self.assertRaises(ValueError):
                provider.validate_consumer_state_replacement(changed_state)
        finally:
            provider.close()


class TlvReadConsumerStateApplyTests(unittest.IsolatedAsyncioTestCase):
    class Store:
        def __init__(self, *, fail: bool = False, block_first: bool = False):
            self.fail = fail
            self.block_first = block_first
            self.saved = []
            self.first_save_started = asyncio.Event()
            self.release_first_save = asyncio.Event()

        async def async_save(self, value):
            self.saved.append(value)
            if self.fail:
                raise RuntimeError("synthetic Store failure")
            if self.block_first and len(self.saved) == 1:
                self.first_save_started.set()
                await self.release_first_save.wait()

    async def test_apply_is_idempotent_and_uses_immediate_store_save(self) -> None:
        source = consumer_state(consumer_pin(envelope(), 1))
        states = {BINDING_ID: source}
        store = self.Store()
        lock = asyncio.Lock()

        first = await read.async_apply_tlv_read_consumer_binding_state(
            states=states,
            store=store,
            lock=lock,
            providers={},
            state=source,
        )
        second = await read.async_apply_tlv_read_consumer_binding_state(
            states=states,
            store=store,
            lock=lock,
            providers={},
            state=source,
        )

        self.assertEqual(first, source)
        self.assertEqual(second, source)
        self.assertEqual(len(store.saved), 2)
        self.assertEqual(store.saved[0]["root_sha256"], store.saved[1]["root_sha256"])
        self.assertEqual(states, {BINDING_ID: source})

    async def test_provider_absence_never_filters_an_offline_binding(self) -> None:
        source = consumer_state(consumer_pin(envelope(), 1))
        states = {}
        store = self.Store()
        await read.async_apply_tlv_read_consumer_binding_state(
            states=states,
            store=store,
            lock=asyncio.Lock(),
            providers={},
            state=source,
        )
        self.assertEqual(states[BINDING_ID], source)
        self.assertEqual(
            read.parse_tlv_read_consumer_state_inventory(store.saved[-1]).bindings[
                BINDING_ID
            ],
            source,
        )

        # A provider created later (for example after a powered-off appliance
        # or broker reconnects) starts directly from the durable state.
        provider = read.TlvReadShadowProvider(
            BINDING_ID,
            PAT_DEVICE_ID,
            profile(),
            FakePrimaryProvider(),
            model_authority=per_model_authority(),
            consumer_state=states[BINDING_ID],
            now=lambda: NOW,
        )
        try:
            self.assertEqual(provider.consumer_state, source)
            self.assertEqual(provider.adopted_projection_version, 1)
        finally:
            provider.close()

    async def test_push_failure_reports_that_durable_state_already_persisted(
        self,
    ) -> None:
        source = consumer_state(consumer_pin(envelope(), 1))

        class RejectingProvider:
            binding_id = BINDING_ID

            @staticmethod
            def validate_consumer_state_replacement(state):
                return state

            @staticmethod
            def replace_consumer_state(_state):
                raise RuntimeError("synthetic live push failure")

        states = {}
        store = self.Store()
        with self.assertRaises(read.TlvReadConsumerStatePushError) as raised:
            await read.async_apply_tlv_read_consumer_binding_state(
                states=states,
                store=store,
                lock=asyncio.Lock(),
                providers={BINDING_ID: RejectingProvider()},
                state=source,
            )
        self.assertTrue(raised.exception.durable_state_persisted)
        self.assertEqual(raised.exception.binding_id, BINDING_ID)
        self.assertEqual(raised.exception.record_sha256, source.record_sha256)
        self.assertEqual(len(store.saved), 1)
        self.assertEqual(states, {BINDING_ID: source})

    async def test_v2_successor_push_failure_retries_the_same_candidate(
        self,
    ) -> None:
        predecessor_pin = read.TlvReadConsumerPin(2, "1" * 64, "2" * 64)
        successor_pin = read.TlvReadConsumerPin(2, "3" * 64, "4" * 64)
        predecessor = consumer_state(predecessor_pin)
        successor = read.adopt_tlv_read_consumer_successor_v2(
            predecessor, successor_pin
        )

        class FailOnceProvider:
            binding_id = BINDING_ID

            def __init__(self):
                self.state = predecessor
                self.fail = True

            def validate_consumer_state_replacement(self, state):
                return read.validate_tlv_read_consumer_state_replacement(
                    self.state, state
                )

            def replace_consumer_state(self, state):
                if self.fail:
                    self.fail = False
                    raise RuntimeError("synthetic first live push failure")
                self.state = state

        provider = FailOnceProvider()
        states = {BINDING_ID: predecessor}
        store = self.Store()
        common = {
            "states": states,
            "store": store,
            "lock": asyncio.Lock(),
            "providers": {BINDING_ID: provider},
            "state": successor,
        }
        with self.assertRaises(read.TlvReadConsumerStatePushError):
            await read.async_apply_tlv_read_consumer_binding_state(**common)
        self.assertEqual(states[BINDING_ID], successor)
        self.assertEqual(provider.state, predecessor)

        applied = await read.async_apply_tlv_read_consumer_binding_state(
            **common
        )
        self.assertEqual(applied, successor)
        self.assertEqual(provider.state, successor)
        self.assertEqual(len(store.saved), 2)

    async def test_overlapping_applies_observe_the_previous_locked_result(self) -> None:
        v1_pin = consumer_pin(envelope(), 1)
        v2_pin = consumer_pin(v2_envelope(), 2)
        source = consumer_state(v1_pin)
        staged = read.stage_tlv_read_consumer_target_pin(source, v2_pin)
        adopted = read.adopt_tlv_read_consumer_projection(staged, v2_pin)
        states = {BINDING_ID: source}
        store = self.Store(block_first=True)
        lock = asyncio.Lock()

        first = asyncio.create_task(
            read.async_apply_tlv_read_consumer_binding_state(
                states=states,
                store=store,
                lock=lock,
                providers={},
                state=staged,
            )
        )
        await store.first_save_started.wait()
        second = asyncio.create_task(
            read.async_apply_tlv_read_consumer_binding_state(
                states=states,
                store=store,
                lock=lock,
                providers={},
                state=adopted,
            )
        )
        await asyncio.sleep(0)
        self.assertEqual(len(store.saved), 1)
        store.release_first_save.set()
        self.assertEqual(await first, staged)
        self.assertEqual(await second, adopted)
        self.assertEqual(states[BINDING_ID], adopted)
        self.assertEqual(len(store.saved), 2)

    async def test_binding_updates_preserve_every_other_inventory_row(self) -> None:
        source = consumer_state(consumer_pin(envelope(), 1))
        other_binding = "pilot_full_read_provider_002"
        other_pin = read.TlvReadConsumerPin(1, "9" * 64, None)
        other_pin_set = read.build_tlv_read_consumer_pin_set(
            other_binding, (other_pin,)
        )
        other = read.build_tlv_read_consumer_binding_state(
            binding_id=other_binding,
            pat_device_id_proof_sha256="8" * 64,
            adopted_pin=other_pin,
            consumer_pin_set=other_pin_set,
        )
        states = {other_binding: other}
        store = self.Store()
        await read.async_apply_tlv_read_consumer_binding_state(
            states=states,
            store=store,
            lock=asyncio.Lock(),
            providers={},
            state=source,
        )
        self.assertEqual(states, {BINDING_ID: source, other_binding: other})

    async def test_store_failure_changes_neither_provider_nor_memory(self) -> None:
        source = consumer_state(consumer_pin(envelope(), 1))
        v2_pin = consumer_pin(v2_envelope(), 2)
        staged = read.stage_tlv_read_consumer_target_pin(source, v2_pin)
        provider = read.TlvReadShadowProvider(
            BINDING_ID,
            PAT_DEVICE_ID,
            profile(),
            FakePrimaryProvider(),
            model_authority=per_model_authority(),
            consumer_state=source,
            now=lambda: NOW,
        )
        states = {BINDING_ID: source}
        try:
            with self.assertRaisesRegex(RuntimeError, "synthetic Store failure"):
                await read.async_apply_tlv_read_consumer_binding_state(
                    states=states,
                    store=self.Store(fail=True),
                    lock=asyncio.Lock(),
                    providers={BINDING_ID: provider},
                    state=staged,
                )
            self.assertEqual(states, {BINDING_ID: source})
            self.assertEqual(provider.consumer_state, source)
        finally:
            provider.close()


class TlvReadBundledCatalogueTests(unittest.TestCase):
    def _artifact_bytes(self):
        directory = MODULE_PATH.parent
        return (
            (directory / read.TLV_READ_ENTITY_CONTRACT_FILENAME).read_bytes(),
            (directory / read.TLV_READ_ENTITY_CONTRACT_DIGEST_FILENAME).read_bytes(),
            (directory / read.TLV_READ_PROFILE_FILENAME).read_bytes(),
            (directory / read.TLV_READ_PROFILE_DIGEST_FILENAME).read_bytes(),
        )

    def test_bundled_contract_accounts_for_all_355_ha_descriptors(self) -> None:
        catalogue = read.load_tlv_read_catalogue()
        fields = [field for profile in catalogue.values() for field in profile.fields]
        self.assertEqual(len(catalogue), 15)
        self.assertEqual(len(fields), 355)
        self.assertEqual(
            {
                domain: sum(field.domain == domain for field in fields)
                for domain in (
                    "binary_sensor",
                    "sensor",
                    "event",
                )
            },
            {"binary_sensor": 85, "sensor": 258, "event": 12},
        )
        for model_id in ("CST_170004_WW", "CST_570004_WW"):
            self.assertNotIn(
                "temperature.target_c",
                {
                    field.semantic_id
                    for field in catalogue[model_id].fields
                },
            )
        self.assertEqual(
            {
                exposure: sum(field.exposure == exposure for field in fields)
                for exposure in (
                    "state",
                    "diagnostic",
                    "event",
                )
            },
            {"state": 194, "diagnostic": 149, "event": 12},
        )
        self.assertEqual(sum(field.enabled_by_default for field in fields), 194)
        self.assertEqual(sum(field.owner == "PAT" for field in fields), 4)
        self.assertEqual(
            {
                (field.value_types, field.event_type): sum(
                    other.domain == "event"
                    and other.value_types == field.value_types
                    and other.event_type == field.event_type
                    for other in fields
                )
                for field in fields
                if field.domain == "event"
            },
            {
                (("number",), "observed"): 11,
                (("string",), "water-tank state changed"): 1,
            },
        )
        self.assertEqual(
            sum(
                field.semantic_id.startswith("diagnostic.capability.mode_memory.")
                for field in fields
            ),
            10,
        )
        self.assertTrue(all(field.label_ko.strip() for field in fields))
        tlv_models = {
            "AIR_2C0001_WW",
            "AIR_910604_WW",
            "CST_170004_WW",
            "CST_570004_WW",
            "DHUM_056905_WW",
            "HUM_056905_WW",
        }
        reviewed_fields = [
            field for field in fields if field.model_id not in tlv_models
        ]
        self.assertEqual(len(reviewed_fields), 67)
        self.assertTrue(
            all(field.label_ko != field.semantic_id for field in reviewed_fields)
        )
        self.assertTrue(
            all(
                any("\uac00" <= char <= "\ud7a3" for char in field.label_ko)
                for field in reviewed_fields
            )
        )

    def test_compact_per_model_authority_covers_offline_appliances(self) -> None:
        authorities = read.load_tlv_read_per_model_authorities()
        self.assertEqual(len(authorities), 15)
        self.assertEqual(
            {
                model_id: authority.semantics_revision
                for model_id, authority in authorities.items()
                if authority.semantics_revision != 32
            },
            {"CST_170004_WW": 33, "CST_570004_WW": 33},
        )
        for model_id in ("DHUM_056905_WW", "ST_R_ETH01Y_"):
            with self.subTest(model_id=model_id):
                authority = authorities[model_id]
                self.assertEqual(
                    authority.profile_id, f"{model_id}:read-sensors-v1"
                )
                self.assertEqual(
                    authority.static_contract_projection_version, 2
                )

        authority = authorities[MODEL_ID]
        static = {
            "schema_version": authority.feed_schema_version,
            "publication_plan_revision": authority.publication_plan_revision,
            "static_contract_projection_version": (
                authority.static_contract_projection_version
            ),
            "profile_id": authority.profile_id,
            "model_contract_sha256": authority.model_contract_sha256,
            "semantics_revision": authority.semantics_revision,
            "binding_id": BINDING_ID,
            "model_id": authority.model_id,
            "platform": authority.platform,
            "binding_generation": 7,
            "pat_device_id_proof_sha256": PROOF,
        }
        self.assertEqual(
            read.tlv_read_publication_static_contract_sha256(static, 2),
            "c73e4f9bc3c690d40e95d9feb4b2f9a9495ea4d21b17f52817d3b4afdf752d59",
        )

    def test_exact_cst_predecessor_state_survives_successor_package_setup(
        self,
    ) -> None:
        catalogue = read.load_tlv_read_catalogue()
        authorities = read.load_tlv_read_per_model_authorities()
        predecessors = {
            "CST_170004_WW": (
                "b6042e904492d2a378186de4fdf2c964aab96d26acb3418703af02816c400912"
            ),
            "CST_570004_WW": (
                "7b3d28cc729641d852ee9c5ebe768532e5afc3582d486d4ae4cf5c4192202421"
            ),
        }
        for model_id, predecessor_sha256 in predecessors.items():
            with self.subTest(model_id=model_id):
                contract = catalogue[model_id]
                old_authority = read.TlvReadPerModelAuthority(
                    profile_id=contract.profile_id,
                    model_id=model_id,
                    platform=contract.platform,
                    semantics_revision=32,
                    model_contract_sha256=predecessor_sha256,
                    feed_schema_version=read.PER_MODEL_TLV_READ_SCHEMA_VERSION,
                    publication_plan_revision=(
                        read.TLV_READ_PUBLICATION_PLAN_REVISION
                    ),
                    static_contract_projection_version=(
                        read.READ_STATIC_CONTRACT_PROJECTION_VERSION
                    ),
                )
                old_binding_authority = read.TlvReadConsumerBindingAuthority(
                    binding_id=BINDING_ID,
                    pat_device_id_proof_sha256=PROOF,
                    profile=contract,
                    model_authority=old_authority,
                )
                old_pin = old_binding_authority.pin_for_projection(2, 7)
                old_state = consumer_state(old_pin)
                primary = FakePrimaryProvider()
                primary.model_id = model_id
                provider = read.TlvReadShadowProvider(
                    BINDING_ID,
                    PAT_DEVICE_ID,
                    contract,
                    primary,
                    model_authority=authorities[model_id],
                    consumer_state=old_state,
                    now=lambda: NOW,
                )
                try:
                    self.assertEqual(provider.consumer_state, old_state)
                finally:
                    provider.close()

                foreign_pin = read.TlvReadConsumerPin(
                    projection_version=2,
                    static_read_contract_sha256=old_pin.static_read_contract_sha256,
                    model_contract_sha256="f" * 64,
                )
                with self.assertRaises(ValueError):
                    read.TlvReadShadowProvider(
                        BINDING_ID,
                        PAT_DEVICE_ID,
                        contract,
                        primary,
                        model_authority=authorities[model_id],
                        consumer_state=consumer_state(foreign_pin),
                        now=lambda: NOW,
                    )

    def test_powered_off_dehumidifier_and_styler_keep_contract_entities(
        self,
    ) -> None:
        catalogue = read.load_tlv_read_catalogue()
        authorities = read.load_tlv_read_per_model_authorities()

        class OfflinePrimary:
            def __init__(self, binding_id, model_id, platform):
                self.binding_id = binding_id
                self.model_id = model_id
                self.platform = platform
                self.expected_proof = PROOF
                self.read_publication_authority = None

            @staticmethod
            def async_add_listener(_callback):
                return lambda: None

        for index, model_id in enumerate(("DHUM_056905_WW", "ST_R_ETH01Y_")):
            with self.subTest(model_id=model_id):
                profile = catalogue[model_id]
                binding_id = f"pilot_offline_contract_{index:02d}"
                provider = read.TlvReadShadowProvider(
                    binding_id,
                    f"pat-offline-device-{index:02d}",
                    profile,
                    OfflinePrimary(binding_id, model_id, profile.platform),
                    model_authority=authorities[model_id],
                    now=lambda: NOW,
                )
                try:
                    self.assertGreater(len(provider.profile.fields), 0)
                    self.assertEqual(provider.adopted_projection_version, 0)
                    self.assertTrue(
                        all(
                            not provider.field_available(field.semantic_id)
                            for field in provider.profile.fields
                        )
                    )
                finally:
                    provider.close()

    def test_offline_authority_and_online_provider_derive_identical_pins(
        self,
    ) -> None:
        catalogue = read.load_tlv_read_catalogue()
        authorities = read.load_tlv_read_per_model_authorities()

        class OfflinePrimary:
            def __init__(self, binding_id, model_id, platform):
                self.binding_id = binding_id
                self.model_id = model_id
                self.platform = platform
                self.expected_proof = PROOF
                self.binding_generation = None
                self.read_publication_authority = None

            @staticmethod
            def async_add_listener(_callback):
                return lambda: None

        for index, (model_id, profile_value) in enumerate(
            sorted(catalogue.items())
        ):
            with self.subTest(model_id=model_id):
                binding_id = f"pilot_pin_equivalence_{index:02d}"
                authority = authorities[model_id]
                provider = read.TlvReadShadowProvider(
                    binding_id,
                    f"pat-pin-equivalence-{index:02d}",
                    profile_value,
                    OfflinePrimary(binding_id, model_id, profile_value.platform),
                    model_authority=authority,
                    now=lambda: NOW,
                )
                try:
                    offline_authority = read.TlvReadConsumerBindingAuthority(
                        binding_id=binding_id,
                        pat_device_id_proof_sha256=PROOF,
                        profile=profile_value,
                        model_authority=authority,
                    )
                    for projection_version in (1, 2):
                        self.assertEqual(
                            provider.consumer_pin_for_projection(
                                projection_version, 17
                            ),
                            offline_authority.pin_for_projection(
                                projection_version, 17
                            ),
                        )
                finally:
                    provider.close()

    def test_per_model_authority_root_is_recomputed_after_sidecar_verified(
        self,
    ) -> None:
        directory = MODULE_PATH.parent
        raw = (
            directory / read.TLV_READ_PER_MODEL_AUTHORITY_FILENAME
        ).read_bytes()
        tampered = json.loads(raw)
        tampered["authorities"][0]["model_contract_sha256"] = "0" * 64
        tampered_raw = json.dumps(
            tampered, ensure_ascii=False, separators=(",", ":")
        ).encode()
        matching_sidecar = (
            f"{hashlib.sha256(tampered_raw).hexdigest()}  "
            f"local/model-contract/{read.TLV_READ_PER_MODEL_AUTHORITY_FILENAME}\n"
        ).encode()
        with self.assertRaises(read.TlvReadCatalogueError):
            read._load_tlv_read_per_model_authorities(
                tampered_raw, matching_sidecar
            )

    def test_root_digest_is_recomputed_after_sidecar_verified(self) -> None:
        entity_raw, _entity_digest, profile_raw, profile_digest = self._artifact_bytes()
        tampered = json.loads(entity_raw)
        tampered["entities"][0]["labelKo"] += " 변조"
        tampered_raw = json.dumps(
            tampered, ensure_ascii=False, separators=(",", ":")
        ).encode()
        matching_sidecar = (
            f"{hashlib.sha256(tampered_raw).hexdigest()}  "
            f"local/model-contract/{read.TLV_READ_ENTITY_CONTRACT_FILENAME}\n"
        ).encode()
        with self.assertRaises(read.TlvReadCatalogueError):
            read._load_tlv_read_catalogue(
                tampered_raw,
                matching_sidecar,
                profile_raw,
                profile_digest,
            )


if __name__ == "__main__":
    unittest.main()
