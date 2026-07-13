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
| 5 | **전 기기(16종) 커버** — 공청기·가습기·냉장고·식세기·정수기·오븐·쿡탑 + 완료알림 event | ✅ |
| 6 | **쓰기 제어** — AC swing·풍향/풍속·절전, 냉장고/냉동고 온도(Number), 세탁/건조/스타일러 운전 START·STOP(Button), 위생건조·제습 풍속 등(wideq 제어) | ✅ |
| 4 | 실 HA 설치 + 대시보드 전환 + 공식·구 fork 제거 | ✅(운영 중) |

Stage 5로 공식 `lg_thinq` + 구 `smartthinq` 둘 다 대체 가능(전 기기 PAT + 필요 필드 wideq).

### wideq (선택)
Stage 2부터 공식 PAT가 못 주는 값(에어컨 실시간 전력 등)을 위해 **wideq**(LG 내부 API)를 벤더링해 쓴다. 통합 설정 시 **wideq refresh token**(선택)을 넣으면 활성화된다. `refresh_devices()` 1콜/주기로 전 기기 snapshot을 받는 저부하 폴링이며, 재시작 시 즉시 폴링하지 않는다. 일부 필드는 읽기뿐 아니라 wideq 쓰기 제어(위생건조·제습 풍속 등)에도 쓴다.

**폴링 간격(옵션에서 조정, 안전 floor 강제):**
- AC 활성 기본 600초 / 일반 기기 활성 300초 / 유휴 1800초
- 하드 상한: 시간당 200콜, 콜 간 최소 3초 간격
- 액세스 토큰(~1h TTL)은 폴링 전 자동 갱신하고, 세션이 죽으면 1회 재접속 후 재시도한다.

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
- wideq 하드 상한: 시간당 200회 논리 작업(폴링/제어) + 작업 간 최소 3초.
  이는 목표 호출량이 아니라 폭주 방지선이며, 기본 snapshot 폴링은 유휴
  시간당 2회, AC·제습기 활성 6회, 워시타워·스타일러 운전 중 12회다.
- 폴링 간격은 통합 **옵션에서 조정 가능**(안전 floor 강제).
- 동작 여부는 wideq가 아니라 **PAT/MQTT push 상태**로 판단한다. AC·제습기는
  활성 600초, 워시타워·스타일러는 운전 중 300초, 모두 유휴면 1800초가
  기본이다(단일 snapshot 호출이 전 기기를 함께 갱신).
- LG 서버가 5xx/점검 상태면 재로그인하지 않고 마지막 snapshot을
  `data_stale`로 유지한다. 3회 연속 실패 후 일반 폴링을 멈추고 15분마다
  snapshot 1회만 복구 probe로 실행하며, 성공 즉시 MQTT 기반 정상 주기로 복귀한다.

## 라이선스
MIT
