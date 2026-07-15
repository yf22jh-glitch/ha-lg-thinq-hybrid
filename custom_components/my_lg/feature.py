"""Shared feature descriptions for data-driven PAT/WideQ entities."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class FeatureSource(str, Enum):
    """Authoritative source for a feature."""

    PAT = "pat"
    WIDEQ = "wideq"


class FeatureAccess(str, Enum):
    """Access exposed by the upstream schema."""

    READ = "read"
    WRITE = "write"
    READ_WRITE = "read_write"
    ACTION = "action"


class FeatureRisk(str, Enum):
    """Safety class used to decide default exposure and retry behavior."""

    READ_ONLY = "read_only"
    LOW = "low"
    OPERATION = "operation"
    HAZARDOUS = "hazardous"
    EXPERIMENTAL = "experimental"


@dataclass(frozen=True)
class FeatureDescription:
    """Source-neutral logical feature metadata."""

    key: str
    source: FeatureSource
    path: tuple[str, ...]
    access: FeatureAccess = FeatureAccess.READ
    risk: FeatureRisk = FeatureRisk.READ_ONLY
    value_type: str = "string"
    options: tuple[Any, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    location: str | None = None
    enabled_default: bool = False
