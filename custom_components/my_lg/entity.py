"""Base entity for my_lg."""

from __future__ import annotations

from typing import Any

from homeassistant.exceptions import HomeAssistantError

try:
    # HA 2023.8+ location; used by official integrations.
    from homeassistant.helpers.device_registry import DeviceInfo
except ImportError:  # pragma: no cover - fallback for older/newer reorgs
    from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .control_router import (
    ControlValidationError,
    build_wideq_request,
    control_readback_verified,
    control_uses_experimental_values,
    control_verification_schedule,
    pat_priority_requested,
    prepare_control_verification,
    remote_control_authorized,
)
from .coordinator import PatDeviceCoordinator
from .coordinator_wideq import WideqCoordinator
from .wideq_control import verified_wideq_field_spec


class MyLgEntity(CoordinatorEntity[PatDeviceCoordinator]):
    """Common base tying an entity to one device coordinator."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PatDeviceCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{coordinator.device_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.device_id)},
            name=coordinator.alias,
            manufacturer="LG",
            model=coordinator.model or coordinator.device_type,
        )

    @property
    def available(self) -> bool:
        # PAT REST is only an hourly fallback. A transient fallback failure must
        # not invalidate a valid state previously received through MQTT.
        return bool(self.coordinator.data)

    def _get(self, *path: str, default: Any = None) -> Any:
        return self.coordinator.get(*path, default=default)


class MyLgWideqEntity(CoordinatorEntity[WideqCoordinator]):
    """Entity backed by the wideq coordinator for a wideq-only field.

    State and control go through wideq (keyed by device alias), but the entity
    attaches to the *same* device as the PAT entities via the shared PAT device
    id — the user sees one device, with both PAT and wideq controls on it.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        wideq_coordinator: WideqCoordinator,
        pat_coordinator: PatDeviceCoordinator,
        key: str,
    ) -> None:
        super().__init__(wideq_coordinator)
        self._device_id = pat_coordinator.device_id
        self._pat_coordinator = pat_coordinator
        self._attr_unique_id = f"{pat_coordinator.device_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, pat_coordinator.device_id)},
            name=pat_coordinator.alias,
            manufacturer="LG",
            model=pat_coordinator.model or pat_coordinator.device_type,
        )

    @property
    def _snapshot(self) -> dict[str, Any]:
        return self.coordinator.snapshot_for(self._device_id)

    @property
    def available(self) -> bool:
        # Keep the last good snapshot available during LG maintenance. Shared
        # diagnostic attributes explicitly mark it stale until a probe succeeds.
        return bool(self._snapshot)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.coordinator.diagnostic_attributes

    def _wideq_field_write_available(
        self,
        control_name: str,
        data_key: str,
        use_dataset: bool,
        *,
        shape: str | None = None,
        allow_hazardous: bool = False,
        allow_experimental: bool = False,
    ) -> bool:
        """Return whether an exact verified field write is currently eligible."""
        spec = verified_wideq_field_spec(
            self._pat_coordinator.model,
            control_name,
            data_key,
            use_dataset,
            shape=shape,
        )
        if spec is None or self.coordinator.circuit_open or not self._snapshot:
            return False
        risk = spec.get("risk", "low")
        if risk == "hazardous" and not allow_hazardous:
            return False
        if risk == "experimental" and not allow_experimental:
            return False
        return risk not in {"operation", "hazardous"} or remote_control_authorized(
            self._pat_coordinator.model,
            pat_data=self._pat_coordinator.data,
            wideq_snapshot=self._snapshot,
        )

    async def _wideq_set(
        self,
        control_name: str,
        data_key: str,
        value: Any,
        use_dataset: bool,
        *,
        shape: str | None = None,
        allow_hazardous: bool = False,
        allow_experimental: bool = False,
    ) -> None:
        """Send one exact-model field write and prove its fresh state echo."""
        spec = verified_wideq_field_spec(
            self._pat_coordinator.model,
            control_name,
            data_key,
            use_dataset,
            shape=shape,
        )
        if spec is None:
            raise HomeAssistantError(
                f"{self._pat_coordinator.alias}: this WideQ field has no exact "
                "acknowledgement/state verification contract"
            )
        values = {data_key: value}
        risk = spec.get("risk", "low")
        if risk == "hazardous" and not allow_hazardous:
            raise HomeAssistantError(
                f"{self._pat_coordinator.alias}: hazardous WideQ controls are locked"
            )
        if (
            risk == "experimental"
            or control_uses_experimental_values(spec, values)
        ) and not allow_experimental:
            raise HomeAssistantError(
                f"{self._pat_coordinator.alias}: experimental WideQ controls are locked"
            )
        duplicate = pat_priority_requested(spec, values)
        if duplicate:
            raise HomeAssistantError(
                f"{self._pat_coordinator.alias}: this field is authoritative through PAT"
            )
        try:
            readback_delays = control_verification_schedule(spec, values)
        except ControlValidationError as err:
            raise HomeAssistantError(
                f"{self._pat_coordinator.alias}: {err}"
            ) from err

        def request_factory() -> dict[str, Any]:
            snapshot = self.coordinator.snapshot_for(self._device_id)
            if risk in {"operation", "hazardous"} and not remote_control_authorized(
                self._pat_coordinator.model,
                pat_data=self._pat_coordinator.data,
                wideq_snapshot=snapshot,
            ):
                raise HomeAssistantError(
                    f"{self._pat_coordinator.alias}: enable remote control on the "
                    "appliance first"
                )
            try:
                prepare_control_verification(spec, values, snapshot)
                return build_wideq_request(
                    spec,
                    command=None,
                    values=values,
                    snapshot=snapshot,
                )
            except ControlValidationError as err:
                raise HomeAssistantError(
                    f"{self._pat_coordinator.alias}: {err}"
                ) from err

        await self.coordinator.async_control_and_verify(
            self._device_id,
            spec["ctrl_key"],
            request_factory=request_factory,
            readback_delays=readback_delays,
            verifier=lambda fresh: control_readback_verified(
                spec, values, fresh
            ),
        )
