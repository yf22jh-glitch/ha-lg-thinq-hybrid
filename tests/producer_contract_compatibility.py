"""Review exact legacy/runtime differences without widening an HA contract.

The fallback bundles are still exercised by the native climate, invalidation,
and frozen-domain tests. A production rollout uses the editable feature DB;
its coverage is in test_feature_database. Neither source is a drop-in copy of
the other's fallback catalogue. Any new difference requires a new review.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

MANIFEST = Path(__file__).with_name("producer-contract-compatibility.v1.json")


def differences(before, after, path=""):
    """Return every semantic difference, including missing versus null values."""
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(before.keys() | after.keys()):
            child = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in before or key not in after:
                result.append({
                    "path": child,
                    "consumer": {"exists": key in before, **({"value": before[key]} if key in before else {})},
                    "producer": {"exists": key in after, **({"value": after[key]} if key in after else {})},
                })
            else:
                result.extend(differences(before[key], after[key], child))
        return result
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        return [
            difference
            for index, (left, right) in enumerate(zip(before, after))
            for difference in differences(left, right, path + "/" + str(index))
        ]
    if type(before) is type(after) and before == after:
        return []
    return [{
        "path": path,
        "consumer": {"exists": True, "value": before},
        "producer": {"exists": True, "value": after},
    }]


def verify_reviewed_pair(name: str, consumer: bytes, producer: bytes) -> None:
    """Accept only the independently reviewed pair and its exact differences."""
    manifest = json.loads(MANIFEST.read_text())
    contract = manifest["contracts"][name]
    for side, content in (("consumer", consumer), ("producer", producer)):
        if hashlib.sha256(content).hexdigest() != contract[side + "_sha256"]:
            raise AssertionError(f"{name}: {side} changed; compatibility review required")
    actual = differences(json.loads(consumer), json.loads(producer))
    if actual != contract["differences"]:
        raise AssertionError(f"{name}: unreviewed semantic contract difference")


def verify_sidecar(artifact: bytes, sidecar: bytes) -> None:
    fields = sidecar.decode("ascii").strip().split()
    if len(fields) != 2 or fields[0] != hashlib.sha256(artifact).hexdigest():
        raise AssertionError("Producer contract sidecar does not match its artifact")


def verify_composite_domain_sidecar(artifact: bytes, sidecar: bytes) -> None:
    """Check the composite format's domain-separated canonical-body digest."""
    document = json.loads(artifact)
    body = {
        key: value for key, value in document.items()
        if key not in {"revision", "rootSha256"}
    }
    canonical = json.dumps(
        body, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    expected = hashlib.sha256(
        b"lg-rethink-local/home-assistant-local-control-composite-domain/v1\0"
        + canonical
    ).hexdigest()
    if document["rootSha256"] != expected or sidecar != (expected + "\n").encode("ascii"):
        raise AssertionError("Composite domain sidecar does not match its canonical body")
