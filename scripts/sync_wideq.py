#!/usr/bin/env python3
"""Build a three-way vendor candidate; never overwrite the working integration."""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile


def merge_trees(base: dict[str, bytes], local: dict[str, bytes],
                upstream: dict[str, bytes]) -> dict[str, bytes]:
    """Retain local additions/patches; require review for overlap and delete/modify."""
    result = {}
    conflicts = []
    for name in sorted(base.keys() | local.keys() | upstream.keys()):
        before, ours, theirs = base.get(name), local.get(name), upstream.get(name)
        if ours == theirs:
            merged = ours
        elif ours == before:
            merged = theirs
        elif theirs == before:
            merged = ours
        elif before is None or ours is None or theirs is None:
            conflicts.append(name)
            continue
        elif any(b"\0" in value for value in (before, ours, theirs)):
            conflicts.append(name + " (binary)")
            continue
        else:
            with tempfile.TemporaryDirectory(prefix="wideq-merge-") as directory:
                paths = [Path(directory) / name for name in ("local", "base", "upstream")]
                for path, value in zip(paths, (ours, before, theirs)):
                    path.write_bytes(value)
                command = subprocess.run(
                    ["git", "merge-file", "-p", *map(str, paths)],
                    capture_output=True, check=False,
                )
                if command.returncode:
                    conflicts.append(name)
                    continue
                merged = command.stdout
        if merged is not None:
            result[name] = merged
    if conflicts:
        raise ValueError("WideQ merge needs review; no source changed: " + ", ".join(conflicts))
    return result


def safe_name(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value or "\0" in value:
        raise ValueError("Unsafe vendor path")
    return path.as_posix()


def git_tree(repository: Path, revision: str, prefix: str) -> dict[str, bytes]:
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("An exact upstream commit is required")
    prefix = safe_name(prefix).rstrip("/") + "/"
    listing = subprocess.check_output(
        ["git", "-C", str(repository), "ls-tree", "-rz", revision, "--", prefix]
    )
    result = {}
    for row in listing.split(b"\0"):
        if not row:
            continue
        info, path = row.decode().split("\t", 1)
        mode, kind, oid = info.split()
        if mode not in ("100644", "100755") or kind != "blob" or not path.startswith(prefix):
            raise ValueError("Upstream vendor tree contains a non-regular entry")
        name = safe_name(path[len(prefix):])
        result[name] = subprocess.check_output(["git", "-C", str(repository), "cat-file", "blob", oid])
    if "__init__.py" not in result or "core_async.py" not in result:
        raise ValueError("Upstream vendor tree is empty or incomplete")
    return result


def local_tree(directory: Path) -> dict[str, bytes]:
    result = {}
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Local vendor tree must be a real directory")
    for path in directory.rglob("*"):
        relative = path.relative_to(directory)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError("Local vendor tree contains a symlink")
        if path.is_file():
            result[safe_name(relative.as_posix())] = path.read_bytes()
        elif not path.is_dir():
            raise ValueError("Local vendor tree contains a non-regular entry")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    manifest = json.loads((args.root / "scripts/wideq-upstream.json").read_text())
    baseline = git_tree(args.upstream, manifest["commit"], manifest["path"])
    incoming = git_tree(args.upstream, args.revision, manifest["path"])
    local = local_tree(args.root / "custom_components/my_lg/wideq")
    merged = merge_trees(baseline, local, incoming)
    # All merges finish before a candidate directory is created. Existing output
    # paths are rejected; a conflict never removes or overwrites local source.
    args.output.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name, value in merged.items():
        target = args.output / "wideq" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
    (args.output / "wideq-upstream.json").write_text(
        json.dumps({**manifest, "commit": args.revision}, indent=2) + "\n"
    )
    print(json.dumps({
        "base": manifest["commit"], "upstream": args.revision,
        "added": sorted(merged.keys() - local.keys()),
        "removed": sorted(local.keys() - merged.keys()),
        "changed": sorted(name for name in merged.keys() & local.keys() if merged[name] != local[name]),
        "source_modified": False,
    }))


if __name__ == "__main__":
    main()
