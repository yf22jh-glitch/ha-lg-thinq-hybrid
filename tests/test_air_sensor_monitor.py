"""AIR910 private sensor-monitoring mode uses the own-state polling select."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from custom_components.my_lg.local_command import LocalCommandClient
from custom_components.my_lg.local_control_confirmed_features import (
    APPLIANCE_VALUE_MODELS,
    augment_confirmed_features,
    load_confirmed_features,
)
from custom_components.my_lg.local_control_contract import (
    load_local_control_entity_contract,
    local_control_value_authorized,
    resolve_local_control_binding_eligibility,
)
from custom_components.my_lg.local_control_entity import (
    MyLgApplianceSettingSelect,
    local_control_entities_for_domain,
)
from tests.test_local_command import Response
from tests.test_local_control_generic_entities import (
    BINDING_ID,
    Coordinator,
    PrimaryProvider,
    Router,
)

MODELS = (
    "AIR_910604_WW",
    "HUM_056905_WW",
    "DHUM_056905_WW",
)
MODEL = MODELS[0]
OTHER_AIR_MODEL = "AIR_2C0001_WW"
CAPABILITY = "air_quality.monitor_mode"


class AirSensorMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_feature_factory_options_state_and_request(self) -> None:
        entity_keys = {
            "AIR_910604_WW": "local_air_sensor_monitor_mode",
            "HUM_056905_WW": "local_hum_sensor_monitor_mode",
            "DHUM_056905_WW": "local_dhum_sensor_monitor_mode",
        }
        for model in MODELS:
            with self.subTest(model=model):
                base = load_local_control_entity_contract()
                models = {BINDING_ID: model}
                contract, scope = augment_confirmed_features(
                    base,
                    resolve_local_control_binding_eligibility({}, base, models),
                    models,
                )
                feature = next(
                    row
                    for row in load_confirmed_features()
                    if row["model_id"] == model
                    and row["capability_id"] == CAPABILITY
                )
                self.assertEqual(feature["entity_key"], entity_keys[model])
                self.assertEqual(
                    [(value["value"], value["label"]) for value in feature["values"]],
                    [("operation_only", "운전 중"), ("always", "항상")],
                )
                self.assertEqual(
                    [value["ordered_frame_sha256s"] for value in feature["values"]],
                    [
                        ["3c7796deda7a8aaf366820dd125f7ebd4588a5ee7e1970378858fd1cf236cbf3"],
                        ["83ce18e4ecf1d3513042dd1e6f955ccacf4a3b0413a54fd6848c9766eeecf2eb"],
                    ],
                )
                self.assertEqual(APPLIANCE_VALUE_MODELS[(model, CAPABILITY)], model)

                coordinator = Coordinator(model)
                primary = PrimaryProvider(model=model)
                router = Router()
                router.async_appliance_setting_state = AsyncMock(return_value="always")
                data = SimpleNamespace(
                    local_control_entity_contract=contract,
                    local_control=router,
                    local_control_binding_eligibility=scope,
                    coordinators={"air": coordinator},
                    local_providers={coordinator.device_id: primary},
                    local_read_providers={},
                )
                selected = [
                    entity
                    for entity in local_control_entities_for_domain(
                        SimpleNamespace(runtime_data=data), "select"
                    )
                    if entity._descriptor.capability_id == CAPABILITY
                ]
                self.assertEqual(len(selected), 1)
                entity = selected[0]
                self.assertIsInstance(entity, MyLgApplianceSettingSelect)
                entity.async_write_ha_state = lambda: None
                self.assertEqual(entity.options, ["운전 중", "항상"])
                await entity.async_update()
                self.assertEqual(entity.current_option, "항상")
                await entity.async_select_option("운전 중")
                self.assertEqual(router.calls[-1][1:], (CAPABILITY, "operation_only"))
                self.assertEqual(entity.current_option, "항상")
                for value in ("operation_only", "always", "inOperating", "1"):
                    self.assertEqual(
                        local_control_value_authorized(
                            contract,
                            scope,
                            binding_id=BINDING_ID,
                            model_id=model,
                            capability_id=CAPABILITY,
                            local_request_value=value,
                        ),
                        value in ("operation_only", "always"),
                    )
        self.assertNotIn((OTHER_AIR_MODEL, CAPABILITY), APPLIANCE_VALUE_MODELS)

    def test_existing_air2c_owner_is_not_replaced_by_air910_polling(self) -> None:
        base = load_local_control_entity_contract()
        models = {BINDING_ID: OTHER_AIR_MODEL}
        contract, scope = augment_confirmed_features(
            base,
            resolve_local_control_binding_eligibility({}, base, models),
            models,
        )
        coordinator = Coordinator(OTHER_AIR_MODEL)
        primary = PrimaryProvider(model=OTHER_AIR_MODEL)
        data = SimpleNamespace(
            local_control_entity_contract=contract,
            local_control=Router(),
            local_control_binding_eligibility=scope,
            coordinators={"air2c": coordinator},
            local_providers={coordinator.device_id: primary},
            local_read_providers={},
        )
        existing = [
            entity
            for entity in local_control_entities_for_domain(
                SimpleNamespace(runtime_data=data), "select"
            )
            if entity._descriptor.capability_id == CAPABILITY
        ]
        self.assertEqual(len(existing), 1)
        self.assertNotIsInstance(existing[0], MyLgApplianceSettingSelect)

    async def test_state_endpoint_requires_exact_model_and_canonical_mode(self) -> None:
        for requested_model in MODELS:
            for reported_model in (*MODELS, OTHER_AIR_MODEL):
                for value in ("operation_only", "always", "inOperating", "1", 1, True, None):
                    response = Response(
                        200,
                        {
                            "schema_version": 1,
                            "model_id": reported_model,
                            "values": {CAPABILITY: value},
                        },
                    )
                    client = LocalCommandClient(
                        SimpleNamespace(get=lambda *args, **kwargs: response)
                    )
                    self.assertEqual(
                        await client.async_appliance_setting_state(
                            "test/device", requested_model, CAPABILITY
                        ),
                        value
                        if reported_model == requested_model
                        and value in ("operation_only", "always")
                        else None,
                    )
