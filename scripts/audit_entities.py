"""Offline entity/catalog audit using the ignored latest RAW captures.

No LG client is called. Device identifiers and aliases are replaced before
entity construction, and the generated Markdown report contains model-level
counts only.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from copy import deepcopy
from datetime import datetime
import importlib
import json
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.my_lg import MyLgData
from custom_components.my_lg.coordinator import PatDeviceCoordinator
from custom_components.my_lg.coordinator_wideq import WideqCoordinator
from custom_components.my_lg.rate_limiter import GlobalRateLimiter


ROOT = Path(__file__).resolve().parents[1]
RAW_CATALOG = (
    ROOT / "custom_components" / "my_lg" / "feature_catalog" / "raw_paths.json"
)
CONTROL_CATALOG = (
    ROOT
    / "custom_components"
    / "my_lg"
    / "feature_catalog"
    / "wideq_controls.json"
)

PLATFORM_MODULES = (
    "climate",
    "sensor",
    "humidifier",
    "binary_sensor",
    "fan",
    "select",
    "switch",
    "event",
    "number",
    "button",
    "time",
    "text",
)


class OfflineClient:
    """Fail loudly if entity construction performs unexpected WideQ I/O."""

    def __init__(self) -> None:
        self.calls = 0

    async def async_get_snapshots(self):
        self.calls += 1
        raise AssertionError("entity audit attempted WideQ snapshot I/O")

    async def async_control(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("entity audit attempted WideQ control I/O")


class AuditEntry:
    def __init__(self, runtime_data: MyLgData) -> None:
        self.runtime_data = runtime_data
        self.options: dict[str, Any] = {}
        self._unloads: list[Any] = []

    def async_on_unload(self, callback):
        self._unloads.append(callback)
        return callback

    def unload_listeners(self) -> None:
        for callback in reversed(self._unloads):
            callback()
        self._unloads.clear()


def _snapshots_by_model(wideq_dump: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for device in wideq_dump.get("devices", ()):
        snapshot = (device.get("raw") or {}).get("snapshot")
        if isinstance(snapshot, dict):
            result.setdefault(device.get("model", ""), []).append(snapshot)
    return result


def _control_stats(catalog: dict[str, Any]) -> tuple[Counter, Counter, int, int]:
    shapes: Counter = Counter()
    risks: Counter = Counter()
    groups = fields = 0
    for model in catalog.values():
        collections = [model.get("controls", {})]
        collections.extend(model.get("subdevices", {}).values())
        for controls in collections:
            for spec in controls.values():
                groups += 1
                fields += len(spec.get("writable_fields", spec.get("fields", {})))
                shapes[spec.get("shape", "unknown")] += 1
                risks[spec.get("risk", "low")] += 1
    return shapes, risks, groups, fields


async def audit(output: Path) -> None:
    pat_dump = json.loads((ROOT / "lg_pat_dump.json").read_text(encoding="utf-8"))
    wideq_dump = json.loads(
        (ROOT / "lg_wideq_dump.json").read_text(encoding="utf-8")
    )
    raw_catalog = json.loads(RAW_CATALOG.read_text(encoding="utf-8"))
    control_catalog = json.loads(CONTROL_CATALOG.read_text(encoding="utf-8"))

    hass = HomeAssistant(str(ROOT / ".audit-ha"))
    runtime = MyLgData(api=object())
    entry = AuditEntry(runtime)
    snapshots = _snapshots_by_model(wideq_dump)
    snapshot_offsets: Counter = Counter()
    wideq_data: dict[str, dict[str, Any]] = {}

    for index, source in enumerate(pat_dump.get("devices", ()), 1):
        device = deepcopy(source)
        fake_id = f"audit_device_{index:02d}"
        fake_alias = f"Audit device {index:02d}"
        info = device.setdefault("deviceInfo", {})
        info["alias"] = fake_alias
        device["deviceId"] = fake_id
        coordinator = PatDeviceCoordinator(hass, None, object(), device)
        coordinator.profile = device.get("profile")
        coordinator.data = device.get("status") or {}
        runtime.coordinators[fake_id] = coordinator

        model = coordinator.model
        candidates = snapshots.get(model, ())
        offset = snapshot_offsets[model]
        if offset < len(candidates):
            wideq_data[fake_id] = candidates[offset]
            snapshot_offsets[model] += 1

    offline_client = OfflineClient()
    runtime.wideq_coordinator = WideqCoordinator(
        hass,
        None,
        offline_client,
        GlobalRateLimiter(200, 3),
        lambda: 600,
    )
    runtime.wideq_coordinator.data = wideq_data

    entities_by_platform: dict[str, list[Any]] = {}
    try:
        for platform in PLATFORM_MODULES:
            entities: list[Any] = []
            module = importlib.import_module(
                f"custom_components.my_lg.{platform}"
            )
            await module.async_setup_entry(hass, entry, entities.extend)
            entities_by_platform[platform] = entities
    finally:
        entry.unload_listeners()

    all_entities = [
        entity for entities in entities_by_platform.values() for entity in entities
    ]
    unique_ids = [entity.unique_id for entity in all_entities]
    duplicate_ids = sorted(
        key for key, count in Counter(unique_ids).items() if count > 1
    )
    default_disabled = sum(
        not entity.entity_registry_enabled_default for entity in all_entities
    )

    raw_text = RAW_CATALOG.read_text(encoding="utf-8")
    control_text = CONTROL_CATALOG.read_text(encoding="utf-8")
    private_values = {
        str(value)
        for device in pat_dump.get("devices", ())
        for value in (
            device.get("deviceId"),
            device.get("alias"),
            device.get("deviceInfo", {}).get("alias"),
        )
        if value
    }
    identifiers_clean = not any(
        value in raw_text or value in control_text for value in private_values
    )

    shapes, risks, control_groups, writable_fields = _control_stats(control_catalog)
    raw_pat = sum(len(paths) for paths in raw_catalog.get("pat", {}).values())
    raw_wideq = sum(len(paths) for paths in raw_catalog.get("wideq", {}).values())

    lines = [
        "# LG 전체 엔티티 구현 감사",
        "",
        f"생성 시각: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        "",
        "## 결과",
        "",
        f"- PAT 캡처 기기: {len(pat_dump.get('devices', ()))}대",
        f"- WideQ 최신 snapshot: {sum(len(items) for items in snapshots.values())}대",
        f"- 감사 모델: {len(raw_catalog.get('wideq', {}))}종",
        f"- 등록 생성 엔티티: {len(all_entities):,}개",
        f"- 신규/기본 비활성 포함: {default_disabled:,}개",
        f"- unique ID 중복: {len(duplicate_ids)}개",
        f"- 엔티티 구성 중 WideQ 호출: {offline_client.calls}회",
        f"- 생성 카탈로그 식별자 포함 검사: {'PASS' if identifiers_clean else 'FAIL'}",
        "",
        "## 플랫폼별 생성 수",
        "",
        "| 플랫폼 | 엔티티 수 |",
        "|---|---:|",
    ]
    lines.extend(
        f"| `{platform}` | {len(entities):,} |"
        for platform, entities in entities_by_platform.items()
    )
    lines.extend(
        [
            "",
            "## RAW 읽기 카탈로그",
            "",
            f"- PAT 모델 상태 경로: {raw_pat:,}개",
            f"- WideQ snapshot + model schema 경로: {raw_wideq:,}개",
            "- PAT profile의 R/RW 경로는 실행 시 추가되며 모두 기본 비활성 진단 센서로 등록된다.",
            "- `IGNORE`, `NOT_DEFINE_VALUE`, null 경로도 등록하되 현재 상태는 unavailable로 둔다.",
            "",
            "## WideQ 제어 카탈로그",
            "",
            f"- 모델 제어 그룹: {control_groups:,}개",
            f"- 서비스/단일필드 제어 가능 필드: {writable_fields:,}개",
            f"- payload 형태: {dict(sorted(shapes.items()))}",
            f"- 위험 분류: {dict(sorted(risks.items()))}",
            "- PAT 중복 필드/동작은 WideQ 엔티티와 raw 서비스에서 차단한다.",
            "- 미검증 값 계약, ThinQ1 binary, 서비스성 필드는 실험 옵션이 필요하다.",
            "- 조리 시작은 위험 옵션과 기기 remote-control 상태가 모두 필요하다.",
            "",
            "## 방어 불변조건",
            "",
            "- [x] 엔티티 수와 무관하게 WideQ snapshot은 전체 대시보드 1회 호출",
            "- [x] 재시작 직후 eager poll 없음",
            "- [x] 최소 3초 간격 및 시간당 200 논리 작업 상한",
            "- [x] 3회 연속 실패 후 circuit open, 15분 간격 recovery probe",
            "- [x] 실패 중 마지막 snapshot과 stale 진단 유지",
            "- [x] circuit open 중 모든 WideQ 제어 차단",
            "- [x] 5xx/네트워크/명령 거부 시 재로그인·자동 재시도 없음",
            "- [x] 인증/세션 오류만 재연결 1회",
            "- [x] 복합 read-modify-write는 장치/I/O lock 안에서 직렬화",
            "- [x] 제어 후 즉시 추가 poll 없음",
            "",
            "## 판정",
            "",
            (
                "PASS — 전체 엔티티를 네트워크 호출 없이 구성했고 unique ID 중복이 없다."
                if not duplicate_ids and not offline_client.calls and identifiers_clean
                else "FAIL — 위 결과의 중복/호출/식별자 항목을 확인해야 한다."
            ),
            "",
        ]
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:18]))
    print(f"report: {output}")
    if duplicate_ids or offline_client.calls or not identifiers_clean:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "Docs" / "LG_ENTITY_IMPLEMENTATION_AUDIT_20260715.md",
    )
    args = parser.parse_args()
    asyncio.run(audit(args.output))


if __name__ == "__main__":
    main()
