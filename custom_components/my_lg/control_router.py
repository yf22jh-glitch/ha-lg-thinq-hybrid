"""Validate and serialize every audited WideQ control shape."""

from __future__ import annotations

import base64
import json
import math
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any


class ControlValidationError(ValueError):
    """Raised before any LG request when a control payload is invalid."""


_PLACEHOLDER = re.compile(r"^\{\{([^{}]+)\}\}$")
_MISSING = object()

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
    if value_type in {"range", "number", "integer"} or "min" in field_spec:
        if isinstance(value, bool):
            raise ControlValidationError(f"{field}: numeric value required")
        try:
            numeric = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as err:
            raise ControlValidationError(f"{field}: numeric value required") from err
        if not numeric.is_finite():
            raise ControlValidationError(f"{field}: finite numeric value required")
        if value_type == "integer" and numeric != numeric.to_integral_value():
            raise ControlValidationError(f"{field}: integer value required")
        if "min" in field_spec and (
            numeric < Decimal(str(field_spec["min"]))
            or numeric > Decimal(str(field_spec["max"]))
        ):
            raise ControlValidationError(
                f"{field}: value must be {field_spec['min']}..{field_spec['max']}"
            )
        if "min" in field_spec and "step" in field_spec:
            try:
                minimum = Decimal(str(field_spec["min"]))
                step = Decimal(str(field_spec["step"]))
            except (InvalidOperation, TypeError, ValueError) as err:
                raise ControlValidationError(
                    f"{field}: invalid model step contract"
                ) from err
            if not step.is_finite() or step <= 0 or (numeric - minimum) % step:
                raise ControlValidationError(
                    f"{field}: value must align to step {field_spec['step']} "
                    f"from {field_spec['min']}"
                )
    if value_type == "boolean" and not isinstance(value, bool):
        raise ControlValidationError(f"{field}: boolean value required")
    if value_type == "string" and not isinstance(value, str):
        raise ControlValidationError(f"{field}: string value required")


def _encoded_field_value(spec: dict[str, Any], field: str, value: Any) -> Any:
    """Encode one validated semantic label into its exact model wire value."""
    field_spec = spec.get("fields", {}).get(field, {})
    value_map = field_spec.get("value_map")
    if isinstance(value_map, dict):
        try:
            return value_map[str(value)]
        except KeyError as err:
            raise ControlValidationError(
                f"{field}: reference value is not mapped"
            ) from err
    if field_spec.get("type") in {"range", "number", "integer"}:
        numeric = Decimal(str(value))
        if numeric == numeric.to_integral_value():
            return int(numeric)
        return float(numeric)
    return value


def _snapshot_field(snapshot: dict[str, Any], field: str) -> Any:
    if field in snapshot:
        return snapshot[field]
    node: Any = snapshot
    for token in field.split("."):
        if not isinstance(node, dict) or token not in node:
            return _MISSING
        node = node[token]
    return node


def _state_value_matches(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is bool and type(expected) is bool and actual is expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        try:
            return Decimal(str(actual)) == Decimal(str(expected))
        except InvalidOperation:
            return False
    return actual == expected


def _validate_condition_map(name: str, conditions: Any) -> dict[str, list[Any]]:
    """Return one complete condition map or reject it before a write."""
    if not isinstance(conditions, dict) or not conditions:
        raise ControlValidationError("control verification contract is invalid")
    for field, allowed in conditions.items():
        if (
            not isinstance(field, str)
            or not field
            or not isinstance(allowed, list)
            or not allowed
        ):
            raise ControlValidationError(
                f"control verification {name} contract is invalid"
            )
    return conditions


def _validate_field_list(name: str, fields: Any) -> list[str]:
    """Return a unique field list or reject it before a write."""
    if (
        not isinstance(fields, list)
        or not all(isinstance(field, str) and field for field in fields)
        or len(fields) != len(set(fields))
    ):
        raise ControlValidationError(
            f"control verification {name} contract is invalid"
        )
    return fields


def _has_observable_transition(
    preconditions: dict[str, list[Any]],
    postconditions: dict[str, list[Any]],
) -> bool:
    """Return whether at least one field proves a pre/post state transition."""
    for field in preconditions.keys() & postconditions.keys():
        if not any(
            _state_value_matches(before, after)
            for before in preconditions[field]
            for after in postconditions[field]
        ):
            return True
    return False


def control_verification_schedule(
    spec: dict[str, Any],
    values: dict[str, Any],
) -> tuple[float, ...]:
    """Validate a complete verification contract without consulting cache."""
    verification = spec.get("verification")
    if not isinstance(verification, dict):
        raise ControlValidationError(
            "this control has no audited acknowledgement/state verification contract"
        )
    required = _validate_field_list(
        "required request fields", verification.get("required_request_fields")
    )
    echoes = _validate_field_list("echo fields", verification.get("echo_fields"))
    mode = verification.get("mode", "state_transition")
    delays = verification.get("readback_delays_s")
    if (
        not isinstance(delays, list)
        or not delays
        or len(delays) > 3
        or any(
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not math.isfinite(delay)
            or delay < 1
            or delay > 30
            for delay in delays
        )
        or any(later <= earlier for earlier, later in zip(delays, delays[1:]))
        or set(required) != set(echoes)
    ):
        raise ControlValidationError("control verification contract is invalid")
    if mode == "state_transition":
        preconditions = _validate_condition_map(
            "precondition", verification.get("preconditions")
        )
        postconditions = _validate_condition_map(
            "postcondition", verification.get("postconditions")
        )
        if not _has_observable_transition(preconditions, postconditions):
            raise ControlValidationError("control verification contract is invalid")
    elif mode == "field_echo":
        # A field-echo contract proves the exact requested value after the API
        # ACK. It deliberately permits setting an already-current value, but
        # only when a fresh pre-command snapshot exposes that same exact field.
        if (
            not required
            or "preconditions" in verification
            or "postconditions" in verification
        ):
            raise ControlValidationError("control verification contract is invalid")
    else:
        raise ControlValidationError("control verification contract is invalid")
    missing = [field for field in required if field not in values]
    if missing:
        raise ControlValidationError(
            f"verification requires request field {', '.join(missing)}"
        )
    unverified = sorted(set(values) - set(echoes))
    if unverified:
        raise ControlValidationError(
            "verification does not cover request field "
            f"{', '.join(unverified)}"
        )
    return tuple(float(delay) for delay in delays)


def prepare_control_verification(
    spec: dict[str, Any],
    values: dict[str, Any],
    snapshot: dict[str, Any],
) -> tuple[float, ...]:
    """Validate a model-specific pre-state before any mutating request."""
    delays = control_verification_schedule(spec, values)
    verification = spec["verification"]
    if verification.get("mode", "state_transition") == "field_echo":
        for field in verification["echo_fields"]:
            if _snapshot_field(snapshot, field) is _MISSING:
                raise ControlValidationError(
                    f"fresh pre-command state does not expose verified field {field}"
                )
        return delays
    preconditions = verification["preconditions"]
    for field, allowed in preconditions.items():
        actual = _snapshot_field(snapshot, field)
        if (
            actual is _MISSING
            or not any(_state_value_matches(actual, candidate) for candidate in allowed)
        ):
            raise ControlValidationError(
                f"current state does not satisfy verified precondition {field}"
            )
    return delays


def control_readback_verified(
    spec: dict[str, Any],
    values: dict[str, Any],
    snapshot: dict[str, Any],
) -> bool:
    """Return whether one fresh snapshot proves the requested appliance state."""
    try:
        control_verification_schedule(spec, values)
    except ControlValidationError:
        return False
    verification = spec["verification"]
    postconditions = verification.get("postconditions", {})
    echoes = verification.get("echo_fields")
    for field, allowed in postconditions.items():
        actual = _snapshot_field(snapshot, field)
        if (
            not isinstance(allowed, list)
            or actual is _MISSING
            or not any(_state_value_matches(actual, candidate) for candidate in allowed)
        ):
            return False
    for field in echoes:
        if field not in values:
            return False
        actual = _snapshot_field(snapshot, field)
        if actual is _MISSING or not _state_value_matches(actual, values[field]):
            return False
    return True


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
        return {
            "command": selected_command,
            "data_key": field,
            "value": _encoded_field_value(spec, field, value),
        }

    if shape == "dataset":
        if not values:
            raise ControlValidationError("dataset controls require at least one field")
        for field, value in values.items():
            if field not in spec.get("fields", {}):
                raise ControlValidationError(f"unsupported control field {field!r}")
            _validate_field(spec, field, value)
        return {
            "command": selected_command,
            "data_set_list": {
                field: _encoded_field_value(spec, field, value)
                for field, value in values.items()
            },
        }

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
            _set_path(
                template,
                resolved,
                _encoded_field_value(spec, resolved, value),
            )
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


def _remote_control_state(data: Any) -> bool | None:
    """Return a tri-state remote-control report from one source.

    ``None`` means that the source did not expose a recognized remote-control
    gate.  Dotted WideQ snapshot keys are accepted in addition to nested
    payloads.  An explicit non-enabled value remains ``False`` so a fresh
    WideQ OFF report cannot be hidden by an older PAT cache entry.
    """
    states: list[bool] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                leaf = str(key).rsplit(".", 1)[-1]
                normalized = leaf.replace("_", "").casefold()
                if normalized in _REMOTE_KEYS:
                    candidate = (
                        child.strip().upper()
                        if isinstance(child, str)
                        else child
                    )
                    try:
                        states.append(candidate in _REMOTE_TRUE)
                    except TypeError:
                        states.append(False)
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(data)
    # Multiple device branches can be present in a unified snapshot. Conflicting
    # reports are not enough evidence to authorize an operation.
    return all(states) if states else None


def remote_control_enabled(data: Any) -> bool:
    """Return whether any PAT/WideQ branch reports remote control enabled."""
    return _remote_control_state(data) is True


def remote_control_authorized(
    model: str | None,
    *,
    pat_data: Any,
    wideq_snapshot: Any,
) -> bool:
    """Authorize an operation using the newest applicable remote-ready gate.

    A WideQ report wins over PAT because command execution refreshes that
    snapshot under the coordinator lock.  The exact Styler model always needs
    its explicit ``styler.remoteStart`` report; a missing WideQ field must not
    fall back to a potentially stale PAT value.
    """
    if model == "ST_R_ETH01Y_":
        # This model's physical remote-ready switch is specifically
        # ``styler.remoteStart``. Other remote-like settings (notably
        # remoteMaintain) or sibling metadata may never substitute for it.
        reports: list[Any] = []
        if isinstance(wideq_snapshot, dict):
            if "styler.remoteStart" in wideq_snapshot:
                reports.append(wideq_snapshot["styler.remoteStart"])
            styler = wideq_snapshot.get("styler")
            if isinstance(styler, dict) and "remoteStart" in styler:
                reports.append(styler["remoteStart"])
        if not reports:
            return False
        return all(
            isinstance(report, str)
            and report.strip().upper() == "REMOTE_START_ON"
            for report in reports
        )
    wideq_state = _remote_control_state(wideq_snapshot)
    if wideq_state is not None:
        return wideq_state
    return _remote_control_state(pat_data) is True
