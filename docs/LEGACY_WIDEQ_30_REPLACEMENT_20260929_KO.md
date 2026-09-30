# 기존 WideQ 엔티티 30개: 로컬 교체 실사

2026-09-29 기준. 여기서 `key`는 기존 HA `unique_id`의 접미사다. 실제 기기 ID·엔티티 ID·캡처는 이 문서에 넣지 않는다. **소스 구현/테스트와 운영 배포는 별개**다. 아래 운영 현황은 16:38 KST 사후 확인 시점이며, 전 항목 전환 완료를 주장하지 않는다.

## 판정

| 구분 | 수 | 의미 |
| --- | ---: | --- |
| 로컬 읽기/동작으로 기존 ID 보존 가능 | 25 | WTL 16, Styler 6, 시그니처 공기청정기 Jet·위생 건조 2, 타워 UVnano 1. 코스 전력 3개는 2026-09-29 소스·테스트에 로컬 Wh 연결을 추가했으며 아직 운영 전환 전이다. 스타일러 전력 읽기는 feature DB 등록·관측 후 활성화가 필요하다 |
| Web 재조회가 필요한 기존 예외 | 2 | 냉장고/김치냉장고 안티글레어 밝기. F010에 밝기는 있지만 own-state는 되읽지 않음. 현재 Web 저장·재조회 방식은 이미 구현됨 |
| 지원하지 않는 구형 범용 스위치 | 1 | 타워 Jet. Web에는 독립 토글이 없고 과거 cloud `airFast`는 전량 0. 운전 모드 탭을 Jet로 바꾸어 부르지 않고 구형 스위치 생성을 중단 |
| 같은 뜻의 로컬 소스를 증명하지 못함 | 2 | Styler 어린이 잠금·오류. 다른 뜻의 로컬 필드로 조용히 교체하면 안 됨 |
| **합계** | **30** | |

## 2026-09-29 ThinQ Web 실기 재확인

- 양문이: 일몰·일출 모드의 밝기 **40% → 50% 저장 → 재진입 시 50% 확인 → 40% 복구 → 재진입 시 40% 확인**. 모드는 바꾸지 않았다.
- 오김냉: 사용자 지정 21:00–06:00의 밝기 **30% → 40% 저장 → 재진입 시 40% 확인 → 30% 복구 → 재진입 시 30% 확인**. 모드와 시간창은 바꾸지 않았다.
- HA의 두 밝기 `number` 구현(`night_mode.py`의 SAVE 후 fresh GET 확인)은 운영 통합 파일과 동일한 해시이고, 각 기기의 현재 모드에 해당하는 number가 엔티티 레지스트리에 있으며 `disabled_by`도 없다. **런타임 가용성·HA number 서비스의 실기 쓰기는 아직 확인하지 않았다.** 위 조작은 ThinQ Web 화면에서 수행했으며 기기 own-state 밝기 되읽기 증거도 아니다.
- 공기청정기 타워: 현재 Web의 **전체 운전 모드 10종**(AI, 클린부스터, 듀얼청정, 싱글청정, 실내외연동, 베이비, 펫, 새집, 요리, 운동), 제품 탭, 유용한 기능, 설정에 독립 Jet 조작이 없다. 별칭 가능성을 확인하려고 **AI → 클린부스터 → AI**를 실행하고 현재 모드가 AI로 돌아왔음을 확인했다. 이때 `airState.miscFuncState.airFast` 전이는 없어서 클린부스터를 Jet로 연결하지 않는다. 모드 변경에 수반된 화면 밝기는 정상 운전값 50%로 복구했다. `airState.powerSave.basic`은 보존된 같은 날 기록에서 사용자 명령 없이 0↔1로 여러 번 전이하며 화면 밝기 50↔10%가 동행하는 **자동 운전 상태**이므로, 시험 직전 값 1을 별도 설정값으로 간주해 강제로 쓰지 않는다. Web의 '절전' 체크박스는 별도 `airState.aiPlus.onOff`를 바꾸며 껐다 켜서 원래 값 1로 돌렸다. `jet_mode`의 기존 미지원 판정을 유지한다.

## 30개 항목별 결과

| 기기 | 기존 key | 판정 | 확인한 근거/남은 조건 |
| --- | --- | --- | --- |
| WTL 세탁 | `washer_state` | 로컬 | `washer.cycle.state`; 현행 상태 코드와 cloud 상태 전이 대조 |
| WTL 세탁 | `washer_course` | 로컬 | `diagnostic.washer.course_raw`; 정확 모델의 23개 코스 코드표 + 관측 114/46 양방향 |
| WTL 세탁 | `washer_spin` | 로컬 | `diagnostic.washer.spin_setting_raw`; 옵션 코드표 및 0/6/8 관측 |
| WTL 세탁 | `washer_water_temp` | 로컬 | `diagnostic.washer.wash_temperature_raw`; 옵션 코드표 및 0/8 관측 |
| WTL 세탁 | `washer_water_level` | 로컬·관측 범위 | `diagnostic.washer.water_level_raw` 0↔`WATERLEVEL_1`만 확인. 다른 값은 unavailable |
| WTL 세탁 | `washer_remain` | 로컬 | `washer.cycle.remaining_min` |
| WTL 세탁 | `washer_error` | 로컬·관측 범위 | `washer.error.code_raw` 0↔`ERROR_NO`만 확인. 다른 값은 unavailable |
| WTL 세탁 | `washer_door_lock` | 로컬 | 상태 비트 0x01; ON/OFF 양쪽 관측 |
| WTL 세탁 | `washer_child_lock` | 로컬·도메인 보강 | 기존 정확 모델 로컬 bit 해독. 보존 이력은 OFF 쪽뿐, ON은 코드/공식 도메인 기반 |
| WTL 세탁 | `washer_energy` | 로컬 소스 준비 | `washer.cycle.energy_wh` u16-BE Wh. 과거 ThinQ `accumulatedEnergyData`는 이 코스 카운터를 약 15분마다 반영한다. 17회 누적값 전이 중 증가 15회는 `periodicEnergyData = 새 누적값 - 직전 누적값`, 리셋 2회는 `periodicEnergyData = 새 누적값`; 증가 전이 간격 중앙값 15.03분. 기존 820개 인접 시각 비교는 서버 반영 지연을 동일 물리량의 반례로 오판했다. 로컬 표시는 지연 없이 현재 코스 Wh를 내므로 같은 시각 숫자가 Web과 다를 수 있다 |
| WTL 건조 | `dryer_state` | 로컬 | `diagnostic.dryer.state_raw`; 정확 모델 enum 0..27 + 9개 관측 상태 대조. 로컬 텍스트 `drying`만 사용하면 RUNNING/DRYING 구분을 잃으므로 raw 사용 |
| WTL 건조 | `dryer_dry_level` | 로컬·관측 범위 | `diagnostic.dryer.dry_level_raw` 0↔`NO_DRYLEVEL`만 확인 |
| WTL 건조 | `dryer_remain` | 로컬 | `dryer.cycle.remaining_min` |
| WTL 건조 | `dryer_duct_clogging` | 로컬·관측 범위 | `diagnostic.dryer.vent_blockage_raw` 0↔`DUCT_CLOGGING_LEVEL_0`만 확인 |
| WTL 건조 | `dryer_error` | 로컬·관측 범위 | `dryer.error.code_raw` 0↔`ERROR_NO`만 확인 |
| WTL 건조 | `dryer_energy` | 로컬 소스 준비 | `dryer.cycle.energy_wh` u16-BE Wh. 누적값 전이 26회 중 증가 18회는 직전값 차이, 새 코스 첫 샘플 8회는 새 누적값이 `periodicEnergyData`와 일치한다. 증가 전이 간격 중앙값 15.03분. 구형 센서 ID·Wh 단위는 유지하되 현재 로컬 카운터를 즉시 표시한다 |
| Styler | `styler_state` | 로컬 | `cycle.state`; 정확 모델 현행 상태 코드표 |
| Styler | `styler_course` | 로컬 | `cycle.course`; 정확 모델 42개 코스 코드표 + 과거 코스 선택 캡처 |
| Styler | `styler_remain` | 로컬 | own-state offset 6–7 u16be ↔ Web `remainTimeMinute`, 366개 중 322개 인접 수치 동일 |
| Styler | `styler_night_dry` | 로컬 | 8월 보존 캡처에서 cloud `NightDry` ON/OFF 변동을 재발견. own-state offset 19 mask 0x20의 로컬 양방향 10전이가 같은 방향 cloud 전이보다 1.1~9.8초 선행. 기존 센서 ID에 `option.night_dry_enabled` 연결 |
| Styler | `styler_door_lock` | 로컬 | own-state offset 19 mask 0x01 양쪽 관측, 인접 356/366 일치 |
| Styler | `styler_child_lock` | 보류 | 어린이 잠금 **읽기 상태**이며 문 잠금과 다르다. 과거 cloud `ChildLock`은 OFF 한쪽뿐이다. 관련 모델의 바이트 배치를 정확 모델에 그대로 이식할 수 없음 |
| Styler | `styler_error` | 보류 | 오류 코드 **읽기 상태**다. 과거 cloud `Error`는 정상 한쪽뿐이다. offset 8은 관련 모델 후보일 뿐 정확 모델 검증 없음 |
| Styler | `styler_energy` | 로컬 소스 준비 | own-state offset 14..15 u16-BE `courseSpendPower` Wh. 이전의 366개 인접 시각 비교는 약 15분 간격으로 반영되는 `accumulatedEnergyData`와 즉시 갱신되는 로컬 값을 대조한 오판이다. 누적값 증가 13회 모두 `periodicEnergyData`가 증가분과 일치하고, 리셋 3회는 새 누적값과 일치한다. 증가 전이 간격 중앙값 15.03분. 로컬 현재 코스 Wh를 기존 ID로 즉시 표시하며 적산 총량과 구분한다 |
| AIR910 | `jet_mode` | 로컬 | 검증된 `rapid_operation.enabled` 로컬 명령·자체 읽기, 기존 스위치 ID 유지 |
| AIR910 | `uv_disinfection` | 로컬·Web 이름 보정 | ThinQ Web의 '위생 건조' OFF→ON 실험에서 명령 `0x0362` 1→0→1, 기기 보고 `0x0362`·`0x0361` 1→0→1, cloud `cleanDry`·`airUVDisinfection` 1→0→1이 함께 따라왔다. 기존 ID를 로컬 `clean_dry.enabled`에 연결하고 표시 이름은 '위생 건조'로 보정. `0x0361` 단독값은 과거 안정 구간 반례가 있어 제어의 되읽기로 사용하지 않음 |
| AIR2C 타워 | `jet_mode` | 미지원 구형 범용 스위치 | exact Web 화면에는 독립 Jet 토글 없이 운전 모드 탭만 있다. 보존 cloud `airFast`는 13,230회 모두 0이고 별도 `fastClean` 키도 없다. 기존 범용 WideQ 스위치 생성 중단; 운전 모드 제어와 동일시하지 않음 |
| AIR2C 타워 | `uv_disinfection` | 로컬·Web 이름 보정 | exact Web의 'UVnano 공기살균' OFF→ON에서 로컬 명령/되읽기 `0x014f` 1→0→1과 cloud `airUvnano` 1→0→1 확인. 범용 WideQ `airUVDisinfection` 키는 이 모델에서 부재. 기존 HA ID를 로컬 `sterilization.uvnano_enabled`로 연결하고 이름을 보정 |
| 냉장고 | 일몰 안티글레어 밝기 number | Web 예외 | F010 brightness write 형식 및 Web 저장/재조회 확인. own-state 밝기 readback 없음 |
| 김치냉장고 | 사용자 안티글레어 밝기 number | Web 예외 | 동일. 기존 cloud-confirmed number가 동작을 담당 |

## 운영 전환 경계

1. Styler와 WTL 읽기 서비스는 세 필드 해독기가 포함된 불변 릴리스로 전환했다. 기존 Styler `state`/`course`와 WTL `washer_state`/`washer_remain`/`washer_child_lock`/`dryer_remain`의 로컬값이 운영 HA의 기존 ID에 도달했다.
2. 운영 feature DB에는 Styler 3개와 WTL 10개를 `producer_registered=1`, `enabled=0`으로 등록했다. 새 own-state 패킷이 아직 없어 publication에 값이 없으므로 HA 전환은 대기한다. 값 없이 먼저 켜면 기존 ID가 `unavailable`이 된다. 기기 명령은 보내지 않았다.
3. 시그니처 공기청정기 Jet/위생 건조와 타워 UVnano의 기존 ID는 로컬 제어 별칭으로 운영 반영했다. 16:38 KST HA 사후 상태는 각각 `off`/`on`/`on`이다. 타워 Jet는 생성 중단했지만 기존 registry 행은 남아 `unavailable`이며, 소비자 확인 없이 storage 파일을 직접 수정하지 않는다.
4. 새 Styler/WTL 상태 패킷에서 모든 필드가 발행되고 값 매핑을 확인한 뒤, 해당 feature만 개별적으로 켜고 기존 ID 상태를 재검증한다. 별도 MQTT pilot과 테스트는 완료했으나 이 실시간 수신 확인을 대신하지 않는다.
5. 코스 전력 3개의 HA 연결 소스는 기존 ID·Wh 단위를 유지하면서 정확 모델의 로컬 코스 Wh를 선택하도록 준비했다. ThinQ의 약 15분 지연을 복제하지 않으므로 동일 시각 숫자가 달라질 수 있고, 리셋 가능한 코스값이라 `total_increasing`을 붙이지 않는다. WTL은 기존 full-read 필드가 있고, Styler는 `diagnostic.cycle.course_spend_power_raw`를 feature DB에 등록한 뒤 실제 값을 확인해 활성화해야 한다. 이 소스 변경은 운영 배포 완료를 뜻하지 않는다. 적산 `energy.total_wh`와 혼동하지 않는다. Styler 미확정 2개도 값/제어를 추측해서 노출하지 않는다. 밝기 두 number는 Web 의존임을 유지한다.
6. 운영 전환은 HA 소스 배포 후 세 기존 ID의 `Wh` 단위·수치·가용성을 한 기기씩 확인한다. Styler 새 full-read 필드는 우선 생산자 등록만 하고 실제 상태 패킷에서 값이 나온 뒤 활성화한다. 현재 WideQ가 남아 있는 동안은 미확정 잠금·오류 2개를 자동으로 로컬에 바꾸지 않는다.
