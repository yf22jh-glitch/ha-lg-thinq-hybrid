"""Styler DND is one exact four-field edit with cached own-state readback."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.exceptions import HomeAssistantError

from custom_components.my_lg.local_command import LocalCommandClient
from custom_components.my_lg.local_control_confirmed_features import augment_confirmed_features
from custom_components.my_lg.local_control_contract import (
    load_local_control_entity_contract,
    local_control_value_authorized,
    resolve_local_control_binding_eligibility,
)
from custom_components.my_lg.local_control_entity import MyLgStylerDndText
from custom_components.my_lg.local_styler_dnd import (
    MODEL,
    RESERVATION,
    canonical_reservation,
    is_canonical_reservation,
)
from tests.test_local_command import Response
from tests.test_local_control_generic_entities import Coordinator, PrimaryProvider, Router


def setup_contract():
    base = load_local_control_entity_contract()
    models = {"test_styler_binding": MODEL}
    return augment_confirmed_features(
        base,
        resolve_local_control_binding_eligibility({}, base, models),
        models,
    )


class StylerDndTests(unittest.IsolatedAsyncioTestCase):
    def test_exact_bundle_validation_and_authority(self):
        for value in (
            "on|01:00-02:00|off",
            "on|23:59-00:00|on",
            "off|00:00-00:00|off",
        ):
            self.assertEqual(canonical_reservation(value), value)
            self.assertTrue(is_canonical_reservation(value))
        for value in (
            "on|01:00-01:00|off",
            "off|01:00-02:00|off",
            "off|00:00-00:00|on",
            "on|24:00-02:00|off",
            "on|01:00-02:00",
            True,
        ):
            self.assertFalse(is_canonical_reservation(value))

        contract, scope = setup_contract()
        descriptor = next(d for d in contract.descriptors if d.capability_id == RESERVATION)
        self.assertEqual((descriptor.entity_domain, descriptor.parameter_schema),
                         ("text", "styler-do-not-disturb-reservation-v1"))
        query = dict(
            binding_id="test_styler_binding",
            model_id=MODEL,
            capability_id=RESERVATION,
            local_request_value="on|01:00-02:00|off",
        )
        self.assertTrue(local_control_value_authorized(contract, scope, **query))
        self.assertFalse(local_control_value_authorized(
            contract, scope, **{**query, "local_request_value": "off|01:00-02:00|off"}
        ))

    async def test_text_entity_uses_cached_state_and_never_keeps_optimistic_value(self):
        contract, _scope = setup_contract()
        descriptor = next(d for d in contract.descriptors if d.capability_id == RESERVATION)
        router = Router()
        router.async_appliance_setting_state = AsyncMock(return_value="on|01:00-02:00|off")
        entity = MyLgStylerDndText(
            Coordinator(MODEL), descriptor, router, PrimaryProvider(model=MODEL), None
        )
        entity.async_write_ha_state = lambda: None
        self.assertEqual((entity.native_min, entity.native_max), (17, 19))
        await entity.async_update()
        self.assertEqual(entity.native_value, "on|01:00-02:00|off")
        router.async_appliance_setting_state.return_value = "off|00:00-00:00|off"
        await entity.async_set_value("off|00:00-00:00|off")
        self.assertEqual(router.calls[-1][1:], (RESERVATION, "off|00:00-00:00|off"))
        self.assertEqual(entity.native_value, "off|00:00-00:00|off")
        with self.assertRaises(HomeAssistantError):
            await entity.async_set_value("off|01:00-02:00|off")
        self.assertEqual(len(router.calls), 1)

    async def test_cache_reader_accepts_only_exact_model_and_canonical_tuple(self):
        good = "on|01:00-02:00|off"
        for model in (MODEL, "other"):
            for value in (good, "off|00:00-00:00|off", "off|01:00-02:00|off", None, True):
                response = Response(200, {
                    "schema_version": 1,
                    "model_id": model,
                    "values": {RESERVATION: value},
                })
                client = LocalCommandClient(SimpleNamespace(get=lambda *args, **kwargs: response))
                expected = value if model == MODEL and is_canonical_reservation(value) else None
                self.assertEqual(
                    await client.async_appliance_setting_state("test/device", MODEL, RESERVATION),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
