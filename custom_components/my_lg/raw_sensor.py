"""Default-disabled entities exposing the complete audited PAT/WideQ RAW set."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.entity import EntityCategory
from .compat import AddConfigEntryEntitiesCallback

from .coordinator import PatDeviceCoordinator
from .coordinator_wideq import WideqCoordinator
from .entity import MyLgEntity, MyLgWideqEntity
from .feature import FeatureAccess
from .feature_catalog import catalog_paths, discover_pat_features
from .value_access import (
    display_path,
    flatten_values,
    is_meaningful,
    raw_attributes,
    read_path,
    stable_feature_key,
    state_value,
)


class PatRawSensor(MyLgEntity, SensorEntity):
    """One scalar or logical list from the PAT status payload."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: PatDeviceCoordinator, path: tuple[str, ...]) -> None:
        self._path = path
        super().__init__(coordinator, stable_feature_key("pat", path))
        self._attr_name = f"PAT RAW · {display_path(path)}"

    @property
    def available(self) -> bool:
        return is_meaningful(read_path(self.coordinator.data, self._path))

    @property
    def native_value(self) -> Any:
        return state_value(read_path(self.coordinator.data, self._path))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        value = read_path(self.coordinator.data, self._path)
        return raw_attributes(value, "PAT", self._path)


class WideqRawSensor(MyLgWideqEntity, SensorEntity):
    """One scalar or logical list from the retained all-device snapshot."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        wideq: WideqCoordinator,
        coordinator: PatDeviceCoordinator,
        path: tuple[str, ...],
    ) -> None:
        self._path = path
        super().__init__(wideq, coordinator, stable_feature_key("wideq", path))
        self._attr_name = f"WideQ RAW · {display_path(path)}"

    @property
    def available(self) -> bool:
        return is_meaningful(read_path(self._snapshot, self._path))

    @property
    def native_value(self) -> Any:
        return state_value(read_path(self._snapshot, self._path))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(self.coordinator.diagnostic_attributes)
        attrs.update(raw_attributes(read_path(self._snapshot, self._path), "WideQ", self._path))
        return attrs


class RawSensorManager:
    """Register catalog paths now and previously unseen live paths later."""

    def __init__(
        self,
        coordinators: list[PatDeviceCoordinator],
        wideq: WideqCoordinator | None,
        async_add_entities: AddConfigEntryEntitiesCallback,
    ) -> None:
        self._coordinators = coordinators
        self._wideq = wideq
        self._add = async_add_entities
        self._known: set[tuple[str, str, tuple[str, ...]]] = set()

    def _paths(self, source: str, model: str, data: Any) -> set[tuple[str, ...]]:
        paths = set(catalog_paths(source, model))
        paths.update(
            path
            for path in flatten_values(data)
            if is_meaningful(read_path(data, path))
        )
        return paths

    def add_new(self) -> None:
        """Add all catalog/live paths that have not been registered yet."""
        entities: list[SensorEntity] = []
        for coordinator in self._coordinators:
            pat_paths = self._paths("pat", coordinator.model, coordinator.data)
            pat_paths.update(
                feature.path
                for feature in discover_pat_features(coordinator.profile)
                if feature.access in {FeatureAccess.READ, FeatureAccess.READ_WRITE}
            )
            for path in sorted(pat_paths):
                identity = (coordinator.device_id, "pat", path)
                if identity in self._known:
                    continue
                self._known.add(identity)
                entities.append(PatRawSensor(coordinator, path))

            if self._wideq is None:
                continue
            snapshot = self._wideq.snapshot_for(coordinator.device_id)
            for path in sorted(self._paths("wideq", coordinator.model, snapshot)):
                identity = (coordinator.device_id, "wideq", path)
                if identity in self._known:
                    continue
                self._known.add(identity)
                entities.append(WideqRawSensor(self._wideq, coordinator, path))
        if entities:
            self._add(entities)
