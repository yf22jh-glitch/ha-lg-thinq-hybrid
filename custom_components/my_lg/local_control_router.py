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
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any, Callable, Mapping, Protocol

from .local_command import (
    CLIMATE_POWER_ON_CAPABILITY,
    CLIMATE_TUPLE_CAPABILITY,
    MAX_SHADOW_AGE,
    POWER_CAPABILITY,
    LocalCommandResult,
    LocalCommandUnavailable,
    climate_tuple,
)

_LOGGER = logging.getLogger(__name__)

_CLIMATE_TUPLE_FIELDS = (
    "operation.mode",
    "fan.mode",
    "temperature.target_c",
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
    mode, fan, rendered_temperature = value.split("|", 2)
    if not rendered_temperature.endswith("C"):
        raise ValueError("confirmed climate tuple has no temperature suffix")
    temperature = float(rendered_temperature[:-1])
    normalized_temperature: int | float = int(temperature) if temperature.is_integer() else temperature
    return _ConfirmedTuple(
        fields={
            "operation.mode": _ConfirmedField(mode, confirmed_at),
            "fan.mode": _ConfirmedField(fan, confirmed_at),
            "temperature.target_c": _ConfirmedField(normalized_temperature, confirmed_at),
        },
        confirmed_at=confirmed_at,
    )


class _Sender(Protocol):
    async def async_send(self, device_id: str, capability: str, value: str) -> LocalCommandResult: ...


class _Shadow(Protocol):
    @property
    def shadow_fields(self) -> Mapping[str, Any]: ...

    @property
    def shadow_healthy(self) -> bool: ...


class LocalControlRouter:
    """One appliance's local control path, or nothing."""

    def __init__(
        self,
        sender: _Sender,
        providers: Mapping[str, _Shadow],
        bridge_device_id: Callable[[str], str | None],
    ) -> None:
        self._sender = sender
        self._providers = providers
        self._bridge_device_id = bridge_device_id
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
        self._tuple_locks: dict[str, asyncio.Lock] = {}

    def _tuple_lock(self, pat_device_id: str) -> asyncio.Lock:
        lock = self._tuple_locks.get(pat_device_id)
        if lock is None:
            lock = asyncio.Lock()
            self._tuple_locks[pat_device_id] = lock
        return lock

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
        for field_name in _CLIMATE_TUPLE_FIELDS:
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
        for field_name in _CLIMATE_TUPLE_FIELDS:
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
        # An unhealthy shadow is one whose readings this project would not act on; a command
        # built from those readings would be a guess wearing the appliance's own numbers.
        if not provider.shadow_healthy:
            self._not_served(pat_device_id, "unhealthy", "the local shadow is not currently healthy")
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
            "Rethink Local control not served for %s, using the cloud: %s "
            "(repeats of this cause are logged at debug level)",
            pat_device_id,
            reason,
        )

    async def _send(
        self, pat_device_id: str, device_id: str, capability: str, value: str
    ) -> LocalCommandResult | None:
        try:
            return await self._sender.async_send(device_id, capability, value)
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
        now: datetime | None = None,
    ) -> LocalCommandResult | None:
        """Change one of mode, fan or setpoint, keeping the other two as reported."""
        # A request naming no field is not a request. Sent anyway it would compose the
        # appliance's current tuple and write it back - a real frame on the wire that changes
        # nothing, issued because a value could not be expressed rather than because anyone
        # asked for it.
        if mode is None and fan is None and target_c is None:
            return None
        return await self._tuple(pat_device_id, CLIMATE_TUPLE_CAPABILITY, mode, fan, target_c, now)

    async def async_turn_on(
        self,
        pat_device_id: str,
        *,
        mode: str | None = None,
        now: datetime | None = None,
    ) -> LocalCommandResult | None:
        """Power on by restating the settings the appliance kept while it was off.

        `mode` is for the caller that is turning it on IN a mode: the frame carries mode, fan and
        setpoint whatever happens, so stating the requested one here is one frame where powering on
        and then setting the mode would be two saying the same thing.
        """
        return await self._tuple(pat_device_id, CLIMATE_POWER_ON_CAPABILITY, mode, None, None, now)

    async def _tuple(
        self,
        pat_device_id: str,
        capability: str,
        mode: str | None,
        fan: str | None,
        target_c: float | None,
        now: datetime | None,
    ) -> LocalCommandResult | None:
        async with self._tuple_lock(pat_device_id):
            target = self._target(pat_device_id)
            if target is None:
                # `None` is the router's promise that cloud fallback is safe. Arm the fence before
                # releasing the per-device lock so a concurrent request cannot compose from the
                # pre-cloud tuple in the gap before the caller dispatches it.
                self._mark_cloud_tuple_dispatch_locked(pat_device_id)
                return None
            device_id, provider = target
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
            try:
                value = climate_tuple(
                    shadow, mode=mode, fan=fan, target_c=target_c, now=command_time
                )
            except LocalCommandUnavailable as err:
                # Keyed on the message: these causes are structurally different - a shadow that has
                # not filled in, auto mode, the power-fan placeholder - and they do not carry the
                # command's value, so each one gets said once rather than the first hiding the rest.
                self._not_served(pat_device_id, str(err), str(err))
                self._mark_cloud_tuple_dispatch_locked(pat_device_id)
                return None
            outcome = await self._send(pat_device_id, device_id, capability, value)
            if outcome is None:
                self._mark_cloud_tuple_dispatch_locked(pat_device_id)
            elif outcome.confirmed:
                confirmed_at = now or datetime.now(timezone.utc)
                self._confirmed_tuples[pat_device_id] = _confirmed_tuple(
                    value, confirmed_at
                )
            return outcome

    async def async_turn_off(self, pat_device_id: str) -> LocalCommandResult | None:
        """Power off, which unlike powering on is a write to the power field alone."""
        target = self._target(pat_device_id)
        if target is None:
            return None
        device_id, _provider = target
        return await self._send(pat_device_id, device_id, POWER_CAPABILITY, "false")

    async def async_set_flag(self, pat_device_id: str, capability: str, enabled: bool) -> LocalCommandResult | None:
        """One boolean setting, for the capabilities whose frame is a single field.

        The caller names the capability because it is the one that knows which appliance field it
        means; whether this model has a codec for it is the bridge's answer, not a table kept here.
        """
        target = self._target(pat_device_id)
        if target is None:
            return None
        device_id, _provider = target
        return await self._send(pat_device_id, device_id, capability, "true" if enabled else "false")

    async def async_execute(
        self, pat_device_id: str, capability: str, value: str = "true"
    ) -> LocalCommandResult | None:
        """Execute one named, parameterless capability such as an observed pause frame."""
        target = self._target(pat_device_id)
        if target is None:
            return None
        device_id, _provider = target
        return await self._send(pat_device_id, device_id, capability, value)
