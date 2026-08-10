"""Interpret verified ThinQ air-conditioner power-save snapshot fields."""

from __future__ import annotations

from typing import Any

POWER_SAVE_FIELDS = {
    "general": "airState.powerSave.basic",
    "comfortable": "airState.powerSave.hum",
    "dehumidification": "airState.powerSave.dry",
}


def _boolean_flag(value: Any) -> bool | None:
    """Normalize ThinQ numeric/boolean power-save flags."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    try:
        return int(float(value)) == 1
    except (TypeError, ValueError):
        normalized = str(value).strip().casefold()
        if normalized in {"on", "true"}:
            return True
        if normalized in {"off", "false"}:
            return False
        return None


def ac_power_save_cache(snapshot: dict[str, Any]) -> dict[str, bool]:
    """Return only validated mode flags that are safe to persist.

    Power and energy measurements intentionally never enter this cache. It is
    used only to bridge the delayed first WideQ poll after an HA restart.
    """
    cached: dict[str, bool] = {}
    for path in POWER_SAVE_FIELDS.values():
        flag = _boolean_flag(snapshot.get(path))
        if flag is not None:
            cached[path] = flag
    return cached


def ac_power_save_flags(snapshot: dict[str, Any]) -> dict[str, bool | None]:
    """Return every independently reported AC power-save flag."""
    return {
        name: _boolean_flag(snapshot.get(path))
        for name, path in POWER_SAVE_FIELDS.items()
    }


def ac_power_save_mode(snapshot: dict[str, Any]) -> str | None:
    """Return a loss-aware summary of the active/reported power-save flags."""
    flags = ac_power_save_flags(snapshot)
    known = [name for name, enabled in flags.items() if enabled is not None]
    if not known:
        return None
    enabled = [name for name, state in flags.items() if state]
    if not enabled:
        return "off"
    if len(enabled) > 1:
        return "mixed"
    return enabled[0]


def ac_power_save_attributes(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return diagnostic attributes without inventing percentage stages."""
    flags = ac_power_save_flags(snapshot)
    return {
        "general_power_save": flags["general"],
        "comfortable_power_save": flags["comfortable"],
        "dehumidification_power_save": flags["dehumidification"],
        "power_save_source": "wideq_snapshot",
        "percentage_level_supported": False,
    }
