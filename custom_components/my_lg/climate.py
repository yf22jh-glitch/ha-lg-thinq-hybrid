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
from homeassistant.exceptions import HomeAssistantError
from .compat import AddConfigEntryEntitiesCallback

from . import MyLgConfigEntry
from .const import DEVICE_TYPE_AIR_CONDITIONER
from .coordinator import PatDeviceCoordinator
from .entity import MyLgEntity
from .local_command import (
    HVAC_TO_LOCAL_MODE,
    WIND_STRENGTH_TO_LOCAL_FAN,
    LocalCommandFailed,
    LocalCommandResult,
    reflects_the_appliance,
    swing_writes,
)
from .local_control_router import LocalControlRouter

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
    """Set up climate entities for air conditioners."""
    entities = [
        MyLgClimate(coordinator, entry.runtime_data.local_control)
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

    def __init__(
        self,
        coordinator: PatDeviceCoordinator,
        local_control: LocalControlRouter | None = None,
    ) -> None:
        super().__init__(coordinator, "climate")
        # None whenever the local bridge cannot be addressed at all - no shadow for this
        # appliance, or no WideQ pairing to name it by. Every write then reads exactly as it
        # did before this path existed.
        self._local_control = local_control
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
        # These wall-mounted units expose neither a user-selectable temperature
        # nor a humidity target in DRY.  Their generic targetTemperature value
        # is only the last setpoint retained from another mode, so do not expose
        # it as an active DRY/FAN target in Home Assistant.
        if (
            self._get("airConJobMode", "currentJobMode")
            in JOBMODES_WITHOUT_TARGET_TEMPERATURE
        ):
            return None
        return self._get("temperature", "targetTemperature")

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Expose target-temperature control only in modes that support it."""
        features = (
            ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )
        if (
            self._get("airConJobMode", "currentJobMode")
            not in JOBMODES_WITHOUT_TARGET_TEMPERATURE
        ):
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        if self._swing_lr or self._swing_ud:
            features |= ClimateEntityFeature.SWING_MODE
        return features

    @property
    def min_temp(self) -> float:
        if self._get("airConJobMode", "currentJobMode") == "AUTO":
            return 18
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
        outcome = await self._sent_locally(mode=mode, fan=fan, target_c=target_c, power=power, flag=flag)
        if outcome is None:
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
                return await router.async_turn_on(device_id, mode=mode)
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
            return await router.async_set_climate(device_id, mode=mode, fan=fan, target_c=target_c)
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
        if self._get("operation", "airConOperationMode") != POWER_ON:
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
        outcome = await self._sent_locally(
            mode=local_mode, fan=None, target_c=None, power=True, flag=None
        )
        if outcome is None:
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
        job_mode = self._get("airConJobMode", "currentJobMode")
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
