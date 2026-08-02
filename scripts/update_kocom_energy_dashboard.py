#!/usr/bin/env python3
"""Repair Kocom status and guard invalid LG/Kocom percentage comparisons."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any

DIAGNOSTIC_ENTITY = "sensor.kocom_eneoji_kocom_energy_usage"
ELECTRICITY_ENTITY = "sensor.kocom_eneoji_kocom_electricity_usage"

STATUS_PRIMARY = (
    "{% set status = state_attr('" + DIAGNOSTIC_ENTITY + "','connection_state') %}"
    "{% if status == 'connected' %}계량기 연결 정상"
    "{% elif status == 'error' %}계량기 연결 오류"
    "{% else %}계량기 동기화 대기{% endif %}"
)

STATUS_SECONDARY = (
    "{% set energy = states('" + ELECTRICITY_ENTITY + "') %}"
    "{% set status = state_attr('" + DIAGNOSTIC_ENTITY + "','connection_state') %}"
    "{% set last = state_attr('" + DIAGNOSTIC_ENTITY + "','last_success') %}"
    "{% if energy not in ['unknown','unavailable','none',''] %}"
    "전기 누적 {{ energy | float(0) | round(1) }} kWh"
    "{% else %}전기 누적값 확인 중{% endif %}\n"
    "{% if last %}최근 성공 {{ as_timestamp(last) | timestamp_custom('%m-%d %H:%M', true) }}"
    "{% else %}성공 이력 확인 중{% endif %}"
    "{% if status == 'error' %} · 자동 재시도 중{% endif %}"
)

STATUS_ICON_COLOR = (
    "{% set status = state_attr('" + DIAGNOSTIC_ENTITY + "','connection_state') %}"
    "{% if status == 'connected' %}yellow"
    "{% elif status == 'error' %}red{% else %}grey{% endif %}"
)

BREAKDOWN_SECONDARY = """{% set electricity = states('sensor.kocom_eneoji_kocom_electricity_usage') %}{% set cooling = states('sensor.pink_fam_lg_cooling_energy_month') %}{% set cold = states('sensor.pink_fam_lg_cold_storage_energy_month') %}{% set kitchen = states('sensor.pink_fam_lg_kitchen_energy_month') %}{% set air_water = states('sensor.pink_fam_lg_air_water_energy_month') %}{% set clothing = states('sensor.pink_fam_lg_clothing_energy_month') %}{% set electricity_value = electricity | float(0) %}{% set ns = namespace(total=0,ready=0) %}{% for entity in ['sensor.pink_fam_lg_cooling_energy_month', 'sensor.pink_fam_lg_cold_storage_energy_month', 'sensor.pink_fam_lg_kitchen_energy_month', 'sensor.pink_fam_lg_air_water_energy_month', 'sensor.pink_fam_lg_clothing_energy_month'] %}{% set value = states(entity) %}{% if value not in ['unknown','unavailable','none',''] %}{% set ns.total = ns.total + (value | float(0)) %}{% set ns.ready = ns.ready + 1 %}{% endif %}{% endfor %}{% set comparable = electricity_value > 0 and ns.ready == 5 and ns.total <= electricity_value %}{% if electricity in ['unknown','unavailable','none',''] %}누적값 동기화 대기{% else %}{{ electricity_value | round(1) }} kWh{% endif %}
이번 달 LG 참고 합계 · {% if ns.ready > 0 %}{{ ns.total | round(3) }} kWh{% if comparable %} · 전체 {{ ((ns.total / electricity_value * 100) | round(1)) }}%{% else %} · 집계 범위 불일치로 비율 보류{% endif %}{% else %}동기화 대기{% endif %}
🔵 냉방 {% if cooling not in ['unknown','unavailable','none',''] %}{{ cooling | float(0) | round(3) }} kWh{% if comparable %} · 전체 {{ (((cooling | float(0)) / electricity_value * 100) | round(1)) }}%{% endif %}{% else %}–{% endif %}
🟣 냉장 {% if cold not in ['unknown','unavailable','none',''] %}{{ cold | float(0) | round(3) }} kWh{% if comparable %} · 전체 {{ (((cold | float(0)) / electricity_value * 100) | round(1)) }}%{% endif %}{% else %}–{% endif %}
🟡 주방 {% if kitchen not in ['unknown','unavailable','none',''] %}{{ kitchen | float(0) | round(3) }} kWh{% if comparable %} · 전체 {{ (((kitchen | float(0)) / electricity_value * 100) | round(1)) }}%{% endif %}{% else %}–{% endif %}
🟢 공기·물 {% if air_water not in ['unknown','unavailable','none',''] %}{{ air_water | float(0) | round(3) }} kWh{% if comparable %} · 전체 {{ (((air_water | float(0)) / electricity_value * 100) | round(1)) }}%{% endif %}{% else %}–{% endif %}
🩷 의류 {% if clothing not in ['unknown','unavailable','none',''] %}{{ clothing | float(0) | round(3) }} kWh{% if comparable %} · 전체 {{ (((clothing | float(0)) / electricity_value * 100) | round(1)) }}%{% endif %}{% else %}–{% endif %}"""


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def update_dashboard(storage: dict[str, Any]) -> tuple[int, int]:
    """Update the two energy cards and return modification counts."""
    status_count = 0
    breakdown_count = 0
    for card in _walk(storage):
        if (
            card.get("entity") == DIAGNOSTIC_ENTITY
            and card.get("type") == "custom:mushroom-template-card"
        ):
            card["primary"] = STATUS_PRIMARY
            card["secondary"] = STATUS_SECONDARY
            card["icon_color"] = STATUS_ICON_COLOR
            status_count += 1
        elif (
            card.get("entity") == ELECTRICITY_ENTITY
            and card.get("primary") == "전기 누적"
            and any(
                marker in str(card.get("secondary", ""))
                for marker in ("LG 분류", "LG 참고 합계")
            )
        ):
            card["secondary"] = BREAKDOWN_SECONDARY
            breakdown_count += 1
    return status_count, breakdown_count


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {Path(sys.argv[0]).name} SOURCE DESTINATION", file=sys.stderr)
        return 2

    source, destination = map(Path, sys.argv[1:])
    storage = json.loads(source.read_text(encoding="utf-8"))
    status_count, breakdown_count = update_dashboard(storage)
    if status_count != 1 or breakdown_count != 1:
        raise RuntimeError(
            "Expected exactly one status and one breakdown card; "
            f"found status={status_count}, breakdown={breakdown_count}"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(
        json.dumps(storage, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    print(f"updated status={status_count}, breakdown={breakdown_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
