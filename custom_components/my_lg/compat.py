"""Small Home Assistant API aliases kept across supported core versions."""

from __future__ import annotations

try:
    from homeassistant.helpers.entity_platform import (
        AddConfigEntryEntitiesCallback,
    )
except ImportError:  # Home Assistant 2024.11
    from homeassistant.helpers.entity_platform import (
        AddEntitiesCallback as AddConfigEntryEntitiesCallback,
    )

__all__ = ["AddConfigEntryEntitiesCallback"]
