"""Fail-closed contracts for ThinQ Connect (PAT) controls.

The public ThinQ Connect API encodes controls as nested JSON objects, while
fresh device status is the acknowledgement source.  A command is safe to send
only when every scalar written by the payload has an explicit state echo path.
This module keeps that encode/decode contract pure so it can be tested without
performing a device request.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Any, Mapping

from .value_access import read_path


_MISSING = object()

PatPath = tuple[str, ...]


@dataclass(frozen=True)
class PatStateExpectation:
    """One exact value that must be present in a fresh post-command status."""

    path: PatPath
    value: Any


@dataclass(frozen=True)
class PatStateRequirement:
    """One fresh pre-command state requirement.

    ``allow_missing`` is reserved for capabilities where the official profile
    explicitly makes a field optional.  It must be selected by the caller; a
    missing value is otherwise a hard failure.
    """

    path: PatPath
    allowed_values: tuple[Any, ...]
    message: str
    allow_missing: bool = False


@dataclass(frozen=True)
class PatControlRequest:
    """A PAT payload paired with its complete readback contract."""

    payload: Mapping[str, Any]
    expectations: tuple[PatStateExpectation, ...]
    requirements: tuple[PatStateRequirement, ...] = ()


EchoTarget = PatPath | PatStateExpectation


def _valid_scalar(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _validate_path(path: PatPath, label: str) -> None:
    if (
        not isinstance(path, tuple)
        or not path
        or any(not isinstance(token, str) or not token for token in path)
        or any(token.startswith("@index=") for token in path)
    ):
        raise ValueError(f"invalid PAT {label} path")


def _payload_leaves(
    node: Any, prefix: PatPath = ()
) -> tuple[tuple[PatPath, Any], ...]:
    if isinstance(node, dict):
        if not node:
            raise ValueError("PAT control payload cannot contain an empty object")
        leaves: list[tuple[PatPath, Any]] = []
        for key, value in node.items():
            if not isinstance(key, str) or not key:
                raise ValueError("PAT control payload keys must be non-empty strings")
            leaves.extend(_payload_leaves(value, (*prefix, key)))
        return tuple(leaves)
    if not prefix or not _valid_scalar(node):
        raise ValueError("PAT control payload leaves must be finite JSON scalars")
    return ((prefix, node),)


def build_pat_control_request(
    payload: Mapping[str, Any],
    *,
    echo_contract: Mapping[PatPath, EchoTarget] | None = None,
    requirements: tuple[PatStateRequirement, ...] = (),
) -> PatControlRequest:
    """Build and validate a complete PAT encode/decode contract.

    Without ``echo_contract`` every payload leaf must reappear at the same
    status path.  Mapped controls (for example refrigerator location writes or
    AC mode-specific temperature fields) must provide exactly one mapping for
    every leaf.  No requested field can be silently omitted from verification.
    """
    if not isinstance(payload, Mapping) or not payload:
        raise ValueError("PAT control payload must be a non-empty mapping")
    plain_payload = deepcopy(dict(payload))
    leaves = _payload_leaves(plain_payload)
    leaf_paths = {path for path, _ in leaves}
    if len(leaf_paths) != len(leaves):
        raise ValueError("PAT control payload has duplicate leaf paths")

    if echo_contract is not None:
        if not isinstance(echo_contract, Mapping):
            raise ValueError("PAT echo contract must be a mapping")
        contract_paths = set(echo_contract)
        if contract_paths != leaf_paths:
            raise ValueError(
                "PAT echo contract must cover every payload leaf exactly"
            )

    expectations: list[PatStateExpectation] = []
    for payload_path, payload_value in leaves:
        target: EchoTarget = (
            payload_path
            if echo_contract is None
            else echo_contract[payload_path]
        )
        if isinstance(target, PatStateExpectation):
            expectation = target
        else:
            expectation = PatStateExpectation(target, payload_value)
        _validate_path(expectation.path, "echo")
        if not _valid_scalar(expectation.value):
            raise ValueError("PAT echo values must be finite JSON scalars")
        expectations.append(expectation)

    seen: dict[PatPath, Any] = {}
    for expectation in expectations:
        previous = seen.get(expectation.path, _MISSING)
        if previous is not _MISSING and not pat_values_equal(
            previous, expectation.value
        ):
            raise ValueError("PAT echo path has conflicting expected values")
        seen[expectation.path] = expectation.value

    for requirement in requirements:
        if not isinstance(requirement, PatStateRequirement):
            raise ValueError("PAT preconditions must be PatStateRequirement values")
        _validate_path(requirement.path, "precondition")
        if not requirement.allowed_values or any(
            not _valid_scalar(value) for value in requirement.allowed_values
        ):
            raise ValueError("PAT precondition values must be finite JSON scalars")
        if not isinstance(requirement.message, str) or not requirement.message.strip():
            raise ValueError("PAT precondition message is required")

    # Copy before wrapping so later mutation of a caller-owned payload cannot
    # alter the validated command. The coordinator deep-copies again at send.
    return PatControlRequest(
        payload=MappingProxyType(plain_payload),
        expectations=tuple(expectations),
        requirements=requirements,
    )


def pat_values_equal(actual: Any, expected: Any) -> bool:
    """Compare state values exactly, except JSON integer/float equivalence."""
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if (
        isinstance(actual, (int, float))
        and isinstance(expected, (int, float))
        and math.isfinite(float(actual))
        and math.isfinite(float(expected))
    ):
        return float(actual) == float(expected)
    return type(actual) is type(expected) and actual == expected


def pat_state_verified(
    state: Any, expectations: tuple[PatStateExpectation, ...]
) -> bool:
    """Return whether a fresh state satisfies every exact echo expectation."""
    if not expectations:
        return False
    return all(
        (actual := read_path(state, expectation.path, _MISSING)) is not _MISSING
        and pat_values_equal(actual, expectation.value)
        for expectation in expectations
    )


def failed_pat_requirement(
    state: Any, requirements: tuple[PatStateRequirement, ...]
) -> str | None:
    """Return the first fresh-state precondition failure, if any."""
    for requirement in requirements:
        actual = read_path(state, requirement.path, _MISSING)
        if actual is _MISSING and requirement.allow_missing:
            continue
        if actual is _MISSING or not any(
            pat_values_equal(actual, allowed)
            for allowed in requirement.allowed_values
        ):
            return requirement.message
    return None


def _required_number(state: Any, path: PatPath) -> int | float:
    value = read_path(state, path, _MISSING)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("PAT preservation field is missing or non-numeric")
    return value


def _cooktop_item(state: Any, location: str) -> dict[str, Any] | None:
    if not isinstance(state, list):
        return None
    for item in state:
        if not isinstance(item, dict):
            continue
        nested = item.get("location")
        nested_name = (
            nested.get("locationName") if isinstance(nested, dict) else None
        )
        if item.get("locationName") == location or nested_name == location:
            return item
    return None


def _cooktop_echo_path(
    item: Mapping[str, Any], selector: str, *path: str
) -> PatPath:
    """Address a field with the location selector matching its status shape."""
    first = path[0]
    group = item.get(first)
    if isinstance(group, list):
        return (selector, first, selector, *path[1:])
    return (selector, *path)


def _cooktop_item_value(
    item: Mapping[str, Any], location: str, *path: str
) -> int | float:
    group = item.get(path[0])
    if isinstance(group, list):
        value_path = (path[0], f"@location={location}", *path[1:])
    else:
        value_path = path
    return _required_number(item, value_path)


def build_cooktop_pat_request(
    state: Any,
    location: str,
    *,
    power_level: int | None = None,
    remain_hour: int | None = None,
    remain_minute: int | None = None,
) -> PatControlRequest:
    """Build a complete cooktop read-modify-write contract from fresh state."""
    if not isinstance(location, str) or not location:
        raise ValueError("PAT cooktop location is required")
    selector = f"@location={location}"
    item = _cooktop_item(state, location)
    if item is None:
        raise ValueError("PAT cooktop location is missing from fresh state")
    current_power = _cooktop_item_value(
        item, location, "power", "powerLevel"
    )
    current_hour = _cooktop_item_value(
        item, location, "timer", "remainHour"
    )
    current_minute = _cooktop_item_value(
        item, location, "timer", "remainMinute"
    )
    payload = {
        "power": {
            "powerLevel": int(
                current_power if power_level is None else power_level
            )
        },
        "timer": {
            "remainHour": int(
                current_hour if remain_hour is None else remain_hour
            ),
            "remainMinute": int(
                current_minute if remain_minute is None else remain_minute
            ),
        },
        "location": {"locationName": location},
    }
    contract = build_pat_control_request(
        payload,
        echo_contract={
            ("power", "powerLevel"): _cooktop_echo_path(
                item, selector, "power", "powerLevel"
            ),
            ("timer", "remainHour"): _cooktop_echo_path(
                item, selector, "timer", "remainHour"
            ),
            ("timer", "remainMinute"): _cooktop_echo_path(
                item, selector, "timer", "remainMinute"
            ),
            ("location", "locationName"): (selector, "location", "locationName"),
        },
        requirements=(),
    )
    requirements: list[PatStateRequirement] = []
    if "remoteControlEnable" in item:
        requirements.append(
            PatStateRequirement(
                _cooktop_echo_path(
                    item,
                    selector,
                    "remoteControlEnable",
                    "remoteControlEnabled",
                ),
                (True,),
                "쿡탑 원격 제어가 꺼져 있어 명령을 차단했어요.",
            )
        )
    if "control" in item:
        requirements.append(
            PatStateRequirement(
                _cooktop_echo_path(
                    item, selector, "control", "controlEnabled"
                ),
                (True,),
                "쿡탑이 현재 제어 가능한 상태가 아니어서 명령을 차단했어요.",
            )
        )
    # Presence of at least one authoritative enable gate is required. This is
    # stricter than the UI's availability fallback and prevents a fresh status
    # that omitted both fields from authorizing a hazardous write.
    if not requirements:
        raise ValueError("PAT cooktop control gate is missing from fresh state")
    return PatControlRequest(
        payload=contract.payload,
        expectations=contract.expectations,
        requirements=tuple(requirements),
    )
