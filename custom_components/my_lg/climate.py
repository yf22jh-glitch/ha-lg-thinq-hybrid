"""Air conditioner climate entity (state via PAT/MQTT, control via PAT)."""

from __future__ import annotations

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
from homeassistant.core import HomeAssistant
from .compat import AddConfigEntryEntitiesCallback

from . import MyLgConfigEntry
from .const import DEVICE_TYPE_AIR_CONDITIONER
from .coordinator import PatDeviceCoordinator
from .entity import MyLgEntity

# ThinQ jobMode <-> HA HVACMode
JOBMODE_TO_HVAC = {
    "COOL": HVACMode.COOL,
    "AIR_DRY": HVACMode.DRY,
    "FAN": HVACMode.FAN_ONLY,
    "AUTO": HVACMode.AUTO,
}
HVAC_TO_JOBMODE = {v: k for k, v in JOBMODE_TO_HVAC.items()}

POWER_ON = "POWER_ON"
POWER_OFF = "POWER_OFF"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up climate entities for air conditioners."""
    entities = [
        MyLgClimate(coordinator)
        for coordinator in entry.runtime_data.coordinators.values()
        if coordinator.device_type == DEVICE_TYPE_AIR_CONDITIONER
    ]
    async_add_entities(entities)


class MyLgClimate(MyLgEntity, ClimateEntity):
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

    def __init__(self, coordinator: PatDeviceCoordinator) -> None:
        super().__init__(coordinator, "climate")
        self._attr_hvac_modes = [
            HVACMode.OFF,
            HVACMode.COOL,
            HVACMode.DRY,
            HVACMode.FAN_ONLY,
            HVACMode.AUTO,
        ]
        self._attr_fan_modes = ["LOW", "MID", "HIGH", "POWER", "AUTO"]
        # Swing: horizontal (rotateLeftRight) / vertical (rotateUpDown), each
        # exposed only if the device profile advertises the field.
        self._swing_lr = coordinator.supports_field("windDirection", "rotateLeftRight")
        self._swing_ud = coordinator.supports_field("windDirection", "rotateUpDown")
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

    # --- read ---
    @property
    def current_temperature(self) -> float | None:
        return self._get("temperature", "currentTemperature")

    @property
    def target_temperature(self) -> float | None:
        return self._get("temperature", "targetTemperature")

    @property
    def min_temp(self) -> float:
        return self._get("temperature", "minTargetTemperature", default=16)

    @property
    def max_temp(self) -> float:
        return self._get("temperature", "maxTargetTemperature", default=30)

    @property
    def current_humidity(self) -> float | None:
        return self._get("airQualitySensor", "humidity")

    @property
    def hvac_mode(self) -> HVACMode | None:
        if self._get("operation", "airConOperationMode") != POWER_ON:
            return HVACMode.OFF
        job = self._get("airConJobMode", "currentJobMode")
        return JOBMODE_TO_HVAC.get(job)

    @property
    def fan_mode(self) -> str | None:
        return self._get("airFlow", "windStrength")

    @property
    def swing_mode(self) -> str | None:
        lr = self._swing_lr and bool(self._get("windDirection", "rotateLeftRight"))
        ud = self._swing_ud and bool(self._get("windDirection", "rotateUpDown"))
        if lr and ud:
            return SWING_BOTH
        if lr:
            return SWING_HORIZONTAL
        if ud:
            return SWING_VERTICAL
        return SWING_OFF

    # --- write ---
    async def _control(self, payload: dict[str, Any]) -> None:
        await self.coordinator.async_control(payload)
        # optimistic: reflect immediately; MQTT push confirms shortly after.
        self.coordinator.handle_mqtt_status(payload)

    async def async_turn_on(self) -> None:
        await self._control({"operation": {"airConOperationMode": POWER_ON}})

    async def async_turn_off(self) -> None:
        await self._control({"operation": {"airConOperationMode": POWER_OFF}})

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
            return
        # Turn power on first if needed (control is rejected while POWER_OFF).
        if self._get("operation", "airConOperationMode") != POWER_ON:
            await self._control({"operation": {"airConOperationMode": POWER_ON}})
        job = HVAC_TO_JOBMODE.get(hvac_mode)
        if job:
            await self._control({"airConJobMode": {"currentJobMode": job}})

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        await self._control({"temperature": {"targetTemperature": temp}})

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        await self._control({"airFlow": {"windStrength": fan_mode}})

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        # LG applies only one windDirection field per command — sending both
        # rotateLeftRight and rotateUpDown in a single payload makes the unit
        # apply just one (or neither). Issue them separately, like the SDK's
        # set_wind_rotate_left_right / set_wind_rotate_up_down.
        if self._swing_lr:
            await self._control(
                {"windDirection": {"rotateLeftRight": swing_mode in (SWING_HORIZONTAL, SWING_BOTH)}}
            )
        if self._swing_ud:
            await self._control(
                {"windDirection": {"rotateUpDown": swing_mode in (SWING_VERTICAL, SWING_BOTH)}}
            )
