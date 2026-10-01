"""Evaluate editable appliance conditions. No release pins or guessed state.

One DB row per model/function is shared with the bridge. There is no duplicate
policy in the entity classes. Conditions only read own local state; they never
evaluate code, make a network request, or modify appliance state.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

INVALID = "기능 DB의 실행 조건 형식을 확인해 주세요."
UNAVAILABLE = "현재 기기 상태에서는 이 기능을 사용할 수 없어요."
_SEMANTIC = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\Z")


def _primitive(value: Any) -> bool:
    return type(value) in (str, bool) or type(value) in (int, float) and math.isfinite(value)


def _same(left: Any, right: Any) -> bool:
    if type(left) is bool or type(right) is bool:
        return type(left) is type(right) and left == right
    return _primitive(left) and _primitive(right) and left == right


def _valid_group(group: Any) -> bool:
    return (isinstance(group, dict) and not group.keys() - {"all", "reasonKo"}
            and ("reasonKo" not in group or isinstance(group["reasonKo"], str))
            and ("all" not in group or isinstance(group["all"], list) and all(
                isinstance(clause, dict) and not clause.keys() - {"semanticId", "values"}
                and isinstance(clause.get("semanticId"), str)
                and _SEMANTIC.fullmatch(clause["semanticId"])
                and isinstance(clause.get("values"), list) and bool(clause["values"])
                and all(_primitive(value) for value in clause["values"])
                for clause in group["all"])))


def evaluate_condition(policy: Any, state: Mapping[str, Any], value: str | None = None) -> tuple[bool, str | None]:
    """None means no policy/legacy behavior; malformed policy is per-function."""
    if policy is None:
        return True, None
    if not isinstance(policy, dict) or policy.keys() - {"all", "byValue", "reasonKo"}:
        return False, INVALID
    base = {key: item for key, item in policy.items() if key != "byValue"}
    choices = policy.get("byValue", {})
    if not _valid_group(base) or not isinstance(choices, dict) or not all(_valid_group(group) for group in choices.values()):
        return False, INVALID
    groups = [base]
    if value is not None and value in choices:
        groups.append(choices[value])
    for group in groups:
        if not all(any(_same(state.get(clause["semanticId"]), candidate) for candidate in clause["values"])
                   for clause in group.get("all", [])):
            return False, group.get("reasonKo") or base.get("reasonKo") or UNAVAILABLE
    return True, None


def load_control_conditions(path: Path) -> dict[tuple[str, str], Any]:
    """Old DBs without the optional table are unchanged. Invalid JSON is denied."""
    from .feature_database import _read_connection

    with closing(_read_connection(path)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='control_conditions'").fetchone():
            return {}
        rows = connection.execute(
            "SELECT c.model_id,c.capability_id,c.condition_json FROM control_conditions c "
            "JOIN model_rollout m ON m.model_id=c.model_id AND m.enabled=1"
        ).fetchall()
    policies = {}
    for model, capability, encoded in rows:
        try:
            policy = json.loads(encoded)
            # Stored null is a malformed policy, not absence of a DB row.
            if policy is None:
                policy = {"invalid": True}
        except (ValueError, TypeError):
            policy = {"invalid": True}
        policies[(model, capability)] = policy
    return policies
