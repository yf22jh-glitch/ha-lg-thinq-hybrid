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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory

from . import MyLgConfigEntry
from .compat import AddConfigEntryEntitiesCallback
from .const import (
    DEVICE_TYPE_STYLER,
    DEVICE_TYPE_WASHTOWER,
    OPT_ALLOW_EXPERIMENTAL_CONTROLS,
    OPT_ALLOW_HAZARDOUS_CONTROLS,
)
from .control_router import build_wideq_request, remote_control_enabled
from .coordinator import PatDeviceCoordinator
from .coordinator_wideq import WideqCoordinator
from .entity import MyLgEntity, MyLgWideqEntity
from .feature_catalog import get_wideq_control
from .local_command import LocalCommandFailed
from .local_control_entity import local_control_entities_for_domain
from .local_control_router import LocalControlRouter
from .local_control_native import native_local_available
from .value_access import stable_feature_key
from .app_setting_entity import app_setting_entities


@dataclass(frozen=True, kw_only=True)
class MyLgButtonDescription(ButtonEntityDescription):
    """Button that posts a fixed control payload on press."""

    payload: dict[str, Any]
    local_capability: str | None = None
    local_value: str = "true"


def _op(
    key: str,
    payload: dict[str, Any],
    local_capability: str | None = None,
    local_value: str = "true",
) -> MyLgButtonDescription:
    return MyLgButtonDescription(
        key=key,
        translation_key=key,
        payload=payload,
        local_capability=local_capability,
        local_value=local_value,
    )


def _washer(mode: str) -> dict[str, Any]:
    return {"washer": {"operation": {"washerOperationMode": mode}}}


def _dryer(mode: str) -> dict[str, Any]:
    return {"dryer": {"operation": {"dryerOperationMode": mode}}}


WASHTOWER_BUTTONS: tuple[MyLgButtonDescription, ...] = (
    # These reuse the existing owners. Routing does not promote a capability:
    # the router still requires the exact per-binding producer authority.
    _op("washer_start", _washer("START"), "washer.operation.start_or_resume"),
    _op("washer_stop", _washer("STOP"), "washer.operation.pause"),
    _op("washer_power_off", _washer("POWER_OFF"), "washer.power_requested", "false"),
    _op("dryer_start", _dryer("START"), "dryer.operation.start_or_resume"),
    _op("dryer_stop", _dryer("STOP"), "dryer.operation.pause"),
    _op("dryer_power_off", _dryer("POWER_OFF"), "dryer.power_requested", "false"),
)

STYLER_BUTTONS: tuple[MyLgButtonDescription, ...] = (
    _op("styler_start", {"operation": {"stylerOperationMode": "START"}}),
    _op(
        "styler_stop",
        {"operation": {"stylerOperationMode": "STOP"}},
        "styler.operation.pause",
    ),
    _op(
        "styler_power_off",
        {"operation": {"stylerOperationMode": "POWER_OFF"}},
        "operation.power_requested",
        "false",
    ),
    MyLgButtonDescription(
        key="styler_power_on",
        translation_key="styler_power_on",
        payload={"operation": {"stylerOperationMode": "POWER_ON"}},
        local_capability="operation.power_requested",
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
    from .feature_runtime import setup_feature_entities

    setup_feature_entities(entry, "button", lambda: _build_entities(entry), async_add_entities)


def _build_entities(entry: MyLgConfigEntry) -> list[ButtonEntity]:
    """Set up operation-control buttons."""
    entities: list[ButtonEntity] = []
    for coordinator in entry.runtime_data.coordinators.values():
        for desc in BUTTONS_BY_TYPE.get(coordinator.device_type, ()):
            entities.append(
                MyLgButton(coordinator, desc, entry.runtime_data.local_control)
            )
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
    entities.extend(local_control_entities_for_domain(entry, "button"))
    entities.extend(app_setting_entities(entry, "button"))
    return entities


class MyLgButton(MyLgEntity, ButtonEntity):
    """One-shot operation command."""

    entity_description: MyLgButtonDescription

    def __init__(
        self,
        coordinator: PatDeviceCoordinator,
        description: MyLgButtonDescription,
        local_control: LocalControlRouter | None = None,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._local_control = local_control

    @property
    def available(self) -> bool:
        capability = ('styler.operation.start_or_resume'
                      if self.entity_description.key == 'styler_start'
                      else self.entity_description.local_capability)
        return native_local_available(self._local_control, self.coordinator.device_id, capability) or super().available

    async def async_press(self) -> None:
        if self.entity_description.key == "styler_start" and self._local_control is not None:
            selected = self._local_control.take_styler_start(self.coordinator.device_id)
            if selected is None:
                raise HomeAssistantError("먼저 실행할 코스 또는 코스·옵션 입력을 선택해 주세요. 선택만으로는 가동되지 않아요.")
            capability, value = selected
            try:
                outcome = await self._local_control.async_execute(self.coordinator.device_id, capability, value)
            except LocalCommandFailed as err:
                raise HomeAssistantError("스타일러 실행 결과를 확인할 수 없어요. 상태를 확인한 뒤 다시 선택해 주세요.") from err
            if outcome is None:
                raise HomeAssistantError("로컬 실행이 전송 전에 거부됐어요. 원격제어 허용과 연결을 확인해 주세요.")
            return  # Never duplicate a course/start through cloud fallback.
        capability = self.entity_description.local_capability
        if capability is not None and self._local_control is not None:
            try:
                outcome = await self._local_control.async_execute(
                    self.coordinator.device_id,
                    capability,
                    self.entity_description.local_value,
                )
            except LocalCommandFailed as err:
                # The frame may already be on the wire. Retrying the same press through LG would
                # be a duplicate command, so surface the uncertainty instead.
                raise HomeAssistantError(f"{self.coordinator.alias}: {err}") from err
            if outcome is not None:
                # Confirmed/already and unverifiable all mean a frame left the bridge. Only a
                # pre-wire refusal returns None and is safe to offer to the cloud.
                return
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
            self._device_id, self._spec["ctrl_key"], **request
        )
