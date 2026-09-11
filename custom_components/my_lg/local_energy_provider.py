"""Validated retained cumulative-energy feed from Rethink Local."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Any


class CumulativeEnergyProviderContractError(ValueError):
    """The cumulative-energy publication is not exactly authorized."""


_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SESSION_ID = re.compile(r"^[a-f0-9]{32}$")
_BINDING_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$"
)
_TOPIC_PREFIX = "lg_rethink_local/v1/energy/current"
_CONTRACT_DOMAIN = b"lg-rethink-local/cumulative-energy-feed-contract/v1\0"
_POLICY_REVISION = "cumulative-energy-materialization-v1"

_MODEL_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "2REFO1DBN3K_U": ("energy.total_wh",),
        "3REK2G03VI230D_2": ("energy.total_wh",),
        "WBEF3": ("energy.total_wh",),
        "WMLJ32RS": ("energy.total_wh",),
        "WTL_KPK_BDH_KR_01": (
            "dryer.energy.total_wh",
            "washer.energy.total_wh",
        ),
    }
)

_ENVELOPE_KEYS = frozenset(
    {
        "baseline_generation",
        "binding_generation",
        "binding_id",
        "cursor_generation_before_publish",
        "feed_contract_sha256",
        "fields",
        "last_counted_generation",
        "ledger_ahead_by_generations",
        "ledger_record_sha256",
        "ledger_schema_version",
        "model_id",
        "pat_device_id_proof_sha256",
        "policy_revision",
        "publication_plan_revision",
        "publication_session_id",
        "published_at",
        "schema_version",
        "totals_wh",
    }
)
_FIELD_KEYS = frozenset(
    {"semantic_id", "value_type", "unit", "device_class", "state_class"}
)


def cumulative_energy_model_supported(model_id: str) -> bool:
    """Return whether the producer has a reviewed durable policy."""
    return model_id in _MODEL_FIELDS


def _field_contracts(model_id: str) -> tuple[dict[str, str], ...]:
    try:
        semantic_ids = _MODEL_FIELDS[model_id]
    except KeyError as err:
        raise CumulativeEnergyProviderContractError(
            "Model has no reviewed cumulative-energy policy"
        ) from err
    return tuple(
        {
            "semantic_id": semantic_id,
            "value_type": "number",
            "unit": "Wh",
            "device_class": "energy",
            "state_class": "total_increasing",
        }
        for semantic_id in semantic_ids
    )


def _stable_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _feed_contract_sha256(model_id: str) -> str:
    body = {
        "schema_version": 1,
        "publication_plan_revision": 1,
        "policy_revision": _POLICY_REVISION,
        "model_id": model_id,
        "fields": _field_contracts(model_id),
    }
    return hashlib.sha256(_CONTRACT_DOMAIN + _stable_json(body).encode()).hexdigest()


def _safe_generation(value: object) -> bool:
    return type(value) is int and 0 <= value <= 9_007_199_254_740_991


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return parsed.utcoffset() is not None


class CumulativeEnergyShadowProvider:
    """Hold one exact monotonic retained cumulative-energy publication."""

    def __init__(
        self,
        binding_id: str,
        model_id: str,
        expected_proof: str,
    ) -> None:
        if not isinstance(binding_id, str) or _BINDING_ID.fullmatch(binding_id) is None:
            raise ValueError("Cumulative-energy binding id is invalid")
        if not cumulative_energy_model_supported(model_id):
            raise ValueError("Cumulative-energy model is unsupported")
        if not isinstance(expected_proof, str) or _SHA256.fullmatch(expected_proof) is None:
            raise ValueError("Cumulative-energy identity proof is invalid")
        self.binding_id = binding_id
        self.model_id = model_id
        self.expected_proof = expected_proof
        self.semantic_ids = _MODEL_FIELDS[model_id]
        self.current_topic = f"{_TOPIC_PREFIX}/{binding_id}"
        self.topics = (self.current_topic,)
        self._transport_ready = False
        self._current_valid = False
        self._totals_wh: Mapping[str, int] = MappingProxyType({})
        self._binding_generation: int | None = None
        self._last_counted_generation: int | None = None
        self._baseline_generation: int | None = None
        self._published_at: str | None = None
        self._ledger_record_sha256: str | None = None
        self._listeners: list[Callable[[], None]] = []

    @property
    def transport_ready(self) -> bool:
        return self._transport_ready

    @property
    def last_counted_generation(self) -> int | None:
        return self._last_counted_generation

    @property
    def baseline_generation(self) -> int | None:
        return self._baseline_generation

    @property
    def published_at(self) -> str | None:
        return self._published_at

    @property
    def ledger_record_sha256(self) -> str | None:
        return self._ledger_record_sha256

    def set_transport_ready(self, ready: bool) -> None:
        if type(ready) is not bool:
            raise TypeError("Cumulative-energy transport readiness must be boolean")
        changed = ready != self._transport_ready or (not ready and self._current_valid)
        self._transport_ready = ready
        if not ready:
            self._current_valid = False
        if changed:
            self._notify()

    def field_available(self, semantic_id: str) -> bool:
        return (
            self._transport_ready
            and self._current_valid
            and semantic_id in self._totals_wh
        )

    def reject_current(self) -> None:
        """Make a rejected exact-topic publication visible as unavailable."""
        if self._current_valid:
            self._current_valid = False
            self._notify()

    def total_wh(self, semantic_id: str) -> int | None:
        value = self._totals_wh.get(semantic_id)
        return value if type(value) is int and value >= 0 else None

    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001 - one HA entity cannot stop the feed
                continue

    def ingest(self, topic: str, payload: bytes, *, qos: int, retained: bool) -> None:
        """Validate and adopt one retained or live QoS1 monotonic level."""
        if topic != self.current_topic or qos != 1 or type(retained) is not bool:
            raise CumulativeEnergyProviderContractError(
                "Cumulative-energy MQTT transport is invalid"
            )
        if not payload:
            raise CumulativeEnergyProviderContractError(
                "Cumulative-energy retained level cannot be empty"
            )
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise CumulativeEnergyProviderContractError(
                "Cumulative-energy payload is invalid"
            ) from err
        if not isinstance(value, dict) or set(value) != _ENVELOPE_KEYS:
            raise CumulativeEnergyProviderContractError(
                "Cumulative-energy envelope shape is unsupported"
            )
        fields = value.get("fields")
        expected_fields = _field_contracts(self.model_id)
        if (
            value.get("schema_version") != 1
            or value.get("publication_plan_revision") != 1
            or value.get("policy_revision") != _POLICY_REVISION
            or value.get("ledger_schema_version") != 1
            or value.get("binding_id") != self.binding_id
            or value.get("model_id") != self.model_id
            or value.get("pat_device_id_proof_sha256") != self.expected_proof
            or value.get("feed_contract_sha256")
            != _feed_contract_sha256(self.model_id)
            or fields != list(expected_fields)
            or not all(isinstance(item, dict) and set(item) == _FIELD_KEYS for item in fields or ())
            or not isinstance(value.get("publication_session_id"), str)
            or _SESSION_ID.fullmatch(value["publication_session_id"]) is None
            or not isinstance(value.get("ledger_record_sha256"), str)
            or _SHA256.fullmatch(value["ledger_record_sha256"]) is None
            or not _valid_timestamp(value.get("published_at"))
        ):
            raise CumulativeEnergyProviderContractError(
                "Cumulative-energy contract pins are unauthorized"
            )
        generations = (
            value.get("binding_generation"),
            value.get("baseline_generation"),
            value.get("last_counted_generation"),
            value.get("cursor_generation_before_publish"),
            value.get("ledger_ahead_by_generations"),
        )
        if not all(_safe_generation(item) for item in generations):
            raise CumulativeEnergyProviderContractError(
                "Cumulative-energy generation is invalid"
            )
        binding_generation, baseline, last_counted, cursor, ahead = generations
        if baseline > last_counted or cursor > last_counted or ahead != last_counted - cursor:
            raise CumulativeEnergyProviderContractError(
                "Cumulative-energy generation relation is invalid"
            )
        totals = value.get("totals_wh")
        if (
            not isinstance(totals, dict)
            or set(totals) != set(self.semantic_ids)
            or any(type(item) is not int or item < 0 for item in totals.values())
        ):
            raise CumulativeEnergyProviderContractError(
                "Cumulative-energy totals are invalid"
            )
        if self._binding_generation is not None:
            binding_advanced = binding_generation > self._binding_generation
            baseline_advanced = (
                self._baseline_generation is not None
                and baseline > self._baseline_generation
            )
            if binding_generation < self._binding_generation:
                raise CumulativeEnergyProviderContractError(
                    "Cumulative-energy binding generation regressed"
                )
            if (
                not binding_advanced
                and self._baseline_generation is not None
                and baseline < self._baseline_generation
            ):
                raise CumulativeEnergyProviderContractError(
                    "Cumulative-energy baseline generation regressed"
                )
            if (
                binding_generation == self._binding_generation
                and self._last_counted_generation is not None
                and last_counted < self._last_counted_generation
            ):
                raise CumulativeEnergyProviderContractError(
                    "Cumulative-energy ledger generation regressed"
                )
            if not (binding_advanced or baseline_advanced) and any(
                totals[key] < self._totals_wh.get(key, 0)
                for key in self.semantic_ids
            ):
                raise CumulativeEnergyProviderContractError(
                    "Cumulative-energy total regressed"
                )
            if (
                last_counted == self._last_counted_generation
                and not (binding_advanced or baseline_advanced)
                and self._totals_wh
                and dict(self._totals_wh) != totals
            ):
                raise CumulativeEnergyProviderContractError(
                    "Cumulative-energy same-generation value changed"
                )
        self._binding_generation = binding_generation
        self._baseline_generation = baseline
        self._last_counted_generation = last_counted
        self._published_at = value["published_at"]
        self._ledger_record_sha256 = value["ledger_record_sha256"]
        changed = not self._current_valid or dict(self._totals_wh) != totals
        self._totals_wh = MappingProxyType(dict(totals))
        self._current_valid = True
        if changed:
            self._notify()

    def close(self) -> None:
        self.set_transport_ready(False)
        self._listeners.clear()
