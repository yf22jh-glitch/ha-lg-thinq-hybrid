"""Validated composite services for audited WideQ model controls."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv

from .const import (
    DOMAIN,
    OPT_ALLOW_EXPERIMENTAL_CONTROLS,
    OPT_ALLOW_HAZARDOUS_CONTROLS,
    SERVICE_LOCAL_READ_CONSUMER_TRANSITION,
    SERVICE_WIDEQ_COMMAND,
)
from .control_router import (
    ControlValidationError,
    build_wideq_request,
    control_uses_experimental_values,
    pat_priority_requested,
    remote_control_enabled,
)
from .feature_catalog import get_wideq_control
from .local_read_provider import (
    TLV_READ_CONSUMER_MUTATIONS,
    TlvReadConsumerStatePushError,
    tlv_read_consumer_binding_state_json,
)


_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("control"): cv.string,
        vol.Optional("subdevice"): cv.string,
        vol.Optional("command"): cv.string,
        vol.Optional("data", default={}): dict,
    }
)

_LOCAL_READ_CONSUMER_OPERATIONS = ("inspect", *TLV_READ_CONSUMER_MUTATIONS)
_LOCAL_READ_CONSUMER_TRANSITION_SCHEMA = vol.Schema(
    {
        vol.Required("operation"): vol.In(_LOCAL_READ_CONSUMER_OPERATIONS),
        vol.Required("binding_id"): cv.string,
        vol.Optional("binding_generation"): vol.All(
            vol.Coerce(int), vol.Range(min=1)
        ),
        vol.Optional("expected_current_record_sha256"): vol.Any(
            None, vol.Match(r"^[a-f0-9]{64}$")
        ),
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

    snapshot = wideq.snapshot_for(coordinator.device_id)
    if risk in {"operation", "hazardous"} and not (
        remote_control_enabled(coordinator.data)
        or remote_control_enabled(snapshot)
    ):
        raise HomeAssistantError(
            f"{coordinator.alias}: enable remote control on the appliance first"
        )

    def request_factory() -> dict[str, object]:
        try:
            return build_wideq_request(
                spec,
                command=call.data.get("command"),
                values=call.data.get("data", {}),
                # Read under the coordinator's command/I/O locks so composite
                # preservation fields cannot be stale due to another command.
                snapshot=wideq.snapshot_for(coordinator.device_id),
            )
        except ControlValidationError as err:
            raise HomeAssistantError(f"{coordinator.alias}: {err}") from err

    # Every shape, including get/actions and ThinQ1, still passes through the
    # shared limiter and open-circuit rejection. No follow-up poll is issued.
    await wideq.async_control(
        coordinator.device_id,
        spec["ctrl_key"],
        request_factory=request_factory,
    )


def _find_local_read_runtime(hass: HomeAssistant, binding_id: str):
    matches = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        runtime = getattr(entry, "runtime_data", None)
        if runtime is None:
            continue
        if (
            binding_id in runtime.local_read_consumer_authorities
            or binding_id in runtime.local_read_consumer_persisted_states
        ):
            matches.append(runtime)
    if len(matches) != 1:
        raise HomeAssistantError(
            "my_lg local-read binding is absent or ambiguous"
        )
    return matches[0]


async def _require_admin_service_user(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    user_id = call.context.user_id
    user = None if user_id is None else await hass.auth.async_get_user(user_id)
    if user is None or not user.is_admin:
        raise HomeAssistantError(
            "my_lg local-read installer transition requires an administrator"
        )


async def _handle_local_read_consumer_transition(
    hass: HomeAssistant, call: ServiceCall
) -> dict[str, object]:
    """Run one admin-only named state transition without device I/O."""
    await _require_admin_service_user(hass, call)
    operation = call.data["operation"]
    binding_id = call.data["binding_id"]
    runtime = _find_local_read_runtime(hass, binding_id)
    current = runtime.local_read_consumer_persisted_states.get(binding_id)
    if operation == "inspect":
        if "binding_generation" in call.data or (
            "expected_current_record_sha256" in call.data
        ):
            raise HomeAssistantError(
                "my_lg local-read inspect accepts no transition values"
            )
        matching_providers = tuple(
            provider
            for provider in runtime.local_read_providers.values()
            if provider.binding_id == binding_id
        )
        if len(matching_providers) > 1:
            raise HomeAssistantError(
                "my_lg local-read binding has duplicate providers"
            )
        live_observation = (
            None
            if not matching_providers
            else matching_providers[0].current_contract_observation()
        )
        return {
            "schema_version": 1,
            "operation": operation,
            "binding_id": binding_id,
            "status": "present" if current is not None else "absent",
            "state": (
                None
                if current is None
                else tlv_read_consumer_binding_state_json(current)
            ),
            # This is convergence evidence only. Null never filters or fails
            # an offline/powered-off binding; the installer keeps both pins.
            "live_observation": (
                None
                if live_observation is None
                else dict(live_observation)
            ),
        }

    try:
        state = await runtime.async_transition_local_read_consumer_state(
            operation=operation,
            binding_id=binding_id,
            binding_generation=call.data.get("binding_generation"),
            expected_current_record_sha256=call.data.get(
                "expected_current_record_sha256"
            ),
        )
    except TlvReadConsumerStatePushError as err:
        # Persistence is already durable. The installer can retry the same
        # idempotent transition or execute its explicit pre-start rollback.
        return {
            "schema_version": 1,
            "operation": operation,
            "binding_id": binding_id,
            "status": "durable-persisted-live-push-pending",
            "state": tlv_read_consumer_binding_state_json(err.state),
        }
    except (TypeError, ValueError, RuntimeError) as err:
        raise HomeAssistantError(
            f"my_lg local-read consumer transition refused: {err}"
        ) from err
    return {
        "schema_version": 1,
        "operation": operation,
        "binding_id": binding_id,
        "status": "applied",
        "state": tlv_read_consumer_binding_state_json(state),
    }


def async_register_services(hass: HomeAssistant) -> None:
    """Register the reload-safe domain service exactly once."""
    if not hass.services.has_service(DOMAIN, SERVICE_WIDEQ_COMMAND):

        async def handle(call: ServiceCall) -> None:
            await _handle_wideq_command(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_WIDEQ_COMMAND,
            handle,
            schema=_SERVICE_SCHEMA,
        )

    if not hass.services.has_service(
        DOMAIN, SERVICE_LOCAL_READ_CONSUMER_TRANSITION
    ):

        async def handle_local_read(
            call: ServiceCall,
        ) -> dict[str, object]:
            return await _handle_local_read_consumer_transition(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_LOCAL_READ_CONSUMER_TRANSITION,
            handle_local_read,
            schema=_LOCAL_READ_CONSUMER_TRANSITION_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
