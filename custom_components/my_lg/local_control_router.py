"""Choose between the local bridge and the cloud for one control, and say which was used.

The local path is not a faster version of the cloud path: it can only send frames that were
observed on the wire, so most of what it is asked for it cannot do. A request to cool to 26C is
refused unless someone has been seen setting exactly that. That is the design, not a gap to
paper over.

Turning a unit on is not a power write, which is why it is composed like any other tuple: the
appliance keeps its settings while off and the app turns it back on by restating them, so
`climate.power_on_with_setpoint` carries a mode, a fan and a setpoint. Every frame the capture
observed restated what the unit already had; a caller turning it on INTO a mode states that one
instead, which the bridge composes from the same per-tag domains it composes any tuple from.

So this answers one question - can the local path serve THIS request - and answers `None` when
it cannot, leaving the caller to do what it already did. What it must never do is answer `None`
after a frame has gone out: `local_command` separates "refused before the write" from "sent and
not accounted for" precisely so that a fallback cannot become a second command.

No Home Assistant imports here on purpose: which appliance, which capability and which value is
a question about this project's own vocabulary, and keeping it that way is what lets it be
tested without a running Home Assistant.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from .local_control_confirmed_features import APPLIANCE_SETTING_MODELS, APPLIANCE_VALUE_MODELS
from .feature_conditions import evaluate_condition

from .local_command import (
    CLIMATE_POWER_ON_CAPABILITY,
    CLIMATE_TUPLE_CAPABILITY,
    COMFORT_PREFERENCE_SEMANTIC,
    MAX_SHADOW_AGE,
    POWER_CAPABILITY,
    TEMPERATURE_TARGET_SEMANTIC,
    LocalCommandResult,
    LocalCommandRetryable,
    LocalCommandUnavailable,
    climate_expected_state,
    climate_state_fields,
    climate_tuple,
)

_LOGGER = logging.getLogger(__name__)

_CLIMATE_COMMON_FIELDS = (
    "operation.mode",
    "fan.mode",
)
_CLIMATE_ALL_FIELDS = (
    *_CLIMATE_COMMON_FIELDS,
    TEMPERATURE_TARGET_SEMANTIC,
    COMFORT_PREFERENCE_SEMANTIC,
)


@dataclass(frozen=True)
class _ConfirmedField:
    """One appliance-confirmed value temporarily newer than the MQTT shadow."""

    value: Any
    observed_at: datetime


@dataclass(frozen=True)
class _ConfirmedTuple:
    fields: Mapping[str, _ConfirmedField]
    confirmed_at: datetime


@dataclass(frozen=True)
class _CloudTupleBarrier:
    """Per-field observations that a post-cloud shadow must advance beyond."""

    baselines: Mapping[str, datetime | None]


def _observed_at(field: Any) -> datetime | None:
    value = getattr(field, "observed_at", None)
    return value if isinstance(value, datetime) else None


def _confirmed_tuple(value: str, confirmed_at: datetime) -> _ConfirmedTuple:
    """Turn the tuple we sent and the appliance confirmed back into shadow-shaped fields."""
    mode, fan, rendered_argument = value.split("|", 2)
    fields: dict[str, _ConfirmedField] = {
        "operation.mode": _ConfirmedField(mode, confirmed_at),
        "fan.mode": _ConfirmedField(fan, confirmed_at),
    }
    # POWER fan reports a temporary 18 C while preserving the real target.
    # The bridge intentionally does not claim that the tuple set a target in
    # this combination, so the HA-side confirmation overlay must not invent
    # that claim either.
    if fan != "power" and mode == "auto":
        if not rendered_argument.startswith("comfort:"):
            raise ValueError("confirmed AUTO climate tuple has no comfort preference")
        preference = int(rendered_argument.removeprefix("comfort:"))
        if preference < -2 or preference > 2:
            raise ValueError("confirmed AUTO comfort preference is out of range")
        fields[COMFORT_PREFERENCE_SEMANTIC] = _ConfirmedField(
            preference, confirmed_at
        )
    elif fan != "power":
        if not rendered_argument.endswith("C"):
            raise ValueError("confirmed climate tuple has no temperature suffix")
        temperature = float(rendered_argument[:-1])
        normalized_temperature: int | float = (
            int(temperature) if temperature.is_integer() else temperature
        )
        fields[TEMPERATURE_TARGET_SEMANTIC] = _ConfirmedField(
            normalized_temperature, confirmed_at
        )
    return _ConfirmedTuple(fields=fields, confirmed_at=confirmed_at)


class _Sender(Protocol):
    async def async_vacuum_auto_emptying_state(self, device_id: str) -> bool | None: ...
    async def async_vacuum_reservation_state(self, device_id: str) -> dict | None: ...
    async def async_air_extra_state(self, device_id: str, capability: str) -> bool | None: ...
    async def async_appliance_setting_state(self, device_id: str, capability: str) -> bool | str | None: ...

    async def async_send(
        self,
        device_id: str,
        capability: str,
        value: str,
        *,
        expected_state: Mapping[str, Any] | None = None,
    ) -> LocalCommandResult: ...


class _Shadow(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def shadow_fields(self) -> Mapping[str, Any]: ...

    @property
    def shadow_healthy(self) -> bool: ...

    @property
    def control_alive(self) -> bool: ...

    @property
    def control_state_ready(self) -> bool: ...

    def control_fields_ready(self, semantic_ids: tuple[str, ...]) -> bool: ...


class LocalFeatureDisabled(ValueError):
    """The operator disabled this function; neither Local nor cloud may run it."""


class LocalControlRouter:
    """One appliance's local control path, or nothing."""

    def __init__(
        self,
        sender: _Sender,
        providers: Mapping[str, _Shadow],
        bridge_device_id: Callable[[str], str | None],
        write_authorized: Callable[[str, str, str], bool] | None = None,
        authorized_values: Callable[[str, str], tuple[str, ...]] | None = None,
        capability_authorized: Callable[[str, str], bool] | None = None,
        *,
        capability_disabled: Callable[[str, str], bool] | None = None,
        condition_policy: Callable[[str, str], Any] | None = None,
        read_providers: Mapping[str, Any] | None = None,
    ) -> None:
        self._sender = sender
        self._providers = providers
        self._bridge_device_id = bridge_device_id
        self._write_authorized = write_authorized
        self._authorized_values = authorized_values
        self._capability_authorized = capability_authorized
        self._capability_disabled = capability_disabled
        self._condition_policy = condition_policy
        self._read_providers = read_providers if read_providers is not None else {}
        # So that "the local path never serves anything" is discoverable without anyone having
        # first suspected it and raised the log level for this component.
        self._reported_refusal: set[tuple[str, str]] = set()
        # A confirmed bridge response can arrive before the retained MQTT shadow. Keep only that
        # appliance-confirmed tuple, briefly, so a second command cannot restate the pre-command
        # mode/fan/setpoint and undo the first. Per-device locks make simultaneous entity writes use
        # the same ordering as the appliance.
        self._confirmed_tuples: dict[str, _ConfirmedTuple] = {}
        # A cloud tuple write is authoritative but its API acknowledgement is not an appliance
        # report. Until every input needed by a composite frame has a newer observation than the
        # pre-dispatch baseline, composing locally would restate retained pre-cloud state.
        self._cloud_tuple_barriers: dict[str, _CloudTupleBarrier] = {}
        # POWER fan temporarily reports 18 C even though the appliance retains the user's target.
        # AUTO reuses the same wire carrier for a unitless comfort preference. Keep the two facts in
        # separate stores so neither can ever fill the other's command variant.
        self._last_temperature_targets_by_mode: dict[str, dict[str, float]] = {}
        self._last_comfort_preferences: dict[str, int] = {}
        self._tuple_locks: dict[str, asyncio.Lock] = {}
        # Desired UI choice only, not an appliance report. No wire traffic until Start.
        # Deliberately not restored across HA reload or an operator's Start press.
        self._styler_course_choices: dict[str, str] = {}
        self._styler_option_choices: dict[str, str] = {}
        self._styler_choice_listeners: dict[str, set[Callable[[], None]]] = {}
        self._feature_drafts: dict[tuple[str, str], str] = {}
        self._feature_draft_listeners: dict[str, set[Callable[[], None]]] = {}

    def feature_draft(self, device_id: str, capability: str) -> str | None:
        return self._feature_drafts.get((device_id, capability))

    def set_feature_draft(self, device_id: str, capability: str, value: str | None) -> None:
        self.ensure_feature_enabled(device_id, capability)
        if value is None:
            self._feature_drafts.pop((device_id, capability), None)
        else:
            self._feature_drafts[(device_id, capability)] = value
        for listener in tuple(self._feature_draft_listeners.get(device_id, ())):
            listener()

    def subscribe_feature_draft(self, device_id: str, listener: Callable[[], None]) -> Callable[[], None]:
        listeners = self._feature_draft_listeners.setdefault(device_id, set())
        listeners.add(listener)
        def remove() -> None:
            listeners.discard(listener)
            if not listeners:
                self._feature_draft_listeners.pop(device_id, None)
        return remove

    def subscribe_styler_choice(self, device_id: str, listener: Callable[[], None]) -> Callable[[], None]:
        listeners = self._styler_choice_listeners.setdefault(device_id, set())
        listeners.add(listener)
        def remove() -> None:
            listeners.discard(listener)
            if not listeners:
                self._styler_choice_listeners.pop(device_id, None)
        return remove

    def _notify_styler_choice(self, device_id: str) -> None:
        # UI notification must not change the result of consuming a start draft.
        for listener in tuple(self._styler_choice_listeners.get(device_id, ())):
            try:
                listener()
            except Exception:
                _LOGGER.warning('Could not refresh a Styler draft entity')

    def select_styler_course(self, pat_device_id: str, value: str) -> None:
        if not self.value_authorized(pat_device_id, "styler.operation.start_or_resume", value):
            raise ValueError("Styler course is not authorized for this binding")
        self._styler_course_choices[pat_device_id] = value
        self._styler_option_choices.pop(pat_device_id, None)
        self._notify_styler_choice(pat_device_id)

    def select_styler_options(self, pat_device_id: str, value: str) -> None:
        from .local_styler_options import CAPABILITY, canonical_program
        request = canonical_program(value)
        if not self.value_authorized(pat_device_id, CAPABILITY, request):
            raise ValueError('Styler option program is not authorized for this binding')
        self._styler_option_choices[pat_device_id] = request
        self._styler_course_choices.pop(pat_device_id, None)
        self._notify_styler_choice(pat_device_id)

    def selected_styler_options(self, pat_device_id: str) -> str | None:
        return self._styler_option_choices.get(pat_device_id)

    def take_styler_start(self, pat_device_id: str) -> tuple[str, str] | None:
        from .local_styler_options import CAPABILITY
        value = self._styler_option_choices.pop(pat_device_id, None)
        if value is not None:
            self._styler_course_choices.pop(pat_device_id, None)
            self._notify_styler_choice(pat_device_id)
            return CAPABILITY, value
        value = self.take_styler_course(pat_device_id)
        return ('styler.operation.start_or_resume', value) if value is not None else None

    def selected_styler_course(self, pat_device_id: str) -> str | None:
        return self._styler_course_choices.get(pat_device_id)

    def take_styler_course(self, pat_device_id: str) -> str | None:
        # Consume before dispatch: an ambiguous delivery must not be retried by another press.
        value = self._styler_course_choices.pop(pat_device_id, None)
        if value is not None:
            self._notify_styler_choice(pat_device_id)
        return value

    def _tuple_lock(self, pat_device_id: str) -> asyncio.Lock:
        lock = self._tuple_locks.get(pat_device_id)
        if lock is None:
            lock = asyncio.Lock()
            self._tuple_locks[pat_device_id] = lock
        return lock

    def control_target_available(self, pat_device_id: str) -> bool:
        """Return whether an authenticated exact target can accept a command.

        This read-only predicate intentionally emits no refusal log and exposes
        no bridge identity. Generic Local-only Home Assistant entities use it
        for availability before they offer a write with no cloud fallback.
        """
        provider = self._providers.get(pat_device_id)
        return (
            provider is not None
            and provider.control_alive
            and self._bridge_device_id(pat_device_id) is not None
        )

    def authorized_values(
        self, pat_device_id: str, capability_id: str
    ) -> tuple[str, ...]:
        """Return the exact reviewed values this private binding may send."""
        if self.feature_disabled(pat_device_id, capability_id):
            return ()
        if self._authorized_values is not None:
            return self._authorized_values(pat_device_id, capability_id)
        return ()

    def value_authorized(
        self, pat_device_id: str, capability_id: str, value: str
    ) -> bool:
        """Check one value without sending or requiring current liveness."""
        if self.feature_disabled(pat_device_id, capability_id):
            return False
        if self._write_authorized is not None:
            return self._write_authorized(pat_device_id, capability_id, value)
        return value in self.authorized_values(pat_device_id, capability_id)

    def capability_authorized(
        self, pat_device_id: str, capability_id: str
    ) -> bool:
        """Check exact binding/model capability evidence without choosing a value."""
        if self.feature_disabled(pat_device_id, capability_id):
            return False
        if self._capability_authorized is not None:
            return self._capability_authorized(pat_device_id, capability_id)
        return bool(self.authorized_values(pat_device_id, capability_id))

    def feature_disabled(self, pat_device_id: str, capability_id: str) -> bool:
        return self._capability_disabled is not None and self._capability_disabled(
            pat_device_id, capability_id
        )

    def ensure_feature_enabled(self, pat_device_id: str, capability_id: str) -> None:
        if self.feature_disabled(pat_device_id, capability_id):
            raise LocalFeatureDisabled("이 기능은 기능 DB에서 비활성화되어 있어요.")

    def reported_local_value(self, pat_device_id: str, semantic_id: str) -> tuple[bool, Any]:
        """Read appliance state only; False/zero are values, not missing reports."""
        read = self._read_providers.get(pat_device_id)
        if read is not None and read.field_available(semantic_id):
            return True, read.field_value(semantic_id)
        primary = self._providers.get(pat_device_id)
        if primary is not None and primary.semantic_field_available(semantic_id):
            return True, primary.field_value(semantic_id)
        return False, None

    def feature_condition_status(self, pat_device_id: str, capability: str, value: str | None = None) -> tuple[bool, str | None]:
        """Use current local reads, never the PAT/cloud fallback or a cached ACK."""
        if self._condition_policy is None:
            return True, None
        primary = self._providers.get(pat_device_id)
        policy = self._condition_policy(getattr(primary, "model_id", ""), capability)
        if policy is None:
            return True, None
        semantics = set()
        if isinstance(policy, dict):
            groups = [policy, *(policy.get("byValue", {}).values() if isinstance(policy.get("byValue"), dict) else ())]
            for group in groups:
                if isinstance(group, dict) and isinstance(group.get("all", []), list):
                    semantics.update(clause["semanticId"] for clause in group.get("all", [])
                                     if isinstance(clause, dict) and isinstance(clause.get("semanticId"), str))
        state = {}
        for semantic in semantics:
            reported, own_value = self.reported_local_value(pat_device_id, semantic)
            if reported:
                state[semantic] = own_value
        return evaluate_condition(policy, state, value)

    def feature_condition_available(self, pat_device_id: str, capability: str, value: str | None = None) -> bool:
        return self.feature_condition_status(pat_device_id, capability, value)[0]

    def subscribe_condition_state(self, pat_device_id: str, callback: Callable[[], None]) -> Callable[[], None]:
        removers = []
        for provider in (self._providers.get(pat_device_id), self._read_providers.get(pat_device_id)):
            if provider is not None:
                removers.append(provider.async_add_listener(callback))
        def remove() -> None:
            for unsubscribe in removers:
                unsubscribe()
            removers.clear()
        return remove

    def ensure_feature_conditions(self, pat_device_id: str, capability: str, value: str) -> None:
        available, reason = self.feature_condition_status(pat_device_id, capability, value)
        if not available:
            # Raising, rather than returning None, prevents a native card from
            # bypassing the same DB condition through its cloud fallback.
            from .local_command import LocalCommandNotReady
            raise LocalCommandNotReady(reason)

    def _remember_mode_argument(
        self, pat_device_id: str, shadow: Mapping[str, Any], now: datetime
    ) -> None:
        """Remember only a fresh, appliance-observed argument of the current variant."""
        mode_field = shadow.get("operation.mode")
        fan_field = shadow.get("fan.mode")
        mode = getattr(mode_field, "value", None)
        argument_semantic = (
            COMFORT_PREFERENCE_SEMANTIC
            if mode == "auto"
            else TEMPERATURE_TARGET_SEMANTIC
        )
        argument_field = shadow.get(argument_semantic)
        observed = (
            _observed_at(mode_field),
            _observed_at(fan_field),
            _observed_at(argument_field),
        )
        if any(
            item is None or now < item or now - item > MAX_SHADOW_AGE
            for item in observed
        ):
            return
        fan = getattr(fan_field, "value", None)
        argument = getattr(argument_field, "value", None)
        if fan == "power":
            return
        if mode == "auto":
            if type(argument) is int and -2 <= argument <= 2:
                self._last_comfort_preferences[pat_device_id] = argument
            return
        if (
            not isinstance(mode, str)
            or type(argument) not in (int, float)
            or not math.isfinite(float(argument))
        ):
            return
        self._last_temperature_targets_by_mode.setdefault(pat_device_id, {})[
            mode
        ] = float(argument)

    def _shadow_with_confirmation(
        self,
        pat_device_id: str,
        shadow: Mapping[str, Any],
        now: datetime,
    ) -> Mapping[str, Any]:
        confirmed = self._confirmed_tuples.get(pat_device_id)
        if confirmed is None:
            return shadow
        if now < confirmed.confirmed_at or now - confirmed.confirmed_at > MAX_SHADOW_AGE:
            self._confirmed_tuples.pop(pat_device_id, None)
            return shadow

        # Each field is independently authoritative once the appliance reports it at or after the
        # confirmation. It wins whether it agrees or conflicts: a newer conflict is an app/remote/
        # physical change, and retaining the old overlay would make the next composite command undo
        # it. Fields not yet newly observed keep their confirmed values so retained lag cannot undo
        # the command either.
        effective = dict(shadow)
        remaining: dict[str, _ConfirmedField] = {}
        for field_name, expected in confirmed.fields.items():
            reported = shadow.get(field_name)
            reported_at = _observed_at(reported)
            if reported_at is not None and reported_at >= confirmed.confirmed_at:
                continue
            remaining[field_name] = expected
            effective[field_name] = expected
        if not remaining:
            self._confirmed_tuples.pop(pat_device_id, None)
            return shadow
        if len(remaining) != len(confirmed.fields):
            self._confirmed_tuples[pat_device_id] = _ConfirmedTuple(
                fields=remaining,
                confirmed_at=confirmed.confirmed_at,
            )
        return effective

    async def async_mark_cloud_tuple_dispatch(self, pat_device_id: str) -> None:
        """Fence local tuple composition until the appliance reports all post-cloud inputs.

        This is deliberately observation-based, not a timer. Waiting ten minutes and then trusting
        the same retained fields would merely make stale state older; each field must advance beyond
        what was known immediately before the cloud dispatch. A confirmed overlay is part of that
        baseline because a delayed pre-cloud MQTT delivery must not clear the fence.
        """
        async with self._tuple_lock(pat_device_id):
            self._mark_cloud_tuple_dispatch_locked(pat_device_id)

    def _mark_cloud_tuple_dispatch_locked(self, pat_device_id: str) -> None:
        """Install/advance the observation fence while holding this device's tuple lock."""
        provider = self._providers.get(pat_device_id)
        shadow = {} if provider is None else provider.shadow_fields
        confirmed = self._confirmed_tuples.get(pat_device_id)
        previous = self._cloud_tuple_barriers.get(pat_device_id)
        baselines: dict[str, datetime | None] = {}
        for field_name in _CLIMATE_ALL_FIELDS:
            candidates = [
                _observed_at(shadow.get(field_name)),
                (
                    None
                    if confirmed is None or field_name not in confirmed.fields
                    else confirmed.fields[field_name].observed_at
                ),
                (
                    None
                    if previous is None
                    else previous.baselines.get(field_name)
                ),
            ]
            present = [candidate for candidate in candidates if candidate is not None]
            baselines[field_name] = max(present) if present else None
        self._cloud_tuple_barriers[pat_device_id] = _CloudTupleBarrier(
            baselines=baselines
        )
        self._confirmed_tuples.pop(pat_device_id, None)

    def _cloud_tuple_barrier_blocks(
        self, pat_device_id: str, shadow: Mapping[str, Any]
    ) -> bool:
        barrier = self._cloud_tuple_barriers.get(pat_device_id)
        if barrier is None:
            return False
        mode = getattr(shadow.get("operation.mode"), "value", None)
        for field_name in climate_state_fields(mode):
            reported_at = _observed_at(shadow.get(field_name))
            baseline = barrier.baselines.get(field_name)
            if reported_at is None or (
                baseline is not None and reported_at <= baseline
            ):
                return True
        self._cloud_tuple_barriers.pop(pat_device_id, None)
        return False

    def _target(self, pat_device_id: str) -> tuple[str, _Shadow] | None:
        """The appliance this can write to, or nothing - and it says which.

        Every refusal here is structural and permanent-looking, which is exactly the kind that
        must not be silent: an install where the local path never serves anything looks identical
        to one where it works, and these three were the ones with nothing to find.
        """
        provider = self._providers.get(pat_device_id)
        if provider is None:
            self._not_served(pat_device_id, "no-shadow", "this appliance has no local shadow")
            return None
        # Presence answers only whether an authenticated appliance connection can receive a
        # command.  It is deliberately independent of semantic state: parameterless/exact writes
        # such as pause need no snapshot, while tuple composition applies its own per-field
        # freshness and mode gates below.  Using `shadow_healthy` here made a quiet but connected
        # appliance unreachable and made stateless commands depend on unrelated state reports.
        if not provider.control_alive:
            self._not_served(
                pat_device_id,
                "not-alive",
                "the appliance's authenticated presence is not online",
            )
            return None
        device_id = self._bridge_device_id(pat_device_id)
        # The bridge addresses appliances by the id its own connection knows them by. Without
        # that pairing there is nothing to send to - not a different appliance to try.
        if device_id is None:
            self._not_served(pat_device_id, "no-pairing", "the bridge's id for this appliance is not resolved")
            return None
        return device_id, provider

    def _not_served(self, pat_device_id: str, cause: str, reason: str) -> None:
        """Say it once per appliance per cause, and name both.

        Keyed on the cause as well as the device because the first refusal is usually the transient
        one - a shadow that has not filled in yet - and keying on the device alone would demote every
        later, permanent cause to a level nobody has turned on. The cause is the capability, never the
        rendered value: every setpoint the bridge declines is a different value and the same cause, so
        keying on the value would put one INFO line on the log per command and grow this set forever.
        """
        seen = (pat_device_id, cause)
        if seen in self._reported_refusal:
            _LOGGER.debug("Rethink Local control not served for %s: %s", pat_device_id, reason)
            return
        self._reported_refusal.add(seen)
        _LOGGER.info(
            "Rethink Local control not served for %s: %s "
            "(repeats of this cause are logged at debug level)",
            pat_device_id,
            reason,
        )

    async def _send(
        self,
        pat_device_id: str,
        device_id: str,
        capability: str,
        value: str,
        *,
        expected_state: Mapping[str, Any] | None = None,
        propagate_retryable: bool = False,
    ) -> LocalCommandResult | None:
        self.ensure_feature_enabled(pat_device_id, capability)
        self.ensure_feature_conditions(pat_device_id, capability, value)
        if self._write_authorized is not None and not self._write_authorized(
            pat_device_id, capability, value
        ):
            seen = (pat_device_id, f"private-eligibility:{capability}")
            if seen not in self._reported_refusal:
                self._reported_refusal.add(seen)
                # Capability ids are public semantic vocabulary. Do not emit a
                # private binding/device id, role, or requested raw value.
                _LOGGER.info(
                    "Rethink Local control capability %s is not authorized for "
                    "this private binding",
                    capability,
                )
            return None
        try:
            return await self._sender.async_send(
                device_id,
                capability,
                value,
                expected_state=expected_state,
            )
        except LocalCommandRetryable as err:
            if propagate_retryable:
                raise
            self._not_served(pat_device_id, capability, f"{capability}={value}: {err}")
            return None
        except LocalCommandUnavailable as err:
            # Every one of these is raised before a frame leaves, so the caller may go to the cloud.
            self._not_served(pat_device_id, capability, f"{capability}={value}: {err}")
            return None

    async def async_set_climate(
        self,
        pat_device_id: str,
        *,
        mode: str | None = None,
        fan: str | None = None,
        target_c: float | None = None,
        comfort_preference: int | None = None,
        retained_target_c: float | None = None,
        retained_comfort_preference: int | None = None,
        now: datetime | None = None,
        cloud_fallback: bool = False,
    ) -> LocalCommandResult | None:
        """Change one of mode, fan or setpoint, keeping the other two as reported."""
        # A request naming no field is not a request. Sent anyway it would compose the
        # appliance's current tuple and write it back - a real frame on the wire that changes
        # nothing, issued because a value could not be expressed rather than because anyone
        # asked for it.
        if (
            mode is None
            and fan is None
            and target_c is None
            and comfort_preference is None
        ):
            return None
        return await self._tuple(
            pat_device_id,
            CLIMATE_TUPLE_CAPABILITY,
            mode,
            fan,
            target_c,
            comfort_preference,
            retained_target_c,
            retained_comfort_preference,
            now,
            cloud_fallback,
        )

    async def async_turn_on(
        self,
        pat_device_id: str,
        *,
        mode: str | None = None,
        target_c: float | None = None,
        comfort_preference: int | None = None,
        retained_target_c: float | None = None,
        retained_comfort_preference: int | None = None,
        now: datetime | None = None,
        cloud_fallback: bool = False,
    ) -> LocalCommandResult | None:
        """Power on by restating the settings the appliance kept while it was off.

        `mode` is for the caller that is turning it on IN a mode: the frame carries mode, fan and
        setpoint whatever happens, so stating the requested one here is one frame where powering on
        and then setting the mode would be two saying the same thing.
        """
        return await self._tuple(
            pat_device_id,
            CLIMATE_POWER_ON_CAPABILITY,
            mode,
            None,
            target_c,
            comfort_preference,
            retained_target_c,
            retained_comfort_preference,
            now,
            cloud_fallback,
        )

    async def _tuple(
        self,
        pat_device_id: str,
        capability: str,
        mode: str | None,
        fan: str | None,
        target_c: float | None,
        comfort_preference: int | None,
        retained_target_c: float | None,
        retained_comfort_preference: int | None,
        now: datetime | None,
        cloud_fallback: bool,
    ) -> LocalCommandResult | None:
        self.ensure_feature_enabled(pat_device_id, capability)
        async with self._tuple_lock(pat_device_id):
            target = self._target(pat_device_id)
            if target is None:
                if cloud_fallback:
                    # A legacy cloud owner will dispatch after this return.
                    # Fence before releasing the device lock so another tuple
                    # cannot compose from pre-cloud state in that gap.
                    self._mark_cloud_tuple_dispatch_locked(pat_device_id)
                return None
            device_id, provider = target
            reported_mode = getattr(
                provider.shadow_fields.get("operation.mode"), "value", None
            )
            required_fields = climate_state_fields(reported_mode)
            if not provider.control_fields_ready(required_fields):
                self._not_served(
                    pat_device_id,
                    "control-state-fields",
                    "not every field of the current climate variant was observed in the authenticated presence epoch",
                )
                if cloud_fallback:
                    self._mark_cloud_tuple_dispatch_locked(pat_device_id)
                return None
            if self._cloud_tuple_barrier_blocks(
                pat_device_id, provider.shadow_fields
            ):
                self._not_served(
                    pat_device_id,
                    "cloud-tuple-awaiting-shadow",
                    "the appliance has not yet reported every tuple field after the cloud command",
                )
                return None
            command_time = now or datetime.now(timezone.utc)
            shadow = self._shadow_with_confirmation(
                pat_device_id, provider.shadow_fields, command_time
            )
            self._remember_mode_argument(pat_device_id, shadow, command_time)
            reported_mode = getattr(shadow.get("operation.mode"), "value", None)
            destination_mode = mode if mode is not None else reported_mode
            remembered_target = (
                None
                if not isinstance(destination_mode, str)
                else self._last_temperature_targets_by_mode.get(
                    pat_device_id, {}
                ).get(
                    destination_mode
                )
            )
            remembered_preference = (
                self._last_comfort_preferences.get(pat_device_id)
                if destination_mode == "auto"
                else None
            )
            try:
                expected_state = climate_expected_state(shadow, command_time)
                value = climate_tuple(
                    shadow,
                    mode=mode,
                    fan=fan,
                    target_c=target_c,
                    comfort_preference=comfort_preference,
                    retained_target_c=(
                        retained_target_c
                        if retained_target_c is not None
                        else remembered_target
                    ),
                    retained_comfort_preference=(
                        retained_comfort_preference
                        if retained_comfort_preference is not None
                        else remembered_preference
                    ),
                    now=command_time,
                )
            except LocalCommandUnavailable as err:
                # Keyed on the message: these causes are structurally different - a shadow that has
                # not filled in, auto mode, the power-fan placeholder - and they do not carry the
                # command's value, so each one gets said once rather than the first hiding the rest.
                self._not_served(pat_device_id, str(err), str(err))
                if cloud_fallback:
                    self._mark_cloud_tuple_dispatch_locked(pat_device_id)
                return None
            outcome = await self._send(
                pat_device_id,
                device_id,
                capability,
                value,
                expected_state=expected_state,
            )
            if outcome is None:
                if cloud_fallback:
                    self._mark_cloud_tuple_dispatch_locked(pat_device_id)
            elif outcome.confirmed:
                confirmed_at = now or datetime.now(timezone.utc)
                confirmed = _confirmed_tuple(value, confirmed_at)
                self._confirmed_tuples[pat_device_id] = confirmed
                confirmed_mode = confirmed.fields.get("operation.mode")
                confirmed_fan = confirmed.fields.get("fan.mode")
                confirmed_target = confirmed.fields.get(
                    TEMPERATURE_TARGET_SEMANTIC
                )
                confirmed_preference = confirmed.fields.get(
                    COMFORT_PREFERENCE_SEMANTIC
                )
                if (
                    confirmed_mode is not None
                    and confirmed_fan is not None
                    and confirmed_fan.value != "power"
                ):
                    if (
                        confirmed_mode.value == "auto"
                        and confirmed_preference is not None
                        and type(confirmed_preference.value) is int
                    ):
                        self._last_comfort_preferences[pat_device_id] = (
                            confirmed_preference.value
                        )
                    elif confirmed_target is not None:
                        self._last_temperature_targets_by_mode.setdefault(
                            pat_device_id, {}
                        )[str(confirmed_mode.value)] = float(confirmed_target.value)
            return outcome

    async def async_turn_off(self, pat_device_id: str) -> LocalCommandResult | None:
        """Power off, which unlike powering on is a write to the power field alone."""
        self.ensure_feature_enabled(pat_device_id, POWER_CAPABILITY)
        target = self._target(pat_device_id)
        if target is None:
            return None
        device_id, _provider = target
        return await self._send(pat_device_id, device_id, POWER_CAPABILITY, "false")

    async def async_appliance_setting_state(self, pat_device_id: str, capability: str) -> bool | str | None:
        target = self._target(pat_device_id)
        if target is None:
            return None
        model = target[1].model_id
        key = (model, capability)
        if key not in APPLIANCE_SETTING_MODELS and key not in APPLIANCE_VALUE_MODELS:
            return None
        return await self._sender.async_appliance_setting_state(target[0], model, capability)

    async def async_air_extra_state(self, pat_device_id: str, capability: str) -> bool | None:
        target = self._target(pat_device_id)
        if target is None or target[1].model_id != 'AIR_910604_WW':
            return None
        return await self._sender.async_air_extra_state(target[0], capability)

    async def async_vacuum_auto_emptying_state(self, pat_device_id: str) -> bool | None:
        target = self._target(pat_device_id)
        if target is None or target[1].model_id != 'HWWA9X3C_F2U':
            return None
        return await self._sender.async_vacuum_auto_emptying_state(target[0])

    async def async_vacuum_reservation_state(self, pat_device_id: str) -> dict | None:
        target = self._target(pat_device_id)
        if target is None or target[1].model_id != 'HWWA9X3C_F2U':
            return None
        return await self._sender.async_vacuum_reservation_state(target[0])

    async def async_set_value(
        self, pat_device_id: str, capability: str, value: str
    ) -> LocalCommandResult | None:
        """Set one exact scalar semantic without composing unrelated state."""
        self.ensure_feature_enabled(pat_device_id, capability)
        target = self._target(pat_device_id)
        if target is None:
            return None
        device_id, _provider = target
        return await self._send(
            pat_device_id, device_id, capability, value
        )

    async def async_set_value_strict(
        self, pat_device_id: str, capability: str, value: str
    ) -> LocalCommandResult | None:
        """Set one Local-only value while surfacing a retryable command fence."""
        self.ensure_feature_enabled(pat_device_id, capability)
        target = self._target(pat_device_id)
        if target is None:
            return None
        device_id, _provider = target
        return await self._send(
            pat_device_id,
            device_id,
            capability,
            value,
            propagate_retryable=True,
        )

    async def async_set_flag(
        self, pat_device_id: str, capability: str, enabled: bool
    ) -> LocalCommandResult | None:
        """One boolean setting, for the capabilities whose frame is a single field.

        The caller names the capability because it is the one that knows which appliance field it
        means; whether this model has a codec for it is the bridge's answer, not a table kept here.
        """
        return await self.async_set_value(
            pat_device_id, capability, "true" if enabled else "false"
        )

    async def async_execute(
        self, pat_device_id: str, capability: str, value: str = "true"
    ) -> LocalCommandResult | None:
        """Execute one named, parameterless capability such as an observed pause frame."""
        return await self.async_set_value(pat_device_id, capability, value)

    async def async_execute_strict(
        self, pat_device_id: str, capability: str, value: str = "true"
    ) -> LocalCommandResult | None:
        """Execute one Local-only action while surfacing a retryable command fence."""
        return await self.async_set_value_strict(pat_device_id, capability, value)
