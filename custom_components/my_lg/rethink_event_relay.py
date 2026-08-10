"""Sanitized ThinQ lifecycle-event relay for the local Rethink service."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import re
from typing import Any, Mapping, Optional


CONF_RETHINK_EVENT_TOKEN = "rethink_event_token"
RETHINK_EVENT_ENDPOINT = "http://127.0.0.1:44401/cloud/device-events"
MIN_TOKEN_LENGTH = 32
MAX_TOKEN_LENGTH = 512
RETHINK_EVENT_TIMEOUT = 5

_LOGGER = logging.getLogger(__name__)
_FIELD_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
_ACCOUNT_PUSH_TYPES = {
    "DEVICE_DISCOVERY",
    "DEVICE_LIFECYCLE",
    "DEVICE_LIST_CHANGED",
    "DEVICE_CHANGED",
}
_REGISTERED_TYPES = {
    "ADD",
    "ADDED",
    "REGISTER",
    "REGISTERED",
    "DEVICE_ADD",
    "DEVICE_ADDED",
    "DEVICE_REGISTER",
    "DEVICE_REGISTERED",
}
_UNREGISTERED_TYPES = {
    "DELETE",
    "DELETED",
    "REMOVE",
    "REMOVED",
    "UNREGISTER",
    "UNREGISTERED",
    "DEVICE_DELETE",
    "DEVICE_DELETED",
    "DEVICE_REMOVE",
    "DEVICE_REMOVED",
    "DEVICE_UNREGISTER",
    "DEVICE_UNREGISTERED",
}
_ALIAS_UPDATED_TYPES = {
    "ALIAS_CHANGE",
    "ALIAS_CHANGED",
    "ALIAS_UPDATE",
    "ALIAS_UPDATED",
    "DEVICE_ALIAS_CHANGE",
    "DEVICE_ALIAS_CHANGED",
    "DEVICE_ALIAS_UPDATE",
    "DEVICE_ALIAS_UPDATED",
    "DEVICE_NICKNAME_CHANGE",
    "DEVICE_NICKNAME_CHANGED",
}
_ALLOWED_KINDS = {
    "subscription_ready",
    "registered",
    "unregistered",
    "alias_updated",
    "unknown",
}
_TYPE_FIELDS = ("eventType", "action", "type", "event", "pushCode", "pushType")
_DEVICE_CONTAINERS = ("data", "device", "deviceInfo", "report")


def _event_token(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", value.strip()).strip("_").upper()
    return normalized or None


def _event_kind(tokens: list[str]) -> Optional[str]:
    for token in tokens:
        if token in _UNREGISTERED_TYPES or (
            "DEVICE" in token and "UNREGISTER" in token
        ):
            return "unregistered"
        if token in _ALIAS_UPDATED_TYPES or (
            ("ALIAS" in token or "NICKNAME" in token)
            and ("UPDATE" in token or "CHANGE" in token)
        ):
            return "alias_updated"
        if token in _REGISTERED_TYPES or (
            "DEVICE" in token
            and "REGISTER" in token
            and "UNREGISTER" not in token
        ):
            return "registered"
    return None


def _safe_device_id(message: Mapping[str, Any]) -> Optional[str]:
    candidates = [message.get("deviceId"), message.get("deviceID")]
    for container_name in _DEVICE_CONTAINERS:
        container = message.get(container_name)
        if isinstance(container, Mapping):
            candidates.extend((container.get("deviceId"), container.get("deviceID")))
    for value in candidates:
        if (
            isinstance(value, str)
            and 0 < len(value) <= 256
            and _CONTROL_CHARACTER.search(value) is None
        ):
            return value
    return None


def normalize_lifecycle_event(message: Any) -> Optional[dict[str, Any]]:
    """Reduce one account-level PAT push to the metadata accepted by Rethink.

    Raw reports, aliases, MAC addresses, credentials, and other payload values are
    deliberately discarded. Unknown account-event shapes remain diagnostic and
    do not trigger an LG Home lookup on the Rethink side.
    """
    if not isinstance(message, Mapping):
        return None

    push_type = _event_token(message.get("pushType"))
    if push_type == "DEVICE_STATUS":
        return None

    tokens = [
        token
        for field in _TYPE_FIELDS
        if (token := _event_token(message.get(field))) is not None
    ]
    for container_name in _DEVICE_CONTAINERS:
        container = message.get(container_name)
        if not isinstance(container, Mapping):
            continue
        tokens.extend(
            token
            for field in _TYPE_FIELDS
            if (token := _event_token(container.get(field))) is not None
        )

    kind = _event_kind(tokens)
    if push_type == "DEVICE_PUSH" and kind is None:
        return None
    if kind is None and not any(token in _ACCOUNT_PUSH_TYPES for token in tokens):
        return None

    event: dict[str, Any] = {
        "kind": kind or "unknown",
        "payloadKeys": sorted(
            str(key)
            for key in message
            if isinstance(key, str) and _FIELD_NAME.fullmatch(key)
        )[:32],
    }
    device_id = _safe_device_id(message)
    if device_id is not None:
        event["deviceId"] = device_id
    return event


class RethinkEventRelay:
    """Send sanitized lifecycle metadata to the loopback-only Rethink API."""

    def __init__(self, session: Any, token: Any) -> None:
        self._session = session
        self._token = token.strip() if isinstance(token, str) else ""

    @property
    def enabled(self) -> bool:
        return (
            MIN_TOKEN_LENGTH <= len(self._token) <= MAX_TOKEN_LENGTH
            and "\r" not in self._token
            and "\n" not in self._token
        )

    async def async_send(self, event: Mapping[str, Any]) -> bool:
        if not self.enabled:
            return False
        kind = event.get("kind")
        if kind not in _ALLOWED_KINDS:
            return False

        payload: dict[str, Any] = {
            "kind": kind,
            "receivedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        device_id = event.get("deviceId")
        if (
            isinstance(device_id, str)
            and 0 < len(device_id) <= 256
            and _CONTROL_CHARACTER.search(device_id) is None
        ):
            payload["deviceId"] = device_id
        payload_keys = event.get("payloadKeys")
        if isinstance(payload_keys, list):
            payload["payloadKeys"] = [
                key
                for key in payload_keys
                if isinstance(key, str) and _FIELD_NAME.fullmatch(key)
            ][:32]

        try:
            async with self._session.post(
                RETHINK_EVENT_ENDPOINT,
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=RETHINK_EVENT_TIMEOUT,
            ) as response:
                accepted = 200 <= response.status < 300
                if not accepted:
                    _LOGGER.warning(
                        "Rethink lifecycle relay rejected metadata (HTTP %d)",
                        response.status,
                    )
                return accepted
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Rethink lifecycle relay failed without retry (%s)",
                type(err).__name__,
            )
            return False
