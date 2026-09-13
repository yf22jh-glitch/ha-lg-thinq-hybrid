"""Local-first dispatch for native scalar cards; protocol stays in the bridge."""

from homeassistant.exceptions import HomeAssistantError

from .local_control_router import LocalControlRouter


async def async_native_local_control(
    router: LocalControlRouter | None,
    device_id: str,
    capability: str | None,
    value: str | None,
) -> bool:
    """False means no local write was sent. Never retry an uncertain write in cloud."""
    if router is None or capability is None or value is None:
        return False
    # The router checks this exact binding's values and current target. It only
    # returns None for pre-send unavailability; delivery errors propagate.
    outcome = await router.async_set_value(device_id, capability, value)
    if outcome is None:
        return False
    if not outcome.confirmed:
        raise HomeAssistantError('Local command was not confirmed; cloud retry suppressed')
    return True
