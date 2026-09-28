#!/usr/bin/env python3
"""Atomically install the two reviewed HA read-display files with a backup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path

FILES = ("local_read_provider.py", "local_entity.py")
LIVE = Path("/home/selian/homeassistant/config/custom_components/my_lg")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def docker(*args: str, check: bool = True) -> str:
    return subprocess.run(
        ["docker", *args], check=check, capture_output=True, text=True, timeout=120
    ).stdout.strip()


def write_once(path: Path, data: bytes, mode: int, uid: int, gid: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "wb") as stream:
        os.fchown(stream.fileno(), uid, gid)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def replace(path: Path, data: bytes, metadata: os.stat_result) -> None:
    temporary = path.with_name(f".{path.name}.display-fallback-{os.getpid()}")
    write_once(temporary, data, stat.S_IMODE(metadata.st_mode), metadata.st_uid, metadata.st_gid)
    try:
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-dir", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--expected-read-sha256", required=True)
    parser.add_argument("--expected-entity-sha256", required=True)
    parser.add_argument("--target-read-sha256", required=True)
    parser.add_argument("--target-entity-sha256", required=True)
    parser.add_argument("--expected-mqtt-sha256")
    parser.add_argument("--target-mqtt-sha256")
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    if bool(args.expected_mqtt_sha256) != bool(args.target_mqtt_sha256):
        raise RuntimeError("MQTT candidate requires both exact digests")
    files = FILES + (("local_mqtt.py",) if args.expected_mqtt_sha256 else ())
    if os.geteuid() != 0 or args.backup_dir.parent != Path("/root") or args.backup_dir.exists():
        raise RuntimeError("root and a new exact /root backup directory are required")
    expected = dict(zip(files, (args.expected_read_sha256, args.expected_entity_sha256, args.expected_mqtt_sha256)))
    target = dict(zip(files, (args.target_read_sha256, args.target_entity_sha256, args.target_mqtt_sha256)))
    if any(not SHA256.fullmatch(value) for value in (*expected.values(), *target.values())):
        raise RuntimeError("file digests are invalid")
    if docker("inspect", "homeassistant", "--format", "{{.State.Running}}") != "true":
        raise RuntimeError("Home Assistant is not running before deployment")
    original: dict[str, bytes] = {}
    candidates: dict[str, bytes] = {}
    metadata: dict[str, os.stat_result] = {}
    for name in files:
        path = LIVE / name
        candidate = args.candidate_dir / name
        meta, candidate_meta = path.stat(follow_symlinks=False), candidate.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1
            or not stat.S_ISREG(candidate_meta.st_mode) or candidate_meta.st_nlink != 1
        ):
            raise RuntimeError(f"{name} is not an exact regular file")
        original[name], candidates[name], metadata[name] = path.read_bytes(), candidate.read_bytes(), meta
        if digest(original[name]) != expected[name] or digest(candidates[name]) != target[name]:
            raise RuntimeError(f"{name} changed from the reviewed bytes")
    if args.preflight_only:
        print(json.dumps({"preflight": "passed", "files": list(files)}))
        return
    args.backup_dir.mkdir(mode=0o700)
    for name in files:
        write_once(args.backup_dir / name, original[name], 0o600, 0, 0)
    docker("stop", "-t", "60", "homeassistant")
    if docker("inspect", "homeassistant", "--format", "{{.State.Running}}") != "false":
        raise RuntimeError("Home Assistant did not stop; no code was changed")
    for name in files:
        replace(LIVE / name, candidates[name], metadata[name])
    docker("start", "homeassistant")
    if docker("inspect", "homeassistant", "--format", "{{.State.Running}}") != "true":
        raise RuntimeError("Home Assistant did not start; backup retained for recovery")
    for name in files:
        if digest((LIVE / name).read_bytes()) != target[name]:
            raise RuntimeError(f"{name} differs after installation")
    print(json.dumps({
        "status": "started",
        "backup_directory": str(args.backup_dir),
        "installed": target,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
