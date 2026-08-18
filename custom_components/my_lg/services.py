"""Validated composite services for audited WideQ model controls."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv

from .const import (
    DOMAIN,
    OPT_ALLOW_EXPERIMENTAL_CONTROLS,
    OPT_ALLOW_HAZARDOUS_CONTROLS,
    SERVICE_WIDEQ_COMMAND,
)
from .control_router import (
    ControlValidationError,
    build_wideq_request,
    control_readback_verified,
    control_verification_schedule,
    control_uses_experimental_values,
    pat_priority_requested,
    prepare_control_verification,
    remote_control_authorized,
)
from .feature_catalog import get_wideq_control


_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("control"): cv.string,
        vol.Optional("subdevice"): cv.string,
        vol.Optional("command"): cv.string,
        vol.Optional("data", default={}): dict,
    }
)

_PAT_PRIORITY_CONTROLS = {
    ("ST_R_ETH01Y_", None, "offPower"),
    ("ST_R_ETH01Y_", None, "onPower"),
    ("WTL_KPK_BDH_KR_01", "dryer", "WMOff"),
    ("WTL_KPK_BDH_KR_01", "dryer", "WMStop"),
    ("WTL_KPK_BDH_KR_01", "washer", "WMOff"),
    ("WTL_KPK_BDH_KR_01", "washer", "WMStop"),
}


def _find_runtime(hass: HomeAssistant, requested: str):
    for entry in hass.config_entries.async_entries(DOMAIN):
        runtime = getattr(entry, "runtime_data", None)
        if runtime is None:
            continue
        for coordinator in runtime.coordinators.values():
            if requested in {coordinator.device_id, coordinator.alias}:
                return entry, runtime, coordinator
    return None, None, None


async def _handle_wideq_command(hass: HomeAssistant, call: ServiceCall) -> None:
    requested = call.data["device_id"]
    entry, runtime, coordinator = _find_runtime(hass, requested)
    if coordinator is None or runtime is None or entry is None:
        raise HomeAssistantError(f"my_lg device not found: {requested}")
    wideq = runtime.wideq_coordinator
    if wideq is None:
        raise HomeAssistantError("WideQ credentials are not configured")

    control_name = call.data["control"]
    subdevice = call.data.get("subdevice")
    spec = get_wideq_control(coordinator.model, control_name, subdevice)
    if spec is None:
        target = f"{subdevice}." if subdevice else ""
        raise HomeAssistantError(
            f"{coordinator.alias}: model {coordinator.model} does not advertise "
            f"{target}{control_name}"
        )

    if (coordinator.model, subdevice, control_name) in _PAT_PRIORITY_CONTROLS:
        raise HomeAssistantError(
            f"{coordinator.alias}: this operation is available through PAT; "
            "use the existing my_lg entity"
        )
    claimed = pat_priority_requested(spec, call.data.get("data", {}))
    if claimed:
        raise HomeAssistantError(
            f"{coordinator.alias}: PAT is authoritative for "
            f"{', '.join(sorted(claimed))}"
        )

    risk = spec.get("risk", "low")
    if risk == "hazardous" and not entry.options.get(
        OPT_ALLOW_HAZARDOUS_CONTROLS, False
    ):
        raise HomeAssistantError(
            "Hazardous cooking controls are locked in the integration options"
        )
    if (
        risk == "experimental"
        or control_uses_experimental_values(spec, call.data.get("data", {}))
    ) and not entry.options.get(OPT_ALLOW_EXPERIMENTAL_CONTROLS, False):
        raise HomeAssistantError(
            "Experimental model controls are locked in the integration options"
        )

    values = dict(call.data.get("data", {}))

    def request_factory() -> dict[str, object]:
        try:
            current_snapshot = wideq.snapshot_for(coordinator.device_id)
            if risk in {"operation", "hazardous"} and not remote_control_authorized(
                coordinator.model,
                pat_data=coordinator.data,
                wideq_snapshot=current_snapshot,
            ):
                raise ControlValidationError(
                    "remote control was disabled before the command was sent"
                )
            prepare_control_verification(spec, values, current_snapshot)
            return build_wideq_request(
                spec,
                command=call.data.get("command"),
                values=values,
                # Read under the coordinator's command/I/O locks so composite
                # preservation fields cannot be stale due to another command.
                snapshot=current_snapshot,
            )
        except ControlValidationError as err:
            raise HomeAssistantError(f"{coordinator.alias}: {err}") from err

    # Mutating composite writes are accepted only with an audited model-state
    # transition and bounded post-ACK snapshot verification.
    try:
        readback_delays = control_verification_schedule(spec, values)
    except ControlValidationError as err:
        raise HomeAssistantError(f"{coordinator.alias}: {err}") from err
    await wideq.async_control_and_verify(
        coordinator.device_id,
        spec["ctrl_key"],
        request_factory=request_factory,
        readback_delays=readback_delays,
        verifier=lambda fresh: control_readback_verified(spec, values, fresh),
    )


def async_register_services(hass: HomeAssistant) -> None:
    """Register the reload-safe domain service exactly once."""
    if hass.services.has_service(DOMAIN, SERVICE_WIDEQ_COMMAND):
        return

    async def handle(call: ServiceCall) -> None:
        await _handle_wideq_command(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_WIDEQ_COMMAND,
        handle,
        schema=_SERVICE_SCHEMA,
    )
