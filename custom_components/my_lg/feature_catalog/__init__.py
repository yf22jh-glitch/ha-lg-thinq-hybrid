"""Data-driven feature catalogs for my_lg."""

from .pat import discover_pat_features
from .raw import _catalog as _raw_catalog
from .raw import catalog_paths
from .wideq import control_catalog, get_wideq_control, list_wideq_controls


def load_catalogs() -> None:
    """Load file-backed catalogs from an executor before platform setup."""
    _raw_catalog()
    control_catalog()

__all__ = [
    "catalog_paths",
    "discover_pat_features",
    "get_wideq_control",
    "list_wideq_controls",
    "load_catalogs",
]
