# LG ThinQ Hybrid (`my_lg`)

우리집 LG 기기를 위한 **자체 제작 Home Assistant 통합**.
공식 **PAT API(ThinQ Connect) + MQTT push를 주력**으로 하고, 공식이 못 주는 필드(에어컨 실시간 전력, 제습기 물탱크, 워시타워/스타일러 상세)만 **wideq를 조건부로** 보완한다.

> 기존 `smartthinq_sensors`(wideq 30초 폴링 → LG 24시간 차단 유발)를 대체하는 것이 목표.
> 설계 원칙과 근거는 내부 설계문서(`DESIGN.md`, 레포 미포함)에 있다.

## 상태 (개발 중)

| Stage | 내용 | 상태 |
|---|---|---|
| 1 | PAT 코디네이터 + MQTT push로 **에어컨 4대** 상태/제어 (전력 제외) | ✅ |
| 2 | 에어컨 실시간 전력·누적에너지 (wideq 단일 저부하 폴링) | ✅ |
| 3 | 제습기 (PAT 제어 + 물탱크 wideq + WATER_IS_FULL push) | ✅ |
| 3.5 | 워시타워·스타일러 상세 (wideq: 코스·spin·물온도·잠금·에너지 등) | ✅ |
| 4 | 대시보드 전환 + 구 `smartthinq_sensors` 제거 | 예정(운영) |

### wideq (선택)
Stage 2부터 공식 PAT가 못 주는 값(에어컨 실시간 전력 등)을 위해 **wideq**(LG 내부 API)를 벤더링해 쓴다. 통합 설정 시 **wideq refresh token**(선택)을 넣으면 활성화된다. 폴링은 `refresh_devices()` 1콜/주기로 전 기기 snapshot을 받아 저부하(기본 120초 = 약 30콜/시)로 유지하며, 재시작 시 즉시 폴링하지 않는다.

## 설치 (HACS 커스텀 레포)

1. HACS → Integrations → 우측 상단 ⋮ → **Custom repositories**
2. URL: `https://github.com/yf22jh-glitch/ha-lg-thinq-hybrid`, Category: **Integration**
3. `LG ThinQ Hybrid (my_lg)` 설치 → HA 재시작
4. 설정 → 기기 및 서비스 → 통합 추가 → **LG ThinQ Hybrid** → PAT 토큰 입력

### 필요한 것
- LG ThinQ **PAT(Personal Access Token)** — [LG ThinQ Developer](https://thinq.developer.lge.com)에서 발급
- 국가코드 (기본 `KR`)

> 이 통합은 공식 `lg_thinq`와 **별도의 MQTT client_id**를 사용하므로 공식 통합과 병행 가능하다.

## 차단 회피 (핵심)

- 상태 갱신은 **MQTT push 주력**, PAT REST는 저빈도 폴백(≥3600초).
- wideq는 화이트리스트 기기 + **조건부 폴링(활성일 때만)** + 하드 floor. 재시작 시 즉시 폴링 금지(버스트 방지).
- 폴링 간격은 통합 **옵션에서 조정 가능**(안전 floor 강제).

## 라이선스
MIT
