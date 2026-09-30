"""Sensor platform for Kocom Energy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import API
from .const import (
    DEFAULT_UPDATE_INTERVAL,
    DEVICE_ID,
    DOMAIN,
    MONTH_START_RETRY_INTERVAL,
)
from .exceptions import EnergyDataPendingError, KocomEnergyError, ProtocolError

_LOGGER = logging.getLogger(__name__)


SENSOR_TYPES: dict[str, dict[str, Any]] = {
    "energy": {
        "name": "Kocom Energy Usage",
        "device_class": SensorDeviceClass.TIMESTAMP,
        "icon": "mdi:api",
        "value_key": None,
        "category": EntityCategory.DIAGNOSTIC,
    },
    "electricity": {
        "name": "Kocom Electricity Usage",
        "device_class": SensorDeviceClass.ENERGY,
        "unit": UnitOfEnergy.KILO_WATT_HOUR,
        "state_class": SensorStateClass.TOTAL_INCREASING,
        "icon": "mdi:flash",
        "value_key": "electricity_usage_this_month",
    },
    "gas": {
        "name": "Kocom Gas Usage",
        "device_class": SensorDeviceClass.GAS,
        "unit": UnitOfVolume.CUBIC_METERS,
        "state_class": SensorStateClass.TOTAL_INCREASING,
        "icon": "mdi:fire",
        "value_key": "gas_usage_this_month",
    },
    "water": {
        "name": "Kocom Water Usage",
        "device_class": SensorDeviceClass.WATER,
        "unit": UnitOfVolume.CUBIC_METERS,
        "state_class": SensorStateClass.TOTAL_INCREASING,
        "icon": "mdi:water",
        "value_key": "water_usage_this_month",
    },
    "hot_water": {
        "name": "Kocom Hot Water Usage",
        "state_class": SensorStateClass.TOTAL_INCREASING,
        "icon": "mdi:water-boiler",
        "value_key": "hot_water_usage_this_month",
    },
    "heating": {
        "name": "Kocom Heating Usage",
        "state_class": SensorStateClass.TOTAL_INCREASING,
        "icon": "mdi:radiator",
        "value_key": "heating_usage_this_month",
    },
}


class KocomEnergyCoordinator(DataUpdateCoordinator[dict[str, object]]):
    """Fetch cumulative readings and retain truthful diagnostics."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        configured_interval = entry.options.get(
            "update_interval",
            entry.data.get("update_interval", DEFAULT_UPDATE_INTERVAL),
        )
        try:
            interval_seconds = max(60, int(configured_interval))
        except (TypeError, ValueError):
            interval_seconds = DEFAULT_UPDATE_INTERVAL

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval_seconds),
        )
        self.entry = entry
        self.last_success_at: datetime | None = None
        self.last_error: str | None = None
        self.last_response_bytes: int | None = None
        self.next_retry_at: datetime | None = None
        self._pending_retry_interval = timedelta(
            seconds=max(interval_seconds, MONTH_START_RETRY_INTERVAL)
        )

    def _new_api(self) -> API:
        return API(
            ip=self.entry.data["ip"],
            username=self.entry.data["username"],
            password=self.entry.data["password"],
            fcm=self.entry.data["fcm"],
            phone=self.entry.data["phone"],
        )

    async def _async_update_data(self) -> dict[str, object]:
        now = datetime.now(UTC)
        if self.next_retry_at is not None and now < self.next_retry_at:
            raise UpdateFailed(self.last_error or "Kocom month-start data is pending")

        api = self._new_api()
        try:
            data = await api.get_energy_data()
            if "electricity_usage_this_month" not in data:
                raise ProtocolError("Kocom response has no current electricity value")
        except EnergyDataPendingError as err:
            self.last_response_bytes = err.response_bytes
            self.last_error = (
                "월초 정산 데이터 생성 대기 중 "
                f"({err.response_bytes}바이트 응답, 30분 간격 재시도)"
            )
            self.next_retry_at = now + self._pending_retry_interval
            raise UpdateFailed(self.last_error) from err
        except (KocomEnergyError, TimeoutError, ConnectionError, OSError) as err:
            self.last_response_bytes = api.last_response_bytes
            self.last_error = str(err)
            self.next_retry_at = None
            raise UpdateFailed(f"Kocom energy update failed: {err}") from err
        except Exception as err:
            self.last_response_bytes = api.last_response_bytes
            self.last_error = f"Unexpected {type(err).__name__}: {err}"
            self.next_retry_at = None
            raise UpdateFailed(self.last_error) from err

        self.last_success_at = now
        self.last_response_bytes = api.last_response_bytes
        self.last_error = None
        self.next_retry_at = None
        _LOGGER.debug(
            "Kocom energy update succeeded (%s bytes, month=%s)",
            self.last_response_bytes,
            data.get("this_month"),
        )
        return data


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Kocom Energy sensors."""
    coordinator = KocomEnergyCoordinator(hass, entry)

    # A month-boundary compact response must not prevent the diagnostic entity
    # from loading.  Utility entities remain unavailable until the first valid
    # response and the coordinator continues scheduled recovery attempts.
    await coordinator.async_refresh()
    hass.data[DOMAIN][entry.entry_id] = coordinator

    async_add_entities(
        KocomEnergySensor(coordinator, entry, sensor_type, sensor_data)
        for sensor_type, sensor_data in SENSOR_TYPES.items()
    )


class KocomEnergySensor(CoordinatorEntity[KocomEnergyCoordinator], SensorEntity):
    """A cumulative utility sensor or always-visible diagnostic timestamp."""

    _attr_has_entity_name = False

    def __init__(
        self,
        coordinator: KocomEnergyCoordinator,
        entry: ConfigEntry,
        sensor_type: str,
        sensor_data: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._sensor_type = sensor_type
        self._value_key: str | None = sensor_data.get("value_key")

        name = sensor_data["name"]
        # Preserve upstream unique IDs so existing entity IDs and statistics stay
        # attached after this refactor.
        self._attr_unique_id = (
            f"{DOMAIN}.{entry.data.get('username')}_"
            f"{name.lower().replace(' ', '_')}"
        )
        self._attr_name = name
        self._attr_icon = sensor_data["icon"]
        self._attr_device_class = sensor_data.get("device_class")
        self._attr_native_unit_of_measurement = sensor_data.get("unit")
        self._attr_state_class = sensor_data.get("state_class")
        self._attr_entity_category = sensor_data.get("category")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, DEVICE_ID)},
            name="코콤 에너지",
            manufacturer="Kocom",
            model="Kocom Energy",
        )

    @property
    def available(self) -> bool:
        """Keep diagnostics visible while utility values reflect poll failure."""
        if self._sensor_type == "energy":
            return True
        return (
            super().available
            and self._value_key is not None
            and self.coordinator.data is not None
            and self.coordinator.data.get(self._value_key) is not None
        )

    @property
    def native_value(self) -> datetime | float | None:
        if self._sensor_type == "energy":
            return self.coordinator.last_success_at
        if not self.coordinator.data or self._value_key is None:
            return None
        value = self.coordinator.data.get(self._value_key)
        return value if isinstance(value, (int, float)) else None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        if self._sensor_type != "energy":
            return None

        attributes: dict[str, object] = {
            "connection_state": (
                "connected" if self.coordinator.last_update_success else "error"
            ),
            "last_success": (
                self.coordinator.last_success_at.isoformat()
                if self.coordinator.last_success_at
                else None
            ),
            "last_error": self.coordinator.last_error,
            "last_response_bytes": self.coordinator.last_response_bytes,
            "next_retry_at": (
                self.coordinator.next_retry_at.isoformat()
                if self.coordinator.next_retry_at
                else None
            ),
        }
        if self.coordinator.data:
            attributes.update(self.coordinator.data)
        return attributes
