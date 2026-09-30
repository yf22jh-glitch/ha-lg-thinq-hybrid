#!/usr/bin/env python3
"""Attach existing DB read/control definitions to a trusted hot-reload module.

Create new definitions with manage_local_features.py upsert first. This only
links model-level metadata, preserves enabled/delete flags and never sends a
command, reloads HA or changes a binding/energy store.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path


def link(path: Path, model: str, feature: str, module: str) -> int:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}\.mjs', module):
        raise ValueError('Handler must be a single .mjs filename')
    with sqlite3.connect(path) as connection:
        if connection.execute('PRAGMA application_id').fetchone()[0] != 0x4C474646 or connection.execute('PRAGMA user_version').fetchone()[0] != 2:
            raise ValueError('Unsupported feature database layout')
        connection.execute('BEGIN IMMEDIATE')
        rows = connection.execute(
            "SELECT channel,profile_id,definition_json FROM features WHERE model_id=? AND feature_id=? AND channel IN ('full-read','control-entity')",
            (model, feature),
        ).fetchall()
        if not rows:
            raise ValueError('Create the feature definitions before linking a handler')
        for channel, profile, encoded in rows:
            definition = json.loads(encoded)
            definition['runtimeHandler'] = module
            connection.execute(
                'UPDATE features SET definition_json=?,producer_registered=CASE WHEN channel=\'full-read\' THEN 1 ELSE producer_registered END WHERE channel=? AND model_id=? AND profile_id=? AND feature_id=?',
                (json.dumps(definition, ensure_ascii=False, separators=(',', ':')), channel, model, profile, feature),
            )
            connection.execute(
                "INSERT INTO feature_changes (channel,model_id,profile_id,feature_id,action) VALUES (?,?,?,?,'upsert')",
                (channel, model, profile, feature),
            )
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--feature', required=True)
    parser.add_argument('--module', required=True)
    args = parser.parse_args()
    print(json.dumps({'linked_definitions': link(args.db, args.model, args.feature, args.module)}))


if __name__ == '__main__':
    main()
