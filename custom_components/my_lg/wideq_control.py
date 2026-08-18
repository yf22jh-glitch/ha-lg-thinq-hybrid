"""Turn audited single-field WideQ controls into entity descriptions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .control_router import (
    PAT_PRIORITY_FIELDS,
    ControlValidationError,
    control_verification_schedule,
    remote_control_authorized,
)
from .feature_catalog import get_wideq_control, list_wideq_controls
from .value_access import stable_feature_key


@dataclass(frozen=True)
class WideqFieldControl:
    key: str
    control_name: str
    ctrl_key: str
    field: str
    shape: str
    use_dataset: bool
    risk: str
    value_type: str
    verification_available: bool
    options: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    step: float = 1


# These are already authoritative PAT controls or existing semantic WideQ
# entities. Omitting them here prevents duplicate controls while preserving the
# original entity unique IDs.
_CLAIMED_FIELDS = PAT_PRIORITY_FIELDS | {
    "airState.wMode.airClean",
    "airState.wMode.smartCare",
    "airState.miscFuncState.autoDry",
    "airState.lightingState.displayControl",
    "airState.humidity.desired",
}

# Favorite is a composite preset snapshot. Its fields are exposed by the
# validated composite service, not as misleading controls of the live state.
_COMPOSITE_ONLY = {"favoriteCtrl"}

_VERIFICATION_FIELD_SENTINEL = object()


def exact_wideq_field_spec(
    model: str,
    control_name: str,
    field: str,
    use_dataset: bool,
    *,
    shape: str | None = None,
) -> dict[str, Any] | None:
    """Return one exact, value-audited model field contract.

    Type-wide entity descriptions are only presentation metadata. They may not
    authorize a write for a newly added model that happens to share a device
    type. The generated exact-model catalog remains the authority.
    """
    spec = get_wideq_control(model, control_name)
    expected_shape = shape or ("dataset" if use_dataset else "data_key")
    if (
        not isinstance(spec, dict)
        or spec.get("ctrl_key") != control_name
        or spec.get("shape") != expected_shape
        or "Set" not in spec.get("commands", ())
    ):
        return None
    fields = spec.get("fields")
    field_spec = fields.get(field) if isinstance(fields, dict) else None
    if not isinstance(field_spec, dict) or field_spec.get("verified") is not True:
        return None
    if expected_shape == "template" and (
        spec.get("platform") != "thinq2"
        or spec.get("parameterless") is True
        or spec.get("requires_data") is not True
        or spec.get("writable_fields") != [field]
    ):
        return None
    return spec


def verified_wideq_field_spec(
    model: str,
    control_name: str,
    field: str,
    use_dataset: bool,
    *,
    shape: str | None = None,
) -> dict[str, Any] | None:
    """Return an exact field spec only when it has end-to-end verification."""
    spec = exact_wideq_field_spec(
        model,
        control_name,
        field,
        use_dataset,
        shape=shape,
    )
    if spec is None:
        return None
    try:
        control_verification_schedule(
            spec, {field: _VERIFICATION_FIELD_SENTINEL}
        )
    except ControlValidationError:
        return None
    return spec


def iter_wideq_field_controls(model: str) -> tuple[WideqFieldControl, ...]:
    """Return every non-duplicate one-field control for a model."""
    result: list[WideqFieldControl] = []
    controls = list_wideq_controls(model).get("controls", {})
    for control_name, control in controls.items():
        shape = control.get("shape")
        if shape not in {"data_key", "dataset", "template"} or control_name in _COMPOSITE_ONLY:
            continue
        if shape == "template" and len(control.get("writable_fields", ())) != 1:
            continue
        for field, value_spec in control.get("fields", {}).items():
            if field in _CLAIMED_FIELDS:
                continue
            value_type = value_spec.get("type", "unknown")
            field_risk = control.get("risk", "low")
            if value_type == "unknown":
                # LG supplied no enum/range contract. Keep the entity present,
                # but require the explicit experimental-controls option.
                field_risk = "experimental"
            result.append(
                WideqFieldControl(
                    key=stable_feature_key(
                        "wideq_control", (control_name, field)
                    ),
                    control_name=control_name,
                    ctrl_key=control["ctrl_key"],
                    field=field,
                    shape=shape,
                    use_dataset=shape == "dataset",
                    risk=field_risk,
                    value_type=value_type,
                    verification_available=(
                        verified_wideq_field_spec(
                            model,
                            control_name,
                            field,
                            shape == "dataset",
                            shape=shape,
                        )
                        is not None
                    ),
                    options=tuple(str(item) for item in value_spec.get("options", ())),
                    minimum=value_spec.get("min"),
                    maximum=value_spec.get("max"),
                    step=value_spec.get("step", 1) or 1,
                )
            )
    return tuple(result)


def normalize_option(value: Any) -> str | None:
    """Normalize numeric snapshot values to model option strings."""
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def control_risk_allowed(
    control: WideqFieldControl,
    *,
    model: str,
    allow_hazardous: bool,
    allow_experimental: bool,
    pat_data: Any,
    snapshot: dict[str, Any],
) -> bool:
    """Apply the common option and remote-start gates to field controls."""
    if not control.verification_available:
        return False
    if control.risk == "hazardous" and not allow_hazardous:
        return False
    if control.risk == "experimental" and not allow_experimental:
        return False
    return control.risk not in {"operation", "hazardous"} or remote_control_authorized(
        model,
        pat_data=pat_data,
        wideq_snapshot=snapshot,
    )
