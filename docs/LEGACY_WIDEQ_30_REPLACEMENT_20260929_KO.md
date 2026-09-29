# 기존 WideQ 엔티티 30개: 로컬 교체 실사

2026-09-29 기준. 여기서 `key`는 기존 HA `unique_id`의 접미사다. 실제 기기 ID·엔티티 ID·캡처는 이 문서에 넣지 않는다. **소스 구현/테스트와 운영 배포는 별개**다. 아래 운영 현황은 16:38 KST 사후 확인 시점이며, 전 항목 전환 완료를 주장하지 않는다.

## 판정

| 구분 | 수 | 의미 |
| --- | ---: | --- |
| 로컬 읽기/동작으로 기존 ID 보존 가능 | 22 | WTL 14, Styler 5, 시그니처 공기청정기 Jet·위생 건조 2, 타워 UVnano 1. HA 연결 소스와 테스트 작성. WTL/Styler 중 신규 읽기 13개는 생산자+HA가 공유하는 feature DB에 모델별 등록한 뒤 파일럿 필요 |
| Web 재조회가 필요한 기존 예외 | 2 | 냉장고/김치냉장고 안티글레어 밝기. F010에 밝기는 있지만 own-state는 되읽지 않음. 현재 Web 저장·재조회 방식은 이미 구현됨 |
| 지원하지 않는 구형 범용 스위치 | 1 | 타워 Jet. Web에는 독립 토글이 없고 과거 cloud `airFast`는 전량 0. 운전 모드 탭을 Jet로 바꾸어 부르지 않고 구형 스위치 생성을 중단 |
| 같은 뜻의 로컬 소스를 증명하지 못함 | 5 | 코스 전력 3, Styler 어린이 잠금·오류 2. 다른 뜻의 로컬 필드로 조용히 교체하면 안 됨 |
| **합계** | **30** | |

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
| WTL 세탁 | `washer_energy` | 보류 | 기존 `accumulatedEnergyData`는 코스 Wh. 로컬 `washer.cycle.energy_wh`와 820개 인접 관측 중 4개만 수치 동일; 레코드 모든 u8/u16 위치도 일치하지 않음. 로컬 누적 kWh는 단위·범위까지 다름 |
| WTL 건조 | `dryer_state` | 로컬 | `diagnostic.dryer.state_raw`; 정확 모델 enum 0..27 + 9개 관측 상태 대조. 로컬 텍스트 `drying`만 사용하면 RUNNING/DRYING 구분을 잃으므로 raw 사용 |
| WTL 건조 | `dryer_dry_level` | 로컬·관측 범위 | `diagnostic.dryer.dry_level_raw` 0↔`NO_DRYLEVEL`만 확인 |
| WTL 건조 | `dryer_remain` | 로컬 | `dryer.cycle.remaining_min` |
| WTL 건조 | `dryer_duct_clogging` | 로컬·관측 범위 | `diagnostic.dryer.vent_blockage_raw` 0↔`DUCT_CLOGGING_LEVEL_0`만 확인 |
| WTL 건조 | `dryer_error` | 로컬·관측 범위 | `dryer.error.code_raw` 0↔`ERROR_NO`만 확인 |
| WTL 건조 | `dryer_energy` | 보류 | 기존 코스 Wh와 로컬 `dryer.cycle.energy_wh`는 820개 중 3개만 수치 동일; 다른 u8/u16 위치도 불일치 |
| Styler | `styler_state` | 로컬 | `cycle.state`; 정확 모델 현행 상태 코드표 |
| Styler | `styler_course` | 로컬 | `cycle.course`; 정확 모델 42개 코스 코드표 + 과거 코스 선택 캡처 |
| Styler | `styler_remain` | 로컬 | own-state offset 6–7 u16be ↔ Web `remainTimeMinute`, 366개 중 322개 인접 수치 동일 |
| Styler | `styler_night_dry` | 로컬 | 8월 보존 캡처에서 cloud `NightDry` ON/OFF 변동을 재발견. own-state offset 19 mask 0x20의 로컬 양방향 10전이가 같은 방향 cloud 전이보다 1.1~9.8초 선행. 기존 센서 ID에 `option.night_dry_enabled` 연결 |
| Styler | `styler_door_lock` | 로컬 | own-state offset 19 mask 0x01 양쪽 관측, 인접 356/366 일치 |
| Styler | `styler_child_lock` | 보류 | 과거 cloud `ChildLock` OFF 한쪽. 관련 모델의 바이트 배치를 정확 모델에 그대로 이식할 수 없음 |
| Styler | `styler_error` | 보류 | 과거 cloud `Error` 정상 한쪽. offset 8은 관련 모델 후보일 뿐 정확 모델 검증 없음 |
| Styler | `styler_energy` | 보류 | 기존 `accumulatedEnergyData`와 로컬 `courseSpendPower`가 다름(366개 중 6개 동일). 로컬 적산 총량도 다른 지표 |
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
5. 기존 전력 3개는 대시보드·통계 소비자가 있으므로, 값의 뜻을 바꾸는 선택 전에는 기존 ID를 유지한다. 로컬 코스 Wh와 적산 kWh 엔티티는 이미 별도 지표다. Styler 미확정 2개도 값/제어를 추측해서 노출하지 않는다. 밝기 두 number는 Web 의존임을 유지한다.
