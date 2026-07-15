"""Load the generated, identifier-free RAW path inventory."""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path


@lru_cache(maxsize=1)
def _catalog() -> dict:
    path = Path(__file__).with_name("raw_paths.json")
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def catalog_paths(source: str, model: str) -> tuple[tuple[str, ...], ...]:
    """Return known paths for a model and source."""
    paths = _catalog().get(source, {}).get(model, [])
    return tuple(tuple(token for token in path) for path in paths)
