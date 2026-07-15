"""Operation-control buttons for washtower (washer/dryer) and styler.

These issue a one-shot ``*OperationMode`` command (START/STOP). Payloads mirror
the thinqconnect SDK — washtower sub-units are location-keyed, the styler is flat:
    washer: {"washer": {"operation": {"washerOperationMode": "START"}}}
    dryer:  {"dryer":  {"operation": {"dryerOperationMode":  "START"}}}
    styler: {"operation": {"stylerOperationMode": "START"}}

STOP typically requires the appliance to be running; START a loaded/ready one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory

from . import MyLgConfigEntry
from .compat import AddConfigEntryEntitiesCallback
from .const import DEVICE_TYPE_STYLER, DEVICE_TYPE_WASHTOWER
from .const import (
    OPT_ALLOW_EXPERIMENTAL_CONTROLS,
    OPT_ALLOW_HAZARDOUS_CONTROLS,
)
from .control_router import build_wideq_request, remote_control_enabled
from .coordinator import PatDeviceCoordinator
from .coordinator_wideq import WideqCoordinator
from .entity import MyLgEntity, MyLgWideqEntity
from .feature_catalog import get_wideq_control
from .value_access import stable_feature_key


@dataclass(frozen=True, kw_only=True)
class MyLgButtonDescription(ButtonEntityDescription):
    """Button that posts a fixed control payload on press."""

    payload: dict[str, Any]


def _op(key: str, payload: dict[str, Any]) -> MyLgButtonDescription:
    return MyLgButtonDescription(key=key, translation_key=key, payload=payload)


def _washer(mode: str) -> dict[str, Any]:
    return {"washer": {"operation": {"washerOperationMode": mode}}}


def _dryer(mode: str) -> dict[str, Any]:
    return {"dryer": {"operation": {"dryerOperationMode": mode}}}


WASHTOWER_BUTTONS: tuple[MyLgButtonDescription, ...] = (
    _op("washer_start", _washer("START")),
    _op("washer_stop", _washer("STOP")),
    _op("washer_power_off", _washer("POWER_OFF")),
    _op("dryer_start", _dryer("START")),
    _op("dryer_stop", _dryer("STOP")),
    _op("dryer_power_off", _dryer("POWER_OFF")),
)

STYLER_BUTTONS: tuple[MyLgButtonDescription, ...] = (
    _op("styler_start", {"operation": {"stylerOperationMode": "START"}}),
    _op("styler_stop", {"operation": {"stylerOperationMode": "STOP"}}),
    _op("styler_power_off", {"operation": {"stylerOperationMode": "POWER_OFF"}}),
    MyLgButtonDescription(
        key="styler_power_on",
        translation_key="styler_power_on",
        payload={"operation": {"stylerOperationMode": "POWER_ON"}},
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
)


@dataclass(frozen=True, kw_only=True)
class TimerClearDescription(ButtonEntityDescription):
    """PAT timer flag that clears a previously configured reservation."""

    group: str
    field: str


_TIMER_CLEAR_FIELDS = (
    ("timer", "absoluteStartTimer", "clear_absolute_start_timer"),
    ("timer", "absoluteStopTimer", "clear_absolute_stop_timer"),
    ("sleepTimer", "relativeStopTimer", "clear_sleep_timer"),
)


# Parameterless model actions that are safe to represent as buttons. Composite
# start/recipe/download commands remain on the validated service because they
# require explicit parameters.
_WIDEQ_ACTION_BUTTONS: dict[str, tuple[tuple[str | None, str], ...]] = {
    "WBEF3": ((None, "setCookStop"), (None, "setClearRecipe")),
    "WMLJ32RS": (
        (None, "SetCookStop"),
        (None, "OVWakeup"),
        (None, "ResetDownloadRecipe"),
    ),
    "ST_R_ETH01Y_": ((None, "wakeup"),),
    "WTL_KPK_BDH_KR_01": (("washer", "WMWakeup"), ("dryer", "WMWakeup")),
}

BUTTONS_BY_TYPE: dict[str, tuple[MyLgButtonDescription, ...]] = {
    DEVICE_TYPE_WASHTOWER: WASHTOWER_BUTTONS,
    DEVICE_TYPE_STYLER: STYLER_BUTTONS,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up operation-control buttons."""
    entities: list[ButtonEntity] = []
    for coordinator in entry.runtime_data.coordinators.values():
        for desc in BUTTONS_BY_TYPE.get(coordinator.device_type, ()):
            entities.append(MyLgButton(coordinator, desc))
        for group, field, key in _TIMER_CLEAR_FIELDS:
            if coordinator.supports_field(group, field):
                entities.append(
                    MyLgTimerClearButton(
                        coordinator,
                        TimerClearDescription(
                            key=key,
                            name=key.replace("_", " ").title(),
                            group=group,
                            field=field,
                            entity_category=EntityCategory.CONFIG,
                            entity_registry_enabled_default=False,
                        ),
                    )
                )

    wideq = entry.runtime_data.wideq_coordinator
    if wideq is not None:
        allow_hazardous = bool(
            entry.options.get(OPT_ALLOW_HAZARDOUS_CONTROLS, False)
        )
        allow_experimental = bool(
            entry.options.get(OPT_ALLOW_EXPERIMENTAL_CONTROLS, False)
        )
        for coordinator in entry.runtime_data.coordinators.values():
            for subdevice, control_name in _WIDEQ_ACTION_BUTTONS.get(
                coordinator.model, ()
            ):
                spec = get_wideq_control(
                    coordinator.model, control_name, subdevice
                )
                if spec is not None:
                    entities.append(
                        MyLgWideqActionButton(
                            wideq,
                            coordinator,
                            subdevice,
                            spec,
                            allow_hazardous,
                            allow_experimental,
                        )
                    )
    async_add_entities(entities)


class MyLgButton(MyLgEntity, ButtonEntity):
    """One-shot operation command."""

    entity_description: MyLgButtonDescription

    def __init__(
        self, coordinator: PatDeviceCoordinator, description: MyLgButtonDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        await self.coordinator.async_control(self.entity_description.payload)


class MyLgTimerClearButton(MyLgEntity, ButtonEntity):
    """Clear one PAT reservation flag without modifying any other timer."""

    entity_description: TimerClearDescription

    def __init__(
        self, coordinator: PatDeviceCoordinator, description: TimerClearDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        desc = self.entity_description
        payload = {desc.group: {desc.field: "UNSET"}}
        await self.coordinator.async_control(payload)
        self.coordinator.handle_mqtt_status(payload)


class MyLgWideqActionButton(MyLgWideqEntity, ButtonEntity):
    """A parameterless audited WideQ action with safety gating."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        wideq_coordinator: WideqCoordinator,
        pat_coordinator: PatDeviceCoordinator,
        subdevice: str | None,
        spec: dict[str, Any],
        allow_hazardous: bool,
        allow_experimental: bool,
    ) -> None:
        key = stable_feature_key(
            "wideq_action",
            tuple(part for part in (subdevice, spec["ctrl_key"]) if part),
        )
        super().__init__(wideq_coordinator, pat_coordinator, key)
        self._pat_coordinator = pat_coordinator
        self._spec = spec
        self._allow_hazardous = allow_hazardous
        self._allow_experimental = allow_experimental
        label = f"{subdevice} {spec['ctrl_key']}" if subdevice else spec["ctrl_key"]
        self._attr_name = f"WideQ · {label}"

    @property
    def available(self) -> bool:
        if self.coordinator.circuit_open or not self._snapshot:
            return False
        risk = self._spec.get("risk", "low")
        if risk == "hazardous" and not self._allow_hazardous:
            return False
        if risk == "experimental" and not self._allow_experimental:
            return False
        if risk in {"operation", "hazardous"}:
            return remote_control_enabled(
                self._pat_coordinator.data
            ) or remote_control_enabled(self._snapshot)
        return True

    async def async_press(self) -> None:
        request = build_wideq_request(
            self._spec, command=None, values={}, snapshot=self._snapshot
        )
        await self.coordinator.async_control(
            self._alias, self._spec["ctrl_key"], **request
        )
