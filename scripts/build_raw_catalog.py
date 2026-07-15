"""Build the identifier-free RAW path catalog from audited local captures.

The input dumps are intentionally not committed. The generated catalog contains
only model names and field paths: no device IDs, aliases, tokens, SSIDs or
values. Run from the repository root after collecting a newer audited capture.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "custom_components" / "my_lg" / "feature_catalog" / "raw_paths.json"


def location_of(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    if isinstance(item.get("locationName"), str):
        return item["locationName"]
    location = item.get("location")
    if isinstance(location, dict) and isinstance(location.get("locationName"), str):
        return location["locationName"]
    return None


def flatten(data: Any, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    if isinstance(data, dict):
        out: list[tuple[str, ...]] = []
        if not data and prefix:
            return [prefix]
        for key, value in data.items():
            out.extend(flatten(value, (*prefix, str(key))))
        return out
    if isinstance(data, list):
        locations = [location_of(item) for item in data]
        if data and all(locations):
            out = []
            for item, location in zip(data, locations):
                out.extend(flatten(item, (*prefix, f"@location={location}")))
            return out
        return [prefix] if prefix else []
    # Preserve unsupported/sentinel/null paths in the static inventory. Runtime
    # entities mark their current value unavailable, but remain registered so a
    # later firmware/device value can appear without a reload or catalog change.
    return [prefix] if prefix else []


def model_read_paths(data: dict[str, Any]) -> set[tuple[str, ...]]:
    """Return snapshot paths declared by the current model schema."""
    paths: set[tuple[str, ...]] = set()
    target_root = data.get("Config", {}).get("targetRoot")
    monitoring_values = data.get("MonitoringValue")
    if isinstance(target_root, str) and isinstance(monitoring_values, dict):
        paths.update((target_root, str(field)) for field in monitoring_values)

    monitoring = data.get("Monitoring")
    if isinstance(monitoring, dict):
        for item in monitoring.get("protocol", ()):
            super_set = item.get("superSet") if isinstance(item, dict) else None
            if isinstance(super_set, str):
                paths.add(tuple(super_set.split(".")))
            elif isinstance(item, dict) and isinstance(item.get("value"), str):
                # ThinQ1 binary monitoring decodes into flat symbolic fields.
                paths.add((item["value"],))

    values = data.get("Value")
    if not isinstance(monitoring, dict) and isinstance(values, dict):
        # Flat ThinQ2 snapshots (notably AC) retain dots inside each key.
        paths.update(
            (str(field),)
            for field in values
            if "." in str(field) and not str(field).startswith("support.")
        )

    for part in ("washer", "dryer"):
        block = data.get(part)
        if not isinstance(block, dict):
            continue
        sub_values = block.get("MonitoringValue")
        if isinstance(sub_values, dict):
            paths.update((part, str(field)) for field in sub_values)
    return paths


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("model_dir", nargs="?", default="/tmp/lg_models")
    args = parser.parse_args()
    pat = json.loads((ROOT / "lg_pat_dump.json").read_text(encoding="utf-8"))
    wideq = json.loads((ROOT / "lg_wideq_dump.json").read_text(encoding="utf-8"))
    catalog: dict[str, dict[str, list[list[str]]]] = {"pat": {}, "wideq": {}}

    for device in pat.get("devices", []):
        info = device.get("deviceInfo", {})
        model = info.get("modelName") or device.get("deviceType")
        paths = set(flatten(device.get("status")))
        catalog["pat"].setdefault(model, [])
        catalog["pat"][model].extend([list(path) for path in sorted(paths)])

    for device in wideq.get("devices", []):
        model = device.get("model")
        if not model:
            continue
        snapshot = device.get("raw", {}).get("snapshot")
        paths = set(flatten(snapshot)) if isinstance(snapshot, dict) else set()
        catalog["wideq"].setdefault(model, [])
        catalog["wideq"][model].extend([list(path) for path in sorted(paths)])

    model_dir = Path(args.model_dir)
    for path in sorted(model_dir.glob("*.json")):
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp949")
        model = path.stem
        catalog["wideq"].setdefault(model, [])
        catalog["wideq"][model].extend(
            [list(item) for item in sorted(model_read_paths(json.loads(text)))]
        )

    for source in catalog.values():
        for model, paths in source.items():
            source[model] = sorted({tuple(path) for path in paths})
            source[model] = [list(path) for path in source[model]]

    OUTPUT.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
