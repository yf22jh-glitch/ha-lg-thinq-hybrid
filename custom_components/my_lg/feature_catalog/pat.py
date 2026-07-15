"""Discover all readable/writable capabilities from a ThinQ Connect profile."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from ..feature import (
    FeatureAccess,
    FeatureDescription,
    FeatureRisk,
    FeatureSource,
)
from ..value_access import stable_feature_key


def _access(mode: Any) -> FeatureAccess:
    values = set(mode if isinstance(mode, list) else ())
    if "r" in values and "w" in values:
        return FeatureAccess.READ_WRITE
    if "w" in values:
        return FeatureAccess.WRITE
    return FeatureAccess.READ


def _risk(path: tuple[str, ...], access: FeatureAccess) -> FeatureRisk:
    joined = ".".join(path).lower()
    if access == FeatureAccess.READ:
        return FeatureRisk.READ_ONLY
    if any(word in joined for word in ("operation", "powerlevel", "cook")):
        if any(word in joined for word in ("oven", "cook", "powerlevel")):
            return FeatureRisk.HAZARDOUS
        return FeatureRisk.OPERATION
    return FeatureRisk.LOW


def _description(path: tuple[str, ...], spec: dict[str, Any]) -> FeatureDescription:
    access = _access(spec.get("mode"))
    value_type = str(spec.get("type", "string")).lower()
    values = spec.get("value", {})
    write_spec = values.get("w") if isinstance(values, dict) else None
    read_spec = values.get("r") if isinstance(values, dict) else None
    detail = write_spec if write_spec is not None else read_spec
    options: tuple[Any, ...] = tuple(detail) if isinstance(detail, list) else ()
    minimum = maximum = step = None
    if isinstance(detail, dict):
        minimum = detail.get("min")
        maximum = detail.get("max")
        step = detail.get("step", 1)
    location = next(
        (
            token.split("=", 1)[1]
            for token in path
            if token.startswith("@location=")
        ),
        None,
    )
    return FeatureDescription(
        key=stable_feature_key("pat_control" if "w" in set(spec.get("mode", ())) else "pat", path),
        source=FeatureSource.PAT,
        path=path,
        access=access,
        risk=_risk(path, access),
        value_type=value_type,
        options=options,
        minimum=minimum,
        maximum=maximum,
        step=step,
        location=location,
    )


def _walk(node: Any, prefix: tuple[str, ...]) -> Iterator[FeatureDescription]:
    if isinstance(node, dict) and "type" in node and "mode" in node:
        yield _description(prefix, node)
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if key in {"notification", "location", "locationName", "unit"}:
                continue
            yield from _walk(value, (*prefix, str(key)))
        return
    if not isinstance(node, list):
        return
    for index, item in enumerate(node):
        if not isinstance(item, dict):
            continue
        location = item.get("locationName")
        if not isinstance(location, str):
            loc = item.get("location")
            location = loc.get("locationName") if isinstance(loc, dict) else None
        selector = f"@location={location}" if location else f"@index={index}"
        yield from _walk(item, (*prefix, selector))


def discover_pat_features(profile: dict[str, Any] | None) -> tuple[FeatureDescription, ...]:
    """Return every profile property, including washtower sub-profiles."""
    if not isinstance(profile, dict):
        return ()
    features: list[FeatureDescription] = []
    prop = profile.get("property")
    if prop is not None:
        features.extend(_walk(prop, ()))
    for part in ("washer", "dryer"):
        sub = profile.get(part)
        if isinstance(sub, dict) and sub.get("property") is not None:
            features.extend(_walk(sub["property"], (part,)))
    # A malformed profile can repeat the same feature; preserve first order.
    unique: dict[tuple[str, ...], FeatureDescription] = {}
    for feature in features:
        unique.setdefault(feature.path, feature)
    return tuple(unique.values())
