"""LG ThinQ Hybrid (my_lg) — PAT + MQTT push primary, wideq conditional (later)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import (
    AIR_PURIFIER_ENERGY_HISTORY_MODELS,
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_COUNTRY,
    CONF_LANGUAGE,
    CONF_WIDEQ_CLIENT_ID,
    CONF_WIDEQ_TOKEN,
    DEFAULT_AC_ACTIVE_INTERVAL,
    DEFAULT_APPLIANCE_ACTIVE_INTERVAL,
    DEFAULT_COUNTRY,
    DEFAULT_IDLE_INTERVAL,
    DEFAULT_LANGUAGE,
    DEVICE_TYPE_AIR_PURIFIER,
    DEVICE_TYPE_AIR_CONDITIONER,
    DEVICE_TYPE_COOKTOP,
    DEVICE_TYPE_DEHUMIDIFIER,
    DEVICE_TYPE_KIMCHI_REFRIGERATOR,
    DEVICE_TYPE_OVEN,
    DEVICE_TYPE_REFRIGERATOR,
    DEVICE_TYPE_STYLER,
    DEVICE_TYPE_WASHTOWER,
    DEVICE_TYPE_WATER_PURIFIER,
    DOMAIN,
    OPT_AC_ACTIVE_INTERVAL,
    OPT_APPLIANCE_ACTIVE_INTERVAL,
    OPT_IDLE_INTERVAL,
    PAT_DEVICE_LIST_TIMEOUT,
    PAT_PREPARE_CALL_TIMEOUT,
    PAT_PREPARE_CONCURRENCY,
    PLATFORMS,
    SUPPORTED_DEVICE_TYPES,
    WATER_PUSH_CODES,
    WIDEQ_DEVICE_MAP_STORE_VERSION,
    WIDEQ_ENERGY_HISTORY_LEGACY_STORE_VERSION,
    WIDEQ_ENERGY_HISTORY_PREVIOUS_STORE_VERSION,
    WIDEQ_ENERGY_HISTORY_STORE_VERSION,
    WIDEQ_MAX_CALLS_PER_HOUR,
    WIDEQ_MIN_CALL_SPACING,
    WIDEQ_POWER_SAVE_STORE_VERSION,
)
from .coordinator import PatDeviceCoordinator
from .coordinator_wideq import WideqCoordinator
from .device_identity import PatDeviceIdentity
from .feature_catalog import load_catalogs
from .feature_database import (
    default_database_path,
    enabled_models,
    feature_change_sequence,
)
from .local_command import LocalCommandClient
from .local_control_contract import (
    LocalControlBindingEligibility,
    LocalControlEligibilityError,
    LocalControlEntityContract,
    LocalControlEntityContractError,
    load_local_control_entity_contract,
    local_control_authorized_values,
    local_control_capability_authorized,
    local_control_value_authorized,
    resolve_local_control_binding_eligibility,
)
from .local_control_composite_domain import (
    LocalControlCompositeDomainContract,
    LocalControlCompositeDomainError,
    load_local_control_composite_domain_contract,
)
from .local_control_router import LocalControlRouter
from .local_energy_provider import (
    CumulativeEnergyShadowProvider,
    cumulative_energy_model_supported,
)
from .local_mqtt import LOCAL_PILOT_MQTT_PORT, LocalPilotMqttSubscriber
from .local_provider import (
    LOCAL_DHUM_WATER_TANK_PROFILE_ID,
    LocalProviderConfigurationError,
    LocalSemanticShadowProvider,
    LocalWaterTankShadowProvider,
    local_shadow_configurations,
)
from .local_read_provider import (
    TLV_READ_CONSUMER_STATE_STORE_KEY,
    TLV_READ_CONSUMER_STATE_STORE_VERSION,
    TlvReadCatalogueError,
    TlvReadConsumerBindingAuthority,
    TlvReadConsumerBindingState,
    TlvReadConsumerPin,
    TlvReadConsumerStatePushError,
    TlvReadShadowProvider,
    async_apply_tlv_read_consumer_binding_state,
    load_tlv_read_catalogue,
    load_tlv_read_per_model_authorities,
    parse_tlv_read_consumer_state_inventory,
    transition_tlv_read_consumer_binding_state,
)
from .mqtt import MyLgMqtt
from .rate_limiter import GlobalRateLimiter
from .rethink_event_relay import CONF_RETHINK_EVENT_TOKEN, RethinkEventRelay
from .services import async_register_services
from .startup import StartupMetrics, async_prepare_coordinators
from .wideq_client import WideqClient

_LOGGER = logging.getLogger(__name__)

POWER_ON = "POWER_ON"

# Non-running states reported through PAT/MQTT for appliances whose wideq-only
# detail is useful while a cycle is active. Everything else (including pause,
# reserved, and error) stays on the active cadence.
_INACTIVE_RUN_STATES = {
    None,
    "COMPLETE",
    "END",
    "INITIAL",
    "POWER_OFF",
    "RUNNING_END",
}


_ENERGY_HISTORY_APPLIANCE_BY_TYPE = {
    DEVICE_TYPE_AIR_CONDITIONER: "aircon",
    DEVICE_TYPE_DEHUMIDIFIER: "aircon",
    DEVICE_TYPE_REFRIGERATOR: "fridge",
    DEVICE_TYPE_KIMCHI_REFRIGERATOR: "fridge",
    DEVICE_TYPE_COOKTOP: "devices",
    DEVICE_TYPE_OVEN: "devices",
    DEVICE_TYPE_WATER_PURIFIER: "devices",
    DEVICE_TYPE_STYLER: "devices",
}


def _energy_history_appliance(device_type: str, model: str) -> str | None:
    """Return only a live-verified ThinQ energy-history route."""
    if device_type == DEVICE_TYPE_AIR_PURIFIER:
        return (
            "air_purifier"
            if model in AIR_PURIFIER_ENERGY_HISTORY_MODELS
            else None
        )
    return _ENERGY_HISTORY_APPLIANCE_BY_TYPE.get(device_type)


def _is_ac_active(coordinator: PatDeviceCoordinator) -> bool:
    """Return whether PAT/MQTT requests the AC-class collection cadence."""
    if coordinator.device_type == DEVICE_TYPE_DEHUMIDIFIER:
        # Preserve the existing 600s behavior while powered on; WATER_IS_FULL
        # push still requests a prompt refresh and this cadence later sees clear.
        return coordinator.get("operation", "dehumidifierOperationMode") == POWER_ON
    return (
        coordinator.device_type == DEVICE_TYPE_AIR_CONDITIONER
        and coordinator.get("operation", "airConOperationMode") == POWER_ON
    )


def _is_appliance_active(coordinator: PatDeviceCoordinator) -> bool:
    """Return whether PAT/MQTT reports a washer/dryer/styler cycle active."""
    if coordinator.device_type == DEVICE_TYPE_WASHTOWER:
        return any(
            coordinator.get(part, "runState", "currentState")
            not in _INACTIVE_RUN_STATES
            for part in ("washer", "dryer")
        )
    if coordinator.device_type == DEVICE_TYPE_STYLER:
        return coordinator.get("runState", "currentState") not in _INACTIVE_RUN_STATES
    return False


def _wideq_interval(
    coordinators: list[PatDeviceCoordinator],
    ac_active_interval: int,
    appliance_active_interval: int,
    idle_interval: int,
) -> int:
    """Choose collection cadence exclusively from PAT/MQTT state."""
    intervals: list[int] = []
    if any(_is_ac_active(c) for c in coordinators):
        intervals.append(ac_active_interval)
    if any(_is_appliance_active(c) for c in coordinators):
        intervals.append(appliance_active_interval)
    return min(intervals) if intervals else idle_interval


@dataclass
class MyLgData:
    """Runtime data stored on the config entry."""

    api: object
    coordinators: dict[str, PatDeviceCoordinator] = field(default_factory=dict)
    mqtt: MyLgMqtt | None = None
    wideq_client: WideqClient | None = None
    wideq_coordinator: WideqCoordinator | None = None
    local_providers: dict[str, LocalSemanticShadowProvider] = field(
        default_factory=dict
    )
    local_mqtt_subscribers: dict[str, LocalPilotMqttSubscriber] = field(
        default_factory=dict
    )
    local_read_providers: dict[str, TlvReadShadowProvider] = field(
        default_factory=dict
    )
    local_read_consumer_state_store: Store[dict[str, Any]] | None = None
    local_read_consumer_states: dict[str, TlvReadConsumerBindingState] = field(
        default_factory=dict
    )
    # Store head and live-provider head normally match. They are separate only
    # across the explicit "durable saved, provider push pending" recovery state.
    local_read_consumer_persisted_states: dict[
        str, TlvReadConsumerBindingState
    ] = field(default_factory=dict)
    local_read_consumer_authorities: dict[
        str, TlvReadConsumerBindingAuthority
    ] = field(default_factory=dict)
    local_read_consumer_state_lock: asyncio.Lock | None = None
    local_energy_providers: dict[str, CumulativeEnergyShadowProvider] = field(
        default_factory=dict
    )
    local_control_entity_contract: LocalControlEntityContract | None = None
    local_control_composite_domain_contract: (
        LocalControlCompositeDomainContract | None
    ) = None
    local_control_binding_eligibility: Mapping[
        str, LocalControlBindingEligibility
    ] = field(default_factory=dict)
    local_control: LocalControlRouter | None = None
    startup_metrics: StartupMetrics | None = None

    async def async_transition_local_read_consumer_state(
        self,
        *,
        operation: str,
        binding_id: str,
        binding_generation: int | None,
        expected_current_record_sha256: str | None,
    ) -> TlvReadConsumerBindingState:
        """Apply one named, JIT-derived consumer transition."""
        return await _async_transition_local_read_consumer_state(
            self,
            operation=operation,
            binding_id=binding_id,
            binding_generation=binding_generation,
            expected_current_record_sha256=expected_current_record_sha256,
        )


MyLgConfigEntry = ConfigEntry  # ConfigEntry[MyLgData] at type-check time


async def _async_apply_local_read_consumer_state(
    data: MyLgData, state: TlvReadConsumerBindingState
) -> TlvReadConsumerBindingState:
    """Durably apply one adapter-owned state, then push it into the provider."""
    store = data.local_read_consumer_state_store
    lock = data.local_read_consumer_state_lock
    if store is None or lock is None:
        raise RuntimeError("TLV read consumer state Store is unavailable")
    try:
        applied = await async_apply_tlv_read_consumer_binding_state(
            states=data.local_read_consumer_persisted_states,
            store=store,
            lock=lock,
            providers=data.local_read_providers,
            state=state,
        )
    except TlvReadConsumerStatePushError as err:
        # The exception precisely reports that Store already advanced. Keep a
        # separate durable head for CAS/retry while the live-provider map stays
        # at the last state it actually received.
        data.local_read_consumer_persisted_states[err.binding_id] = err.state
        raise
    data.local_read_consumer_states[applied.binding_id] = applied
    return applied


async def _async_transition_local_read_consumer_state(
    data: MyLgData,
    *,
    operation: str,
    binding_id: str,
    binding_generation: int | None,
    expected_current_record_sha256: str | None,
) -> TlvReadConsumerBindingState:
    """Derive a named target from HA authority; accept no operator hashes."""
    authority = data.local_read_consumer_authorities.get(binding_id)
    if authority is None:
        raise ValueError("TLV read consumer binding authority is unavailable")
    needs_generation = operation in {
        "bootstrap-v1",
        "stage-v2",
        "adopt-successor-v2",
        "restore-predecessor-v2",
        "retire-predecessor-v2",
    }
    if needs_generation != (binding_generation is not None):
        raise ValueError("TLV read consumer binding generation presence is invalid")

    matching_providers = tuple(
        provider
        for provider in data.local_read_providers.values()
        if provider.binding_id == binding_id
    )
    if len(matching_providers) > 1:
        raise RuntimeError("TLV read consumer binding has duplicate providers")
    provider = matching_providers[0] if matching_providers else None

    v1_pin = None
    v2_pin = None
    predecessor_v2_pin = None
    observed_adopted_pin = None
    if operation == "bootstrap-v1":
        assert binding_generation is not None
        v1_pin = (
            provider.consumer_pin_for_projection(1, binding_generation)
            if provider is not None
            else authority.pin_for_projection(1, binding_generation)
        )
    elif operation == "stage-v2":
        assert binding_generation is not None
        v2_pin = (
            provider.consumer_pin_for_projection(2, binding_generation)
            if provider is not None
            else authority.pin_for_projection(2, binding_generation)
        )
        # MQTT acceptance can advance the provider's process-only latch before
        # the reviewed adapter advances the durable Store row. Feed that exact
        # pin into the pure transition so a reconciliation stage never lowers
        # an already-adopted v2 projection. Provider absence remains valid for
        # offline and not-yet-started appliances.
        if provider is not None and provider.adopted_projection_version == 2:
            live_state = provider.consumer_state
            if live_state is None:
                raise RuntimeError(
                    "TLV read live v2 adoption has no consumer state"
                )
            observed_adopted_pin = TlvReadConsumerPin(
                projection_version=live_state.adopted_projection_version,
                static_read_contract_sha256=(
                    live_state.adopted_static_read_contract_sha256
                ),
                model_contract_sha256=(
                    live_state.adopted_model_contract_sha256
                ),
            )
    elif operation in {
        "adopt-successor-v2",
        "restore-predecessor-v2",
        "retire-predecessor-v2",
    }:
        assert binding_generation is not None
        v2_pin = (
            provider.consumer_pin_for_projection(2, binding_generation)
            if provider is not None
            else authority.pin_for_projection(2, binding_generation)
        )
        predecessor_v2_pin = (
            authority.reviewed_predecessor_pin_for_v2_successor(
                binding_generation
            )
        )
        persisted = data.local_read_consumer_persisted_states.get(binding_id)
        if (
            operation == "adopt-successor-v2"
            and provider is not None
            and persisted is not None
            and persisted.schema_version == 1
            and (
                provider.consumer_state is None
                or provider.consumer_state.record_sha256
                != persisted.record_sha256
            )
        ):
            raise RuntimeError(
                "TLV read v2 successor predecessor is not the live provider head"
            )

    target = transition_tlv_read_consumer_binding_state(
        operation=operation,  # type: ignore[arg-type]
        current=data.local_read_consumer_persisted_states.get(binding_id),
        binding_id=binding_id,
        pat_device_id_proof_sha256=authority.pat_device_id_proof_sha256,
        v1_pin=v1_pin,
        v2_pin=v2_pin,
        predecessor_v2_pin=predecessor_v2_pin,
        observed_adopted_pin=observed_adopted_pin,
        expected_current_record_sha256=expected_current_record_sha256,
    )
    return await _async_apply_local_read_consumer_state(data, target)


async def async_setup_entry(hass: HomeAssistant, entry: MyLgConfigEntry) -> bool:
    """Set up my_lg from a config entry."""
    from thinqconnect import ThinQApi

    session = async_get_clientsession(hass)
    token = entry.data[CONF_ACCESS_TOKEN]
    country = entry.data.get(CONF_COUNTRY, DEFAULT_COUNTRY)
    client_id = entry.data[CONF_CLIENT_ID]

    api = ThinQApi(
        session=session,
        access_token=token,
        country_code=country,
        client_id=client_id,
    )

    try:
        devices = await asyncio.wait_for(
            api.async_get_device_list(), timeout=PAT_DEVICE_LIST_TIMEOUT
        )
    except Exception as err:
        raise ConfigEntryNotReady(f"device list failed: {err}") from err

    data = MyLgData(api=api)

    for device in devices or []:
        info = device.get("deviceInfo", {})
        if info.get("deviceType") not in SUPPORTED_DEVICE_TYPES:
            continue  # everything else stays on official lg_thinq
        coordinator = PatDeviceCoordinator(hass, entry, api, device)
        data.coordinators[coordinator.device_id] = coordinator

    # Seed initial PAT state/profile with a small bounded fan-out.  One offline
    # or stalled appliance is non-fatal and later recovers through MQTT push or
    # the hourly REST fallback.  WideQ still performs no eager setup poll.
    _, data.startup_metrics = await async_prepare_coordinators(
        list(data.coordinators.values()),
        concurrency=PAT_PREPARE_CONCURRENCY,
        call_timeout=PAT_PREPARE_CALL_TIMEOUT,
    )
    _LOGGER.info(
        "my_lg PAT prepared %d devices in %.3fs "
        "(status=%d, profile=%d, timeouts=%d/%d)",
        data.startup_metrics.supported_devices,
        data.startup_metrics.preparation_seconds,
        data.startup_metrics.status_ready,
        data.startup_metrics.profile_ready,
        data.startup_metrics.status_timeouts,
        data.startup_metrics.profile_timeouts,
    )

    if not data.coordinators:
        _LOGGER.warning(
            "my_lg: no supported devices found (Stage 1 = air conditioners)"
        )

    # Dispatch DEVICE_PUSH notifications to that device's event entity, and on a
    # water-tank push also refresh wideq promptly (rate-limited).
    def _on_push(device_id: str, code: str) -> None:
        async_dispatcher_send(hass, f"{DOMAIN}_push_{device_id}", code)
        if code in WATER_PUSH_CODES and data.wideq_coordinator is not None:
            hass.async_create_task(data.wideq_coordinator.async_request_refresh())

    rethink_relay = RethinkEventRelay(
        session, entry.options.get(CONF_RETHINK_EVENT_TOKEN, "")
    )

    def _on_lifecycle(event: dict[str, Any]) -> None:
        hass.async_create_task(rethink_relay.async_send(event))

    # MQTT push (best-effort; REST fallback keeps working if this fails).
    mqtt = MyLgMqtt(
        hass,
        api,
        client_id,
        data.coordinators,
        on_push=_on_push,
        on_lifecycle=_on_lifecycle if rethink_relay.enabled else None,
    )
    await mqtt.async_start()
    data.mqtt = mqtt

    # wideq (optional): AC realtime power/energy, dehumidifier water tank, etc.
    if entry.data.get(CONF_WIDEQ_TOKEN):
        await _setup_wideq(hass, entry, data)

    # Generated catalogs are file-backed. Warm their process-wide caches in an
    # executor so synchronous entity factories never perform disk I/O on the
    # Home Assistant event loop.
    await hass.async_add_executor_job(load_catalogs)

    feature_db_sequence: int | None = None
    feature_db_path = default_database_path()
    if feature_db_path.is_file():
        try:
            feature_db_sequence = await hass.async_add_executor_job(
                feature_change_sequence, feature_db_path
            )
        except (OSError, ValueError):
            _LOGGER.exception("Local feature database edit watcher is unavailable")

    # Rethink Local is a shadow first: it reads, and every entity's state still comes from
    # LG. It also offers a write path for the few commands the bridge has observed on the
    # wire, which entities try before the cloud and fall back from silently. Started after
    # every potentially blocking setup read, immediately before entity setup.
    await _setup_local_shadows(hass, entry, data)
    # Separate from the shadows themselves: this needs the HTTP session, and reaching for it
    # inside the shadow setup made that function require a fully built Home Assistant.
    _start_local_control(hass, data)
    entry.runtime_data = data
    try:
        async_register_services(hass)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        entry.async_on_unload(entry.add_update_listener(_async_reload_on_options))
        if feature_db_sequence is not None:
            _watch_feature_database(hass, entry, feature_db_sequence)
    except Exception:
        await _stop_local_shadows(data)
        raise
    return True


def _watch_feature_database(
    hass: HomeAssistant, entry: MyLgConfigEntry, initial_sequence: int
) -> None:
    """Adopt committed DB edits with one ordinary integration reload.

    This never modifies the SQLite file, entity registry, or energy ledger.
    The old listener is canceled on unload, and a new setup starts at the
    sequence it actually used, so an edit during setup is not lost.
    """
    sequence = initial_sequence
    checking = asyncio.Lock()
    path = default_database_path()

    async def check(_now: object) -> None:
        nonlocal sequence
        if checking.locked():
            return
        async with checking:
            try:
                current = await hass.async_add_executor_job(feature_change_sequence, path)
            except (OSError, ValueError):
                _LOGGER.exception("Local feature database edit check failed")
                return
            if current == sequence:
                return
            sequence = current
            _LOGGER.info("Local feature database changed; reloading my_lg")
            await hass.config_entries.async_reload(entry.entry_id)

    entry.async_on_unload(async_track_time_interval(
        hass, check, timedelta(seconds=20), name="my_lg feature database"
    ))


async def _setup_wideq(
    hass: HomeAssistant, entry: MyLgConfigEntry, data: MyLgData
) -> None:
    """Build the wideq client + single low-rate coordinator (AC energy, etc.)."""
    session = async_create_clientsession(hass)
    client = WideqClient(
        session,
        entry.data[CONF_WIDEQ_TOKEN],
        entry.data.get(CONF_COUNTRY, DEFAULT_COUNTRY),
        entry.data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
        entry.data.get(CONF_WIDEQ_CLIENT_ID),
    )
    limiter = GlobalRateLimiter(WIDEQ_MAX_CALLS_PER_HOUR, WIDEQ_MIN_CALL_SPACING)

    coordinators = list(data.coordinators.values())
    # PAT exposes none of these energy quantities. Each mapping below was
    # verified against the device's current ThinQ app module and a live
    # response. WashTower is intentionally absent: LG returns zero from its
    # history service for this combined model, so its snapshot counters remain
    # the only truthful energy source.
    energy_history_targets: dict[str, str] = {}
    for coordinator in coordinators:
        appliance = _energy_history_appliance(
            coordinator.device_type, coordinator.model
        )
        if appliance is not None:
            energy_history_targets[coordinator.device_id] = appliance
    pat_devices = {
        coordinator.device_id: PatDeviceIdentity(
            device_id=coordinator.device_id,
            alias=coordinator.alias,
            model=coordinator.model,
        )
        for coordinator in coordinators
    }

    opts = entry.options
    ac_active_interval = opts.get(OPT_AC_ACTIVE_INTERVAL, DEFAULT_AC_ACTIVE_INTERVAL)
    appliance_active_interval = opts.get(
        OPT_APPLIANCE_ACTIVE_INTERVAL, DEFAULT_APPLIANCE_ACTIVE_INTERVAL
    )
    idle_interval = opts.get(OPT_IDLE_INTERVAL, DEFAULT_IDLE_INTERVAL)

    def interval_fn() -> int:
        # Device activity comes from PAT/MQTT; wideq only collects missing data.
        return _wideq_interval(
            coordinators,
            ac_active_interval,
            appliance_active_interval,
            idle_interval,
        )

    data.wideq_client = client
    energy_history_store: Store[dict[str, Any]] = Store(
        hass,
        WIDEQ_ENERGY_HISTORY_STORE_VERSION,
        f"{DOMAIN}.wideq_energy_history_v3.{entry.entry_id}",
    )
    previous_energy_history_store: Store[dict[str, Any]] = Store(
        hass,
        WIDEQ_ENERGY_HISTORY_PREVIOUS_STORE_VERSION,
        f"{DOMAIN}.wideq_energy_history_v2.{entry.entry_id}",
    )
    legacy_energy_history_store: Store[dict[str, Any]] = Store(
        hass,
        WIDEQ_ENERGY_HISTORY_LEGACY_STORE_VERSION,
        f"{DOMAIN}.wideq_energy_history.{entry.entry_id}",
    )
    device_map_store: Store[dict[str, Any]] = Store(
        hass,
        WIDEQ_DEVICE_MAP_STORE_VERSION,
        f"{DOMAIN}.wideq_device_map.{entry.entry_id}",
    )
    power_save_store: Store[dict[str, Any]] = Store(
        hass,
        WIDEQ_POWER_SAVE_STORE_VERSION,
        f"{DOMAIN}.wideq_power_save_v1.{entry.entry_id}",
    )
    data.wideq_coordinator = WideqCoordinator(
        hass,
        entry,
        client,
        limiter,
        interval_fn,
        energy_history_targets=energy_history_targets,
        energy_history_store=energy_history_store,
        pat_devices=pat_devices,
        device_map_store=device_map_store,
        legacy_energy_history_store=legacy_energy_history_store,
        previous_energy_history_store=previous_energy_history_store,
        power_save_store=power_save_store,
    )
    await data.wideq_coordinator.async_restore_device_map()
    await data.wideq_coordinator.async_restore_power_save()
    await data.wideq_coordinator.async_restore_energy_history()
    for coordinator in coordinators:
        entry.async_on_unload(
            coordinator.async_add_listener(data.wideq_coordinator.reconcile_interval)
        )
    # No first_refresh: wideq must not eager-poll on setup (restart-burst
    # avoidance). The first poll fires one interval after entities subscribe.


async def _setup_local_shadows(
    hass: HomeAssistant, entry: MyLgConfigEntry, data: MyLgData
) -> None:
    """Start each exact Local shadow binding with failure isolation."""
    try:
        configs = await hass.async_add_executor_job(
            local_shadow_configurations, entry.options
        )
    except LocalProviderConfigurationError:
        _LOGGER.error("Rethink Local shadow options are invalid; provider disabled")
        return
    if not configs:
        return

    feature_database_models: frozenset[str] = frozenset()
    if default_database_path().is_file():
        try:
            feature_database_models = await hass.async_add_executor_job(
                enabled_models, default_database_path()
            )
        except (OSError, ValueError):
            _LOGGER.exception("Local feature database is invalid; shadow setup disabled")
            return
    try:
        read_profiles = await hass.async_add_executor_job(load_tlv_read_catalogue)
    except TlvReadCatalogueError:
        # The primary pilot shadow remains independently useful.  A broken or
        # missing complete-read artifact must never disable its availability or
        # control-presence fences.
        _LOGGER.exception(
            "Complete TLV read catalogue is invalid; full read entities disabled"
        )
        read_profiles = {}

    if all(config.model_id in feature_database_models for config in configs):
        # Existing pin state is left untouched for rollback, but it no longer
        # decides whether an identified, type-compatible read is displayed.
        read_model_authorities = {}
        read_consumer_states: dict[str, TlvReadConsumerBindingState] = {}
    else:
        try:
            read_model_authorities = await hass.async_add_executor_job(
                load_tlv_read_per_model_authorities
            )
        except TlvReadCatalogueError:
            _LOGGER.exception(
                "Per-model TLV read authority is invalid; v2 transition disabled"
            )
            read_model_authorities = {}

        read_consumer_store: Store[dict[str, Any]] = Store(
            hass,
            TLV_READ_CONSUMER_STATE_STORE_VERSION,
            f"{DOMAIN}.{TLV_READ_CONSUMER_STATE_STORE_KEY}.{entry.entry_id}",
        )
        data.local_read_consumer_state_store = read_consumer_store
        data.local_read_consumer_state_lock = asyncio.Lock()
        try:
            stored_consumer_state = await read_consumer_store.async_load()
            if stored_consumer_state is None:
                read_consumer_states = {}
                _LOGGER.warning(
                    "TLV read consumer pin Store is not staged; exact read entities "
                    "will remain unavailable until the installer writes binding pins"
                )
            else:
                read_consumer_states = dict(
                    parse_tlv_read_consumer_state_inventory(
                        stored_consumer_state
                    ).bindings
                )
        except Exception:  # noqa: BLE001 - Store failure must not omit offline entities
            _LOGGER.exception(
                "TLV read consumer pin Store is invalid; exact read values disabled"
            )
            read_consumer_states = {}
        data.local_read_consumer_states = read_consumer_states
        data.local_read_consumer_persisted_states = dict(read_consumer_states)

    try:
        control_entity_contract = await hass.async_add_executor_job(
            load_local_control_entity_contract
        )
    except LocalControlEntityContractError:
        # Generic control owners are independent of both Local reads and the
        # existing Local-first cloud owners. A broken/new artifact disables only
        # the new owners and must never weaken those other layers.
        _LOGGER.exception(
            "Rethink Local control entity contract is invalid; generic controls disabled"
        )
    else:
        # Derive owners from the valid catalogue and configured model identities.
        # An optional explicit scope can restrict them; its failure must not
        # disable independent Local reads or silently expand that scope.
        data.local_control_entity_contract = control_entity_contract
        binding_models = {
            config.binding_id: config.model_id for config in configs
        }
        try:
            eligibility = resolve_local_control_binding_eligibility(
                entry.options,
                control_entity_contract,
                binding_models,
            )
        except LocalControlEligibilityError:
            _LOGGER.error(
                "Rethink Local private binding eligibility is invalid; generic controls disabled"
            )
        else:
            data.local_control_binding_eligibility = eligibility
            from .local_control_confirmed_features import augment_confirmed_features
            try:
                extended_contract, extended_eligibility = await hass.async_add_executor_job(
                    augment_confirmed_features, control_entity_contract, eligibility, binding_models
                )
            except (OSError, ValueError, KeyError, TypeError):
                _LOGGER.error('Confirmed Local feature catalogue unavailable; existing controls unchanged')
            else:
                data.local_control_entity_contract = extended_contract
                data.local_control_binding_eligibility = extended_eligibility

    try:
        data.local_control_composite_domain_contract = (
            await hass.async_add_executor_job(
                load_local_control_composite_domain_contract
            )
        )
        from .local_control_confirmed_features import augment_confirmed_climate_domain
        try:
            data.local_control_composite_domain_contract = await hass.async_add_executor_job(
                augment_confirmed_climate_domain, data.local_control_composite_domain_contract
            )
        except (OSError, ValueError, KeyError, TypeError):
            _LOGGER.error('Confirmed climate extension unavailable; base climate domain unchanged')
    except LocalControlCompositeDomainError:
        # Exact scalar controls and every Local read remain independent. Only
        # composable climate writes fail closed when this additive authority is
        # absent, stale or malformed.
        _LOGGER.exception(
            "Rethink Local composite control domain is invalid; composite controls disabled"
        )

    for config in configs:
        pat_coordinator = data.coordinators.get(config.pat_device_id)
        if pat_coordinator is None or pat_coordinator.model != config.model_id:
            _LOGGER.error(
                "Rethink Local shadow target does not match its pinned model; "
                "binding disabled"
            )
            continue
        if config.profile_id == LOCAL_DHUM_WATER_TANK_PROFILE_ID and (
            pat_coordinator.device_type != DEVICE_TYPE_DEHUMIDIFIER
            or data.wideq_coordinator is None
        ):
            _LOGGER.error(
                "Rethink Local dehumidifier shadow requires its existing WideQ "
                "water-tank provider; binding disabled"
            )
            continue

        provider: LocalSemanticShadowProvider
        if config.profile_id == LOCAL_DHUM_WATER_TANK_PROFILE_ID:
            provider = LocalWaterTankShadowProvider(
                config.binding_id,
                profile=config.profile,
                pat_device_id=config.pat_device_id,
                require_identity=config.require_identity,
            )
        else:
            provider = LocalSemanticShadowProvider(
                config.binding_id,
                config.profile,
                pat_device_id=config.pat_device_id,
                require_identity=config.require_identity,
            )
        read_provider = None
        energy_provider = None
        read_profile = read_profiles.get(config.model_id)
        model_feature_database_active = config.model_id in feature_database_models
        if read_profile is not None:
            if not provider.control_presence_enabled:
                _LOGGER.error(
                    "Complete TLV read feed requires the V3 authenticated presence "
                    "profile; full read binding disabled"
                )
            else:
                try:
                    read_provider = TlvReadShadowProvider(
                        config.binding_id,
                        config.pat_device_id,
                        read_profile,
                        provider,
                        model_authority=(
                            None if model_feature_database_active
                            else read_model_authorities.get(config.model_id)
                        ),
                        consumer_state=(
                            None if model_feature_database_active
                            else read_consumer_states.get(config.binding_id)
                        ),
                        read_contract_policy=(
                            "field-compatible"
                            if model_feature_database_active
                            else config.read_contract_policy
                        ),
                    )
                    if not model_feature_database_active and config.read_contract_policy == "pinned":
                        data.local_read_consumer_authorities[
                            config.binding_id
                        ] = read_provider.consumer_binding_authority
                except (TypeError, ValueError):
                    _LOGGER.exception(
                        "Complete TLV read provider identity is invalid; full read "
                        "binding disabled"
                    )
        if cumulative_energy_model_supported(config.model_id):
            try:
                energy_provider = CumulativeEnergyShadowProvider(
                    config.binding_id,
                    config.model_id,
                    provider.expected_proof,
                )
            except (TypeError, ValueError):
                _LOGGER.exception(
                    "Cumulative-energy provider identity is invalid; energy feed disabled"
                )
        subscriber = LocalPilotMqttSubscriber(
            hass.loop,
            provider,
            host="127.0.0.1",
            port=LOCAL_PILOT_MQTT_PORT,
            username=config.mqtt_username,
            password=config.mqtt_password,
            read_provider=read_provider,
            energy_provider=energy_provider,
        )
        try:
            await subscriber.async_start()
        except Exception:
            try:
                await subscriber.async_stop()
            except Exception:  # noqa: BLE001 - best-effort partial-start cleanup
                _LOGGER.warning("Rethink Local shadow partial-start cleanup failed")
            if read_provider is not None:
                read_provider.close()
            if energy_provider is not None:
                energy_provider.close()
            _LOGGER.exception(
                "Rethink Local shadow transport could not start; cloud providers "
                "remain active"
            )
            continue
        data.local_providers[config.pat_device_id] = provider
        data.local_mqtt_subscribers[config.pat_device_id] = subscriber
        if read_provider is not None:
            data.local_read_providers[config.pat_device_id] = read_provider
        if energy_provider is not None:
            data.local_energy_providers[config.pat_device_id] = energy_provider

    if data.local_providers:
        _LOGGER.info(
            "Rethink Local read-only shadow providers started (count=%d; "
            "operational owners unchanged)",
            len(data.local_providers),
        )


def _start_local_control(hass: HomeAssistant, data: MyLgData) -> None:
    """Offer the local write path where there is a shadow to build a command from.

    Only where: the bridge sends frames observed on the wire, so it can serve some requests
    and not others, and the appliance's own latest reading is what the rest of a command is
    taken from. Without the WideQ pairing there is no id to address, so the router is not
    built at all rather than built to fail one request at a time.
    """
    if not data.local_providers:
        _LOGGER.debug("Rethink Local control not offered: no local shadows are running")
        return
    if data.wideq_coordinator is None:
        _LOGGER.debug("Rethink Local control not offered: WideQ identities are not resolved")
        return
    _LOGGER.debug("Rethink Local control offered for %d appliance(s)", len(data.local_providers))
    def _write_authorized(
        pat_device_id: str, capability_id: str, local_request_value: str
    ) -> bool:
        provider = data.local_providers.get(pat_device_id)
        contract = data.local_control_entity_contract
        if provider is None or contract is None:
            return False
        composite = data.local_control_composite_domain_contract
        if (
            composite is not None
            and composite.authorizes(
                provider.model_id, capability_id, local_request_value
            )
            and local_control_capability_authorized(
                contract,
                data.local_control_binding_eligibility,
                binding_id=provider.binding_id,
                model_id=provider.model_id,
                capability_id=capability_id,
            )
        ):
            return True
        return local_control_value_authorized(
            contract,
            data.local_control_binding_eligibility,
            binding_id=provider.binding_id,
            model_id=provider.model_id,
            capability_id=capability_id,
            local_request_value=local_request_value,
        )

    def _authorized_values(
        pat_device_id: str, capability_id: str
    ) -> tuple[str, ...]:
        provider = data.local_providers.get(pat_device_id)
        contract = data.local_control_entity_contract
        if provider is None or contract is None:
            return ()
        return local_control_authorized_values(
            contract,
            data.local_control_binding_eligibility,
            binding_id=provider.binding_id,
            model_id=provider.model_id,
            capability_id=capability_id,
        )

    def _capability_authorized(
        pat_device_id: str, capability_id: str
    ) -> bool:
        provider = data.local_providers.get(pat_device_id)
        contract = data.local_control_entity_contract
        if provider is None or contract is None:
            return False
        return local_control_capability_authorized(
            contract,
            data.local_control_binding_eligibility,
            binding_id=provider.binding_id,
            model_id=provider.model_id,
            capability_id=capability_id,
        )

    data.local_control = LocalControlRouter(
        LocalCommandClient(async_get_clientsession(hass)),
        data.local_providers,
        data.wideq_coordinator.wideq_device_id,
        _write_authorized,
        _authorized_values,
        _capability_authorized,
    )


async def _stop_local_shadows(data: MyLgData) -> None:
    """Detach all Local shadows, isolating every subscriber shutdown."""
    subscribers = tuple(data.local_mqtt_subscribers.values())
    read_providers = tuple(data.local_read_providers.values())
    energy_providers = tuple(data.local_energy_providers.values())
    data.local_providers.clear()
    data.local_mqtt_subscribers.clear()
    data.local_read_providers.clear()
    data.local_read_consumer_authorities.clear()
    data.local_energy_providers.clear()
    data.local_control_binding_eligibility = {}
    for subscriber in subscribers:
        try:
            await subscriber.async_stop()
        except Exception:
            _LOGGER.exception("Rethink Local shadow transport shutdown failed")
    for provider in read_providers:
        provider.close()
    for provider in energy_providers:
        provider.close()


async def async_unload_entry(hass: HomeAssistant, entry: MyLgConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False
    data: MyLgData | None = getattr(entry, "runtime_data", None)
    if data:
        await _stop_local_shadows(data)
    if data and data.mqtt:
        await data.mqtt.async_stop()
    if data and data.wideq_coordinator:
        await data.wideq_coordinator.async_persist_power_save()
        await data.wideq_coordinator.async_persist_energy_history()
        await data.wideq_coordinator.async_persist_device_map()
    if data and data.wideq_client:
        await data.wideq_client.async_close()
    return True


async def _async_reload_on_options(hass: HomeAssistant, entry: MyLgConfigEntry) -> None:
    """Reload when options (e.g. polling intervals) change."""
    await hass.config_entries.async_reload(entry.entry_id)
