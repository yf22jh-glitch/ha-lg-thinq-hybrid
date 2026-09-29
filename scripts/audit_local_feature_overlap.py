#!/usr/bin/env python3
"""Read-only model-level source overlap in the live feature database.

No account, binding or device identifiers are printed.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path


def audit() -> list[dict]:
    document = json.loads(Path("/config/.storage/core.config_entries").read_text())
    entries = [
        row for row in document["data"]["entries"]
        if row.get("domain") == "my_lg"
    ]
    if len(entries) != 1:
        raise ValueError("Expected exactly one my_lg config entry")
    models = {
        (row["model_id"], row["profile_id"])
        for row in entries[0]["options"]["local_bindings"]
    }
    with sqlite3.connect("file:/config/my_lg_features.sqlite3?mode=ro", uri=True) as db:
        rows = db.execute(
            "SELECT channel, model_id, profile_id, feature_id, definition_json "
            "FROM features WHERE channel IN ('full-read', 'pilot-read') AND enabled=1"
        ).fetchall()
    grouped: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    for channel, model, profile, semantic, encoded in rows:
        grouped[(channel, model, profile)][semantic] = json.loads(encoded)
    result = []
    for model, pilot_profile in sorted(models):
        full = grouped[("full-read", model, f"{model}:read-sensors-v1")]
        pilot = grouped[("pilot-read", model, pilot_profile)]
        shared = full.keys() & pilot.keys()
        compatible = sorted(
            semantic for semantic in shared
            if tuple(full[semantic].get("valueTypes", ()))
            == (pilot[semantic].get("value_type"),)
            and full[semantic].get("unit") == pilot[semantic].get("unit")
            and full[semantic].get("exposure") == pilot[semantic].get("exposure")
            and full[semantic].get("domain")
            == ("binary_sensor" if pilot[semantic].get("value_type") == "boolean" else "sensor")
        )
        result.append({
            "model": model,
            "full": len(full),
            "pilot": len(pilot),
            "shared": len(shared),
            "compatible": compatible,
            "incompatible": sorted(shared - set(compatible)),
        })
    return result


if __name__ == "__main__":
    print(json.dumps(audit(), ensure_ascii=False, sort_keys=True))
