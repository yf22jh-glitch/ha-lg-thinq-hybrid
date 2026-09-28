#!/usr/bin/env python3
"""Read-only HA/SQLite comparison after a Local feature DB rollout.

Run inside the HA container with ``pilot_ha_field_compatible`` and its
in-memory auth helper on PYTHONPATH. No identifiers or tokens are printed.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

from custom_components.my_lg.local_entity import local_semantic_unique_id
from pilot_ha_field_compatible import admin_token, current_form, one_entry, websocket_command

DATABASE = Path("/config/my_lg_features.sqlite3")


async def audit() -> dict:
    token = admin_token()
    entry = await one_entry(token)
    async with aiohttp.ClientSession(
        headers={"Authorization": f"Bearer {token}"},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as session:
        _, options = await current_form(session, entry["entry_id"])
    bindings = options["local_bindings"]
    registry = await websocket_command(token, {"type": "config/entity_registry/list"})
    states = await websocket_command(token, {"type": "get_states"})
    assert isinstance(bindings, list) and isinstance(registry, list)
    assert isinstance(states, list)
    by_unique = {
        row["unique_id"]: row for row in registry
        if isinstance(row, dict) and row.get("platform") == "my_lg"
        and isinstance(row.get("unique_id"), str)
    }
    state_by_id = {
        row.get("entity_id"): row
        for row in states if isinstance(row, dict)
    }

    with closing(sqlite3.connect(f"{DATABASE.as_uri()}?mode=ro", uri=True)) as connection:
        rollout = dict(connection.execute(
            "SELECT model_id, enabled FROM model_rollout"
        ))
        rows = connection.execute(
            "SELECT model_id, feature_id, definition_json, enabled "
            "FROM features WHERE channel = 'full-read'"
        ).fetchall()
    active: dict[str, set[str]] = defaultdict(set)
    disabled: dict[str, set[str]] = defaultdict(set)
    optional: dict[str, set[str]] = defaultdict(set)
    transient: dict[str, set[str]] = defaultdict(set)
    for model, semantic, encoded, enabled in rows:
        definition = json.loads(encoded)
        if enabled:
            if definition.get("owner") == "none":
                active[model].add(semantic)
                if definition.get("enabledByDefault") is False:
                    optional[model].add(semantic)
                if definition.get("publicationMode") == "transient-event" or definition.get("domain") == "event":
                    transient[model].add(semantic)
        else:
            disabled[model].add(semantic)

    configured = set()
    active_entity_ids: set[str] = set()
    per_model: dict[str, dict[str, int | list[str]]] = {}
    missing_total: list[tuple[str, str]] = []
    resurrected_total: list[tuple[str, str]] = []
    for binding in bindings:
        assert isinstance(binding, dict)
        model = binding["model_id"]
        device = binding["pat_device_id"]
        configured.add(model)
        missing = sorted(
            semantic for semantic in active[model]
            if local_semantic_unique_id(device, semantic) not in by_unique
        )
        resurrected = sorted(
            semantic for semantic in disabled[model]
            if (row := by_unique.get(local_semantic_unique_id(device, semantic)))
            and row.get("disabled_by") is None
        )
        registered = sum(
            local_semantic_unique_id(device, semantic) in by_unique
            for semantic in active[model]
        )
        with_state = sum(
            row.get("entity_id") in state_by_id
            for semantic in active[model]
            if (row := by_unique.get(local_semantic_unique_id(device, semantic)))
        )
        unavailable = sum(
            state_by_id[row["entity_id"]].get("state") in ("unknown", "unavailable")
            for semantic in active[model]
            if (row := by_unique.get(local_semantic_unique_id(device, semantic)))
            and row.get("entity_id") in state_by_id
        )
        unavailable_with_field = sum(
            state_by_id[row["entity_id"]].get("state") in ("unknown", "unavailable")
            and "observed_at" in state_by_id[row["entity_id"]].get("attributes", {})
            for semantic in active[model]
            if (row := by_unique.get(local_semantic_unique_id(device, semantic)))
            and row.get("entity_id") in state_by_id
        )
        optional_unavailable = sum(
            state_by_id[row["entity_id"]].get("state") in ("unknown", "unavailable")
            for semantic in optional[model]
            if (row := by_unique.get(local_semantic_unique_id(device, semantic)))
            and row.get("entity_id") in state_by_id
        )
        transient_unavailable = sum(
            state_by_id[row["entity_id"]].get("state") in ("unknown", "unavailable")
            for semantic in transient[model]
            if (row := by_unique.get(local_semantic_unique_id(device, semantic)))
            and row.get("entity_id") in state_by_id
        )
        key = model
        previous = per_model.get(key)
        if previous is not None:
            key = f"{model} (second binding)"
        per_model[key] = {
            "owner_none_enabled": len(active[model]),
            "registered": registered,
            "with_state": with_state,
            "unknown_or_unavailable": unavailable,
            "unavailable_with_accepted_field": unavailable_with_field,
            "optional_enabled": len(optional[model]),
            "optional_unavailable": optional_unavailable,
            "transient_enabled": len(transient[model]),
            "transient_unavailable": transient_unavailable,
            "missing_semantics": missing,
            "disabled_resurrected": resurrected,
        }
        active_entity_ids.update(
            row["entity_id"]
            for semantic in active[model]
            if (row := by_unique.get(local_semantic_unique_id(device, semantic)))
            and isinstance(row.get("entity_id"), str)
        )
        missing_total.extend((model, semantic) for semantic in missing)
        resurrected_total.extend((model, semantic) for semantic in resurrected)
    energy_rows = [
        row for unique_id, row in by_unique.items()
        if "_local_semantic_cumulative." in unique_id
        and row.get("disabled_by") is None
    ]
    energy_values = [
        state_by_id.get(row.get("entity_id"), {}).get("state")
        for row in energy_rows
    ]
    energy_numeric = sum(
        isinstance(value, str)
        and value not in ("unknown", "unavailable")
        and _is_nonnegative_number(value)
        for value in energy_values
    )
    result = {
        "configured_bindings": len(bindings),
        "configured_models": len(configured),
        "database_models": len(rollout),
        "database_rollout_enabled": sum(rollout.values()),
        "configured_not_in_database": sorted(configured - rollout.keys()),
        "database_without_binding": sorted(rollout.keys() - configured),
        "owner_none_missing_count": len(missing_total),
        "disabled_resurrected_count": len(resurrected_total),
        "cumulative_energy_registered": len(energy_rows),
        "cumulative_energy_numeric": energy_numeric,
        "per_model": per_model,
    }
    if os.environ.get("LG_COMPARE_RECORDER") == "1":
        result["pre_rollout_recorder"] = _pre_rollout_recorder(
            active_entity_ids
        )
    return result


def _is_nonnegative_number(value: str) -> bool:
    try:
        return float(value) >= 0
    except ValueError:
        return False


def _pre_rollout_recorder(entity_ids: set[str]) -> dict[str, int]:
    """One indexed HA-state comparison, not protocol/semantic ledger replay."""
    cutoff = datetime(2026, 9, 25, 1, 0, tzinfo=timezone(timedelta(hours=9))).timestamp()
    with closing(sqlite3.connect(
        "file:/config/home-assistant_v2.db?mode=ro", uri=True
    )) as connection:
        connection.execute("PRAGMA query_only=ON")
        placeholders = ",".join("?" for _ in entity_ids)
        metadata = dict(connection.execute(
            f"SELECT entity_id, metadata_id FROM states_meta WHERE entity_id IN ({placeholders})",
            tuple(entity_ids),
        ))
        states = [
            connection.execute(
                "SELECT state FROM states WHERE metadata_id = ? AND last_updated_ts <= ? "
                "ORDER BY last_updated_ts DESC LIMIT 1",
                (metadata[entity_id], cutoff),
            ).fetchone()
            for entity_id in entity_ids if entity_id in metadata
        ]
    return {
        "entities_checked": len(entity_ids),
        "had_prior_state": sum(row is not None for row in states),
        "available_before": sum(
            row is not None and row[0] not in ("unknown", "unavailable")
            for row in states
        ),
        "unavailable_before": sum(
            row is not None and row[0] in ("unknown", "unavailable")
            for row in states
        ),
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(audit()), ensure_ascii=False, sort_keys=True))
