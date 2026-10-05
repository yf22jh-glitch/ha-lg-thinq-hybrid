"""Entity adapters for DB-enabled app/server settings, separate from Local."""
from datetime import time

from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.time import TimeEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN


def app_setting_entities(entry, domain):
    settings = getattr(entry.runtime_data, 'app_settings', None)
    if settings is None:
        return []
    classes = {'select': AppSettingSelect, 'sensor': TemperatureDisplaySensor, 'time': NightModeTime}
    if domain not in classes:
        return []
    return [(NightModeSelect if domain == 'select' and spec['feature_id'].startswith('night_mode.')
             else classes[domain])(settings, metadata, spec)
            for metadata in entry.runtime_data.coordinators.values()
            for spec in settings.definitions
            if spec['model_id'] == metadata.model and spec['domain'] == domain
            and (not spec['feature_id'].startswith('night_mode.') or entry.runtime_data.wideq_coordinator is not None)]


class _Setting(CoordinatorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, settings, metadata, spec, coordinator=None):
        super().__init__(coordinator or settings)
        self._settings = settings
        self._metadata, self._spec = metadata, spec
        self._attr_unique_id = f"{metadata.device_id}_app_setting_{spec['feature_id']}"
        self._attr_name = spec['label_ko']
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, metadata.device_id)}, name=metadata.alias,
                                            manufacturer='LG', model=metadata.model)

    @property
    def available(self):
        return self.coordinator.value(self._metadata.device_id, self._spec) is not None

    @property
    def extra_state_attributes(self):
        return {'value_source': self._spec['source'], 'local_appliance_control': False,
                'feature_id': self._spec['feature_id'],
                'write_enabled': self._spec.get('write_enabled', True)}


class AppSettingSelect(_Setting, SelectEntity):
    @property
    def extra_state_attributes(self):
        attrs = super().extra_state_attributes
        if self._spec['feature_id'] == 'smart_pairing':
            attrs['execution_allowed'] = (self.coordinator.data or {}).get(self._metadata.device_id, {}).get('pairing_allowed', False)
        return attrs

    def _options(self):
        if self._spec['feature_id'] != 'smart_pairing':
            return self._spec['options']
        rows = (self.coordinator.data or {}).get(self._metadata.device_id, {}).get('pairing_candidates', [])
        result = {'사용 안 함': 'none'}
        for row in rows:
            if row['pairingYn'] == 'Y' or row['ownerYn'] == 'Y' and row['pairingYn'] == 'N':
                label = row['alias']
                if sum(other['alias'] == label for other in rows) > 1:
                    label += ' · ' + row['deviceId'][-6:]
                result[label] = row['deviceId']
        return result

    @property
    def options(self):
        return list(self._options())

    @property
    def current_option(self):
        value = self.coordinator.value(self._metadata.device_id, self._spec)
        return next((label for label, raw in self._options().items() if raw == value), None)

    async def async_select_option(self, option):
        await self.coordinator.async_set(self._metadata, self._spec['feature_id'], self._options()[option])


class _NightModeSetting(_Setting):
    """DB-owned entities sharing the existing confirmed Web night-mode cache."""
    def __init__(self, settings, metadata, spec):
        super().__init__(settings, metadata, spec, settings.entry.runtime_data.wideq_coordinator)

    def _saved(self):
        return self.coordinator.night_mode_for(self._metadata.device_id)

    @property
    def available(self):
        saved = self._saved()
        required = self._spec.get('required_mode')
        return (saved is not None and not self.coordinator.circuit_open
                and (required is None or saved.mode == required))

    async def _set(self, value):
        spec = self._settings.definition(self._metadata.model, self._spec['feature_id'])
        if spec is None or not spec.get('write_enabled', True):
            raise HomeAssistantError('DB에서 비활성화된 설정이에요.')
        if not self.available:
            raise HomeAssistantError('현재 야간 모드에서 사용할 수 없는 설정이에요.')
        await self.coordinator.async_set_night_mode_setting(self._metadata.device_id,
            expected=self._saved(), feature=self._spec['feature_id'].removeprefix('night_mode.'), value=value)


class NightModeSelect(_NightModeSetting, SelectEntity):
    @property
    def options(self):
        return list(self._spec['options'])

    @property
    def current_option(self):
        saved = self._saved()
        return next((label for label, mode in self._spec['options'].items()
                     if saved is not None and mode == saved.mode), None)

    async def async_select_option(self, option):
        await self._set(self._spec['options'][option])


class NightModeTime(_NightModeSetting, TimeEntity):
    @property
    def native_value(self):
        if not self.available:
            return None
        field = self._spec['feature_id'].removeprefix('night_mode.')
        return time.fromisoformat(getattr(self._saved(), field))

    async def async_set_value(self, value):
        if value.second or value.microsecond or value.tzinfo is not None:
            raise HomeAssistantError('야간 일정은 현지 시각의 시·분만 설정할 수 있어요.')
        await self._set(value.strftime('%H:%M'))


class TemperatureDisplaySensor(_Setting, SensorEntity):
    _attr_entity_category = None

    def _values(self):
        provider = self.coordinator.entry.runtime_data.local_providers.get(self._metadata.device_id)
        if provider is None:
            return None, None
        current, target = tuple(provider.field_value(key) if provider.semantic_field_available(key) else None
                                for key in ('temperature.current_c', 'temperature.target_c'))
        if (not provider.semantic_field_available('operation.mode') or provider.field_value('operation.mode') != 'cool'
                or provider.field_value('fan.mode') == 'power'):
            target = None
        return current, target

    @property
    def available(self):
        return self._values()[0] is not None

    @property
    def native_value(self):
        current, target = self._values()
        if current is None:
            return None
        fmt = self.coordinator.temperature_format(self._metadata.device_id) or '0.5℃'
        def render(value):
            if value is None: return '—'
            return f'{value * 9 / 5 + 32:.0f}℉' if fmt == '1℉' else f'{value:g}℃'
        return f'현재 {render(current)} / 설정 {render(target)}'

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        provider = self.coordinator.entry.runtime_data.local_providers.get(self._metadata.device_id)
        if provider is not None:
            self.async_on_remove(provider.async_add_listener(self.async_write_ha_state))
