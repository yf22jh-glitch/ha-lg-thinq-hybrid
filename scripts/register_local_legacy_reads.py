"""Register exact AABB read fields needed by established legacy HA sensor IDs.

Plan-only by default. Apply only after the matching producer decoder and HA
integration are installed; the feature database is shared by both processes.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from custom_components.my_lg.feature_database import (
    APPLICATION_ID,
    SCHEMA_VERSION,
    upsert_feature,
)


WTL = "WTL_KPK_BDH_KR_01"
STYLER = "ST_R_ETH01Y_"

# model, semantic, HA domain, value type, exposure, Korean label, unit
READS = (
    (WTL, "diagnostic.washer.course_raw", "sensor", "number", "diagnostic", "세탁 코스 원시 코드", None),
    (WTL, "diagnostic.washer.spin_setting_raw", "sensor", "number", "diagnostic", "세탁 탈수 원시 코드", None),
    (WTL, "diagnostic.washer.wash_temperature_raw", "sensor", "number", "diagnostic", "세탁 수온 원시 코드", None),
    (WTL, "diagnostic.washer.water_level_raw", "sensor", "number", "diagnostic", "세탁 수위 원시 코드", None),
    (WTL, "washer.error.code_raw", "sensor", "number", "diagnostic", "세탁 오류 원시 코드", None),
    (WTL, "diagnostic.washer.lock_detection_options_bitmap_raw", "sensor", "number", "diagnostic", "세탁 문잠금 원시 비트", None),
    (WTL, "diagnostic.dryer.state_raw", "sensor", "number", "diagnostic", "건조 운전 원시 코드", None),
    (WTL, "diagnostic.dryer.dry_level_raw", "sensor", "number", "diagnostic", "건조 단계 원시 코드", None),
    (WTL, "diagnostic.dryer.vent_blockage_raw", "sensor", "number", "diagnostic", "건조 배기 막힘 원시 코드", None),
    (WTL, "dryer.error.code_raw", "sensor", "number", "diagnostic", "건조 오류 원시 코드", None),
    (STYLER, "cycle.remaining_min", "sensor", "number", "state", "스타일러 남은 시간", "min"),
    (STYLER, "lock.door_enabled", "binary_sensor", "boolean", "state", "스타일러 문잠금", None),
    (STYLER, "option.night_dry_enabled", "binary_sensor", "boolean", "state", "스타일러 야간 건조", None),
    (
        STYLER, "diagnostic.cycle.course_spend_power_raw", "sensor", "number",
        "diagnostic", "스타일러 코스 사용 전력량", "Wh",
    ),
)


def feature_definition(row: tuple[str, ...]) -> dict:
    model, semantic, domain, value_type, exposure, label, unit = row
    return {
        "descriptorKey": f"{model}|{semantic}",
        "domain": domain,
        "enabledByDefault": exposure == "state",
        "entityCategory": "diagnostic" if exposure == "diagnostic" else None,
        "exposure": exposure,
        "labelKo": label,
        "owner": "none",
        "publicationMode": "retained-current",
        "semanticId": semantic,
        "valueTypes": [value_type],
        **({"unit": unit} if unit is not None else {}),
    }


def checked_existing(
    path: Path, *, model: str | None = None, expected_enabled: bool = True
) -> set[tuple[str, str]]:
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError("Feature database path is missing or symlinked")
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as conn:
        if conn.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise ValueError("Feature database identity is invalid")
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError("Feature database schema is unsupported")
        existing = set()
        for row in READS:
            row_model, semantic = row[:2]
            if model is not None and row_model != model:
                continue
            profile = f"{row_model}:read-sensors-v1"
            found = conn.execute(
                "SELECT platform, definition_json, enabled, producer_registered "
                "FROM features WHERE channel = 'full-read' AND model_id = ? "
                "AND profile_id = ? AND feature_id = ?",
                (row_model, profile, semantic),
            ).fetchone()
            if found is None:
                continue
            platform, definition_json, row_enabled, producer_registered = found
            if (
                platform != "thinq2"
                or json.loads(definition_json) != feature_definition(row)
                or row_enabled != int(expected_enabled)
                or producer_registered != 1
            ):
                raise ValueError(f"Existing feature differs: {row_model}|{semantic}")
            existing.add((row_model, semantic))
        return existing


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--model", choices=(WTL, STYLER), required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--producer-only", action="store_true",
        help="register decoder fields without enabling HA entities until readback is observed",
    )
    args = parser.parse_args()
    desired_enabled = not args.producer_only
    existing = checked_existing(
        args.database, model=args.model, expected_enabled=desired_enabled
    )
    selected = [row for row in READS if row[0] == args.model]
    pending = [row for row in selected if (row[0], row[1]) not in existing]
    print(f"model={args.model} existing={len(selected) - len(pending)} pending={len(pending)}")
    for row in pending:
        print(row[1])
    if not args.apply:
        return
    for row in pending:
        model, semantic = row[:2]
        upsert_feature(
            args.database,
            "full-read",
            model,
            f"{model}:read-sensors-v1",
            semantic,
            "thinq2",
            feature_definition(row),
            enabled=desired_enabled,
            register_producer=True,
        )
    if len(checked_existing(
        args.database, model=args.model, expected_enabled=desired_enabled
    )) != len(selected):
        raise RuntimeError("Feature registration did not persist exactly")
    print(f"applied={len(pending)}")


if __name__ == "__main__":
    main()
