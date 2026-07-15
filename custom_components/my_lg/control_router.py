"""Validate and serialize every audited WideQ control shape."""

from __future__ import annotations

import base64
from copy import deepcopy
import json
import re
from typing import Any


class ControlValidationError(ValueError):
    """Raised before any LG request when a control payload is invalid."""


_PLACEHOLDER = re.compile(r"^\{\{([^{}]+)\}\}$")

# Same logical controls are authoritative through PAT/MQTT. They are omitted
# from generic WideQ entities and rejected by the raw WideQ service as well.
PAT_PRIORITY_FIELDS = {
    "airState.operation",
    "airState.opMode",
    "airState.windStrength",
    "airState.tempState.target",
    "airState.powerSave.basic",
    "airState.wDir.upDown",
    "airState.wDir.leftRight",
    "refState.expressMode",
    "refState.freezerTemp",
    "refState.fridgeTemp",
}


def _leaf_values(data: Any, prefix: str = "") -> dict[str, Any]:
    values: dict[str, Any] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                values.update(_leaf_values(value, path))
            else:
                values[path] = value
                values.setdefault(str(key), value)
    return values


def _render_placeholders(node: Any, snapshot: dict[str, Any]) -> Any:
    current = _leaf_values(snapshot)
    normalized: dict[str, list[Any]] = {}
    for key, value in current.items():
        token = re.sub(r"[^a-z0-9]", "", key.casefold())
        normalized.setdefault(token, []).append(value)

    def render(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: render(child) for key, child in value.items()}
        if isinstance(value, list):
            return [render(child) for child in value]
        if not isinstance(value, str):
            return value
        match = _PLACEHOLDER.match(value)
        def replacement(token: str) -> Any:
            for candidate in (
                token,
                token[0].lower() + token[1:] if token else token,
            ):
                if candidate in current:
                    return current[candidate]
            compact = re.sub(r"[^a-z0-9]", "", token.casefold())
            matches = normalized.get(compact, ())
            if len(matches) == 1:
                return matches[0]
            raise ControlValidationError(
                f"current value for required preservation field {token!r} is unavailable"
            )

        if match:
            return replacement(match.group(1))
        if "{{" in value:
            return re.sub(
                r"\{\{([^{}]+)\}\}",
                lambda found: str(replacement(found.group(1))),
                value,
            )
        return value

    return render(node)


def _leaf_paths(node: Any, prefix: str = "") -> dict[str, str]:
    paths: dict[str, str] = {}
    if not isinstance(node, dict):
        return paths
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            paths.update(_leaf_paths(value, path))
        else:
            paths[path] = path
            paths.setdefault(str(key), path)
    return paths


def _set_path(node: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    target: Any = node
    for part in parts[:-1]:
        if not isinstance(target, dict) or part not in target:
            raise ControlValidationError(f"unknown control field {path!r}")
        target = target[part]
    if not isinstance(target, dict) or parts[-1] not in target:
        raise ControlValidationError(f"unknown control field {path!r}")
    target[parts[-1]] = value


def _validate_field(spec: dict[str, Any], field: str, value: Any) -> None:
    field_spec = spec.get("fields", {}).get(field, {})
    value_type = field_spec.get("type")
    options = field_spec.get("options")
    if options and str(value) not in {str(option) for option in options}:
        raise ControlValidationError(
            f"{field}: {value!r} is not one of {', '.join(map(str, options))}"
        )
    if value_type in {"range", "number"} or "min" in field_spec:
        try:
            numeric = float(value)
        except (TypeError, ValueError) as err:
            raise ControlValidationError(f"{field}: numeric value required") from err
        if "min" in field_spec and (
            numeric < field_spec["min"] or numeric > field_spec["max"]
        ):
            raise ControlValidationError(
                f"{field}: value must be {field_spec['min']}..{field_spec['max']}"
            )
    if value_type == "boolean" and not isinstance(value, bool):
        raise ControlValidationError(f"{field}: boolean value required")
    if value_type == "string" and not isinstance(value, str):
        raise ControlValidationError(f"{field}: string value required")


def control_uses_experimental_values(
    spec: dict[str, Any], values: dict[str, Any]
) -> bool:
    """Return whether requested fields lack a complete audited value contract."""
    experimental = set(spec.get("experimental_fields", ()))
    if not experimental or not values:
        return False
    if spec.get("shape") != "template":
        return any(field in experimental for field in values)
    allowed = _leaf_paths(spec.get("template"))
    return any(allowed.get(field, field) in experimental for field in values)


def pat_priority_requested(
    spec: dict[str, Any], values: dict[str, Any]
) -> set[str]:
    """Return requested WideQ fields that must instead use PAT entities."""
    if spec.get("shape") == "template":
        allowed = _leaf_paths(spec.get("template"))
        resolved = {allowed.get(field, field) for field in values}
    else:
        resolved = set(values)
    return resolved & PAT_PRIORITY_FIELDS


def build_wideq_request(
    spec: dict[str, Any],
    *,
    command: str | None,
    values: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Return kwargs accepted by ``WideqClient.async_control``.

    No network I/O occurs here. Unknown fields, commands, invalid ranges and
    missing read-modify-write preservation values fail locally.
    """
    commands = spec.get("commands") or ["Set"]
    selected_command = command or ("Set" if "Set" in commands else commands[0])
    if selected_command not in commands:
        raise ControlValidationError(
            f"unsupported command {selected_command!r}; allowed: {commands}"
        )
    shape = spec.get("shape")

    if shape == "command":
        if values:
            raise ControlValidationError("this command accepts no data fields")
        return {"command": selected_command}

    if shape == "data_key":
        if selected_command == "Get" and not values:
            return {"command": selected_command}
        if len(values) != 1:
            raise ControlValidationError("data-key controls require exactly one field")
        field, value = next(iter(values.items()))
        if field not in spec.get("fields", {}):
            raise ControlValidationError(f"unsupported control field {field!r}")
        _validate_field(spec, field, value)
        return {"command": selected_command, "data_key": field, "value": value}

    if shape == "dataset":
        if not values:
            raise ControlValidationError("dataset controls require at least one field")
        for field, value in values.items():
            if field not in spec.get("fields", {}):
                raise ControlValidationError(f"unsupported control field {field!r}")
            _validate_field(spec, field, value)
        return {"command": selected_command, "data_set_list": dict(values)}

    if shape == "template":
        template = deepcopy(spec.get("template"))
        if template is None:
            if values:
                raise ControlValidationError("this command accepts no data fields")
            template = {}
        if not isinstance(template, dict):
            raise ControlValidationError("unexpected non-object ThinQ2 template")
        allowed = _leaf_paths(template)
        writable = set(spec.get("writable_fields", allowed.values()))
        if spec.get("requires_data") and not values:
            raise ControlValidationError(
                "this composite command requires explicit data fields"
            )
        for field, value in values.items():
            resolved = allowed.get(field)
            if resolved is None:
                raise ControlValidationError(f"unknown control field {field!r}")
            if resolved not in writable:
                raise ControlValidationError(
                    f"control field {field!r} is fixed or preservation-only"
                )
            _validate_field(spec, resolved, value)
            _set_path(template, resolved, value)
        template = _render_placeholders(template, snapshot)
        payload = {
            "ctrlKey": spec["ctrl_key"],
            "command": selected_command,
            "dataSetList": template,
        }
        return {"payload": payload}

    if shape == "binary":
        if not snapshot:
            raise ControlValidationError(
                "ThinQ1 read-modify-write requires a current full snapshot"
            )
        writable = set(spec.get("writable_fields", ()))
        for field, value in values.items():
            if field not in writable:
                raise ControlValidationError(
                    f"unsupported or preservation-only control field {field!r}"
                )
            _validate_field(spec, field, value)
        current = dict(snapshot)
        current.update(values)
        template = _render_placeholders(spec.get("template"), current)
        if not isinstance(template, str):
            raise ControlValidationError("invalid ThinQ1 binary template")
        try:
            data = json.loads(template)
        except json.JSONDecodeError as err:
            raise ControlValidationError("invalid rendered ThinQ1 data") from err
        payload = dict(spec.get("base", {}))
        payload["format"] = "B64"
        payload["data"] = base64.b64encode(bytes(data)).decode("ascii")
        return {"legacy_payload": payload}

    raise ControlValidationError(f"unsupported control shape {shape!r}")


_REMOTE_KEYS = {
    "remotestart",
    "remotecontrolenabled",
    "lworemote",
    "cooktopremotestart",
}
_REMOTE_TRUE = {
    True,
    1,
    "1",
    "ON",
    "ENABLE",
    "ENABLED",
    "REMOTE_START_ON",
    "REMOTE_CONTROL_ON",
}


def remote_control_enabled(data: Any) -> bool:
    """Return whether any PAT/WideQ branch reports remote control enabled."""
    if isinstance(data, dict):
        for key, value in data.items():
            if key.replace("_", "").casefold() in _REMOTE_KEYS and value in _REMOTE_TRUE:
                return True
            if remote_control_enabled(value):
                return True
    elif isinstance(data, list):
        return any(remote_control_enabled(item) for item in data)
    return False
