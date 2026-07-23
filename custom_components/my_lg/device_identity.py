"""Stable PAT-to-WideQ device identity resolution.

PAT and WideQ use different device identifiers.  User-facing aliases are useful
for the first match but are not stable enough to key runtime state, controls, or
energy history.  Once a unique alias+model match is observed, persist the two
stable identifiers and use the PAT identifier everywhere in Home Assistant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PatDeviceIdentity:
    """Stable PAT identity plus fields used only for initial matching."""

    device_id: str
    alias: str
    model: str


@dataclass(frozen=True)
class WideqDeviceData:
    """One WideQ device record returned by an account snapshot refresh."""

    device_id: str
    alias: str
    model: str
    snapshot: dict[str, Any]


@dataclass(frozen=True)
class IdentityResolution:
    """Result of resolving WideQ records to stable PAT identifiers."""

    snapshots: dict[str, dict[str, Any]]
    pat_to_wideq: dict[str, str]
    ambiguous_pat_ids: set[str] = field(default_factory=set)
    unmatched_pat_ids: set[str] = field(default_factory=set)


def _signature(alias: str, model: str) -> tuple[str, str]:
    return alias.strip().casefold(), model.strip().casefold()


def resolve_wideq_devices(
    pat_devices: dict[str, PatDeviceIdentity],
    wideq_devices: list[WideqDeviceData],
    existing_map: dict[str, str] | None = None,
) -> IdentityResolution:
    """Resolve WideQ records without ever guessing between duplicate devices.

    A previously persisted stable-ID mapping wins even if the user renamed a
    device.  Unmapped devices are paired only when alias+model is unique on both
    sides.  Ambiguous devices remain unavailable and their controls stay
    blocked rather than risking a command to the wrong appliance.
    """
    wideq_by_id = {device.device_id: device for device in wideq_devices}
    mapping: dict[str, str] = {}
    used_wideq: set[str] = set()
    ambiguous: set[str] = set()

    # Restore only one-to-one persisted mappings for PAT devices that still
    # exist. Keep an offline/missing WideQ id: it may reappear on a later poll.
    # A corrupt store that maps two PAT ids to one WideQ id blocks *both*
    # devices; accepting whichever dict item happened to appear first would
    # make the result order-dependent and could route a command incorrectly.
    persisted_by_wideq: dict[str, list[str]] = {}
    for pat_id, wideq_id in (existing_map or {}).items():
        if pat_id not in pat_devices or not isinstance(wideq_id, str) or not wideq_id:
            continue
        persisted_by_wideq.setdefault(wideq_id, []).append(pat_id)

    for wideq_id, pat_ids in persisted_by_wideq.items():
        if len(pat_ids) != 1:
            ambiguous.update(pat_ids)
            used_wideq.add(wideq_id)
            continue
        mapping[pat_ids[0]] = wideq_id
        used_wideq.add(wideq_id)

    pat_by_signature: dict[tuple[str, str], list[str]] = {}
    for pat_id, identity in pat_devices.items():
        if pat_id in mapping or pat_id in ambiguous:
            continue
        pat_by_signature.setdefault(
            _signature(identity.alias, identity.model), []
        ).append(pat_id)

    wideq_by_signature: dict[tuple[str, str], list[str]] = {}
    for device in wideq_devices:
        if device.device_id in used_wideq:
            continue
        wideq_by_signature.setdefault(
            _signature(device.alias, device.model), []
        ).append(device.device_id)

    for signature, pat_ids in pat_by_signature.items():
        wideq_ids = wideq_by_signature.get(signature, [])
        if len(pat_ids) == 1 and len(wideq_ids) == 1:
            mapping[pat_ids[0]] = wideq_ids[0]
            used_wideq.add(wideq_ids[0])
        elif wideq_ids:
            ambiguous.update(pat_ids)

    snapshots: dict[str, dict[str, Any]] = {}
    for pat_id, wideq_id in mapping.items():
        device = wideq_by_id.get(wideq_id)
        if device is not None and device.snapshot:
            snapshots[pat_id] = device.snapshot

    return IdentityResolution(
        snapshots=snapshots,
        pat_to_wideq=mapping,
        ambiguous_pat_ids=ambiguous,
        unmatched_pat_ids=set(pat_devices) - set(mapping) - ambiguous,
    )
