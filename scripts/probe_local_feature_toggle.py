#!/usr/bin/env python3
"""Read-only HA registry/state probe for one model/semantic; no device IDs."""

from __future__ import annotations

import asyncio
import json
import os

import aiohttp

from custom_components.my_lg.local_entity import local_semantic_unique_id
from pilot_ha_field_compatible import admin_token, current_form, one_entry, websocket_command


async def probe() -> dict:
    model = os.environ["LG_TEST_MODEL_ID"]
    semantic = os.environ["LG_TEST_SEMANTIC_ID"]
    token = admin_token()
    entry = await one_entry(token)
    async with aiohttp.ClientSession(
        headers={"Authorization": f"Bearer {token}"},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as session:
        _, options = await current_form(session, entry["entry_id"])
    binding = next(row for row in options["local_bindings"] if row["model_id"] == model)
    unique_id = local_semantic_unique_id(binding["pat_device_id"], semantic)
    registry = await websocket_command(token, {"type": "config/entity_registry/list"})
    states = await websocket_command(token, {"type": "get_states"})
    row = next((item for item in registry if item.get("unique_id") == unique_id), None)
    state = next((item for item in states if row and item.get("entity_id") == row.get("entity_id")), None)
    return {
        "model": model,
        "semantic": semantic,
        "registered": row is not None,
        "registry_disabled": row is not None and row.get("disabled_by") is not None,
        "state_present": state is not None,
        "state": state.get("state") if state is not None else None,
        "observed_at_present": bool(state and "observed_at" in state.get("attributes", {})),
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probe()), sort_keys=True))
