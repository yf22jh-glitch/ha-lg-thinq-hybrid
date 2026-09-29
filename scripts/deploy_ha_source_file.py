#!/usr/bin/env python3
"""Install one reviewed my_lg source file with a recoverable byte-for-byte backup."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import subprocess
from pathlib import Path


LIVE_DIR = Path("/home/selian/homeassistant/config/custom_components/my_lg")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=120
    ).stdout.strip()


def regular_file(path: Path) -> os.stat_result:
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RuntimeError(f"not an exact regular file: {path.name}")
    return metadata


def write_once(path: Path, data: bytes, mode: int, uid: int, gid: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "wb") as stream:
        os.fchown(stream.fileno(), uid, gid)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--filename", required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--expected-current-sha256", required=True)
    parser.add_argument("--expected-candidate-sha256", required=True)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("root required for a stopped-container source update")
    if (not args.filename.endswith(".py") or Path(args.filename).name != args.filename
            or args.filename.startswith(".")):
        raise ValueError("filename must be one my_lg Python source basename")
    if args.backup_dir.parent != Path("/root") or args.backup_dir.exists():
        raise ValueError("backup-dir must be a new exact /root child")
    if not all(SHA256.fullmatch(value) for value in
               (args.expected_current_sha256, args.expected_candidate_sha256)):
        raise ValueError("expected hashes must be SHA-256 digests")
    live = LIVE_DIR / args.filename
    metadata = regular_file(live)
    regular_file(args.candidate)
    original, candidate = live.read_bytes(), args.candidate.read_bytes()
    if digest(original) != args.expected_current_sha256:
        raise RuntimeError("live source changed from reviewed bytes")
    if digest(candidate) != args.expected_candidate_sha256:
        raise RuntimeError("candidate changed from reviewed bytes")
    if docker("inspect", "homeassistant", "--format", "{{.State.Running}}") != "true":
        raise RuntimeError("Home Assistant is not running")
    if args.preflight_only:
        print("preflight passed")
        return
    args.backup_dir.mkdir(mode=0o700)
    write_once(args.backup_dir / args.filename, original, 0o600, 0, 0)
    docker("stop", "-t", "60", "homeassistant")
    if docker("inspect", "homeassistant", "--format", "{{.State.Running}}") != "false":
        raise RuntimeError("Home Assistant did not stop; no source was changed")
    temporary = live.with_name(f".{args.filename}.install-{os.getpid()}")
    try:
        write_once(temporary, candidate, stat.S_IMODE(metadata.st_mode), metadata.st_uid, metadata.st_gid)
        os.replace(temporary, live)
        directory = os.open(live.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()
        # Whether the old or new inode remains, do not leave HA stopped after
        # a file-system error.  The exact hash check below identifies which.
        if docker("inspect", "homeassistant", "--format", "{{.State.Running}}") == "false":
            docker("start", "homeassistant")
    if docker("inspect", "homeassistant", "--format", "{{.State.Running}}") != "true":
        raise RuntimeError("Home Assistant failed to start; exact backup is preserved")
    if digest(live.read_bytes()) != args.expected_candidate_sha256:
        raise RuntimeError("installed file differs from reviewed candidate")
    print(f"started; backup={args.backup_dir}; installed={args.expected_candidate_sha256}")


if __name__ == "__main__":
    main()
