"""Manage the Local feature SQLite database without rebuilding the integration.

Examples:
  python scripts/manage_local_features.py seed --db /config/my_lg_features.sqlite3
  python scripts/manage_local_features.py list --db /config/my_lg_features.sqlite3
  python scripts/manage_local_features.py disable --db ... --channel full-read \
      --model MODEL --profile MODEL:read-sensors-v1 --feature semantic.id
  python scripts/manage_local_features.py upsert --db ... --channel full-read \
      --model MODEL --profile MODEL:read-sensors-v1 --feature semantic.id \
      --platform thinq2 --definition feature.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "my_lg"
sys.path.insert(0, str(COMPONENT))

from feature_database import (  # noqa: E402
    CHANNELS,
    create_database,
    default_database_path,
    enabled_models,
    load_features,
    set_feature_enabled,
    set_model_rollout,
    upsert_feature,
)


def disabled_read_keys_from_receipt(path: Path) -> frozenset[tuple[str, str]]:
    """Import only model/semantic decisions, never entity IDs or credentials."""
    document = json.loads(path.read_text())
    targets = document.get("targets") if isinstance(document, dict) else None
    if not isinstance(targets, list):
        raise ValueError("HA read cleanup receipt has no targets")
    keys: set[tuple[str, str]] = set()
    for target in targets:
        if (
            not isinstance(target, dict)
            or not isinstance(target.get("model_id"), str)
            or not isinstance(target.get("semantic_id"), str)
            or target.get("new_disabled_by") is None
        ):
            raise ValueError("HA read cleanup receipt has an invalid target")
        keys.add((target["model_id"], target["semantic_id"]))
    return frozenset(keys)


def current_definitions(
    disabled_read_keys: frozenset[tuple[str, str]] = frozenset(),
) -> list[dict[str, object]]:
    """Import all existing definitions once, preserving their exact IDs."""
    profiles = json.loads((COMPONENT / "full-read-sensor-profiles.v1.json").read_text())
    pilot = json.loads((COMPONENT / "pilot-profiles.v1.json").read_text())
    controls = json.loads(
        (COMPONENT / "home-assistant-local-control-entity-contract.v1.json").read_text()
    )
    confirmed = json.loads((COMPONENT / "local-control-confirmed-features.v1.json").read_text())
    definitions: list[dict[str, object]] = []
    for profile in profiles["profiles"]:
        for field in profile["fields"]:
            definitions.append({
                "channel": "full-read",
                "model_id": profile["modelId"],
                "profile_id": profile["profileId"],
                "feature_id": field["semanticId"],
                "platform": profile["platform"],
                "definition": field,
                "enabled": (profile["modelId"], field["semanticId"])
                    not in disabled_read_keys,
            })
    for profile in pilot["profiles"]:
        definitions.append({
            "channel": "pilot-profile",
            "model_id": profile["model_id"],
            "profile_id": profile["profile_id"],
            "feature_id": "profile",
            "platform": profile["platform"],
            "definition": {key: value for key, value in profile.items() if key != "fields"},
        })
        for field in profile["fields"]:
            definitions.append({
                "channel": "pilot-read",
                "model_id": profile["model_id"],
                "profile_id": profile["profile_id"],
                "feature_id": field["semantic_id"],
                "platform": profile["platform"],
                "definition": field,
                "enabled": (profile["model_id"], field["semantic_id"])
                    not in disabled_read_keys,
            })
    for entity in controls["entities"]:
        definitions.append({
            "channel": "control-entity",
            "model_id": entity["modelId"],
            "feature_id": entity["capabilityId"],
            "platform": "",
            "definition": entity,
        })
    for feature in confirmed["features"]:
        definitions.append({
            "channel": "confirmed-control",
            "model_id": feature["model_id"],
            "feature_id": feature["capability_id"],
            "platform": "",
            "definition": feature,
        })
    return definitions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("seed", "list", "enable", "disable", "upsert", "pilot-on", "pilot-off"))
    parser.add_argument("--db", type=Path, default=default_database_path())
    parser.add_argument("--channel", choices=sorted(CHANNELS))
    parser.add_argument("--model")
    parser.add_argument("--profile", default="")
    parser.add_argument("--feature")
    parser.add_argument("--platform", choices=("thinq1", "thinq2", ""))
    parser.add_argument("--definition", type=Path)
    parser.add_argument("--all", action="store_true", help="include disabled rows in list")
    parser.add_argument("--register-producer", action="store_true",
                        help="allow an already decoded full-read field into producer output")
    parser.add_argument("--disabled-from", type=Path,
                        help="HA cleanup receipt whose disabled read entities must stay disabled")
    args = parser.parse_args()
    if args.action == "seed":
        disabled = (
            disabled_read_keys_from_receipt(args.disabled_from)
            if args.disabled_from else frozenset()
        )
        definitions = current_definitions(disabled)
        if disabled:
            observed = {
                (str(row["model_id"]), str(row["feature_id"]))
                for row in definitions
                if row["channel"] in ("full-read", "pilot-read")
                and not row["enabled"]
            }
            if observed != disabled:
                raise ValueError("HA disabled read inventory differs from the seeded database")
        counts = create_database(args.db, definitions)
        print(json.dumps(counts, sort_keys=True))
        return 0
    if args.action == "list":
        print("database models:", ", ".join(sorted(enabled_models(args.db))) or "none")
        channels = (args.channel,) if args.channel else sorted(CHANNELS)
        for channel in channels:
            for row in load_features(args.db, channel, include_disabled=args.all):
                if args.model and row["model_id"] != args.model:
                    continue
                print(
                    channel,
                    row["model_id"],
                    row["profile_id"],
                    row["feature_id"],
                    "enabled" if row["enabled"] else "disabled",
                    sep="\t",
                )
        return 0
    if args.action in ("pilot-on", "pilot-off"):
        if not args.model:
            parser.error("pilot-on/off requires --model")
        set_model_rollout(args.db, args.model, args.action == "pilot-on")
        print("ok")
        return 0
    if not args.channel or not args.model or not args.feature:
        parser.error("--channel, --model and --feature are required")
    if args.action in ("enable", "disable"):
        set_feature_enabled(
            args.db, args.channel, args.model, args.profile, args.feature,
            args.action == "enable",
        )
    else:
        if args.platform is None or args.definition is None:
            parser.error("upsert requires --platform and --definition")
        if args.register_producer:
            parser.error(
                "producer DB registration is not deployed to immutable runtimes; "
                "add an existing emitted read field without this flag, or deploy "
                "the producer DB reader before registering a new producer field"
            )
        definition = json.loads(args.definition.read_text())
        upsert_feature(
            args.db, args.channel, args.model, args.profile, args.feature,
            args.platform, definition, register_producer=args.register_producer,
        )
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
