"""Editable SQLite inventory for Local read and control features.

The database contains model-level feature definitions only. Appliance identity,
credentials, packet captures and runtime state remain outside this database.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

DATABASE_NAME = "my_lg_features.sqlite3"
APPLICATION_ID = 0x4C474646  # LGFF
SCHEMA_VERSION = 2  # SQLite layout, not a device or publication generation.
CHANNELS = frozenset({
    "full-read", "pilot-read", "pilot-profile", "control-entity", "confirmed-control"
})

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS features (
    channel TEXT NOT NULL CHECK (channel IN (
        'full-read', 'pilot-read', 'pilot-profile', 'control-entity', 'confirmed-control'
    )),
    model_id TEXT NOT NULL,
    profile_id TEXT NOT NULL DEFAULT '',
    feature_id TEXT NOT NULL,
    platform TEXT NOT NULL CHECK (platform IN ('thinq1', 'thinq2', '')),
    definition_json TEXT NOT NULL CHECK (json_valid(definition_json)),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    producer_registered INTEGER NOT NULL DEFAULT 0 CHECK (producer_registered IN (0, 1)),
    PRIMARY KEY (channel, model_id, profile_id, feature_id)
);
CREATE INDEX IF NOT EXISTS features_enabled_model
    ON features (channel, model_id, enabled);
CREATE TABLE IF NOT EXISTS model_rollout (
    model_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1))
);
CREATE TABLE IF NOT EXISTS feature_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    channel TEXT NOT NULL,
    model_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    feature_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('enable', 'disable', 'upsert'))
);
"""


def default_database_path() -> Path:
    """Both HA and the host producer can address this one mounted file."""
    return Path(__file__).resolve().parents[2] / DATABASE_NAME


def _read_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError(f"Local feature database is missing: {path}")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        connection.close()
        raise ValueError("Local feature database identity is invalid")
    if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        connection.close()
        raise ValueError("Local feature database layout is unsupported")
    return connection


def load_features(
    path: Path, channel: str, *, include_disabled: bool = False
) -> tuple[Mapping[str, Any], ...]:
    """Read current definitions; a feature edit never changes a global pin."""
    if channel not in CHANNELS:
        raise ValueError("Unknown Local feature channel")
    with closing(_read_connection(path)) as connection:
        rows = connection.execute(
            "SELECT model_id, profile_id, feature_id, platform, definition_json, "
            "enabled FROM features WHERE channel = ? AND (? OR enabled = 1) "
            "ORDER BY model_id, profile_id, feature_id",
            (channel, int(include_disabled)),
        ).fetchall()
    features: list[Mapping[str, Any]] = []
    for row in rows:
        definition = json.loads(row["definition_json"])
        if not isinstance(definition, dict):
            raise ValueError("Local feature definition is not an object")
        features.append(
            {
                "model_id": row["model_id"],
                "profile_id": row["profile_id"],
                "feature_id": row["feature_id"],
                "platform": row["platform"],
                "enabled": bool(row["enabled"]),
                "definition": definition,
            }
        )
    return tuple(features)


def enabled_models(path: Path) -> frozenset[str]:
    """Return models explicitly piloted onto the editable database."""
    with closing(_read_connection(path)) as connection:
        rows = connection.execute(
            "SELECT model_id FROM model_rollout WHERE enabled = 1 ORDER BY model_id"
        ).fetchall()
    return frozenset(row["model_id"] for row in rows)


def all_models_enabled(path: Path) -> bool:
    """True after the last model has been explicitly moved to the database."""
    with closing(_read_connection(path)) as connection:
        total, enabled = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(enabled), 0) FROM model_rollout"
        ).fetchone()
    return total > 0 and total == enabled


def feature_change_sequence(path: Path) -> int:
    """A committed edit counter for the HA reload watcher, not a contract pin."""
    with closing(_read_connection(path)) as connection:
        return int(connection.execute(
            "SELECT COALESCE(MAX(id), 0) FROM feature_changes"
        ).fetchone()[0])


def create_database(
    path: Path, definitions: Iterable[Mapping[str, Any]]
) -> dict[str, int]:
    """One-time import into a new file; never overwrite an operating database."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    os.close(descriptor)
    try:
        connection = sqlite3.connect(path)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    try:
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.executescript(SCHEMA)
        counts: dict[str, int] = {}
        models: set[str] = set()
        with connection:
            for row in definitions:
                channel = str(row["channel"])
                if channel not in CHANNELS:
                    raise ValueError("Unknown Local feature channel")
                definition = row["definition"]
                if not isinstance(definition, dict):
                    raise ValueError("Local feature definition is not an object")
                connection.execute(
                    "INSERT INTO features (channel, model_id, profile_id, "
                    "feature_id, platform, definition_json, enabled, producer_registered) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        channel,
                        str(row["model_id"]),
                        str(row.get("profile_id", "")),
                        str(row["feature_id"]),
                        str(row.get("platform", "")),
                        json.dumps(definition, ensure_ascii=False, sort_keys=True),
                        int(row.get("enabled", True)),
                        int(row.get("producer_registered", False)),
                    ),
                )
                counts[channel] = counts.get(channel, 0) + 1
                models.add(str(row["model_id"]))
            connection.executemany(
                "INSERT INTO model_rollout (model_id, enabled) VALUES (?, 0)",
                ((model_id,) for model_id in sorted(models)),
            )
        return counts
    except BaseException:
        connection.close()
        path.unlink(missing_ok=True)
        raise
    finally:
        connection.close()


def set_feature_enabled(
    path: Path,
    channel: str,
    model_id: str,
    profile_id: str,
    feature_id: str,
    enabled: bool,
) -> None:
    """Add/remove an HA feature without deleting history or changing code."""
    if type(enabled) is not bool:
        raise TypeError("Local feature enabled flag must be boolean")
    if channel == "pilot-profile" and not enabled:
        raise ValueError("Disable pilot fields or the model rollout, not its presence profile")
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError(f"Local feature database is missing: {path}")
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True)) as connection:
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise ValueError("Local feature database identity is invalid")
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError("Local feature database layout is unsupported")
        with connection:
            cursor = connection.execute(
                "UPDATE features SET enabled = ? WHERE channel = ? AND model_id = ? "
                "AND profile_id = ? AND feature_id = ?",
                (int(enabled), channel, model_id, profile_id, feature_id),
            )
            if cursor.rowcount != 1:
                raise KeyError("Local feature does not exist")
            connection.execute(
                "INSERT INTO feature_changes "
                "(channel, model_id, profile_id, feature_id, action) "
                "VALUES (?, ?, ?, ?, ?)",
                (channel, model_id, profile_id, feature_id,
                 "enable" if enabled else "disable"),
            )


def set_model_rollout(path: Path, model_id: str, enabled: bool) -> None:
    """Explicit one-model cutover; new databases start in legacy mode."""
    if type(enabled) is not bool or not isinstance(model_id, str) or not model_id:
        raise ValueError("Local feature rollout target is invalid")
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError(f"Local feature database is missing: {path}")
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True)) as connection:
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise ValueError("Local feature database identity is invalid")
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError("Local feature database layout is unsupported")
        with connection:
            cursor = connection.execute(
                "UPDATE model_rollout SET enabled = ? WHERE model_id = ?",
                (int(enabled), model_id),
            )
            if cursor.rowcount != 1:
                raise KeyError("Local model does not exist in the feature database")
            connection.execute(
                "INSERT INTO feature_changes "
                "(channel, model_id, profile_id, feature_id, action) "
                "VALUES ('model-rollout', ?, '', 'model', ?)",
                (model_id, "enable" if enabled else "disable"),
            )


def upsert_feature(
    path: Path,
    channel: str,
    model_id: str,
    profile_id: str,
    feature_id: str,
    platform: str,
    definition: Mapping[str, Any],
    *,
    enabled: bool = True,
    register_producer: bool = False,
) -> None:
    """Add or change one feature, atomically, without regenerating contracts."""
    if channel not in CHANNELS or platform not in ("thinq1", "thinq2", ""):
        raise ValueError("Local feature channel or platform is invalid")
    if not all(isinstance(value, str) and value for value in (model_id, feature_id)):
        raise ValueError("Local feature identity is invalid")
    if (not isinstance(definition, Mapping) or type(enabled) is not bool
            or type(register_producer) is not bool):
        raise TypeError("Local feature definition is invalid")
    if register_producer and channel != "full-read":
        raise ValueError("Only full-read features can be registered with the producer")
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError(f"Local feature database is missing: {path}")
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True)) as connection:
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise ValueError("Local feature database identity is invalid")
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError("Local feature database layout is unsupported")
        with connection:
            # A newly declared model stays on the released path until it is
            # explicitly piloted; no separate inventory regeneration is needed.
            connection.execute(
                "INSERT OR IGNORE INTO model_rollout (model_id, enabled) VALUES (?, 0)",
                (model_id,),
            )
            connection.execute(
                "INSERT INTO features (channel, model_id, profile_id, feature_id, "
                "platform, definition_json, enabled, producer_registered) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (channel, model_id, profile_id, feature_id) DO UPDATE SET "
                "platform = excluded.platform, definition_json = excluded.definition_json, "
                "enabled = excluded.enabled, "
                "producer_registered = MAX(features.producer_registered, "
                "excluded.producer_registered)",
                (channel, model_id, profile_id, feature_id, platform,
                 json.dumps(dict(definition), ensure_ascii=False, sort_keys=True),
                 int(enabled), int(register_producer)),
            )
            connection.execute(
                "INSERT INTO feature_changes "
                "(channel, model_id, profile_id, feature_id, action) "
                "VALUES (?, ?, ?, ?, 'upsert')",
                (channel, model_id, profile_id, feature_id),
            )
