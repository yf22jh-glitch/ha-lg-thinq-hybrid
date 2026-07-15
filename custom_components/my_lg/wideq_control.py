"""Turn audited single-field WideQ controls into entity descriptions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .control_router import PAT_PRIORITY_FIELDS, remote_control_enabled
from .feature_catalog import list_wideq_controls
from .value_access import stable_feature_key


@dataclass(frozen=True)
class WideqFieldControl:
    key: str
    ctrl_key: str
    field: str
    use_dataset: bool
    risk: str
    value_type: str
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


def iter_wideq_field_controls(model: str) -> tuple[WideqFieldControl, ...]:
    """Return every non-duplicate one-field control for a model."""
    result: list[WideqFieldControl] = []
    controls = list_wideq_controls(model).get("controls", {})
    for control_name, control in controls.items():
        shape = control.get("shape")
        if shape not in {"data_key", "dataset"} or control_name in _COMPOSITE_ONLY:
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
                    ctrl_key=control["ctrl_key"],
                    field=field,
                    use_dataset=shape == "dataset",
                    risk=field_risk,
                    value_type=value_type,
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
    allow_hazardous: bool,
    allow_experimental: bool,
    pat_data: Any,
    snapshot: dict[str, Any],
) -> bool:
    """Apply the common option and remote-start gates to field controls."""
    if control.risk == "hazardous" and not allow_hazardous:
        return False
    if control.risk == "experimental" and not allow_experimental:
        return False
    if control.risk == "operation" and not (
        remote_control_enabled(pat_data) or remote_control_enabled(snapshot)
    ):
        return False
    return True
