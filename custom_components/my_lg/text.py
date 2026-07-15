"""Default-disabled controls for model fields without enum/range metadata."""

from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory

from . import MyLgConfigEntry
from .compat import AddConfigEntryEntitiesCallback
from .const import OPT_ALLOW_EXPERIMENTAL_CONTROLS, OPT_ALLOW_HAZARDOUS_CONTROLS
from .coordinator import PatDeviceCoordinator
from .coordinator_wideq import WideqCoordinator
from .entity import MyLgWideqEntity
from .value_access import is_meaningful
from .wideq_control import control_risk_allowed, iter_wideq_field_controls


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    wideq = entry.runtime_data.wideq_coordinator
    if wideq is None:
        return
    allow_experimental = bool(
        entry.options.get(OPT_ALLOW_EXPERIMENTAL_CONTROLS, False)
    )
    allow_hazardous = bool(
        entry.options.get(OPT_ALLOW_HAZARDOUS_CONTROLS, False)
    )
    entities: list[TextEntity] = []
    for coordinator in entry.runtime_data.coordinators.values():
        for control in iter_wideq_field_controls(coordinator.model):
            if control.value_type not in {"enum", "range"}:
                entities.append(
                    MyLgWideqCatalogText(
                        wideq,
                        coordinator,
                        control,
                        allow_hazardous,
                        allow_experimental,
                    )
                )
    async_add_entities(entities)


class MyLgWideqCatalogText(MyLgWideqEntity, TextEntity):
    """Raw typed control used only when LG omits value metadata."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False
    _attr_native_min = 0
    _attr_native_max = 255

    def __init__(
        self,
        wideq_coordinator: WideqCoordinator,
        pat_coordinator: PatDeviceCoordinator,
        control,
        hazardous_controls_allowed: bool,
        experimental_controls_allowed: bool,
    ) -> None:
        super().__init__(wideq_coordinator, pat_coordinator, control.key)
        self._control = control
        self._hazardous_controls_allowed = hazardous_controls_allowed
        self._experimental_controls_allowed = experimental_controls_allowed
        self._attr_name = f"WideQ · {control.field}"

    @property
    def available(self) -> bool:
        if not control_risk_allowed(
            self._control,
            allow_hazardous=self._hazardous_controls_allowed,
            allow_experimental=self._experimental_controls_allowed,
            pat_data=self._pat_coordinator.data,
            snapshot=self._snapshot,
        ):
            return False
        return (
            not self.coordinator.circuit_open
            and is_meaningful(self._snapshot.get(self._control.field))
        )

    @property
    def native_value(self) -> str | None:
        value = self._snapshot.get(self._control.field)
        return None if value is None else str(value)

    async def async_set_value(self, value: str) -> None:
        await self._wideq_set(
            self._control.ctrl_key,
            self._control.field,
            value,
            self._control.use_dataset,
            optimistic=self._control.risk == "low",
        )
