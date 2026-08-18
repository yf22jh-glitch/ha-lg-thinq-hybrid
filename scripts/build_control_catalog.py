"""Build a compact WideQ control catalog from audited LG model JSON files."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "custom_components" / "my_lg" / "feature_catalog" / "wideq_controls.json"

_PARAMETERLESS_ACTIONS = {
    "getactivesaving",
    "getactiveiceplus",
    "getsmartfresh",
    "offpower",
    "onpower",
    "ovwakeup",
    "pausecourse",
    "resetdownloadrecipe",
    "resumecourse",
    "setclearrecipe",
    "setcookstop",
    "wakeup",
    "wmoff",
    "wmpause",
    "wmresume",
    "wmstop",
    "wmwakeup",
}

_PAYLOAD_METADATA_TOKENS = {
    "cmdoptioncontentstype",
    "cmdoptiondatalength",
    "contenttype",
    "controldatatype",
    "controldatavaluelength",
    "coursedownloaddatalength",
    "coursedownloadtype",
    "datalength",
    "producttype",
    "reqdevtype",
}

_NON_COMMAND_ENUM_VALUES = frozenset({"IGNORE"})

# The exact vacuum app renders these as one-shot actions. ModelJSON reuses the
# monitoring enum and therefore also lists terminal/error states that the app
# never sends as command input.
_CONTROL_ENUM_ALLOWLIST: dict[tuple[str, str, str], tuple[str, ...]] = {
    (
        "HWWA9X3C_F2U",
        "QMControl_RemoteDustEmptying",
        "qmState.remoteDustEmptying",
    ): ("ON", "ROBOT_ON"),
    (
        "HWWA9X3C_F2U",
        "QMControl_RemoteUvc",
        "qmState.remoteUvc",
    ): ("ON",),
}

# These exact vacuum fields have repeated live-write evidence: 59 actual
# writes with API receipts and immediate local echo, plus 58 cloud state
# transitions. Only these one-field templates may use ACK + fresh field echo
# as their end-to-end verification contract. The other vacuum controls remain
# catalogued for diagnostics but fail closed in Home Assistant.
_FIELD_ECHO_VERIFICATION_ALLOWLIST: dict[
    tuple[str, str | None, str], str
] = {
    ("HWWA9X3C_F2U", None, "QMControl_AiSuctionForce"): (
        "qmState.aiSuctionForce"
    ),
    ("HWWA9X3C_F2U", None, "QMControl_BatteryLifeMode"): (
        "qmState.batteryLifeMode"
    ),
    ("HWWA9X3C_F2U", None, "QMControl_Brightness"): "qmState.brightness",
    ("HWWA9X3C_F2U", None, "QMControl_MopWithSucking"): (
        "qmState.mopWithSucking"
    ),
    ("HWWA9X3C_F2U", None, "QMControl_StopAndGoSetting"): (
        "qmState.stopAndGoSetting"
    ),
    ("HWWA9X3C_F2U", None, "QMControl_SuctionForce"): (
        "qmState.suctionForce"
    ),
    ("HWWA9X3C_F2U", None, "QMControl_WaterSupply"): "qmState.waterSupply",
    ("HWWA9X3C_F2U", None, "QMControl_SteamSupply"): "qmState.steamSupply",
    ("HWWA9X3C_F2U", None, "QMControl_Volume"): "qmState.volume",
    ("HWWA9X3C_F2U", None, "QMControl_SettingButtonSound"): (
        "qmState.settingButtonSound"
    ),
    ("HWWA9X3C_F2U", None, "QMControl_ChargingMelody"): (
        "qmState.chargingMelody"
    ),
    ("HWWA9X3C_F2U", None, "QMControl_DustEmptyingMelody"): (
        "qmState.dustEmptyingMelody"
    ),
}

_FIELD_ECHO_CONTROL_DATA_TYPES: dict[tuple[str, str | None, str], str] = {
    ("HWWA9X3C_F2U", None, "QMControl_AiSuctionForce"): "AI_SUCTION_FORCE",
    ("HWWA9X3C_F2U", None, "QMControl_BatteryLifeMode"): "BATTERY_LIFE_MODE",
    ("HWWA9X3C_F2U", None, "QMControl_Brightness"): "BRIGHTNESS",
    ("HWWA9X3C_F2U", None, "QMControl_MopWithSucking"): "MOP_SETTING",
    ("HWWA9X3C_F2U", None, "QMControl_StopAndGoSetting"): (
        "STOP_AND_GO_SETTING"
    ),
    ("HWWA9X3C_F2U", None, "QMControl_SuctionForce"): "SUCTION_FORCE",
    ("HWWA9X3C_F2U", None, "QMControl_WaterSupply"): "WATER_SUPPLY",
    ("HWWA9X3C_F2U", None, "QMControl_SteamSupply"): "STEAM_SUPPLY",
    ("HWWA9X3C_F2U", None, "QMControl_Volume"): "VOLUME",
    ("HWWA9X3C_F2U", None, "QMControl_SettingButtonSound"): (
        "SETTING_BUTTON_SOUND"
    ),
    ("HWWA9X3C_F2U", None, "QMControl_ChargingMelody"): "CHARGING_MELODY",
    ("HWWA9X3C_F2U", None, "QMControl_DustEmptyingMelody"): (
        "DUST_EMPTYING_MELODY"
    ),
}

_STYLER_ACTIVE_STATES = [
    "RESERVED",
    "DETECTING",
    "PRESTEAM",
    "STERILIZE",
    "STEAM_SPRAY",
    "REFRESH",
    "DRYING",
    "AIRCARE",
    "DEHUME",
    "STEAMER_RUNNING",
    "STEAMER_CLEANING",
    "STEAMER_CLEANING_AUTO",
]

# State transitions below come from the exact model State domain and are kept
# intentionally small. Controls without one of these contracts stay blocked by
# the HA composite service rather than treating HTTP 200 as appliance success.
_CONTROL_VERIFICATION: dict[tuple[str, str | None, str], dict[str, Any]] = {
    **{
        contract_key: {
            "mode": "field_echo",
            "required_request_fields": [field],
            "echo_fields": [field],
            "readback_delays_s": [5, 10],
        }
        for contract_key, field in _FIELD_ECHO_VERIFICATION_ALLOWLIST.items()
    },
    ("ST_R_ETH01Y_", None, "pauseCourse"): {
        "preconditions": {"styler.state": _STYLER_ACTIVE_STATES},
        "postconditions": {"styler.state": ["PAUSE"]},
        "required_request_fields": [],
        "echo_fields": [],
        "readback_delays_s": [5, 10],
    },
    ("ST_R_ETH01Y_", None, "wakeup"): {
        "preconditions": {"styler.state": ["POWEROFF", "SLEEP"]},
        "postconditions": {"styler.state": ["INITIAL"]},
        "required_request_fields": [],
        "echo_fields": [],
        "readback_delays_s": [5, 10],
    },
}

_CONTROL_VERIFIED_TEMPLATES: dict[
    tuple[str, str | None, str], dict[str, Any]
] = {
    ("ST_R_ETH01Y_", None, "pauseCourse"): {
        "styler": {
            "controlDataType": "PAUSE",
            "controlDataValueLength": 1,
            "controlDataValue": 0,
        }
    },
    ("ST_R_ETH01Y_", None, "wakeup"): {
        "styler": {
            "controlDataType": "WAKEUP",
            "controlDataValueLength": 0,
        }
    },
}


def risk(model: str, control: str) -> str:
    name = control.casefold()
    if model in {"WBEF3", "WMLJ32RS"} and any(
        token in name for token in ("cookstart", "autocook", "rawdatastart")
    ):
        return "hazardous"
    if model == "2REK1D04AR170":
        return "experimental"
    if model == "HWWA9X3C_F2U" and control in {
        "QMControl_RemoteDustEmptying",
        "QMControl_RemoteUvc",
        "QMControl_ReserveDustEmptying",
        "QMControl_ReserveDustEmptyingEnable",
    }:
        return "operation"
    if any(token in name for token in ("startcourse", "resumecourse", "pausecourse", "wakeup", "wmstop", "wmoff")):
        return "operation"
    if name in {"remotemon", "qualitymngctrl", "racaddctrl", "alleventenable", "energystatectrl"}:
        return "experimental"
    return "low"


def validate_control_verification_source(
    model: str,
    subdevice: str | None,
    control: str,
    template: Any,
    verification: dict[str, Any],
    values: dict[str, Any],
    document: dict[str, Any],
) -> None:
    """Bind a writable verification contract to exact audited model data."""
    contract_key = (model, subdevice, control)
    if verification.get("mode") == "field_echo":
        expected_field = _FIELD_ECHO_VERIFICATION_ALLOWLIST.get(contract_key)
        expected_data_type = _FIELD_ECHO_CONTROL_DATA_TYPES.get(contract_key)
        expected_contract = {
            "mode": "field_echo",
            "required_request_fields": [expected_field],
            "echo_fields": [expected_field],
            "readback_delays_s": [5, 10],
        }
        writable_leaves = [
            (".".join(path), value)
            for path, value in template_leaves(template)
            if not is_payload_metadata(path) and not is_preservation_only(path)
        ]
        qm_state = template.get("qmState") if isinstance(template, dict) else None
        expected_leaf = expected_field.rsplit(".", 1)[-1] if expected_field else None
        if (
            expected_field is None
            or expected_data_type is None
            or verification != expected_contract
            or len(writable_leaves) != 1
            or writable_leaves[0][0] != expected_field
            or not isinstance(qm_state, dict)
            or set(qm_state)
            != {expected_leaf, "controlDataType", "controlDataValueLength"}
            or qm_state.get("controlDataType") != expected_data_type
            or qm_state.get("controlDataValueLength") != 1
        ):
            raise RuntimeError(
                f"verified field-echo template changed for {model}.{control}"
            )
        field_contract = template_field_spec(
            values,
            tuple(expected_field.split(".")),
            writable_leaves[0][1],
            document,
        )
        if not field_contract.get("verified"):
            raise RuntimeError(
                f"verified field domain missing for {model}.{control}.{expected_field}"
            )
        return
    if template != _CONTROL_VERIFIED_TEMPLATES.get(contract_key):
        raise RuntimeError(
            f"verified control template changed for {model}.{control}"
        )
    for phase in ("preconditions", "postconditions"):
        conditions = verification.get(phase)
        if not isinstance(conditions, dict) or not conditions:
            raise RuntimeError(
                f"verified control {phase} missing for {model}.{control}"
            )
        for field, candidates in conditions.items():
            field_contract = value_spec(values, field, document)
            options = field_contract.get("options")
            if not field_contract.get("verified") or not isinstance(options, list):
                raise RuntimeError(
                    f"verified state domain missing for {model}.{control}.{field}"
                )
            unknown = set(candidates) - set(options)
            if unknown:
                raise RuntimeError(
                    f"verified state values disappeared for {model}.{control}.{field}: "
                    f"{', '.join(sorted(map(str, unknown)))}"
                )


def value_spec(
    values: dict[str, Any],
    field: str,
    document: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec = values.get(field) or values.get(field.split(".")[-1]) or {}
    if not spec:
        wanted = field.split(".")[-1].casefold()
        spec = next(
            (value for key, value in values.items() if str(key).casefold() == wanted),
            {},
        )
    value_type = spec.get("data_type") or spec.get("dataType") or spec.get("type")
    normalized_type = str(value_type or "unknown").lower()
    result: dict[str, Any] = {
        "type": normalized_type,
        "verified": bool(spec),
    }
    mapping = (
        spec.get("value_mapping")
        or spec.get("valueMapping")
        or spec.get("option")
    )
    if (
        normalized_type in {"range", "number", "integer"}
        and isinstance(mapping, dict)
        and "min" in mapping
        and "max" in mapping
    ):
        result.update(
            {
                "min": mapping["min"],
                "max": mapping["max"],
                "step": mapping.get("step", 1),
            }
        )
    elif isinstance(mapping, dict):
        options = [
            option
            for option in mapping
            if str(option).upper() not in _NON_COMMAND_ENUM_VALUES
        ]
        if options:
            result["options"] = options
        else:
            result["verified"] = False
    validation = (
        spec.get("value_validation")
        or spec.get("valueValidation")
        or spec.get("valueMapping")
    )
    if isinstance(validation, dict) and "min" in validation:
        result.update(
            {
                "min": validation.get("min"),
                "max": validation.get("max"),
                "step": validation.get("step", 1),
            }
        )
    if normalized_type == "reference":
        reference_names = spec.get("option")
        if isinstance(reference_names, str):
            reference_names = [reference_names]
        wire_options: list[str] = []
        if isinstance(reference_names, list) and isinstance(document, dict):
            for reference_name in reference_names:
                table = document.get(reference_name)
                rule = document.get("ConvertingRule", {}).get(reference_name, {})
                control_rule = (
                    rule.get("ControlConvertingRule")
                    if isinstance(rule, dict)
                    else None
                )
                if not isinstance(table, dict) or not isinstance(control_rule, dict):
                    continue
                for raw_value, item in table.items():
                    if not isinstance(item, dict):
                        continue
                    # The official appModule passes the selected table object
                    # to gsmT2DataController. Its Reference encoder takes the
                    # object's id, then applies ControlConvertingRule. The
                    # resulting semantic protocol token (not the numeric table
                    # key and not the human _comment) is the wire value.
                    item_id = str(item.get("id", raw_value))
                    wire_value = control_rule.get(item_id)
                    if isinstance(wire_value, str) and wire_value:
                        wire_options.append(wire_value)
        if wire_options:
            result["options"] = list(dict.fromkeys(wire_options))
        else:
            # A Reference node without its exact target domain is not a
            # validated writable value contract.
            result["verified"] = False
    return result


def template_leaves(
    node: Any, prefix: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], Any]]:
    if isinstance(node, dict):
        leaves: list[tuple[tuple[str, ...], Any]] = []
        for key, value in node.items():
            leaves.extend(template_leaves(value, (*prefix, str(key))))
        return leaves
    if isinstance(node, list):
        return []
    return [(prefix, node)] if prefix else []


def is_payload_metadata(path: tuple[str, ...]) -> bool:
    leaf = path[-1].replace("_", "").casefold()
    return leaf in _PAYLOAD_METADATA_TOKENS or leaf.startswith("reservedvalue")


def is_preservation_only(path: tuple[str, ...]) -> bool:
    """Identify observations/internal course-engine values copied into a payload."""
    leaf = path[-1].replace("_", "").casefold()
    if leaf in {
        "atleastonedooropen",
        "diddooropen",
        "door",
        "doorlock",
        "error",
        "monstatus",
        "notification",
        "rinserefill",
        "saltrefill",
        "state",
        "tempunit",
    }:
        return True
    if "dooropen" in leaf or "rpm" in leaf:
        return True
    return any(
        leaf.startswith(prefix) and leaf.endswith("time")
        for prefix in (
            "cooling",
            "drying",
            "heating",
            "preheat",
            "presteam",
            "steam",
        )
    )


def template_field_spec(
    values: dict[str, Any],
    path: tuple[str, ...],
    template_value: Any,
    document: dict[str, Any],
) -> dict[str, Any]:
    token = None
    if isinstance(template_value, str):
        match = re.fullmatch(r"\{\{([^{}]+)\}\}", template_value)
        token = match.group(1) if match else None
        if token is None and template_value in values:
            token = template_value
    spec = value_spec(values, token or path[-1], document)
    if spec["type"] == "unknown":
        if isinstance(template_value, bool):
            spec["type"] = "boolean"
        elif isinstance(template_value, (int, float)):
            spec["type"] = "number"
        elif isinstance(template_value, str):
            spec["type"] = "string"
    return spec


def ac_controls(model: str, data: dict[str, Any]) -> dict[str, Any]:
    controls: dict[str, Any] = {}
    values = data.get("Value", {})
    for item in data.get("ControlDevice", []):
        key = item.get("ctrlKey")
        if not key:
            continue
        fields = []
        shape = "command"
        if isinstance(item.get("dataSetList"), dict):
            fields = list(item["dataSetList"])
            shape = "dataset"
        elif isinstance(item.get("dataKey"), str):
            fields = [field for field in item["dataKey"].split("|") if field]
            shape = "data_key"
        fields_spec = {
            field: value_spec(values, field, data) for field in fields
        }
        controls[key] = {
            "ctrl_key": key,
            "shape": shape,
            "commands": [cmd for cmd in str(item.get("command", "Set")).split("|") if cmd],
            "fields": fields_spec,
            "experimental_fields": [
                field for field, spec in fields_spec.items() if not spec["verified"]
            ],
            "risk": risk(model, key),
        }
    return controls


def wifi_controls(model: str, data: dict[str, Any], subdevice: str | None = None) -> dict[str, Any]:
    wifi = data.get("ControlWifi")
    if not isinstance(wifi, dict):
        return {}
    controls: dict[str, Any] = {}
    actions = wifi.get("action")
    if isinstance(actions, dict):
        items = actions.items()
        platform = "thinq1" if wifi.get("type") == "BINARY(BYTE)" else "thinq2"
    else:
        items = ((key, value) for key, value in wifi.items() if key not in {"type", "action"})
        platform = "thinq2"
    values = data.get("Value") or data.get("MonitoringValue") or {}
    for key, item in items:
        if not isinstance(item, dict):
            continue
        template = item.get("dataForm")
        if template is None:
            template = item.get("data")
        # Empty Set payloads are model declarations, not usable writes.
        if item.get("command") == "Set" and not template:
            continue
        parameterless = (
            str(item.get("command") or item.get("cmdOpt", "")).casefold() == "get"
            or key.casefold() in _PARAMETERLESS_ACTIONS
        )
        leaves = template_leaves(template)
        fields = {
            ".".join(path): template_field_spec(values, path, value, data)
            for path, value in leaves
            if not is_payload_metadata(path) and not is_preservation_only(path)
        }
        for field, field_spec in fields.items():
            allowlist = _CONTROL_ENUM_ALLOWLIST.get((model, key, field))
            if allowlist is not None:
                model_options = field_spec.get("options")
                if not isinstance(model_options, list) or not set(allowlist).issubset(
                    model_options
                ):
                    raise RuntimeError(
                        f"audited control enum changed for {model}.{key}.{field}"
                    )
                field_spec["options"] = list(allowlist)
        writable_fields = list(fields) if not parameterless else []
        controls[key] = {
            "ctrl_key": key,
            "shape": "binary" if platform == "thinq1" else "template",
            "commands": [item.get("command") or item.get("cmdOpt") or "Set"],
            "template": template,
            "base": {
                name: value
                for name, value in item.items()
                if name not in {"command", "data", "dataForm"}
            },
            "platform": platform,
            "risk": risk(model, key),
            "parameterless": parameterless,
        }
        if platform == "thinq2":
            controls[key]["fields"] = fields
            controls[key]["writable_fields"] = writable_fields
            controls[key]["requires_data"] = bool(writable_fields)
            controls[key]["experimental_fields"] = [
                field for field in writable_fields if not fields[field]["verified"]
            ]
        if platform == "thinq1" and isinstance(template, str):
            fields = list(dict.fromkeys(re.findall(r"\{\{([^{}]+)\}\}", template)))
            controls[key]["fields"] = {
                field: value_spec(data.get("Value", {}), field, data)
                for field in fields
            }
            # These values are required in the all-fields binary packet but are
            # observations, not user controls. They are preserved from the live
            # snapshot and may never be overridden by a service call.
            controls[key]["writable_fields"] = [
                field
                for field in fields
                if field not in {"DoorOpenState", "FreshAirFilter"}
            ]
            controls[key]["requires_data"] = True
            controls[key]["experimental_fields"] = controls[key][
                "writable_fields"
            ]
        if subdevice:
            controls[key]["subdevice"] = subdevice
        verification = _CONTROL_VERIFICATION.get((model, subdevice, key))
        if verification is not None:
            validate_control_verification_source(
                model,
                subdevice,
                key,
                template,
                verification,
                values,
                data,
            )
            controls[key]["verification"] = verification
    return controls


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("model_dir", nargs="?", default="/tmp/lg_models")
    parser.add_argument(
        "--base-catalog",
        type=Path,
        help=(
            "optional existing audited catalogue whose model/control scope is "
            "preserved while field contracts are regenerated"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT,
        help="catalogue output path",
    )
    args = parser.parse_args()
    model_dir = Path(args.model_dir)
    catalog: dict[str, Any] = {}
    for path in sorted(model_dir.glob("*.json")):
        model = path.stem
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp949")
        data = json.loads(text)
        controls = ac_controls(model, data)
        controls.update(wifi_controls(model, data))
        subdevices: dict[str, Any] = {}
        for part in ("washer", "dryer"):
            block = data.get(part)
            if isinstance(block, dict):
                subdevices[part] = wifi_controls(model, block, part)
        catalog[model] = {"controls": controls, "subdevices": subdevices}
    if args.base_catalog is not None:
        audited = json.loads(args.base_catalog.read_text(encoding="utf-8"))
        scoped: dict[str, Any] = {}
        for model, existing in audited.items():
            generated = catalog.get(model)
            if generated is None:
                raise RuntimeError(
                    f"audited model disappeared from generated catalogue: {model}"
                )
            missing_controls = set(existing.get("controls", {})) - set(
                generated.get("controls", {})
            )
            if missing_controls:
                raise RuntimeError(
                    f"audited controls disappeared for {model}: "
                    f"{', '.join(sorted(missing_controls))}"
                )
            scoped_controls = {
                name: generated["controls"][name]
                for name in existing.get("controls", {})
            }
            scoped_subdevices: dict[str, Any] = {}
            for subdevice, existing_controls in existing.get(
                "subdevices", {}
            ).items():
                generated_controls = generated.get("subdevices", {}).get(subdevice)
                if generated_controls is None:
                    raise RuntimeError(
                        f"audited subdevice disappeared for {model}: {subdevice}"
                    )
                missing_subdevice_controls = set(existing_controls) - set(
                    generated_controls
                )
                if missing_subdevice_controls:
                    raise RuntimeError(
                        f"audited subdevice controls disappeared for {model}.{subdevice}: "
                        f"{', '.join(sorted(missing_subdevice_controls))}"
                    )
                scoped_subdevices[subdevice] = {
                    name: generated_controls[name]
                    for name in existing_controls
                }
            scoped[model] = {
                "controls": scoped_controls,
                "subdevices": scoped_subdevices,
            }
        catalog = scoped
    args.output.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
