"""HA presentation and ThinQ account settings; never routed as local commands."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import logging
import sqlite3
from contextlib import closing

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .wideq.web_settings import FOOD_MODELS, PAIRING_MODEL, pairing_allowed, read_food, read_pairing, set_food, set_pairing

_LOGGER = logging.getLogger(__name__)


def load_app_settings(path):
    """Optional table in the existing DB: no device identities or credentials."""
    with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_settings'").fetchone():
            return ()
        return tuple(dict(model_id=model, feature_id=feature, **json.loads(encoded))
                     for model, feature, encoded in c.execute(
                         'SELECT model_id,feature_id,definition_json FROM app_settings '
                         'WHERE enabled=1 AND delete_requested=0 ORDER BY model_id,feature_id'))


class AppSettingsCoordinator(DataUpdateCoordinator):
    """One shared poll, editable DB definitions, and persistent HA preferences."""
    def __init__(self, hass, entry, definitions):
        super().__init__(hass, _LOGGER, name='my_lg_app_settings', update_interval=timedelta(minutes=30), config_entry=entry)
        self.entry = entry
        self.definitions = definitions
        self.store = Store(hass, 1, f'my_lg.app_preferences.{entry.entry_id}')
        self.preferences = {}
        self._write_lock = asyncio.Lock()
        self._cloud_lock = asyncio.Lock()

    async def async_restore(self):
        saved = await self.store.async_load()
        self.preferences = saved if isinstance(saved, dict) else {}

    def definition(self, model, feature):
        return next((x for x in self.definitions if x['model_id'] == model and x['feature_id'] == feature), None)

    def temperature_format(self, device_id):
        metadata = self.entry.runtime_data.coordinators.get(device_id)
        if metadata is None or self.definition(metadata.model, 'display.temperature_format') is None:
            return None
        return self.preferences.get(device_id, {}).get('display.temperature_format')

    def value(self, device_id, spec):
        if spec['source'] == 'ha-preference':
            return self.temperature_format(device_id) or '0.5℃'
        return (self.data or {}).get(device_id, {}).get(spec['feature_id'])

    async def _request(self, method, path, body=None):
        wideq = self.entry.runtime_data.wideq_coordinator
        if wideq is None or wideq.circuit_open:
            raise HomeAssistantError('LG 서버 설정에 연결할 수 없어요.')
        return await wideq.async_web_setting_request(method, path, body)

    async def _read_device(self, metadata):
        wideq = self.entry.runtime_data.wideq_coordinator
        if wideq is None:
            return {}
        device_id = wideq.wideq_device_id(metadata.device_id)
        if device_id is None:
            return {}
        if metadata.model in FOOD_MODELS:
            value = await read_food(self._request, metadata.model, device_id)
            return {'food.recommended_period': value}
        if metadata.model == PAIRING_MODEL:
            rows = await read_pairing(self._request, device_id)
            spec = self.definition(metadata.model, 'smart_pairing') or {}
            context = await wideq.async_pairing_context(device_id)
            return {'smart_pairing': next((x['deviceId'] for x in rows if x['pairingYn'] == 'Y'), 'none'),
                    'pairing_candidates': rows, 'pairing_allowed': pairing_allowed(context,spec.get('execution_condition',{}))}
        return {}

    async def _async_update_data(self):
        result = {}
        models = {x['model_id'] for x in self.definitions if x['source'] == 'thinq-server'}
        async with self._cloud_lock:
            for metadata in self.entry.runtime_data.coordinators.values():
                if metadata.model not in models:
                    continue
                try:
                    result[metadata.device_id] = await self._read_device(metadata)
                except Exception:  # Optional account feature never fails appliance state.
                    _LOGGER.warning('ThinQ account settings unavailable for model %s', metadata.model)
        return result

    async def async_set(self, metadata, feature, value):
        spec = self.definition(metadata.model, feature)
        if spec is None:
            raise HomeAssistantError('DB에서 비활성화된 설정이에요.')
        async with self._write_lock:
            if spec['source'] == 'ha-preference':
                if value not in spec['options']:
                    raise HomeAssistantError('지원하지 않는 표시 설정이에요.')
                previous = self.preferences
                self.preferences = {**previous, metadata.device_id: {**previous.get(metadata.device_id, {}), feature: value}}
                try:
                    await self.store.async_save(self.preferences)
                except BaseException:
                    self.preferences = previous
                    raise
                self.async_set_updated_data(dict(self.data or {}))
                return
            wideq = self.entry.runtime_data.wideq_coordinator
            device_id = wideq.wideq_device_id(metadata.device_id) if wideq else None
            if device_id is None:
                raise HomeAssistantError('LG 계정의 대상 기기를 아직 확인하지 못했어요.')
            async with self._cloud_lock:
                try:
                    if feature == 'food.recommended_period':
                        result = await set_food(self._request, metadata.model, device_id, value)
                        current = {feature: result}
                    elif feature == 'smart_pairing':
                        if not spec.get('write_enabled', False):
                            raise ValueError('DB에서 스마트 페어링 변경을 비활성화했어요.')
                        context = await wideq.async_pairing_context(device_id)
                        if not pairing_allowed(context,spec.get('execution_condition',{})):
                            raise ValueError('온라인·잠금 해제·오류 없음·설정 가능한 대기 상태인지 확인해 주세요.')
                        old = (self.data or {}).get(metadata.device_id, {}).get('smart_pairing')
                        if old is None:
                            raise ValueError('현재 페어링을 먼저 읽어야 해요.')
                        rows = await set_pairing(self._request, device_id, None if value == 'none' else value,
                                                 None if old == 'none' else old)
                        current = {'smart_pairing': value, 'pairing_candidates': rows, 'pairing_allowed': True}
                    else:
                        raise ValueError('지원하지 않는 서버 설정이에요.')
                except Exception as err:
                    updated = dict(self.data or {})
                    updated.pop(metadata.device_id, None)
                    self.async_set_updated_data(updated)
                    if isinstance(err, ValueError):
                        raise HomeAssistantError(str(err)) from err
                    raise HomeAssistantError('저장을 확인하지 못했어요. 재전송하지 않았으니 ThinQ 설정을 확인해 주세요.') from err
                self.async_set_updated_data({**(self.data or {}), metadata.device_id: current})
