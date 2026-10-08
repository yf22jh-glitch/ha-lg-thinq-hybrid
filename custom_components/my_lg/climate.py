"""Air conditioner climate entity with stable Local-or-cloud ownership."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from typing import Any

from homeassistant.components.climate import (
    SWING_BOTH,
    SWING_HORIZONTAL,
    SWING_OFF,
    SWING_VERTICAL,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

try:
    from homeassistant.helpers.device_registry import DeviceInfo
except ImportError:  # pragma: no cover - compatibility with older HA layouts
    from homeassistant.helpers.entity import DeviceInfo
from .compat import AddConfigEntryEntitiesCallback

from . import MyLgConfigEntry
from .const import DEVICE_TYPE_AIR_CONDITIONER, DOMAIN
from .coordinator import PatDeviceCoordinator
from .entity import MyLgEntity
from .local_command import (
    CLIMATE_POWER_ON_CAPABILITY,
    CLIMATE_TUPLE_CAPABILITY,
    HVAC_TO_LOCAL_MODE,
    LOCAL_NATIVE_HVAC_TO_MODE,
    SWING_HORIZONTAL_CAPABILITY,
    SWING_VERTICAL_CAPABILITY,
    WIND_STRENGTH_TO_LOCAL_FAN,
    LocalCommandFailed,
    LocalCommandPending,
    LocalCommandResult,
    reflects_the_appliance,
    swing_writes,
)
from .local_control_router import LocalControlRouter
from .local_control_composite_domain import (
    LocalControlCompositeCapability,
    LocalControlCompositeDomainContract,
    LocalControlCompositeInputDomain,
)
from .local_read_owner import (
    local_climate_control_domain,
    resolve_tlv_read_owned_field,
    tlv_read_owner_configured,
)
from .local_read_provider import TlvReadShadowProvider
from .local_provider import LocalSemanticShadowProvider

_LOGGER = logging.getLogger(__name__)

# ThinQ jobMode <-> HA HVACMode
JOBMODE_TO_HVAC = {
    "COOL": HVACMode.COOL,
    "AIR_DRY": HVACMode.DRY,
    "FAN": HVACMode.FAN_ONLY,
    "AUTO": HVACMode.AUTO,
}
HVAC_TO_JOBMODE = {v: k for k, v in JOBMODE_TO_HVAC.items()}

# The complete TLV feed uses the packet decoder's stable semantic tokens. Keep
# those separate from ThinQ Connect's resource vocabulary: the entity still
# exposes the same HA modes and fan actions it did before the read overlay.
LOCAL_MODE_TO_JOBMODE = {
    "cool": "COOL",
    "dry": "AIR_DRY",
    "fan_only": "FAN",
    "auto": "AUTO",
}
LOCAL_FAN_TO_HA = {
    # The climate contract is intentionally the coarse five-step control. The
    # separate detailed-fan select owns SLOW_LOW, so very-low is displayed as
    # LOW here without adding a new climate action.
    "very low": "LOW",
    "low": "LOW",
    "medium": "MID",
    "high": "HIGH",
    "power": "POWER",
    "auto": "AUTO",
}

# Local-native climate uses the decoder's complete fan vocabulary.  Keep
# `very low` distinct instead of collapsing it into LOW: collapsing two Local
# states would make a round-trip write choose a value the user did not select.
LOCAL_NATIVE_FAN_TO_HA = {
    "very low": "VERY_LOW",
    "low": "LOW",
    "medium": "MID",
    "high": "HIGH",
    "power": "POWER",
    "auto": "AUTO",
}
HA_TO_LOCAL_NATIVE_FAN = {
    value: key for key, value in LOCAL_NATIVE_FAN_TO_HA.items()
}
_ATTR_HVAC_MODE = "hvac_mode"

POWER_ON = "POWER_ON"
POWER_OFF = "POWER_OFF"

# LG exposes different writable target-temperature properties for each job
# mode.  Sending the generic targetTemperature while AUTO is active is
# rejected by these wall-mounted units with COMMAND_NOT_SUPPORTED_IN_MODE.
TEMPERATURE_FIELD_BY_JOBMODE = {
    "COOL": "coolTargetTemperature",
    "AUTO": "autoTargetTemperature",
}

JOBMODES_WITHOUT_TARGET_TEMPERATURE = {"AIR_DRY", "FAN"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    from .feature_runtime import setup_feature_entities

    setup_feature_entities(entry, "climate", lambda: _build_entities(entry), async_add_entities)


def _build_entities(entry: MyLgConfigEntry) -> list[ClimateEntity]:
    """Set up climate entities for air conditioners."""
    primary_providers = getattr(entry.runtime_data, "local_providers", {})
    read_providers = getattr(entry.runtime_data, "local_read_providers", {})
    local_control = entry.runtime_data.local_control
    composite_contract = getattr(
        entry.runtime_data, "local_control_composite_domain_contract", None
    )
    entities: list[ClimateEntity] = []
    for coordinator in entry.runtime_data.coordinators.values():
        local_owner = primary_providers.get(coordinator.device_id)
        local_domain = local_climate_control_domain(
            local_owner,
            coordinator.model,
            composite_contract,
        )
        if local_owner is not None and local_domain is not None:
            # A Local read owner gets a Local-only climate object.  The legacy
            # PAT object is not instantiated, so no missing Local value can
            # fall through to cloud state or control.
            entities.append(
                MyLgLocalClimate(
                    coordinator,
                    local_control,
                    local_owner,
                    composite_contract,
                )
            )
        elif coordinator.device_type == DEVICE_TYPE_AIR_CONDITIONER:
            entities.append(
                MyLgClimate(
                    coordinator,
                    local_control,
                    read_providers.get(coordinator.device_id),
                )
            )
    settings = getattr(entry.runtime_data, 'app_settings', None)
    for entity in entities:
        metadata = getattr(entity, '_metadata', None) or getattr(entity, 'coordinator', None)
        if settings is not None and metadata.model in ('CST_170004_WW', 'CST_570004_WW'):
            entity.configure_temperature_presentation(settings, metadata.device_id)
    return entities


from .temperature_presentation import TemperaturePresentationMixin


class _LocalClimateState(ClimateEntity):
    """AC climate whose live state and every offered command are Local-only."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: PatDeviceCoordinator,
        router: LocalControlRouter | None,
        read_provider: LocalSemanticShadowProvider,
        composite_contract: LocalControlCompositeDomainContract | None,
    ) -> None:
        self._metadata = coordinator
        self._router = router
        self._local_read = read_provider
        self._remove_local_read_listener: Callable[[], None] | None = None
        self._command_lock = asyncio.Lock()
        self._reported_unreviewed_local_values: set[str] = set()
        self._last_targets_by_mode: dict[str, float] = {}
        self._last_comfort_preference: int | None = None
        self._attr_unique_id = f"{coordinator.device_id}_climate"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.device_id)},
            name=coordinator.alias,
            manufacturer="LG",
            model=coordinator.model or coordinator.device_type,
        )

        self._tuple_capability = self._capability(
            composite_contract, CLIMATE_TUPLE_CAPABILITY
        )
        self._power_on_capability = self._capability(
            composite_contract, CLIMATE_POWER_ON_CAPABILITY
        )
        self._domain = self._shared_domain(
            self._tuple_capability, self._power_on_capability
        )
        self._configure_surface()
        self._remember_current_target()

    def _capability(
        self,
        contract: LocalControlCompositeDomainContract | None,
        capability_id: str,
    ) -> LocalControlCompositeCapability | None:
        if contract is None:
            return None
        return contract.capability(self._metadata.model, capability_id)

    @staticmethod
    def _shared_domain(
        tuple_capability: LocalControlCompositeCapability | None,
        power_capability: LocalControlCompositeCapability | None,
    ) -> LocalControlCompositeInputDomain | None:
        if (
            tuple_capability is None
            or power_capability is None
            or tuple_capability.input_domain != power_capability.input_domain
        ):
            return None
        return tuple_capability.input_domain

    def _configure_surface(self) -> None:
        domain = self._domain
        modes = [] if domain is None else [
            JOBMODE_TO_HVAC[LOCAL_MODE_TO_JOBMODE[token]]
            for token in domain.modes
            if token in LOCAL_MODE_TO_JOBMODE
            and LOCAL_MODE_TO_JOBMODE[token] in JOBMODE_TO_HVAC
        ]
        self._attr_hvac_modes = [HVACMode.OFF, *modes]
        self._attr_fan_modes = [] if domain is None else [
            LOCAL_NATIVE_FAN_TO_HA[token]
            for token in domain.fans
            if token in LOCAL_NATIVE_FAN_TO_HA
        ]
        self._swing_lr = self._local_boolean_control_complete(
            "swing.horizontal_enabled", SWING_HORIZONTAL_CAPABILITY
        )
        self._swing_ud = self._local_boolean_control_complete(
            "swing.vertical_enabled", SWING_VERTICAL_CAPABILITY
        )
        if self._swing_lr or self._swing_ud:
            modes = [SWING_OFF]
            if self._swing_lr:
                modes.append(SWING_HORIZONTAL)
            if self._swing_ud:
                modes.append(SWING_VERTICAL)
            if self._swing_lr and self._swing_ud:
                modes.append(SWING_BOTH)
            self._attr_swing_modes = modes

    def _local_boolean_control_complete(
        self, semantic_id: str, capability_id: str
    ) -> bool:
        router = self._router
        contract = self._local_read.profile.fields.get(semantic_id)
        return (
            contract is not None
            and contract.value_type == "boolean"
            and router is not None
            and router.capability_authorized(
                self._metadata.device_id, capability_id
            )
            and router.value_authorized(
                self._metadata.device_id, capability_id, "false"
            )
            and router.value_authorized(
                self._metadata.device_id, capability_id, "true"
            )
        )

    @property
    def available(self) -> bool:
        """Stay available while powered off; require only live Local authorities."""
        router = self._router
        return (
            self._domain is not None
            and self._local_read.shadow_healthy
            and router is not None
            and router.control_target_available(self._metadata.device_id)
            and router.capability_authorized(
                self._metadata.device_id, CLIMATE_TUPLE_CAPABILITY
            )
            and router.capability_authorized(
                self._metadata.device_id, CLIMATE_POWER_ON_CAPABILITY
            )
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._remove_local_read_listener = self._local_read.async_add_listener(
            self._handle_local_read_update
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_local_read_listener is not None:
            self._remove_local_read_listener()
            self._remove_local_read_listener = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_local_read_update(self) -> None:
        self._remember_current_target()
        self.async_write_ha_state()

    def _field(self, semantic_id: str) -> object | None:
        if not self._local_read.semantic_field_available(semantic_id):
            return None
        return self._local_read.field_value(semantic_id)

    def _report_unreviewed_local_value(self, semantic_id: str) -> None:
        if semantic_id in self._reported_unreviewed_local_values:
            return
        self._reported_unreviewed_local_values.add(semantic_id)
        _LOGGER.warning(
            "Unsupported Rethink Local value for semantic %s; no state was guessed",
            semantic_id,
        )

    def _mode(self) -> str | None:
        value = self._field("operation.mode")
        if isinstance(value, str) and value in LOCAL_MODE_TO_JOBMODE:
            return value
        if value is not None:
            self._report_unreviewed_local_value("operation.mode")
        return None

    def _fan(self) -> str | None:
        value = self._field("fan.mode")
        if isinstance(value, str) and value in LOCAL_NATIVE_FAN_TO_HA:
            return value
        if value is not None:
            self._report_unreviewed_local_value("fan.mode")
        return None

    def _number(self, semantic_id: str) -> float | None:
        value = self._field(semantic_id)
        if type(value) in (int, float) and math.isfinite(float(value)):
            return float(value)
        if value is not None:
            self._report_unreviewed_local_value(semantic_id)
        return None

    def _remember_current_target(self) -> None:
        if not self._local_read.shadow_healthy:
            self._last_targets_by_mode.clear()
            self._last_comfort_preference = None
            return
        mode = self._mode()
        fan = self._fan()
        if fan == "power":
            return
        if mode == "auto":
            preference = self._number("comfort.preference_step")
            domain = self._domain
            if (
                preference is not None
                and preference.is_integer()
                and domain is not None
                and fan is not None
                and domain.render_comfort(mode, fan, int(preference)) is not None
            ):
                self._last_comfort_preference = int(preference)
            return
        target = self._number("temperature.target_c")
        domain = self._domain
        if (
            mode is None
            or target is None
            or domain is None
            or domain.target_range(mode) is None
        ):
            return
        # Reuse the public authority's numeric grid without inventing a second
        # range in HA.  A normal fan is sufficient to exercise every numeric
        # component; interlocks concern POWER only.
        normal_fan = next((fan for fan in domain.fans if fan != "power"), None)
        if normal_fan is not None and domain.render(mode, normal_fan, target) is not None:
            self._last_targets_by_mode[mode] = target

    def _retained_target(self, mode: str | None) -> float | None:
        return None if mode is None else self._last_targets_by_mode.get(mode)

    def _retained_comfort_preference(self, mode: str | None) -> int | None:
        return self._last_comfort_preference if mode == "auto" else None

    def _display_target_range(self):
        domain = self._domain
        if domain is None:
            return None
        return (
            domain.target_range(self._mode() or "")
            or domain.target_range("cool")
            or next(iter(domain.target_ranges_by_mode.values()), None)
        )

    @property
    def current_temperature(self) -> float | None:
        return self._number("temperature.current_c")

    @property
    def current_humidity(self) -> float | None:
        return self._number("humidity.current_pct")

    @property
    def hvac_mode(self) -> HVACMode | None:
        power = self._field("operation.power_requested")
        if type(power) is not bool:
            return None
        if not power:
            return HVACMode.OFF
        mode = self._mode()
        return None if mode is None else JOBMODE_TO_HVAC.get(
            LOCAL_MODE_TO_JOBMODE[mode]
        )

    @property
    def fan_mode(self) -> str | None:
        fan = self._fan()
        return None if fan is None else LOCAL_NATIVE_FAN_TO_HA[fan]

    @property
    def target_temperature(self) -> float | None:
        # AUTO reuses the carrier for a five-step, unitless comfort preference;
        # its Local control is a select and must never appear here as degrees C.
        # POWER reports a placeholder. Every ordinary mode with an exact range
        # in the bundled Local command authority owns its Celsius target here.
        mode = self._mode()
        fan = self._fan()
        if (
            mode is None
            or self._domain is None
            or self._domain.target_range(mode) is None
            or fan is None
            or fan == "power"
        ):
            return None
        target = self._number("temperature.target_c")
        if target is None or self._domain.render(mode, fan, target) is None:
            if target is not None:
                self._report_unreviewed_local_value("temperature.target_c")
            return None
        return target

    @property
    def min_temp(self) -> float:
        target_range = self._display_target_range()
        return 16.0 if target_range is None else target_range.min_c

    @property
    def max_temp(self) -> float:
        target_range = self._display_target_range()
        return 30.0 if target_range is None else target_range.max_c

    @property
    def target_temperature_step(self) -> float:
        target_range = self._display_target_range()
        return 0.5 if target_range is None else target_range.step_c

    @property
    def supported_features(self) -> ClimateEntityFeature:
        features = (
            ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )
        mode = self._mode()
        if (
            mode is not None
            and self._domain is not None
            and self._domain.target_range(mode) is not None
            and mode not in self._domain.preserve_setpoint_modes
            and self._fan() != "power"
        ):
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        if self._swing_lr or self._swing_ud:
            features |= ClimateEntityFeature.SWING_MODE
        return features

    def _read_swing(self, semantic_id: str) -> bool | None:
        value = self._field(semantic_id)
        return value if type(value) is bool else None

    @property
    def swing_mode(self) -> str | None:
        lr = self._read_swing("swing.horizontal_enabled") if self._swing_lr else False
        ud = self._read_swing("swing.vertical_enabled") if self._swing_ud else False
        if lr is None or ud is None:
            return None
        if lr and ud:
            return SWING_BOTH
        if lr:
            return SWING_HORIZONTAL
        if ud:
            return SWING_VERTICAL
        return SWING_OFF

    async def _send(self, operation) -> None:
        try:
            outcome = await operation
        except LocalCommandPending:
            # A frame may already be applied.  Never retry or reflect an
            # optimistic state; the Local provider is the sole reconciliation
            # source.
            return
        except LocalCommandFailed as err:
            raise HomeAssistantError(
                f"{self._metadata.alias}: Local 명령 응답을 확인할 수 없어요."
            ) from err
        if outcome is None:
            raise HomeAssistantError(
                f"{self._metadata.alias}: Local 명령이 전송 전에 거부됐어요."
            )
        # `unverifiable` also means the frame was sent.  Do not turn that into
        # a retry invitation and do not synthesize state; readback settles it.

    def _require_router(self) -> LocalControlRouter:
        if not self.available or self._router is None:
            raise HomeAssistantError(
                f"{self._metadata.alias}: Local 제어 연결을 확인할 수 없어요."
            )
        return self._router

    async def async_turn_on(self) -> None:
        async with self._command_lock:
            router = self._require_router()
            mode = self._mode()
            await self._send(
                router.async_turn_on(
                    self._metadata.device_id,
                    retained_target_c=self._retained_target(mode),
                    retained_comfort_preference=self._retained_comfort_preference(
                        mode
                    ),
                    cloud_fallback=False,
                )
            )

    async def async_turn_off(self) -> None:
        async with self._command_lock:
            router = self._require_router()
            await self._send(router.async_turn_off(self._metadata.device_id))

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
            return
        local_mode = LOCAL_NATIVE_HVAC_TO_MODE.get(hvac_mode)
        if local_mode is None or self._domain is None or local_mode not in self._domain.modes:
            raise HomeAssistantError(
                f"{self._metadata.alias}: 이 운전 모드는 Local 제어 도메인에 없어요."
            )
        async with self._command_lock:
            router = self._require_router()
            power = self._field("operation.power_requested")
            if type(power) is not bool:
                raise HomeAssistantError(
                    f"{self._metadata.alias}: 현재 Local 전원 상태를 확인할 수 없어요."
                )
            operation = (
                router.async_turn_on(
                    self._metadata.device_id,
                    mode=local_mode,
                    retained_target_c=self._retained_target(local_mode),
                    retained_comfort_preference=self._retained_comfort_preference(
                        local_mode
                    ),
                    cloud_fallback=False,
                )
                if not power
                else router.async_set_climate(
                    self._metadata.device_id,
                    mode=local_mode,
                    retained_target_c=self._retained_target(local_mode),
                    retained_comfort_preference=self._retained_comfort_preference(
                        local_mode
                    ),
                    cloud_fallback=False,
                )
            )
            await self._send(operation)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        target = kwargs.get(ATTR_TEMPERATURE)
        if target is None:
            return
        requested_hvac_mode = kwargs.get(_ATTR_HVAC_MODE)
        local_mode = (
            self._mode()
            if requested_hvac_mode is None
            else LOCAL_NATIVE_HVAC_TO_MODE.get(requested_hvac_mode)
        )
        if (
            local_mode is None
            or self._domain is None
            or local_mode not in self._domain.modes
            or self._domain.target_range(local_mode) is None
            or local_mode in self._domain.preserve_setpoint_modes
            or self._fan() == "power"
        ):
            raise HomeAssistantError(
                f"{self._metadata.alias}: 현재 Local 운전 상태에서는 온도를 설정할 수 없어요."
            )
        async with self._command_lock:
            router = self._require_router()
            power = self._field("operation.power_requested")
            if type(power) is not bool:
                raise HomeAssistantError(
                    f"{self._metadata.alias}: 현재 Local 전원 상태를 확인할 수 없어요."
                )
            operation = (
                router.async_turn_on(
                    self._metadata.device_id,
                    mode=local_mode,
                    target_c=float(target),
                    retained_target_c=self._retained_target(local_mode),
                    retained_comfort_preference=self._retained_comfort_preference(
                        local_mode
                    ),
                    cloud_fallback=False,
                )
                if requested_hvac_mode is not None and not power
                else router.async_set_climate(
                    self._metadata.device_id,
                    mode=(local_mode if requested_hvac_mode is not None else None),
                    target_c=float(target),
                    retained_target_c=self._retained_target(local_mode),
                    retained_comfort_preference=self._retained_comfort_preference(
                        local_mode
                    ),
                    cloud_fallback=False,
                )
            )
            await self._send(
                operation
            )

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        local_fan = HA_TO_LOCAL_NATIVE_FAN.get(fan_mode)
        if self._domain is None or local_fan not in self._domain.fans:
            raise HomeAssistantError(
                f"{self._metadata.alias}: 이 바람 세기는 Local 제어 도메인에 없어요."
            )
        async with self._command_lock:
            router = self._require_router()
            mode = self._mode()
            await self._send(
                router.async_set_climate(
                    self._metadata.device_id,
                    fan=local_fan,
                    retained_target_c=self._retained_target(mode),
                    retained_comfort_preference=self._retained_comfort_preference(
                        mode
                    ),
                    cloud_fallback=False,
                )
            )

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        router = self._require_router()
        failures: list[Exception] = []
        async with self._command_lock:
            for write in swing_writes(
                horizontal=(
                    swing_mode in (SWING_HORIZONTAL, SWING_BOTH)
                    if self._swing_lr
                    else None
                ),
                vertical=(
                    swing_mode in (SWING_VERTICAL, SWING_BOTH)
                    if self._swing_ud
                    else None
                ),
            ):
                try:
                    await self._send(
                        router.async_set_flag(
                            self._metadata.device_id,
                            write.capability,
                            write.enabled,
                        )
                    )
                except Exception as err:  # noqa: BLE001 - attempt the other axis
                    failures.append(err)
        if failures:
            if len(failures) > 1:
                raise failures[0] from failures[1]
            raise failures[0]


class MyLgLocalClimate(TemperaturePresentationMixin, _LocalClimateState):
    """Presentation-only settings wrap the unchanged local Celsius implementation."""


class _PatClimateState(MyLgEntity, ClimateEntity):
    """LG air conditioner."""

    _attr_name = None  # use the device name
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(
        self,
        coordinator: PatDeviceCoordinator,
        local_control: LocalControlRouter | None = None,
        local_read: TlvReadShadowProvider | None = None,
    ) -> None:
        super().__init__(coordinator, "climate")
        # The presence of this provider fixes Local state ownership. Temporary
        # unavailability is never permission to substitute PAT/WideQ state.
        self._local_control = local_control
        self._local_read = local_read
        self._remove_local_read_listener = None
        self._reported_unreviewed_local_values: set[str] = set()
        self._attr_hvac_modes = [
            HVACMode.OFF,
            HVACMode.COOL,
            HVACMode.DRY,
            HVACMode.FAN_ONLY,
            HVACMode.AUTO,
        ]
        self._attr_fan_modes = ["LOW", "MID", "HIGH", "POWER", "AUTO"]
        # A Climate swing surface is both state and control. Under Local
        # ownership, expose a direction only when its read contract exists and
        # both boolean writes are authorized. Read-only direction state remains
        # available through its canonical Local binary sensor.
        if local_read is not None:
            self._swing_lr = self._local_boolean_control_complete(
                "swing.horizontal_enabled", SWING_HORIZONTAL_CAPABILITY
            )
            self._swing_ud = self._local_boolean_control_complete(
                "swing.vertical_enabled", SWING_VERTICAL_CAPABILITY
            )
        else:
            self._swing_lr = coordinator.supports_field(
                "windDirection", "rotateLeftRight"
            )
            self._swing_ud = coordinator.supports_field(
                "windDirection", "rotateUpDown"
            )
        if self._swing_lr or self._swing_ud:
            self._attr_supported_features = (
                self._attr_supported_features | ClimateEntityFeature.SWING_MODE
            )
            modes = [SWING_OFF]
            if self._swing_lr:
                modes.append(SWING_HORIZONTAL)
            if self._swing_ud:
                modes.append(SWING_VERTICAL)
            if self._swing_lr and self._swing_ud:
                modes.append(SWING_BOTH)
            self._attr_swing_modes = modes

    async def async_added_to_hass(self) -> None:
        """Subscribe the existing climate entity to Local read updates too."""
        await super().async_added_to_hass()
        if self._local_read is not None:
            self._remove_local_read_listener = self._local_read.async_add_listener(
                self._handle_local_read_update
            )

    async def async_will_remove_from_hass(self) -> None:
        """Detach the Local listener without owning or closing its provider."""
        if self._remove_local_read_listener is not None:
            self._remove_local_read_listener()
            self._remove_local_read_listener = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_local_read_update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Use provider/session liveness, not one optional composite field."""
        if self._local_read is None:
            return super().available
        return self._local_read.event_available

    def _local_boolean_control_complete(
        self, semantic_id: str, capability_id: str
    ) -> bool:
        if not tlv_read_owner_configured(self._local_read, semantic_id):
            return False
        router = self._local_control
        return (
            router is not None
            and router.value_authorized(
                self.coordinator.device_id, capability_id, "false"
            )
            and router.value_authorized(
                self.coordinator.device_id, capability_id, "true"
            )
        )

    def _local_field(self, semantic_id: str):
        """Resolve one field without treating unavailability as a provider switch."""
        return resolve_tlv_read_owned_field(self._local_read, semantic_id)

    def _report_unreviewed_local_value(self, semantic_id: str) -> None:
        """Log one semantic-only diagnostic without exposing raw payload data."""
        if semantic_id in self._reported_unreviewed_local_values:
            return
        self._reported_unreviewed_local_values.add(semantic_id)
        _LOGGER.warning(
            "Unsupported Rethink Local value for semantic %s; cloud fallback disabled",
            semantic_id,
        )

    def _read_job_mode(self) -> str | None:
        state = self._local_field("operation.mode")
        local = state.value
        if isinstance(local, str):
            job_mode = LOCAL_MODE_TO_JOBMODE.get(local)
            if job_mode is not None:
                return job_mode
        if state.owner_configured:
            if state.available:
                self._report_unreviewed_local_value("operation.mode")
            return None
        value = self._get("airConJobMode", "currentJobMode")
        return value if isinstance(value, str) else None

    def _read_power_requested(self) -> bool | None:
        state = self._local_field("operation.power_requested")
        local = state.value
        if type(local) is bool:
            return local
        if state.owner_configured:
            if state.available:
                self._report_unreviewed_local_value("operation.power_requested")
            return None
        return self._get("operation", "airConOperationMode") == POWER_ON

    def _read_number(self, semantic_id: str, *pat_path: str) -> float | None:
        state = self._local_field(semantic_id)
        local = state.value
        if isinstance(local, (int, float)) and not isinstance(local, bool):
            return local
        if state.owner_configured:
            if state.available:
                self._report_unreviewed_local_value(semantic_id)
            return None
        fallback = self._get(*pat_path)
        return (
            fallback
            if isinstance(fallback, (int, float)) and not isinstance(fallback, bool)
            else None
        )

    def _read_swing(self, semantic_id: str, pat_field: str) -> bool | None:
        state = self._local_field(semantic_id)
        local = state.value
        if type(local) is bool:
            return local
        if state.owner_configured:
            if state.available:
                self._report_unreviewed_local_value(semantic_id)
            return None
        return bool(self._get("windDirection", pat_field))

    # --- read ---
    @property
    def current_temperature(self) -> float | None:
        return self._read_number(
            "temperature.current_c", "temperature", "currentTemperature"
        )

    @property
    def target_temperature(self) -> float | None:
        # These wall-mounted units expose neither a user-selectable temperature
        # nor a humidity target in DRY.  Their generic targetTemperature value
        # is only the last setpoint retained from another mode, so do not expose
        # it as an active DRY/FAN target in Home Assistant.
        job_mode = self._read_job_mode()
        if job_mode in JOBMODES_WITHOUT_TARGET_TEMPERATURE:
            return None
        if job_mode != "COOL":
            # This legacy hybrid class has no pinned model-specific composite
            # domain, so it cannot validate AUTO's numeric grid. Production
            # Local owners use MyLgLocalClimate instead; if this compatibility
            # path is injected directly, it stays source-pure and exposes no
            # target rather than substituting PAT or guessing a range.
            if self._local_read is not None:
                return None
            fallback = self._get("temperature", "targetTemperature")
            return (
                fallback
                if isinstance(fallback, (int, float))
                and not isinstance(fallback, bool)
                else None
            )
        return self._read_number(
            "temperature.target_c", "temperature", "targetTemperature"
        )

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Expose target-temperature control only in modes that support it."""
        features = (
            ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )
        job_mode = self._read_job_mode()
        if job_mode is not None and job_mode not in JOBMODES_WITHOUT_TARGET_TEMPERATURE:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        if self._swing_lr or self._swing_ud:
            features |= ClimateEntityFeature.SWING_MODE
        return features

    @property
    def min_temp(self) -> float:
        if self._read_job_mode() == "AUTO":
            return 18
        return self._get("temperature", "minTargetTemperature", default=16)

    @property
    def max_temp(self) -> float:
        return self._get("temperature", "maxTargetTemperature", default=30)

    @property
    def current_humidity(self) -> float | None:
        return self._read_number(
            "humidity.current_pct", "airQualitySensor", "humidity"
        )

    @property
    def hvac_mode(self) -> HVACMode | None:
        power_requested = self._read_power_requested()
        if power_requested is None:
            return None
        if not power_requested:
            return HVACMode.OFF
        job = self._read_job_mode()
        return JOBMODE_TO_HVAC.get(job)

    @property
    def fan_mode(self) -> str | None:
        state = self._local_field("fan.mode")
        local = state.value
        if isinstance(local, str):
            fan_mode = LOCAL_FAN_TO_HA.get(local)
            if fan_mode is not None:
                return fan_mode
        if state.owner_configured:
            if state.available:
                self._report_unreviewed_local_value("fan.mode")
            return None
        fallback = self._get("airFlow", "windStrength")
        return fallback if fallback in self._attr_fan_modes else None

    @property
    def swing_mode(self) -> str | None:
        lr = self._swing_lr and self._read_swing(
            "swing.horizontal_enabled", "rotateLeftRight"
        )
        ud = self._swing_ud and self._read_swing(
            "swing.vertical_enabled", "rotateUpDown"
        )
        if lr is None or ud is None:
            return None
        if lr and ud:
            return SWING_BOTH
        if lr:
            return SWING_HORIZONTAL
        if ud:
            return SWING_VERTICAL
        return SWING_OFF

    # --- write ---
    @staticmethod
    def _cloud_write_restates_tuple(
        payload: dict[str, Any],
        *,
        power: bool | None,
        flag: tuple[str, bool] | None,
    ) -> bool:
        """Whether this cloud write makes retained mode/fan/setpoint unsafe to reuse."""
        # Swing is independent, and power-off preserves the settings the appliance will use next.
        # Neither should discard an appliance-confirmed tuple merely because its local codec was
        # unavailable. Power-on is itself a tuple restatement on the wire, while mode/fan/target
        # payloads include values with no local spelling (AUTO/POWER) and therefore cannot be
        # recognized from the local keyword arguments alone.
        if flag is not None or power is False:
            return False
        if power is True:
            return True
        if any(key in payload for key in ("airConJobMode", "airFlow", "temperature")):
            return True
        operation = payload.get("operation")
        return isinstance(operation, dict) and operation.get(
            "airConOperationMode"
        ) == POWER_ON

    async def _control(
        self,
        payload: dict[str, Any],
        *,
        mode: str | None = None,
        fan: str | None = None,
        target_c: float | None = None,
        power: bool | None = None,
        flag: tuple[str, bool] | None = None,
    ) -> bool:
        """Send it, over the local bridge when that can serve this exact request.

        The keyword arguments say the same thing as `payload` in the vocabulary the bridge
        speaks. Both are stated here because this is the one place that knows both; the local
        path can only send frames observed on the wire, so most requests still go to the cloud
        and the caller cannot tell which did without asking.

        They are named rather than taken as `**kwargs` so that a misspelling is a TypeError
        here instead of a write that silently stops using the local path.

        Returns whether the requested state may now be shown. False means the write went out and
        nothing could confirm it, so a caller with its own follow-up `handle_mqtt_status` - the
        setpoint's normalizing update is the one - must skip that too, or the state this declined
        to show arrives by the other door.
        """
        try:
            outcome = await self._sent_locally(
                mode=mode, fan=fan, target_c=target_c, power=power, flag=flag
            )
        except LocalCommandPending:
            # Something may already be on the wire. Report no optimistic state
            # and let the next Local readback settle it; never invite a retry
            # by issuing or suggesting a cloud copy.
            return False
        if outcome is None:
            if self._local_read is not None:
                # None is a guaranteed pre-wire refusal. Local ownership makes
                # that a visible error, never permission to issue a cloud copy.
                raise HomeAssistantError(
                    f"{self.coordinator.alias}: 이 요청은 검증된 Local 명령 범위에 없어서 전송하지 않았어요."
                )
            # A cloud tuple write becomes authoritative, but API acknowledgement is not an
            # appliance report. Fence later local composition before dispatch, until every tuple
            # input has a newer appliance observation. Independent swing and power-off writes keep
            # the confirmed tuple because they do not change those settings.
            if self._local_control is not None and self._cloud_write_restates_tuple(
                payload, power=power, flag=flag
            ):
                await self._local_control.async_mark_cloud_tuple_dispatch(
                    self.coordinator.device_id
                )
            await self.coordinator.async_control(payload)
        if not reflects_the_appliance(outcome):
            # The frame went out and nothing that arrives could confirm or deny it. Returning False
            # rather than updating anything: a caller with a second, normalizing update must skip
            # that one too, or the state this refused to show arrives by the other door.
            return False
        # optimistic: reflect immediately; MQTT push confirms shortly after. The appliance's own
        # reading came back with a confirmed local write, but in the bridge's vocabulary rather
        # than LG's, and the shadow that speaks it already carries the same reading.
        self.coordinator.handle_mqtt_status(payload)
        return True

    async def _sent_locally(
        self,
        *,
        mode: str | None,
        fan: str | None,
        target_c: float | None,
        power: bool | None,
        flag: tuple[str, bool] | None,
    ) -> LocalCommandResult | None:
        """The bridge's answer, or None when the local path could not serve this request.

        A local refusal is not an error: the bridge refuses before anything reaches the wire,
        which is what makes going to the cloud afterwards one command rather than two. A frame
        that DID go out and was not accounted for is an error, and is raised - retrying that over
        the cloud would be the second command.
        """
        router = self._local_control
        if router is None:
            return None
        device_id = self.coordinator.device_id
        try:
            if power is True:
                # The power-on frame states mode, fan and setpoint whatever happens, so a caller
                # turning it on IN a mode passes that here rather than following with a second
                # frame saying the same three values.
                return await router.async_turn_on(
                    device_id, mode=mode, cloud_fallback=True
                )
            if power is False:
                return await router.async_turn_off(device_id)
            if flag is not None:
                capability, enabled = flag
                return await router.async_set_flag(device_id, capability, enabled)
            if mode is None and fan is None and target_c is None:
                # This entity has nothing to say in the bridge's vocabulary - a write with no local
                # form, or a value with no mapping. Not the same as the router's own rule, which
                # protects `async_set_climate` from every caller; this is the caller declining to
                # ask, so that a purely cloud write reads as one.
                return None
            return await router.async_set_climate(
                device_id,
                mode=mode,
                fan=fan,
                target_c=target_c,
                cloud_fallback=True,
            )
        except LocalCommandFailed as err:
            raise HomeAssistantError(f"{self.coordinator.alias}: {err}") from err

    async def async_turn_on(self) -> None:
        # Powering on is not a write to the power field: the appliance keeps its settings while
        # off and is turned back on by restating them, which is the frame the bridge composes.
        await self._control({"operation": {"airConOperationMode": POWER_ON}}, power=True)

    async def async_turn_off(self) -> None:
        await self._control({"operation": {"airConOperationMode": POWER_OFF}}, power=False)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
            return
        job = HVAC_TO_JOBMODE.get(hvac_mode)
        local_mode = HVAC_TO_LOCAL_MODE.get(hvac_mode)
        # Turn power on first if needed (control is rejected while POWER_OFF).
        power_requested = self._read_power_requested()
        if power_requested is None:
            raise HomeAssistantError(
                f"{self.coordinator.alias}: 현재 Local 전원 상태를 확인할 수 없어요."
            )
        if not power_requested:
            # Only when the mode has a local form. A power-on frame with no mode stated restates
            # whatever the appliance last reported, so taking this path for AUTO - which has no
            # local form - would turn the unit on in its previous mode and skip the mode write
            # entirely, leaving it cooling while Home Assistant showed AUTO.
            if local_mode is not None and await self._powered_on_locally(job, local_mode):
                return
            await self._control({"operation": {"airConOperationMode": POWER_ON}})
            # The mode now goes the same way the power-on did. The appliance may still be coming
            # up, and the local path would send a tuple it cannot yet obey, then wait out the
            # bridge's twenty-second report window and report a failure for a change the cloud
            # would simply have made.
            local_mode = None
        if job:
            await self._control({"airConJobMode": {"currentJobMode": job}}, mode=local_mode)

    async def _powered_on_locally(self, job: str | None, local_mode: str) -> bool:
        """One local frame that both powers on and states the mode, or nothing.

        LG's API cannot combine them - it rejects a setting write while the unit is off - so this
        exists only for the local path, where powering on IS a restatement of mode, fan and
        setpoint and the requested mode simply goes in it.
        """
        try:
            outcome = await self._sent_locally(
                mode=local_mode, fan=None, target_c=None, power=True, flag=None
            )
        except LocalCommandPending:
            # The combined power/mode frame may already be applied. Treat the
            # request as served without optimistic state so the caller cannot
            # send a second command.
            return True
        if outcome is None:
            if self._local_read is not None:
                raise HomeAssistantError(
                    f"{self.coordinator.alias}: 이 전원 켜기 조합은 검증된 Local 명령 범위에 없어서 전송하지 않았어요."
                )
            return False
        if outcome.confirmed:
            # One state write, not two: two would show the unit on in its previous mode first.
            payload: dict[str, Any] = {"operation": {"airConOperationMode": POWER_ON}}
            if job:
                payload["airConJobMode"] = {"currentJobMode": job}
            self.coordinator.handle_mqtt_status(payload)
        return True

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        job_mode = self._read_job_mode()
        field = TEMPERATURE_FIELD_BY_JOBMODE.get(job_mode)
        if field is None:
            if job_mode == "AIR_DRY":
                raise HomeAssistantError(
                    f"{self.coordinator.alias}: 제습 모드에서는 목표 온도나 "
                    "목표 습도를 직접 설정할 수 없어요."
                )
            if job_mode == "FAN":
                raise HomeAssistantError(
                    f"{self.coordinator.alias}: 송풍 모드에서는 온도를 설정할 수 없어요."
                )
            raise HomeAssistantError(
                f"{self.coordinator.alias}: 현재 운전 모드에서는 온도를 설정할 수 없어요."
            )
        reflected = await self._control({"temperature": {field: temp}}, target_c=temp)
        # The climate entity reads the normalized targetTemperature field.
        # Reflect it immediately while waiting for the next MQTT status push - unless the write
        # went out with nothing able to confirm it, in which case showing the new setpoint here
        # would be the same guess `_control` just declined to make.
        if reflected and field != "targetTemperature":
            self.coordinator.handle_mqtt_status(
                {"temperature": {"targetTemperature": temp}}
            )

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        await self._control(
            {"airFlow": {"windStrength": fan_mode}},
            fan=WIND_STRENGTH_TO_LOCAL_FAN.get(fan_mode),
        )

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        # LG applies only one windDirection field per command — sending both
        # rotateLeftRight and rotateUpDown in a single payload makes the unit
        # apply just one (or neither). Issue them separately, like the SDK's
        # set_wind_rotate_left_right / set_wind_rotate_up_down.
        # Both are attempted even if the first fails, and by anything - not only the errors this
        # entity raises. They are two halves of one setting, and an error on the first used to
        # leave the second unsent by either path: the appliance half-set, with nothing to say
        # which half.
        failures: list[Exception] = []
        for write in swing_writes(
            horizontal=swing_mode in (SWING_HORIZONTAL, SWING_BOTH) if self._swing_lr else None,
            vertical=swing_mode in (SWING_VERTICAL, SWING_BOTH) if self._swing_ud else None,
        ):
            try:
                await self._control(
                    {"windDirection": {write.field: write.enabled}},
                    flag=(write.capability, write.enabled),
                )
            except Exception as err:  # noqa: BLE001 - the other half must still be attempted
                failures.append(err)
        if not failures:
            return
        if len(failures) > 1:
            # Chained only when there IS a second: `raise ... from None` would strip the first
            # failure's own cause, which is what says what the bridge actually answered.
            raise failures[0] from failures[1]
        raise failures[0]


class MyLgClimate(TemperaturePresentationMixin, _PatClimateState):
    """Compatibility climate with the same opt-in HA display preference."""
