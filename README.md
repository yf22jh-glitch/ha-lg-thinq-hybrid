# LG ThinQ Hybrid (`my_lg`)

우리집 LG 기기를 위한 **자체 제작 Home Assistant 통합**.
공식 **PAT API(ThinQ Connect) + MQTT push를 주력**으로 하고, 공식이 못 주는 필드(에어컨 실시간 전력, ThinQ 앱 에너지 이력, 제습기 물탱크, 워시타워/스타일러 상세)만 **wideq를 조건부로** 보완한다.

> 기존 `smartthinq_sensors`(wideq 30초 폴링 → LG 24시간 차단 유발)를 대체하는 것이 목표.
> 설계 원칙과 근거는 내부 설계문서(`DESIGN.md`, 레포 미포함)에 있다.

## 상태 (개발 중)

| Stage | 내용 | 상태 |
|---|---|---|
| 1 | PAT 코디네이터 + MQTT push로 **에어컨 4대** 상태/제어 (전력 제외) | ✅ |
| 2 | 에어컨 실시간 전력·누적에너지 (wideq 단일 저부하 폴링) | ✅ |
| 3 | 제습기 (PAT 제어 + 물탱크 wideq + WATER_IS_FULL push) | ✅ |
| 3.5 | 워시타워·스타일러 상세 (wideq: 코스·spin·물온도·잠금·에너지 등) | ✅ |
| 3.6 | ThinQ 앱 에너지 이력 (제습기·인덕션·오븐·정수기·스타일러·냉장고) | ✅ |
| 5 | **전 기기(16종) 커버** — 공청기·가습기·냉장고·식세기·정수기·오븐·쿡탑 + 완료알림 event | ✅ |
| 6 | **검증된 쓰기 제어** — fresh pre-state + API ACK + fresh state echo가 정확히 대응하는 PAT/WideQ 제어만 실행 | ✅ (미검증 동작 차단) |
| 7 | **전체 RAW/모델 기능 카탈로그** — 모든 읽기 경로, PAT 누락 제어, WideQ 113개 제어 그룹과 복합 명령 | ✅ (신규 기능 기본 비활성) |
| 4 | 실 HA 설치 + 대시보드 전환 + 공식·구 fork 제거 | ✅(운영 중) |

Stage 5로 공식 `lg_thinq` + 구 `smartthinq` 둘 다 대체 가능(전 기기 PAT + 필요 필드 wideq).

### wideq (선택)
Stage 2부터 공식 PAT가 못 주는 값(에어컨 실시간 전력, 기기별 에너지 이력 등)을 위해 **wideq**(LG 내부 API)를 벤더링해 쓴다. 통합 설정 시 **wideq refresh token**(선택)을 넣으면 활성화된다. `refresh_devices()` 1콜/주기로 전 기기 snapshot을 받는 저부하 폴링이며, 재시작 시 즉시 폴링하지 않는다. 에너지 이력은 별도 저주기 캐시로 수집하며 같은 전역 요청 제한기를 통과한다. 일부 필드는 읽기뿐 아니라 wideq 쓰기 제어(위생건조·제습 풍속 등)에도 쓴다.

**폴링 간격(옵션에서 조정, 안전 floor 강제):**
- AC 활성 기본 600초 / 일반 기기 활성 300초 / 유휴 1800초
- 하드 상한: 시간당 200콜, 콜 간 최소 3초 간격
- 액세스 토큰(~1h TTL)은 폴링 전 자동 갱신한다. 인증/세션 오류만 1회
  재접속하며, 서버 5xx·네트워크 장애·기기 명령 거부에는 재로그인하거나
  자동 재시도하지 않는다.

### 전체 엔티티와 제어

- 최신 PAT profile/status와 WideQ snapshot/model schema의 모든 경로를
  진단 센서로 등록한다. 기존 핵심 엔티티 외 신규 RAW 엔티티는 기본 비활성이다.
- `IGNORE`, `NOT_DEFINE_VALUE`, null 필드도 엔티티는 등록하되 실제 값이
  생길 때까지 `unavailable`로 유지한다.
- PAT와 WideQ가 중복되면 PAT/MQTT를 우선하며, 중복 WideQ 제어는 raw
  서비스에서도 거부한다.
- PAT 쓰기는 명령 직전 REST 상태, ThinQ Connect API ACK, ACK 뒤 5/10초
  fresh REST 상태를 순서대로 확인한다. payload의 모든 leaf가 공식 profile의
  읽기 필드에 정확히 대응해야 하며, 대응하지 않는 write-only 동작은 엔티티를
  unavailable로 유지하고 실제 요청 전에 차단한다. 현재 세탁/건조/스타일러
  START·STOP·POWER_*는 상태 전이와 1:1 echo 계약이 없어 차단 상태다.
- 모델이 enum/range를 명시한 WideQ 단일 필드는 기본 비활성
  `select`/`number`/`text` 엔티티로 제공한다.
- 복합 코스·프리셋·레시피 명령은 `my_lg.wideq_command` 서비스에서 모델
  payload와 필드·enum·범위를 검증한다. 정확한 사전 상태, HTTP ACK, bounded
  post-command 상태 readback 계약까지 있는 제어만 실행하며 나머지는 전송 전에
  거부한다. 미검증 값 계약은 실험 옵션만으로 이 경계를 우회할 수 없다.
- 오븐/인덕션 조리 시작은 기본 잠금이며, 위험 제어 옵션과 기기의
  remote-control 허용 상태가 모두 확인되어야 한다.
- 제어는 snapshot 폴링과 같은 rate limiter/circuit breaker를 통과한다. 검증된
  복합 쓰기는 ACK 뒤 최대 2회의 bounded readback으로 실제 상태 전이를 확인한다.

현재 집의 exact inventory는 18대/16개 모델이고, WideQ 읽기 경로 1,636개와
제어 그룹 113개를 포함한다. 신규 `AIR_2C0001_WW`는 exact 빈 control scope로
고정해 다른 공기청정기 제어를 상속하지 않는다. `HWWA9X3C_F2U`의 ModelJSON
제어 20개 중 실제 쓰기·ACK·상태 echo가 입증된 단일 필드 12개만 5/10초 fresh
readback 계약으로 허용하고, 나머지 8개 제어 그룹은 전송 전에 차단한다.
두 기기 추가 전 16대 RAW로 수행한 오프라인
구성 감사에서는 총 2,732개 엔티티가 생성됐고, 2,550개가 기본 비활성,
unique ID 중복과 구성 중 WideQ 호출은 각각 0개/0회였다.

## 설치 (HACS 커스텀 레포)

1. HACS → Integrations → 우측 상단 ⋮ → **Custom repositories**
2. URL: `https://github.com/yf22jh-glitch/ha-lg-thinq-hybrid`, Category: **Integration**
3. `LG ThinQ Hybrid (my_lg)` 설치 → HA 재시작
4. 설정 → 기기 및 서비스 → 통합 추가 → **LG ThinQ Hybrid** → PAT 토큰 입력

### 필요한 것
- LG ThinQ **PAT(Personal Access Token)** — [LG ThinQ Developer](https://thinq.developer.lge.com)에서 발급
- 국가코드 (기본 `KR`)

> 이 통합은 공식 `lg_thinq`와 **별도의 MQTT client_id**를 사용하므로 공식 통합과 병행 가능하다.

### Rethink 기기 등록 이벤트 연동 (선택)

같은 호스트의 Rethink가 기기 등록·삭제·이름 변경을 자동 대조하도록 하려면
통합 옵션의 `Rethink 기기 등록 이벤트 연동 토큰`에 Rethink의 owner-only
`cloud-events.token` 값을 입력한다. 토큰이 설정된 경우에만 ThinQ Connect의
계정 단위 `push/devices` 구독을 추가하고, 고정 loopback 주소
`http://127.0.0.1:44401/cloud/device-events`로 이벤트를 전달한다.

- 원본 PAT payload, 별칭, MAC, 인증정보는 전달하거나 저장하지 않는다.
- 이벤트 종류, device ID와 최상위 필드명만 Bearer 인증으로 전송한다.
- 등록·삭제·이름 변경 이벤트에만 Rethink가 제한된 LG Home 확인을 예약한다.
- 자동 재시도, 주기적인 기기 목록 조회, LG 계정의 기기 추가·삭제는 수행하지 않는다.
- 토큰이 없거나 잘못된 경우 기존 PAT/MQTT 상태·제어 경로에는 영향이 없다.

지원 검증 조합은 Home Assistant 2024.11.3 + ThinQ Connect 1.0.12 및 최신
Home Assistant + 최신 ThinQ Connect다.

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
