"""Interpret verified ThinQ air-conditioner power-save snapshot fields."""

from __future__ import annotations

from typing import Any

POWER_SAVE_FIELDS = {
    "general": "airState.powerSave.basic",
    "comfortable": "airState.powerSave.hum",
    "dehumidification": "airState.powerSave.dry",
}

LOCAL_COMFORT_POWER_SAVE_SEMANTIC = "comfort_energy_saving.enabled"
LOCAL_COMFORT_POWER_SAVE_PROFILES = {
    "cst570-core-state-v1": "CST_570004_WW",
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


def local_comfort_power_save_configured(
    provider: Any, model_id: str | None
) -> bool:
    """Return whether one provider has the exact static Local overlay contract.

    Runtime health is deliberately excluded so an eligible preferred provider
    stays subscribed while offline and can publish its recovery immediately.
    """
    if provider is None:
        return False
    if getattr(provider, "mode", None) != "preferred":
        return False
    if getattr(provider, "snapshot_schema_version", None) != 3:
        return False
    profile_id = getattr(provider, "profile_id", None)
    if (
        LOCAL_COMFORT_POWER_SAVE_PROFILES.get(profile_id) != model_id
        or getattr(provider, "model_id", None) != model_id
    ):
        return False
    profile = getattr(provider, "profile", None)
    fields = getattr(profile, "fields", {})
    contract = fields.get(LOCAL_COMFORT_POWER_SAVE_SEMANTIC)
    return not (
        getattr(profile, "availability_policy", None) != "attested-session"
        or getattr(contract, "value_type", None) != "boolean"
        or getattr(contract, "exposure", None) != "state"
    )


def local_comfort_power_save_value(
    provider: Any, model_id: str | None
) -> bool | None:
    """Return an exact, healthy Local readback for the current pilot model."""
    if not local_comfort_power_save_configured(provider, model_id):
        return None
    if not getattr(provider, "shadow_healthy", False):
        return None
    value = provider.field_value(LOCAL_COMFORT_POWER_SAVE_SEMANTIC)
    return value if type(value) is bool else None


def ac_power_save_snapshot_with_local(
    snapshot: dict[str, Any], provider: Any, model_id: str | None
) -> dict[str, Any]:
    """Overlay only the attested Local comfort flag on the cloud snapshot."""
    merged = dict(snapshot)
    local_value = local_comfort_power_save_value(provider, model_id)
    if local_value is not None:
        merged[POWER_SAVE_FIELDS["comfortable"]] = local_value
    return merged


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


def ac_power_save_attributes_with_local(
    snapshot: dict[str, Any], provider: Any, model_id: str | None
) -> dict[str, Any]:
    """Describe the exact per-field provider mix after a Local overlay."""
    attributes = ac_power_save_attributes(
        ac_power_save_snapshot_with_local(snapshot, provider, model_id)
    )
    if local_comfort_power_save_value(provider, model_id) is not None:
        attributes["power_save_source"] = "mixed_local_wideq"
        attributes["comfortable_power_save_provider"] = "local"
    else:
        attributes["comfortable_power_save_provider"] = "wideq"
    return attributes
