"""AIR2C power is a generic exact-state switch, never an appliance-cache owner."""

import unittest
from types import SimpleNamespace

from custom_components.my_lg.local_control_confirmed_features import (
    APPLIANCE_SETTING_MODELS,
    augment_confirmed_features,
    load_confirmed_features,
)
from custom_components.my_lg.local_control_contract import (
    load_local_control_entity_contract,
    local_control_value_authorized,
    resolve_local_control_binding_eligibility,
)
from custom_components.my_lg.local_control_entity import (
    MyLgApplianceSettingSwitch,
    MyLgLocalContractSwitch,
    local_control_entities_for_domain,
)
from tests.test_local_control_generic_entities import (
    BINDING_ID,
    Coordinator,
    PrimaryProvider,
    Router,
)

MODEL = "AIR_2C0001_WW"
CAPABILITY = "operation.power_requested"


class AirTowerPowerTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_new_owner_uses_the_existing_semantic_read_feed(self) -> None:
        base = load_local_control_entity_contract()
        models = {BINDING_ID: MODEL}
        contract, scope = augment_confirmed_features(
            base,
            resolve_local_control_binding_eligibility({}, base, models),
            models,
        )
        self.assertFalse(
            any(
                descriptor.capability_id == CAPABILITY
                for descriptor in base.descriptors_by_model[MODEL]
            )
        )
        descriptor = next(
            descriptor
            for descriptor in contract.descriptors_by_model[MODEL]
            if descriptor.capability_id == CAPABILITY
        )
        self.assertEqual(descriptor.entity_domain, "switch")
        self.assertEqual(descriptor.exact_state_semantic, CAPABILITY)
        self.assertTrue(descriptor.factory_eligible)
        self.assertFalse(descriptor.existing_owner)
        self.assertNotIn((MODEL, CAPABILITY), APPLIANCE_SETTING_MODELS)

        feature = next(
            feature
            for feature in load_confirmed_features()
            if feature["model_id"] == MODEL
            and feature["capability_id"] == CAPABILITY
        )
        self.assertFalse(feature["existing_owner"])
        self.assertEqual(
            [value["ordered_frame_sha256s"] for value in feature["values"]],
            [
                ["7497f7a6dd76e3b2ff4cbd2fb5ab5a2b3f8d7f9eb793acf2e5466e679c72345c"],
                ["eb0d5dda0c6b756f878d3c21dbdce8b3693b2051af8292b43b12c77047931921"],
            ],
        )

        coordinator = Coordinator(MODEL)
        primary = PrimaryProvider({CAPABILITY: False}, model=MODEL)
        router = Router()
        data = SimpleNamespace(
            local_control_entity_contract=contract,
            local_control=router,
            local_control_binding_eligibility=scope,
            coordinators={"air2c": coordinator},
            local_providers={coordinator.device_id: primary},
            local_read_providers={},
        )
        entities = [
            entity
            for entity in local_control_entities_for_domain(
                SimpleNamespace(runtime_data=data), "switch"
            )
            if entity._descriptor.capability_id == CAPABILITY
        ]
        self.assertEqual(len(entities), 1)
        entity = entities[0]
        self.assertIsInstance(entity, MyLgLocalContractSwitch)
        self.assertNotIsInstance(entity, MyLgApplianceSettingSwitch)
        self.assertFalse(entity.is_on)
        await entity.async_turn_on()
        await entity.async_turn_off()
        self.assertEqual(
            router.calls[-2:],
            [
                (coordinator.device_id, CAPABILITY, "true"),
                (coordinator.device_id, CAPABILITY, "false"),
            ],
        )
        for value in ("false", "true", "0", "1"):
            self.assertEqual(
                local_control_value_authorized(
                    contract,
                    scope,
                    binding_id=BINDING_ID,
                    model_id=MODEL,
                    capability_id=CAPABILITY,
                    local_request_value=value,
                ),
                value in ("false", "true"),
            )
