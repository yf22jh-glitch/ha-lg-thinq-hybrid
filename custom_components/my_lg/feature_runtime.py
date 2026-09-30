"""Reconcile editable feature menus on the running HA entity platforms.

Only entity definitions change here. MQTT clients, command routers, accepted
observations, device identities and energy providers are not restarted.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import EntityPlatform, async_get_current_platform

from .feature_database import (
    copy_feature_database, disabled_control_capabilities, disabled_read_semantics,
    feature_database_token,
)
from .local_control_composite_domain import load_local_control_composite_domain_contract
from .local_control_confirmed_features import (
    _refresh_appliance_feature_maps,
    augment_confirmed_climate_domain,
    augment_confirmed_features,
    load_confirmed_features,
)
from .local_control_contract import (
    load_local_control_entity_contract, resolve_local_control_binding_eligibility,
)
from .local_provider import load_local_semantic_profile_catalogue
from .local_read_provider import load_tlv_read_catalogue

_LOGGER = logging.getLogger(__name__)
_PRESENTATION_ATTRIBUTES = (
    "_attr_name", "_attr_icon", "_attr_native_unit_of_measurement",
    "_attr_device_class", "_attr_state_class", "_attr_entity_category",
    "_attr_entity_registry_enabled_default", "_attr_supported_features",
    "_attr_options", "_attr_event_types", "_attr_native_min_value",
    "_attr_native_max_value", "_attr_native_step", "_attr_hvac_modes",
    "_attr_fan_modes", "_attr_preset_modes", "_attr_available_modes",
)


def _definition_value(value: Any) -> Any:
    """Compare constructor definitions, never observed/accumulated state."""
    if is_dataclass(value) and not isinstance(value, type):
        return (type(value), tuple(
            (field.name, _definition_value(getattr(value, field.name)))
            for field in fields(value)
        ))
    if isinstance(value, Mapping):
        return tuple(sorted((key, _definition_value(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_definition_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_definition_value(item) for item in value)
    if callable(value):
        return (getattr(value, "__module__", None), getattr(value, "__qualname__", None))
    return value


def _entity_definition(entity: Entity) -> tuple[Any, ...]:
    return (
        type(entity),
        tuple(_definition_value(getattr(entity, name, None)) for name in (
            "entity_description", "_contract", "_descriptor", "_domain",
            "_control", "_climate_domain", "_config", "_cfg", "_spec",
            "_tuple_capability", "_power_on_capability",
        )),
        tuple(_definition_value(getattr(entity, name, None))
              for name in _PRESENTATION_ATTRIBUTES),
        # Local/cloud owner changes must update subscriptions even if their
        # public names and unique IDs are identical.
        tuple(id(getattr(entity, name, None)) for name in (
            "_provider", "_local_provider", "_primary_provider",
            "_read_provider", "_local_read_provider", "_local_read",
        )),
    )


def _indexed_entities(entities: list[Entity]) -> dict[str, Entity]:
    indexed: dict[str, Entity] = {}
    for entity in entities:
        unique_id = entity.unique_id
        if not unique_id or unique_id in indexed:
            raise ValueError("Feature factory returned a missing or duplicate entity identity")
        indexed[unique_id] = entity
    return indexed


def _build_enabled_entities(entry, build: Callable[[], list[Entity]]) -> list[Entity]:
    """Apply a DB disable to native aliases too, not only generic controls."""
    data = entry.runtime_data
    disabled = getattr(data, "local_disabled_controls", frozenset())
    disabled_reads = getattr(data, "local_disabled_reads", frozenset())
    controls = getattr(data, "local_control_entity_contract", None)
    editable = controls is not None and controls.revision.startswith("feature-db:")
    entities: list[Entity] = []
    for entity in build():
        descriptor = getattr(entity, "_descriptor", None)
        description = getattr(entity, "entity_description", None)
        metadata = (getattr(entity, "_pat_coordinator", None)
                    or getattr(entity, "_metadata", None)
                    or getattr(entity, "coordinator", None))
        model = getattr(metadata, "model", None)
        source = getattr(entity, "_provider", None) or getattr(entity, "_read_provider", None)
        if model is None and source is not None:
            model = getattr(source, "model_id", None)
        # Primary/full-read can own the same unique ID. Hiding one must not
        # silently reconstruct the same read entity through the other source.
        read_semantic = getattr(entity, "_semantic_id", None)
        if descriptor is None and (model, read_semantic) in disabled_reads:
            continue
        capabilities = tuple(capability for capability in (
            getattr(descriptor, "capability_id", None),
            getattr(description, "local_control_semantic", None),
            getattr(description, "local_scalar_semantic", None),
            getattr(description, "local_capability", None),
            getattr(description, "local_semantic", None),
        ) if capability is not None)
        if any((model, capability) in disabled for capability in capabilities):
            continue
        if editable and descriptor is not None:
            entity._attr_name = descriptor.label_ko
            # DB-enabled newly created controls need no second opt-in. Existing
            # HA registry disables (especially USER) are never cleared here.
            entity._attr_entity_registry_enabled_default = True
        elif editable and capabilities:
            aliases = [item for item in controls.descriptors_by_model.get(model, ())
                       if item.existing_owner and item.capability_id in capabilities]
            if len(aliases) == 1:
                entity._attr_name = aliases[0].label_ko
        entities.append(entity)
    return entities


@dataclass
class _PlatformMenu:
    platform: EntityPlatform
    build: Callable[[], list[Entity]]
    entities: dict[str, Entity]
    definitions: dict[str, tuple[Any, ...]]


@dataclass(frozen=True)
class _Menus:
    token: str
    primary: Mapping[str, Any]
    reads: Mapping[str, Any]
    controls: Any
    eligibility: Mapping[str, Any]
    composite: Any
    confirmed: list[dict[str, Any]]
    disabled_controls: frozenset[tuple[str, str]]
    disabled_reads: frozenset[tuple[str, str]]


def _load_menus(path: Path, options: Mapping[str, Any], binding_models: Mapping[str, str]) -> _Menus:
    # All loaders see the same SQLite commit, including when the source uses
    # WAL. This temporary menu copy contains no connection/runtime state and
    # is removed on exit; no operating database or historical ledger is edited.
    with TemporaryDirectory(prefix="my-lg-feature-menu-") as directory:
        snapshot = Path(directory) / "features.sqlite3"
        copy_feature_database(path, snapshot)
        controls = load_local_control_entity_contract(snapshot)
        eligibility = resolve_local_control_binding_eligibility(options, controls, binding_models)
        confirmed = load_confirmed_features(snapshot)
        controls, eligibility = augment_confirmed_features(
            controls, eligibility, binding_models, features=confirmed, refresh_maps=False,
        )
        composite = augment_confirmed_climate_domain(
            load_local_control_composite_domain_contract(), features=confirmed,
        )
        return _Menus(
            token=feature_database_token(snapshot),
            primary=load_local_semantic_profile_catalogue(snapshot)[1],
            reads=load_tlv_read_catalogue(snapshot), controls=controls,
            eligibility=eligibility, composite=composite, confirmed=confirmed,
            disabled_controls=disabled_control_capabilities(snapshot),
            disabled_reads=disabled_read_semantics(snapshot),
        )


class FeatureEntityRuntime:
    """One entry's running feature menu; registry identities remain unchanged."""

    def __init__(self, hass, entry, path: Path) -> None:
        self.hass = hass
        self.entry = entry
        self.path = path
        self._platforms: dict[str, _PlatformMenu] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    def close(self) -> None:
        """Do not add entities after the entry starts unloading."""
        self._closed = True
        self._platforms.clear()

    async def async_unload(self, unload: Callable[[], Awaitable[bool]]) -> bool:
        """Serialize platform unload against edits; resume if HA refuses it."""
        async with self._lock:
            self._closed = True
            try:
                unloaded = await unload()
            except BaseException:
                self._closed = False
                raise
            if unloaded:
                self.close()
            else:
                self._closed = False
            return unloaded

    def register(
        self, domain: str, platform: EntityPlatform, build: Callable[[], list[Entity]],
        entities: list[Entity],
    ) -> None:
        indexed = _indexed_entities(entities)
        self._platforms[domain] = _PlatformMenu(
            platform, build, indexed,
            {unique_id: _entity_definition(entity) for unique_id, entity in indexed.items()},
        )

    async def async_refresh(self) -> str:
        """Load committed definitions and update only changed entities."""
        async with self._lock:
            if self._closed:
                raise RuntimeError("Feature runtime is unloading")
            data = self.entry.runtime_data
            binding_models = {
                provider.binding_id: provider.model_id
                for provider in data.local_providers.values()
            }
            menus = await self.hass.async_add_executor_job(
                _load_menus, self.path, self.entry.options, binding_models,
            )
            if self._closed:
                raise RuntimeError("Feature runtime is unloading")

            primary_before = [(provider, provider.profile) for provider in data.local_providers.values()]
            reads_before = [(provider, provider.profile) for provider in data.local_read_providers.values()]
            control_before = (
                data.local_control_entity_contract, data.local_control_binding_eligibility,
                data.local_control_composite_domain_contract, data.local_disabled_controls,
                getattr(data, "local_disabled_reads", frozenset()),
            )
            confirmed_before = list(getattr(data, "local_confirmed_features", ()))
            try:
                for provider, _old in primary_before:
                    profile = menus.primary.get(provider.profile_id)
                    if profile is None:
                        raise ValueError("Feature edit removed the connected appliance profile")
                    provider.update_profile(profile, notify=False)
                for provider, old in reads_before:
                    # An empty menu removes all visible reads without removing
                    # their subscription; a later enable can reuse the current.
                    profile = menus.reads.get(old.model_id, replace(old, fields=()))
                    provider.update_profile(profile, notify=False)
                data.local_control_entity_contract = menus.controls
                data.local_control_binding_eligibility = menus.eligibility
                data.local_control_composite_domain_contract = menus.composite
                data.local_disabled_controls = menus.disabled_controls
                data.local_disabled_reads = menus.disabled_reads
                data.local_confirmed_features = menus.confirmed
                _refresh_appliance_feature_maps(menus.confirmed)
                # Materialize all changed menus before removing any live entity.
                next_entities = {
                    domain: _indexed_entities(_build_enabled_entities(self.entry, menu.build))
                    for domain, menu in self._platforms.items()
                }
            except Exception:
                for provider, profile in primary_before:
                    provider.update_profile(profile, notify=False)
                for provider, profile in reads_before:
                    provider.update_profile(profile, notify=False)
                (
                    data.local_control_entity_contract, data.local_control_binding_eligibility,
                    data.local_control_composite_domain_contract, data.local_disabled_controls,
                    data.local_disabled_reads,
                ) = control_before
                data.local_confirmed_features = confirmed_before
                _refresh_appliance_feature_maps(confirmed_before)
                raise

            added = removed = changed = 0
            for domain, menu in self._platforms.items():
                if self._closed:
                    raise RuntimeError("Feature runtime is unloading")
                candidates = next_entities[domain]
                definitions = {unique_id: _entity_definition(entity)
                               for unique_id, entity in candidates.items()}
                replacements = {unique_id for unique_id in menu.entities.keys() & candidates.keys()
                                if menu.definitions[unique_id] != definitions[unique_id]}
                retiring = (menu.entities.keys() - candidates.keys()) | replacements
                for unique_id in retiring:
                    active = next((entity for entity in menu.platform.entities.values()
                                   if entity.unique_id == unique_id), None)
                    if active is not None:
                        await menu.platform.async_remove_entity(active.entity_id)
                    menu.entities.pop(unique_id, None)
                    menu.definitions.pop(unique_id, None)
                additions = [entity for unique_id, entity in candidates.items()
                             if unique_id not in menu.entities]
                if additions:
                    # Use the platform's awaitable API so a completed refresh
                    # actually includes entity registration, not queued work.
                    await menu.platform.async_add_entities(additions)
                added += len(additions) - len(replacements)
                removed += len(retiring) - len(replacements)
                changed += len(replacements)
                for unique_id, entity in candidates.items():
                    menu.entities.setdefault(unique_id, entity)
                menu.definitions = definitions

            # Notify retained listeners only after the new entity menus exist.
            for provider, _profile in (*primary_before, *reads_before):
                provider._notify_listeners()
            _LOGGER.info(
                "Local feature database applied live (added=%d, removed=%d, changed=%d)",
                added, removed, changed,
            )
            return menus.token


def setup_feature_entities(entry, domain: str, build: Callable[[], list[Entity]], async_add_entities) -> None:
    """Keep each platform's entity factory reusable after initial setup."""
    entities = _build_enabled_entities(entry, build)
    runtime = getattr(entry.runtime_data, "feature_runtime", None)
    if runtime is not None:
        runtime.register(domain, async_get_current_platform(), build, entities)
    async_add_entities(entities)
