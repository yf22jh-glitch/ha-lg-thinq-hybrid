"""Access the generated WideQ model-control catalog."""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def control_catalog() -> dict[str, Any]:
    path = Path(__file__).with_name("wideq_controls.json")
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def get_wideq_control(
    model: str, control: str, subdevice: str | None = None
) -> dict[str, Any] | None:
    """Return an audited control definition for a model."""
    model_data = control_catalog().get(model, {})
    if subdevice:
        return model_data.get("subdevices", {}).get(subdevice, {}).get(control)
    return model_data.get("controls", {}).get(control)


def list_wideq_controls(model: str) -> dict[str, Any]:
    """Return top-level and sub-device controls for diagnostics/tests."""
    return control_catalog().get(model, {"controls": {}, "subdevices": {}})
