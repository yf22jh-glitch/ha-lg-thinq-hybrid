"""Binary sensors for fields the PAT API cannot provide (dehumidifier water tank)."""

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MyLgConfigEntry
from .compat import AddConfigEntryEntitiesCallback
from .const import (
    DEVICE_TYPE_DEHUMIDIFIER,
    DEVICE_TYPE_DISH_WASHER,
    DEVICE_TYPE_REFRIGERATOR,
    DOMAIN,
    OPT_LOCAL_READ_DUPLICATE_OVERLAY,
)
from .coordinator import PatDeviceCoordinator
from .coordinator_wideq import WideqCoordinator
from .entity import MyLgEntity
from .local_entity import (
    LocalSemanticEntityMixin,
    TlvReadEntityMixin,
    iter_local_semantic_contracts,
    iter_tlv_read_contracts,
)
from .local_provider import (
    LOCAL_WATER_TANK_FIELD,
    WIDEQ_WATER_TANK_KEY,
    LocalSemanticFieldContract,
    LocalSemanticShadowProvider,
    WaterTankProviderResolver,
)
from .local_read_provider import (
    TlvReadFieldContract,
    TlvReadShadowProvider,
)

# WideQ fallback key: 0 = not full, 1 = full-stop, 2 = full fan-mode.
# PAT has no equivalent level state; only the WATER_IS_FULL edge push.
WATER_TANK_KEY = WIDEQ_WATER_TANK_KEY


@dataclass(frozen=True, kw_only=True)
class MyLgBinaryDescription(BinarySensorEntityDescription):
    """PAT binary sensor with an is_on getter."""

    is_on_fn: Callable[[PatDeviceCoordinator], bool | None]


def _door_flat(c: PatDeviceCoordinator) -> bool | None:
    v = c.get("doorStatus", "doorState")
    return None if v is None else v == "OPEN"


def _door_loc(location: str) -> Callable[[PatDeviceCoordinator], bool | None]:
    def fn(c: PatDeviceCoordinator) -> bool | None:
        v = c.get_location("doorStatus", location, "doorState")
        return None if v is None else v == "OPEN"

    return fn


PAT_BINARY_BY_TYPE: dict[str, tuple[MyLgBinaryDescription, ...]] = {
    DEVICE_TYPE_REFRIGERATOR: (
        MyLgBinaryDescription(
            key="door",
            translation_key="door",
            device_class=BinarySensorDeviceClass.DOOR,
            is_on_fn=_door_loc("MAIN"),
        ),
    ),
    DEVICE_TYPE_DISH_WASHER: (
        MyLgBinaryDescription(
            key="door",
            translation_key="door",
            device_class=BinarySensorDeviceClass.DOOR,
            is_on_fn=_door_flat,
        ),
        MyLgBinaryDescription(
            key="rinse_refill",
            translation_key="rinse_refill",
            device_class=BinarySensorDeviceClass.PROBLEM,
            is_on_fn=lambda c: (
                None
                if (v := c.get("dishWashingStatus", "rinseRefill")) is None
                else bool(v)
            ),
        ),
    ),
}

_PAT_BINARY_SEMANTICS: dict[str, dict[str, str]] = {
    DEVICE_TYPE_DISH_WASHER: {
        "door": "door.open",
        "rinse_refill": "consumable.rinse_aid_refill_required",
    }
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    from .feature_runtime import setup_feature_entities

    setup_feature_entities(entry, "binary_sensor", lambda: _build_entities(entry), async_add_entities)


def _build_entities(entry: MyLgConfigEntry) -> list[BinarySensorEntity]:
    data = entry.runtime_data
    overlay_duplicates = (
        getattr(entry, "options", {}).get(OPT_LOCAL_READ_DUPLICATE_OVERLAY) is True
    )
    entities: list[BinarySensorEntity] = []
    promoted_local_semantics: dict[str, frozenset[str]] = {}
    # wideq-backed water tank (dehumidifier).
    if data.wideq_coordinator is not None:
        for coordinator in data.coordinators.values():
            if coordinator.device_type != DEVICE_TYPE_DEHUMIDIFIER:
                continue
            local_owner = data.local_providers.get(coordinator.device_id)
            local_contract = (
                local_owner.profile.fields.get(LOCAL_WATER_TANK_FIELD)
                if local_owner is not None
                else None
            )
            if local_contract is None or local_contract.value_type != "boolean":
                local_owner = None
            else:
                promoted_local_semantics[coordinator.device_id] = frozenset(
                    {LOCAL_WATER_TANK_FIELD}
                )
            entities.append(
                WaterTankFullSensor(
                    data.wideq_coordinator,
                    coordinator,
                    local_owner,
                )
            )
    # PAT binary sensors (door, rinse refill, ...).
    for coordinator in data.coordinators.values():
        established_semantics: set[str] = set()
        promoted_semantics = set(
            promoted_local_semantics.get(coordinator.device_id, frozenset())
        )
        read_provider = getattr(data, "local_read_providers", {}).get(
            coordinator.device_id
        )
        if (
            data.wideq_coordinator is not None
            and coordinator.device_type == DEVICE_TYPE_DEHUMIDIFIER
        ):
            established_semantics.add("water_tank.full")
        for desc in PAT_BINARY_BY_TYPE.get(coordinator.device_type, ()):
            if desc.is_on_fn(coordinator) is not None:
                entities.append(MyLgBinarySensor(coordinator, desc))
                semantic_id = _PAT_BINARY_SEMANTICS.get(
                    coordinator.device_type, {}
                ).get(desc.key)
                if semantic_id is not None:
                    established_semantics.add(semantic_id)
        if read_provider is not None:
            for semantic_id, contract in iter_tlv_read_contracts(
                read_provider,
                "binary_sensor",
                established_semantics=established_semantics,
                excluded_semantics=promoted_semantics,
                overlay_duplicates=overlay_duplicates,
            ):
                entities.append(
                    TlvReadBinarySensor(
                        read_provider, coordinator, semantic_id, contract
                    )
                )
        local_provider = data.local_providers.get(coordinator.device_id)
        if local_provider is not None:
            full_semantics = (
                set(read_provider.profile.fields_by_semantic_id)
                if read_provider is not None
                else set()
            )
            for semantic_id, contract in iter_local_semantic_contracts(
                local_provider,
                "boolean",
                wideq_configured=data.wideq_coordinator is not None,
                established_semantics=established_semantics,
                excluded_semantics=full_semantics | set(promoted_semantics),
                overlay_duplicates=overlay_duplicates,
            ):
                entities.append(
                    LocalSemanticBinarySensor(
                        local_provider, coordinator, semantic_id, contract
                    )
                )
    return entities


class MyLgBinarySensor(MyLgEntity, BinarySensorEntity):
    entity_description: MyLgBinaryDescription

    def __init__(
        self, coordinator: PatDeviceCoordinator, description: MyLgBinaryDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.is_on_fn(self.coordinator)


class LocalSemanticBinarySensor(LocalSemanticEntityMixin, BinarySensorEntity):
    """One exact boolean from the read-only Rethink Local profile."""

    def __init__(
        self,
        provider: LocalSemanticShadowProvider,
        pat_coordinator: PatDeviceCoordinator,
        semantic_id: str,
        contract: LocalSemanticFieldContract,
    ) -> None:
        if contract.value_type != "boolean":
            raise ValueError("Local binary sensor requires a boolean contract")
        super().__init__(provider, pat_coordinator, semantic_id, contract)

    @property
    def is_on(self) -> bool | None:
        field = self._shadow_field
        if field is None or type(field.value) is not bool:
            return None
        return field.value


class TlvReadBinarySensor(TlvReadEntityMixin, BinarySensorEntity):
    """One exact boolean from the complete TLV read feed."""

    def __init__(
        self,
        provider: TlvReadShadowProvider,
        pat_coordinator: PatDeviceCoordinator,
        semantic_id: str,
        contract: TlvReadFieldContract,
    ) -> None:
        if contract.domain != "binary_sensor" or contract.value_types != ("boolean",):
            raise ValueError("Complete TLV binary sensor requires a boolean contract")
        super().__init__(provider, pat_coordinator, semantic_id, contract)

    @property
    def is_on(self) -> bool | None:
        field = self._read_field
        if field is None or type(field.value) is not bool:
            return None
        return field.value


class WaterTankFullSensor(CoordinatorEntity[WideqCoordinator], BinarySensorEntity):
    """Stable water-tank entity, Local-owned when an exact provider exists."""

    _attr_has_entity_name = True
    _attr_translation_key = "water_tank_full"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        wideq_coordinator: WideqCoordinator,
        pat_coordinator: PatDeviceCoordinator,
        local_provider: LocalSemanticShadowProvider | None = None,
    ) -> None:
        super().__init__(wideq_coordinator)
        self._device_id = pat_coordinator.device_id
        self._provider_resolver = WaterTankProviderResolver(local_provider)
        self._local_provider = local_provider
        self._remove_local_provider_listener = None
        self._attr_unique_id = f"{pat_coordinator.device_id}_water_tank_full"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, pat_coordinator.device_id)},
            name=pat_coordinator.alias,
            manufacturer="LG",
            model=pat_coordinator.model or pat_coordinator.device_type,
        )

    @property
    def available(self) -> bool:
        return self._provider_resolver.available(
            self._device_id in (self.coordinator.data or {})
        )

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return self.coordinator.diagnostic_attributes

    @property
    def is_on(self) -> bool | None:
        return self._provider_resolver.resolve(
            self.coordinator.snapshot_for(self._device_id)
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._local_provider is not None:
            self._remove_local_provider_listener = (
                self._local_provider.async_add_listener(
                    self._handle_local_provider_update
                )
            )

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_local_provider_listener is not None:
            self._remove_local_provider_listener()
            self._remove_local_provider_listener = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_local_provider_update(self) -> None:
        self.async_write_ha_state()
