"""Build a compact WideQ control catalog from audited LG model JSON files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
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


def risk(model: str, control: str) -> str:
    name = control.casefold()
    if model in {"WBEF3", "WMLJ32RS"} and any(
        token in name for token in ("cookstart", "autocook", "rawdatastart")
    ):
        return "hazardous"
    if model == "2REK1D04AR170":
        return "experimental"
    if any(token in name for token in ("startcourse", "resumecourse", "pausecourse", "wakeup", "wmstop", "wmoff")):
        return "operation"
    if name in {"remotemon", "qualitymngctrl", "racaddctrl", "alleventenable", "energystatectrl"}:
        return "experimental"
    return "low"


def value_spec(values: dict[str, Any], field: str) -> dict[str, Any]:
    spec = values.get(field) or values.get(field.split(".")[-1]) or {}
    if not spec:
        wanted = field.split(".")[-1].casefold()
        spec = next(
            (value for key, value in values.items() if str(key).casefold() == wanted),
            {},
        )
    value_type = spec.get("data_type") or spec.get("dataType") or spec.get("type")
    result: dict[str, Any] = {
        "type": str(value_type or "unknown").lower(),
        "verified": bool(spec),
    }
    mapping = (
        spec.get("value_mapping")
        or spec.get("valueMapping")
        or spec.get("option")
    )
    if isinstance(mapping, dict):
        result["options"] = list(mapping)
    validation = spec.get("value_validation") or spec.get("valueMapping")
    if isinstance(validation, dict) and "min" in validation:
        result.update(
            {
                "min": validation.get("min"),
                "max": validation.get("max"),
                "step": validation.get("step", 1),
            }
        )
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
    values: dict[str, Any], path: tuple[str, ...], template_value: Any
) -> dict[str, Any]:
    token = None
    if isinstance(template_value, str):
        match = re.fullmatch(r"\{\{([^{}]+)\}\}", template_value)
        token = match.group(1) if match else None
        if token is None and template_value in values:
            token = template_value
    spec = value_spec(values, token or path[-1])
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
        fields_spec = {field: value_spec(values, field) for field in fields}
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
            ".".join(path): template_field_spec(values, path, value)
            for path, value in leaves
            if not is_payload_metadata(path) and not is_preservation_only(path)
        }
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
                field: value_spec(data.get("Value", {}), field) for field in fields
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
    return controls


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("model_dir", nargs="?", default="/tmp/lg_models")
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
    OUTPUT.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
