"""The existing DHUM UVnano switch must not wait for an empty cloud snapshot."""

from types import SimpleNamespace
import unittest

from homeassistant.exceptions import HomeAssistantError

from custom_components.my_lg import switch as platform
from custom_components.my_lg.const import DEVICE_TYPE_DEHUMIDIFIER
from custom_components.my_lg.local_command import LocalCommandFailed
from tests.test_ac_control_contract import FakeLocalRouter, FakePatCoordinator, FakeWideqCoordinator
from tests.test_ac_local_select_switch import FakeReadProvider


SEMANTIC = "sterilization.uvnano_enabled"


class DhumLocalUvnanoTests(unittest.IsolatedAsyncioTestCase):
    def make(self, *, model="DHUM_056905_WW", value=False, available=True):
        pat = FakePatCoordinator()
        pat.model = model
        pat.device_type = DEVICE_TYPE_DEHUMIDIFIER
        wideq = FakeWideqCoordinator()
        wideq.snapshots = {}
        provider = FakeReadProvider({SEMANTIC: value}, available={SEMANTIC} if available else set())
        router = FakeLocalRouter()
        desc = platform.WIDEQ_SWITCHES_BY_TYPE[DEVICE_TYPE_DEHUMIDIFIER][0]
        entity = platform.MyLgWideqSwitch(wideq, pat, desc, router, provider)
        return entity, pat, wideq, provider, router

    def test_empty_cloud_uses_exact_local_boolean_and_preserves_identity(self):
        for value in (False, True):
            entity, pat, wideq, provider, router = self.make(value=value)
            legacy = platform.MyLgWideqSwitch(
                wideq, pat, platform.WIDEQ_SWITCHES_BY_TYPE[DEVICE_TYPE_DEHUMIDIFIER][0]
            )
            self.assertEqual(entity.unique_id, legacy.unique_id)
            self.assertEqual(entity.entity_description.key, "uvnano")
            self.assertTrue(entity.available)
            self.assertIs(entity.is_on, value)
            self.assertEqual(entity.entity_description.local_control_semantic, SEMANTIC)

    def test_invalid_or_unavailable_local_read_cannot_use_cloud(self):
        for value, available in ((1, True), ("true", True), (None, True), (False, False)):
            entity, pat, wideq, provider, router = self.make(value=value, available=available)
            wideq.snapshots[pat.device_id] = {"airState.miscFuncState.Uvnano": 1}
            self.assertFalse(entity.available)

    def test_other_models_and_absent_local_owner_keep_cloud_behavior(self):
        for model in ("DHUM_OTHER", "DHUM_056905_WW"):
            entity, pat, wideq, provider, router = self.make(model=model)
            if model == "DHUM_056905_WW":
                entity._configure_local_read(None, SEMANTIC)
            wideq.snapshots[pat.device_id] = {"airState.miscFuncState.Uvnano": 1}
            self.assertTrue(entity.available)
            self.assertTrue(entity.is_on)
            self.assertFalse(entity._local_read_owns_state())

    async def test_both_directions_use_only_local_acknowledged_route(self):
        entity, pat, wideq, provider, router = self.make()
        await entity.async_turn_on()
        await entity.async_turn_off()
        self.assertEqual(router.calls, [
            ("flag", {"device_id": pat.device_id, "capability": SEMANTIC, "enabled": True}),
            ("flag", {"device_id": pat.device_id, "capability": SEMANTIC, "enabled": False}),
        ])
        self.assertEqual(wideq.controls, [])
        self.assertEqual(pat.controls, [])
        self.assertFalse(entity.is_on)  # command acknowledgement is not a state report

    async def test_local_rejection_never_falls_back_to_cloud(self):
        entity, pat, wideq, provider, router = self.make()
        async def rejected(*args):
            raise LocalCommandFailed("rejected")
        router.async_set_flag = rejected
        with self.assertRaises(HomeAssistantError):
            await entity.async_turn_on()
        async def unauthorized(*args):
            return None
        router.async_set_flag = unauthorized
        with self.assertRaises(HomeAssistantError):
            await entity.async_turn_off()
        self.assertEqual(wideq.controls, [])

    async def test_factory_requires_both_value_grants_and_keeps_one_legacy_switch(self):
        for values in ((), ("true",), ("false", "true")):
            entity, pat, wideq, provider, router = self.make()
            router.authorized_values = lambda *args: values
            entry = SimpleNamespace(options={}, runtime_data=SimpleNamespace(
                coordinators={pat.device_id: pat}, wideq_coordinator=wideq,
                local_control=router, local_read_providers={pat.device_id: provider},
            ))
            entities = []
            await platform.async_setup_entry(None, entry, entities.extend)
            old = [e for e in entities if e.entity_description.key == "uvnano"]
            self.assertEqual(len(old), int(len(values) == 2))
            if old:
                self.assertEqual(old[0].unique_id, entity.unique_id)
                self.assertTrue(old[0].available)
