"""Local-first dispatch for native scalar cards; protocol stays in the bridge."""

from homeassistant.exceptions import HomeAssistantError

from .local_control_router import LocalControlRouter


def native_local_available(router: LocalControlRouter | None, device_id: str, *capabilities: str | None) -> bool:
    """A missing PAT snapshot must not hide a live, authorized local owner."""
    return router is not None and any(
        capability is not None and router.capability_authorized(device_id, capability)
        and router.feature_condition_available(device_id, capability)
        for capability in capabilities
    ) and router.control_target_available(device_id)


class LocalConditionEntityMixin:
    """Refresh native cards from the same local states used by DB conditions."""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        router = self._local_control
        self._remove_condition_listener = None if router is None else router.subscribe_condition_state(
            self.coordinator.device_id, self.async_write_ha_state
        )

    async def async_will_remove_from_hass(self) -> None:
        if remove := getattr(self, "_remove_condition_listener", None):
            remove()
            self._remove_condition_listener = None
        await super().async_will_remove_from_hass()


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
