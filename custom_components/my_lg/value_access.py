"""Pure helpers for stable RAW paths and PAT/WideQ value access."""

from __future__ import annotations

from collections.abc import Iterator
from hashlib import sha1
import json
import re
from typing import Any


_SENTINELS = {"IGNORE", "NOT_DEFINE_VALUE"}
_SELECTOR_PREFIX = "@location="


def location_of(item: Any) -> str | None:
    """Return a location name from PAT list records when present."""
    if not isinstance(item, dict):
        return None
    location = item.get("locationName")
    if isinstance(location, str):
        return location
    nested = item.get("location")
    if isinstance(nested, dict) and isinstance(nested.get("locationName"), str):
        return nested["locationName"]
    return None


def read_path(data: Any, path: tuple[str, ...], default: Any = None) -> Any:
    """Read a mixed dict/location-list path.

    A dotted WideQ key is checked as an exact dictionary key before any other
    interpretation, so flat ThinQ2 snapshots remain lossless.
    """
    node = data
    for token in path:
        if token.startswith(_SELECTOR_PREFIX):
            if not isinstance(node, list):
                return default
            wanted = token[len(_SELECTOR_PREFIX) :]
            node = next((item for item in node if location_of(item) == wanted), default)
            if node is default:
                return default
            continue
        if not isinstance(node, dict) or token not in node:
            return default
        node = node[token]
    return node


def flatten_values(data: Any, prefix: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    """Yield stable paths for every scalar or logical list in a RAW payload."""
    if isinstance(data, dict):
        if not data and prefix:
            yield prefix
            return
        for key, value in data.items():
            yield from flatten_values(value, (*prefix, str(key)))
        return
    if isinstance(data, list):
        locations = [location_of(item) for item in data]
        if data and all(locations):
            for item, location in zip(data, locations):
                yield from flatten_values(item, (*prefix, f"{_SELECTOR_PREFIX}{location}"))
            return
        # Course lists, content lists and metadata arrays are one logical value;
        # indexing them would make unique IDs depend on list order.
        if prefix:
            yield prefix
        return
    if prefix:
        yield prefix


def is_meaningful(value: Any) -> bool:
    """Return whether a current value represents supported, usable data."""
    if value is None:
        return False
    if isinstance(value, str) and value.upper() in _SENTINELS:
        return False
    return True


def state_value(value: Any) -> Any:
    """Convert complex RAW values to a bounded Home Assistant state."""
    if isinstance(value, (list, dict)):
        return len(value)
    if isinstance(value, str) and len(value) > 255:
        return f"{value[:252]}..."
    return value


def raw_attributes(value: Any, source: str, path: tuple[str, ...]) -> dict[str, Any]:
    """Build lossless diagnostic attributes for a RAW entity."""
    attrs: dict[str, Any] = {"source": source, "raw_path": display_path(path)}
    if isinstance(value, (list, dict)):
        attrs["raw_value"] = value
    elif isinstance(value, str) and len(value) > 255:
        attrs["raw_value"] = value
    return attrs


def display_path(path: tuple[str, ...]) -> str:
    """Return a readable, stable dotted path."""
    return ".".join(
        token[len(_SELECTOR_PREFIX) :] if token.startswith(_SELECTOR_PREFIX) else token
        for token in path
    )


def stable_feature_key(source: str, path: tuple[str, ...]) -> str:
    """Build a readable collision-resistant key for a RAW entity."""
    readable = display_path(path)
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", readable).lower()
    snake = re.sub(r"[^a-z0-9]+", "_", snake).strip("_")
    digest = sha1(json.dumps(path, ensure_ascii=False).encode()).hexdigest()[:8]
    return f"raw_{source}_{snake[:72]}_{digest}"


def nested_payload(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    """Build a nested PAT control payload for a non-location path."""
    if not path or any(token.startswith(_SELECTOR_PREFIX) for token in path):
        raise ValueError("location paths require a device-specific payload")
    result: Any = value
    for token in reversed(path):
        result = {token: result}
    return result
