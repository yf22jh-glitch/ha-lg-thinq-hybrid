#!/usr/bin/env python3
"""Read-only broker check; print model-level delivery, never credentials or IDs."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import paho.mqtt.client as mqtt

TARGETS = {
    "AIR_2C0001_WW",
    "HWWA9X3C_F2U",
    "WTL_KPK_BDH_KR_01",
    "CST_170004_WW",
}


def bindings() -> list[dict]:
    # The public options form masks passwords. Read the current HA Store inside
    # the container and keep secrets in memory; never serialize them to output.
    document = json.loads(Path("/config/.storage/core.config_entries").read_text())
    entries = [
        row for row in document["data"]["entries"]
        if row.get("domain") == "my_lg"
    ]
    if len(entries) != 1:
        raise ValueError("Expected exactly one my_lg config entry")
    options = entries[0]["options"]
    return [
        row for row in options["local_bindings"]
        if (
            row["binding_id"] == os.environ["LG_AUDIT_BINDING_ID"]
            if "LG_AUDIT_BINDING_ID" in os.environ
            else row["model_id"] == os.environ["LG_AUDIT_MODEL_ID"]
            if "LG_AUDIT_MODEL_ID" in os.environ
            else os.environ.get("LG_AUDIT_ALL") == "1" or row["model_id"] in TARGETS
        )
    ]


def probe(row: dict) -> dict:
    binding_id = row["binding_id"]
    topics = {
        "read_current": f"lg_rethink_local/v1/read/current/{binding_id}",
        "pilot_state": f"lg_rethink_local/v1/state/{binding_id}",
        "availability": f"lg_rethink_local/v1/availability/{binding_id}",
        "runtime": f"lg_rethink_local/v1/runtime/{binding_id}/availability",
        "presence": f"lg_rethink_local/v1/presence/{binding_id}",
    }
    observed: dict[str, list[bool]] = {name: [] for name in topics}
    payloads: dict[str, dict] = {}
    current_bytes: bytes | None = None
    read_field_counts_seen: list[int] = []
    read_field_sets_seen: list[dict[str, object]] = []
    tombstones: dict[str, int] = {name: 0 for name in topics}
    connected = threading.Event()
    status: dict[str, object] = {"connect_code": None, "suback_codes": []}
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"lg-audit-{uuid4().hex[:12]}",
        protocol=mqtt.MQTTv311,
    )
    client.username_pw_set(f"shadow-{binding_id}", row["mqtt_password"])

    def on_connect(client, _userdata, _flags, reason_code, _properties):
        status["connect_code"] = str(reason_code)
        if reason_code == 0:
            client.subscribe([(topic, 1) for topic in topics.values()])
        connected.set()

    def on_subscribe(_client, _userdata, _mid, reason_codes, _properties):
        status["suback_codes"] = [str(code) for code in reason_codes]

    def on_message(_client, _userdata, message):
        nonlocal current_bytes
        for name, topic in topics.items():
            if message.topic == topic:
                observed[name].append(bool(message.retain))
                if not message.payload:
                    tombstones[name] += 1
                    payloads.pop(name, None)
                    if name == "read_current":
                        current_bytes = None
                    break
                try:
                    decoded = json.loads(message.payload)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    break
                if isinstance(decoded, dict):
                    payloads[name] = decoded
                if name == "read_current":
                    current_bytes = bytes(message.payload)
                    fields = decoded.get("fields") if isinstance(decoded, dict) else None
                    if isinstance(fields, dict):
                        read_field_counts_seen.append(len(fields))
                        if os.environ.get("LG_SHOW_FIELD_NAMES") == "1":
                            invalidated = decoded.get("invalidated_fields", {})
                            read_field_sets_seen.append({
                                "fields": sorted(fields),
                                "invalidated": sorted(invalidated) if isinstance(invalidated, dict) else [],
                                "cohort_generation": decoded.get("cohort_generation"),
                            })
                break

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    try:
        client.connect("127.0.0.1", 18883, keepalive=10)
        client.loop_start()
        connected.wait(3)
        time.sleep(float(os.environ.get("LG_AUDIT_LIVE_SECONDS", "1")))
    finally:
        client.disconnect()
        client.loop_stop()
    summary = {
        "model": row["model_id"],
        "connected": connected.is_set(),
        **status,
        "delivery": {
            name: {"messages": len(values), "retained": sum(values), "tombstones": tombstones[name]}
            for name, values in observed.items()
        },
        "read_field_counts_seen": read_field_counts_seen,
    }
    for name, payload in payloads.items():
        if name in ("availability", "runtime", "presence"):
            summary[f"{name}_status"] = payload.get("status")
            summary[f"{name}_keys"] = sorted(payload)
            if name == "presence":
                summary["presence_evidence"] = payload.get("evidence")
        summary[f"{name}_cohort"] = payload.get("cohort_generation")
        summary[f"{name}_binding_generation"] = payload.get("binding_generation")
        for timestamp_key in ("published_at", "observed_at", "captured_at"):
            timestamp = payload.get(timestamp_key)
            if isinstance(timestamp, str):
                try:
                    summary[f"{name}_age_s"] = round(
                        (datetime.now(timezone.utc) - datetime.fromisoformat(
                            timestamp.replace("Z", "+00:00")
                        )).total_seconds()
                    )
                except ValueError:
                    pass
                break
    presence = payloads.get("presence", {})
    runtime = payloads.get("runtime", {})
    current = payloads.get("read_current", {})
    valid_until = presence.get("valid_until")
    if isinstance(valid_until, str):
        summary["presence_valid_until_delta_s"] = round(
            (datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
             - datetime.now(timezone.utc)).total_seconds()
        )
    summary["presence_runtime_session_match"] = (
        presence.get("service_instance_id") == runtime.get("service_instance_id")
    )
    summary["current_presence_session_match"] = (
        current.get("publication_session_id") == presence.get("service_instance_id")
    ) if current else None
    summary["current_pilot_cohort_relation"] = (
        "current" if current.get("cohort_generation") == payloads.get("pilot_state", {}).get("cohort_generation")
        else "older" if isinstance(current.get("cohort_generation"), int)
        and isinstance(payloads.get("pilot_state", {}).get("cohort_generation"), int)
        and current["cohort_generation"] < payloads["pilot_state"]["cohort_generation"]
        else "other"
    ) if current else None
    current_payload = payloads.get("read_current")
    pilot_fields = payloads.get("pilot_state", {}).get("fields")
    summary["pilot_fields"] = len(pilot_fields) if isinstance(pilot_fields, dict) else None
    for name, fields in (("pilot", pilot_fields), ("read", current_payload.get("fields") if isinstance(current_payload, dict) else None)):
        if isinstance(fields, dict):
            ages = []
            for field in fields.values():
                observed_at = field.get("observed_at") if isinstance(field, dict) else None
                if isinstance(observed_at, str):
                    try:
                        ages.append(round((datetime.now(timezone.utc) - datetime.fromisoformat(observed_at.replace("Z", "+00:00"))).total_seconds()))
                    except ValueError:
                        pass
            if ages:
                summary[f"{name}_field_min_age_s"] = min(ages)
                summary[f"{name}_field_max_age_s"] = max(ages)
    if os.environ.get("LG_SHOW_FIELD_NAMES") == "1":
        summary["pilot_field_ids"] = sorted(pilot_fields) if isinstance(pilot_fields, dict) else []
        summary["read_field_ids"] = sorted(current_payload.get("fields", {})) if isinstance(current_payload, dict) else []
        summary["read_field_sets_seen"] = read_field_sets_seen
    if isinstance(pilot_fields, dict) and isinstance(current_payload, dict):
        current_fields = current_payload.get("fields")
        if isinstance(current_fields, dict):
            summary["pilot_current_overlap"] = len(pilot_fields.keys() & current_fields.keys())
    if current_payload is not None:
        fields = current_payload.get("fields")
        if isinstance(fields, dict):
            with sqlite3.connect("file:/config/my_lg_features.sqlite3?mode=ro", uri=True) as db:
                definitions = {
                    semantic: json.loads(encoded)
                    for semantic, encoded in db.execute(
                        "SELECT feature_id, definition_json FROM features "
                        "WHERE channel = 'full-read' AND model_id = ? AND enabled = 1",
                        (row["model_id"],),
                    )
                }
            shared = set(fields) & definitions.keys()
            compatible = sum(
                isinstance(fields[key], dict)
                and fields[key].get("value_type") in definitions[key]["valueTypes"]
                and fields[key].get("exposure") == definitions[key]["exposure"]
                and fields[key].get("unit") == definitions[key].get("unit")
                for key in shared
            )
            summary["read_fields"] = len(fields)
            summary["db_shared_fields"] = len(shared)
            summary["type_compatible_fields"] = compatible
            summary["db_enabled_fields"] = len(definitions)
            summary["db_missing_from_current"] = sorted(definitions.keys() - fields.keys())
            if isinstance(pilot_fields, dict):
                summary["pilot_full_contract_overlap"] = len(
                    pilot_fields.keys() & definitions.keys()
                )
                missing_both = definitions.keys() - fields.keys() - pilot_fields.keys()
                summary["db_missing_from_both"] = sorted(missing_both)
                summary["db_missing_from_both_default_enabled"] = sorted(
                    key for key in missing_both
                    if definitions[key].get("enabledByDefault") is True
                )
        published_at = current_payload.get("published_at")
        if isinstance(published_at, str):
            summary["read_age_s"] = round(
                (datetime.now(timezone.utc) - datetime.fromisoformat(
                    published_at.replace("Z", "+00:00")
                )).total_seconds()
            )
        summary["read_schema"] = current_payload.get("schema_version")
    if current_bytes is not None:
        from custom_components.my_lg.local_provider import local_pat_device_identity_proof
        from custom_components.my_lg.local_read_provider import (
            TlvReadShadowProvider, load_tlv_read_catalogue,
        )

        profile = load_tlv_read_catalogue()[row["model_id"]]

        class Primary:
            binding_id = row["binding_id"]
            model_id = row["model_id"]
            platform = profile.platform
            expected_proof = local_pat_device_identity_proof(
                binding_id, model_id, platform, row["pat_device_id"]
            )
            read_publication_authority = None

            @staticmethod
            def async_add_listener(_callback):
                return lambda: None

        try:
            provider = TlvReadShadowProvider(
                binding_id, row["pat_device_id"], profile, Primary(),
                read_contract_policy="field-compatible",
            )
            provider.set_transport_ready(True)
            provider.ingest(topics["read_current"], current_bytes, qos=1, retained=True)
            summary["offline_accepted_fields"] = len(provider.fields)
        except Exception as error:  # diagnostic only; never print credentials
            summary["offline_reject"] = f"{type(error).__name__}: {error}"
    return summary


if __name__ == "__main__":
    selected = bindings()
    if not selected:
        print("[]")
    else:
        with ThreadPoolExecutor(max_workers=min(20, len(selected))) as pool:
            results = list(pool.map(probe, selected))
            if os.environ.get("LG_AUDIT_SUMMARY") == "1":
                results = [{
                    "model": row["model"],
                    "pilot_fields": row.get("pilot_fields"),
                    "read_fields": row.get("read_fields", 0),
                    "read_field_counts_seen": row["read_field_counts_seen"],
                    "presence_status": row.get("presence_status"),
                    "presence_age_s": row.get("presence_age_s"),
                    "presence_messages": row["delivery"]["presence"]["messages"],
                    "presence_retained": row["delivery"]["presence"]["retained"],
                    "availability_status": row.get("availability_status"),
                } for row in results]
            print(json.dumps(results, sort_keys=True))
