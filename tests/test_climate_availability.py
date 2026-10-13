"""실제 제공자·라우터·HA 엔티티로 수집 묶음 전환과 연결 단절을 구분해요."""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from homeassistant import loader
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry, entity_registry, frame
from homeassistant.helpers.entity_component import EntityComponent

from custom_components.my_lg.climate import MyLgLocalClimate
from custom_components.my_lg.local_control_composite_domain import (
    load_local_control_composite_domain_contract,
)
from custom_components.my_lg.local_control_router import LocalControlRouter
from custom_components.my_lg.local_provider import (
    LocalSemanticShadowProvider,
    load_local_semantic_profile_catalogue,
    local_pat_device_identity_proof,
)
from custom_components.my_lg.local_read_owner import local_climate_control_domain
from custom_components.my_lg.select import MyLgLocalAutoComfortSelect


NOW = datetime(2026, 10, 13, 1, 0, tzinfo=timezone.utc)
PAT = "1" * 64
BINDING = "synthetic_climate_availability"
SERVICE = "2" * 32


class NoDeviceTransport:
    """실제 네트워크 대신 예기치 않은 기기 명령을 즉시 실패시켜요."""

    async def async_send(self, *args, **kwargs):
        raise AssertionError("가용성 검사에서 기기 명령이 전송되면 안 돼요")


class ClimateAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    model_id = "CST_170004_WW"

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.hass = HomeAssistant(self.directory.name)
        loader.async_setup(self.hass)
        if hasattr(frame, "async_setup"):
            frame.async_setup(self.hass)
        if hasattr(device_registry, "async_setup"):
            device_registry.async_setup(self.hass)
        await device_registry.async_load(self.hass)
        await entity_registry.async_load(self.hass)
        self.components = []
        self.clock = NOW
        _, catalogue, _ = load_local_semantic_profile_catalogue()
        self.profile = next(
            profile for profile in catalogue.values()
            if profile.model_id == self.model_id
            and "comfort.preference_step" in profile.fields
        )
        self.provider = LocalSemanticShadowProvider(
            BINDING, self.profile, pat_device_id=PAT, require_identity=True,
            now=lambda: self.clock,
        )
        self.proof = local_pat_device_identity_proof(
            BINDING, self.model_id, "thinq2", PAT,
        )
        self.authorized = True
        self.paired = True
        self.router = LocalControlRouter(
            NoDeviceTransport(), {PAT: self.provider},
            lambda _: "synthetic_bridge" if self.paired else None,
            capability_authorized=lambda *_: self.authorized,
        )
        metadata = SimpleNamespace(
            device_id=PAT, alias="검증용 에어컨", model=self.model_id,
            device_type="DEVICE_AIR_CONDITIONER",
        )
        contract = load_local_control_composite_domain_contract()
        domain = local_climate_control_domain(self.provider, self.model_id, contract)
        assert domain is not None
        self.climate = MyLgLocalClimate(metadata, self.router, self.provider, contract)
        self.select = MyLgLocalAutoComfortSelect(
            metadata, self.provider, self.router, domain,
        )
        for entity, entity_id in (
            (self.climate, "climate.test_ac"),
            (self.select, "select.test_comfort"),
        ):
            entity.entity_id = entity_id
            component = EntityComponent(
                logging.getLogger(__name__), entity_id.split(".")[0], self.hass,
            )
            self.components.append(component)
            await component.async_add_entities([entity])
        self.bootstrap()

    async def asyncTearDown(self):
        for entity in (self.climate, self.select):
            if entity.hass is not None:
                await entity.async_remove()
        await self.hass.async_stop()

    def stamp(self, seconds=0):
        return (NOW + timedelta(seconds=seconds)).isoformat()

    def state(self, *, cohort=1, generation=1, values=None):
        values = values if values is not None else {
            "operation.power_requested": False,
            "operation.mode": "cool",
            "fan.mode": "high",
            "temperature.current_c": 25,
            "temperature.target_c": 24,
        }
        fields = {}
        for name, value in values.items():
            contract = self.profile.fields[name]
            fields[name] = {
                "value": value, "value_type": contract.value_type,
                "observed_at": self.stamp(-1),
                "confidence": contract.confidence[0], "exposure": contract.exposure,
            }
            if contract.unit is not None:
                fields[name]["unit"] = contract.unit
        return {
            "schema_version": 3,
            "semantics_revision": self.profile.semantics_revision,
            "binding_id": BINDING, "model_id": self.model_id,
            "platform": "thinq2", "session_id": SERVICE, "sequence": generation,
            "binding_generation": generation, "cohort_generation": cohort,
            "pat_device_id_proof_sha256": self.proof,
            "published_at": self.stamp(), "fields": fields,
            "diagnostics": dict.fromkeys(
                ("rejected_frames", "unresolved_fields", "invalid_values", "unsupported_frames"), 0,
            ),
        }

    def availability(self, status="online", *, cohort=1, generation=1):
        return {
            "schema_version": 3, "status": status, "session_id": SERVICE,
            "observed_at": self.stamp(), "binding_generation": generation,
            "cohort_generation": cohort, "state_sequence": generation,
            "pat_device_id_proof_sha256": self.proof,
        }

    def presence(self, status="online", *, sequence=1, generation=1, service=SERVICE):
        return {
            "schema_version": 1, "status": status, "evidence": "attested-session",
            "binding_generation": generation, "sequence": sequence,
            "pat_device_id_proof_sha256": self.proof,
            "profile_id": self.profile.profile_id,
            "service_instance_id": service, "observed_at": self.stamp(-2),
            "valid_until": None,
        }

    def runtime(self, status="online"):
        return {
            "status": status, "service_instance_id": SERVICE,
            "observed_at": self.stamp(-2 if status == "online" else 0),
        }

    @staticmethod
    def encode(payload):
        return payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def ingest(self, topic, payload, *, retained=False):
        return self.provider.ingest_live_semantic_publication(
            topic, self.encode(payload), qos=1, retained=retained,
        )

    def bootstrap(self, *, retained_presence=False):
        p = self.provider
        p.ingest_bootstrap_final_current({
            p.state_topic: (self.encode(self.state()), 1, True),
            p.availability_topic: (self.encode(self.availability()), 1, True),
            p.runtime_availability_topic: (self.encode(self.runtime()), 1, True),
            p.presence_topic: (self.encode(self.presence()), 1, retained_presence),
        })
        p.set_transport_ready(True)

    def assert_available(self, expected):
        for entity in (self.climate, self.select):
            self.assertEqual(entity.available, expected)
            entity.async_write_ha_state()
            state = self.hass.states.get(entity.entity_id)
            self.assertIsNotNone(state)
            self.assertEqual(state.state != "unavailable", expected)

    def test_cohort_rotation_does_not_emit_unavailable(self):
        self.assert_available(True)
        self.assertEqual(self.hass.states.get(self.climate.entity_id).state, "off")
        self.ingest(self.provider.availability_topic, self.availability("offline"))
        self.assertFalse(self.provider.shadow_healthy)
        self.assert_available(True)
        self.ingest(self.provider.state_topic, self.state(cohort=2))
        self.assert_available(True)
        self.ingest(self.provider.availability_topic, self.availability(cohort=2))
        self.assert_available(True)
        self.assertEqual(self.provider.cohort_generation, 2)
        self.assertEqual(self.hass.states.get(self.select.entity_id).state, "unknown")

    def test_real_presence_offline_is_immediate(self):
        self.ingest(self.provider.presence_topic, self.presence("offline", sequence=2))
        self.assert_available(False)

    def test_runtime_offline_is_immediate(self):
        self.ingest(self.provider.runtime_availability_topic, self.runtime("offline"))
        self.assert_available(False)

    def test_transport_disconnect_is_immediate(self):
        self.provider.set_transport_ready(False)
        self.assert_available(False)
        self.provider.set_transport_ready(True)
        self.assert_available(False)

    def test_presence_expiry_notifies_ha_without_another_packet(self):
        expiry = self.provider.read_publication_authority_expiry
        self.clock = expiry[1] + timedelta(milliseconds=1)
        self.assertTrue(self.provider.expire_read_publication_authority(expiry))
        for entity in (self.climate, self.select):
            self.assertEqual(self.hass.states.get(entity.entity_id).state, "unavailable")

    def test_retained_presence_alone_does_not_enable_controls(self):
        self.provider.set_transport_ready(False)
        self.bootstrap(retained_presence=True)
        self.assert_available(False)

    def test_new_binding_waits_for_matching_state(self):
        self.ingest(self.provider.presence_topic, self.presence(generation=2))
        self.assertTrue(self.provider.control_alive)
        self.assert_available(False)
        self.ingest(self.provider.state_topic, self.state(generation=2))
        self.ingest(self.provider.availability_topic, self.availability(generation=2))
        self.assert_available(True)

    def test_foreign_service_does_not_authorize_old_state(self):
        self.ingest(self.provider.presence_topic, self.presence(service="3" * 32))
        self.assert_available(False)

    def test_deleted_state_fails_closed(self):
        self.ingest(self.provider.state_topic, b"")
        self.assert_available(False)

    def test_deleted_availability_fails_closed(self):
        self.ingest(self.provider.availability_topic, b"")
        self.assert_available(False)

    def test_deleted_presence_fails_closed(self):
        self.ingest(self.provider.presence_topic, b"")
        self.assert_available(False)

    def test_deleted_runtime_fails_closed(self):
        self.ingest(self.provider.runtime_availability_topic, b"")
        self.assert_available(False)

    def test_missing_power_is_unavailable(self):
        self.ingest(self.provider.state_topic, self.state(cohort=2, values={"operation.mode": "cool"}))
        self.ingest(self.provider.availability_topic, self.availability(cohort=2))
        self.assert_available(False)

    def test_capability_and_pairing_guards_remain(self):
        self.authorized = False
        self.assert_available(False)
        self.authorized = True
        self.paired = False
        self.assert_available(False)

    async def test_cohort_does_not_bypass_command_prestate_guard(self):
        # 새 연결 직후 아직 갱신되지 않은 복합 명령 입력은 전송하지 않아요.
        presence = self.presence(sequence=2)
        presence["observed_at"] = self.stamp()
        self.ingest(self.provider.presence_topic, presence)
        self.ingest(self.provider.availability_topic, self.availability("offline"))
        self.assert_available(True)
        with self.assertRaises(HomeAssistantError):
            await self.select.async_select_option("comfortable")


class Cst570ClimateAvailabilityTests(ClimateAvailabilityTests):
    model_id = "CST_570004_WW"
