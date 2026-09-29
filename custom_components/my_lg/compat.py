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

try:
    from homeassistant.const import UnitOfDensity
except ImportError:  # Home Assistant 2024.11
    from homeassistant.const import CONCENTRATION_MICROGRAMS_PER_CUBIC_METER

    class UnitOfDensity:
        """Backport the density member used by reviewed particulate sensors."""

        MICROGRAMS_PER_CUBIC_METER = CONCENTRATION_MICROGRAMS_PER_CUBIC_METER


__all__ = ["AddConfigEntryEntitiesCallback", "UnitOfDensity"]
