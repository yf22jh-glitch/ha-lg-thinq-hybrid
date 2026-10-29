"""Cloud protection which never unloads Local providers or HA entities.

PAT and WideQ have independent gates. Only verified metadata is cached for
offline startup; measurements and control success are never restored from it.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from functools import wraps
import inspect
import logging
import time
from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)
SOURCES = ('pat', 'wideq')
SERVICE_CLOUD_CONNECTION = 'cloud_connection'


class CloudPausedError(HomeAssistantError):
    """A cloud account needs explicit recovery; Local remains independent."""


def authentication_failure(error: BaseException) -> bool:
    """Recognize auth/account failures, not appliance or transient errors."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if type(error).__name__ in {'InvalidCredentialError', 'UseOfficialAPIError',
                                   'TokenError'}:
            return True
        # thinqconnect encodes PAT authentication/terms errors in its own
        # exception code, not HTTP status. Device permission/offline errors
        # (e.g. 1308/1222) must not protect the entire account.
        if type(error).__name__ == 'ThinQAPIException' and str(getattr(error, 'code', '')) in {
            '1103', '1218', '1302', '1304'
        }:
            return True
        if getattr(error, 'status', None) in (401, 403):
            return True
        message = str(error)
        if any(marker in message for marker in (
            'InvalidCredentialError', 'UseOfficialAPIError',
            'new Term of Service to accept', 'temporarily blocked for 24 hours',
            'ThinQ Web user login failed',
        )):
            return True
        error = error.__cause__ or error.__context__
    return False


class CloudGate:
    def __init__(self, source: str, changed) -> None:
        self.source = source
        self.reason: str | None = None
        self.generation = 0
        self.tasks: set[asyncio.Task] = set()
        self.changed = changed

    def block(self, reason: str) -> None:
        self.reason = reason
        self.generation += 1

    def check(self) -> None:
        if self.reason is not None:
            raise CloudPausedError(f'LG {self.source} 연결이 보호 중이에요. 로컬 연결은 유지돼요.')

    async def call(self, method, *args, **kwargs):
        self.check()
        generation = self.generation
        task = asyncio.create_task(method(*args, **kwargs))
        self.tasks.add(task)
        try:
            result = await task
            self.check()
            if self.generation != generation:
                raise CloudPausedError('보호 이전의 늦은 클라우드 응답은 적용하지 않아요.')
            return result
        except asyncio.CancelledError:
            self.check()
            raise
        except Exception as error:
            if not isinstance(error, CloudPausedError) and authentication_failure(error):
                # Fence concurrent calls before scheduling lifecycle cleanup.
                self.block('authentication')
                self.changed(self.source)
            raise
        finally:
            self.tasks.discard(task)

    async def cancel_pending(self) -> None:
        pending = tuple(self.tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


class GuardedCloudApi:
    """Gate every asynchronous SDK entry point, including commands/settings."""
    def __init__(self, api, gate: CloudGate) -> None:
        self.wrapped = api
        self.gate = gate

    def __getattr__(self, name):
        method = getattr(self.wrapped, name)
        if name == 'async_close' or not inspect.iscoroutinefunction(method):
            return method

        @wraps(method)
        async def guarded(*args, **kwargs):
            return await self.gate.call(method, *args, **kwargs)
        return guarded


class CloudRuntime:
    """Persist source protection and manage only cloud network lifetimes."""
    def __init__(self, hass, entry, data) -> None:
        self.hass, self.entry, self.data = hass, entry, data
        self.store = Store(hass, 1, f'my_lg.cloud_protection.{entry.entry_id}')
        self.inventory_store = Store(hass, 1, f'my_lg.device_inventory.{entry.entry_id}')
        self.gates = {source: CloudGate(source, self._failed) for source in SOURCES}
        self.resumed_at = {source: 0.0 for source in SOURCES}
        self.inventory: dict[str, Any] = {'devices': [], 'profiles': {}}
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._closed = False
        self.start_source = None

    def _task(self, coroutine, name):
        task = self.entry.async_create_background_task(self.hass, coroutine, name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _failed(self, source):
        if not self._closed:
            self._task(self.async_pause((source,), 'authentication'), f'my_lg {source} protection')

    async def async_restore(self):
        saved = await self.store.async_load()
        if isinstance(saved, dict):
            resumed = saved.get("resumed_at", {})
            if isinstance(resumed, dict):
                for source in SOURCES:
                    value = resumed.get(source)
                    if isinstance(value, (float, int)) and value >= 0:
                        self.resumed_at[source] = value
            for source in SOURCES:
                if isinstance(saved.get(source), str):
                    self.gates[source].block(saved[source])
        inventory = await self.inventory_store.async_load()
        if isinstance(inventory, dict) and isinstance(inventory.get('devices'), list):
            # Validate exact persisted identities before constructing Local owners.
            devices, seen = [], set()
            for item in inventory['devices']:
                if not isinstance(item, dict) or not isinstance(item.get('deviceId'), str):
                    continue
                info = item.get('deviceInfo')
                if (not item['deviceId'] or item['deviceId'] in seen or not isinstance(info, dict)
                        or not isinstance(info.get('modelName'), str)
                        or not isinstance(info.get('deviceType'), str)):
                    continue
                seen.add(item['deviceId']); devices.append(deepcopy(item))
            profiles = inventory.get('profiles', {})
            self.inventory = {'devices': devices, 'profiles': {
                key: deepcopy(value) for key, value in profiles.items()
                if key in seen and isinstance(value, dict)
            } if isinstance(profiles, dict) else {}}

    async def async_save_inventory(self):
        self.inventory = {
            'devices': [deepcopy(c.device_info_raw) for c in self.data.coordinators.values()],
            'profiles': {c.device_id: deepcopy(c.profile) for c in self.data.coordinators.values()
                         if isinstance(c.profile, dict)},
        }
        await self.inventory_store.async_save(self.inventory)

    def status(self):
        return {'entry_id': self.entry.entry_id,
                'sources': {source: {'paused': gate.reason is not None, 'reason': gate.reason,
                                     'resumed_at': self.resumed_at[source]}
                            for source, gate in self.gates.items()},
                'local_preserved': not self._closed}

    async def async_pause(self, sources, reason='manual'):
        async with self._lock:
            for source in sources:
                self.gates[source].block(reason)
            # Persist before acknowledging the transition; a reload must not
            # silently re-enable an account that is still blocked by its vendor.
            await self.store.async_save({**{s: g.reason for s, g in self.gates.items()},
                                         'resumed_at': self.resumed_at})
            for source in sources:
                await self.gates[source].cancel_pending()
                if source == 'pat':
                    for coordinator in self.data.coordinators.values():
                        coordinator.cloud_paused = True
                        coordinator.async_update_listeners()
                    if self.data.mqtt is not None:
                        await self.data.mqtt.async_shutdown()
                else:
                    coordinator = self.data.wideq_coordinator
                    if coordinator is not None:
                        coordinator.cloud_paused = True
                        coordinator.async_update_listeners()
                    if self.data.wideq_client is not None:
                        await self.data.wideq_client.async_close()
            return self.status()

    async def async_resume(self, sources):
        async with self._lock:
            sources = tuple(source for source in sources if self.gates[source].reason is not None)
            for source in sources:
                gate = self.gates[source]
                self.resumed_at[source] = time.time()
                gate.reason = None
                gate.generation += 1
            await self.store.async_save({**{s: g.reason for s, g in self.gates.items()},
                                         'resumed_at': self.resumed_at})
        if self.start_source is not None and not self._closed:
            for source in sources:
                self._task(self.start_source(source), f'my_lg {source} resume')
        return self.status()

    async def async_close(self):
        self._closed = True
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for gate in self.gates.values():
            await gate.cancel_pending()
