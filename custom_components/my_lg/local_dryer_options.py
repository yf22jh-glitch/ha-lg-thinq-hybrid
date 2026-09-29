"""Explicit replacement form; course domains and wire checks stay in producer."""
import re

MODEL = 'WTL_KPK_BDH_KR_01'
CAPABILITY = 'dryer.course_option_program'
SCHEMA = 'dryer-course-option-replacement-v2'


def canonical_program(value):
    if isinstance(value, str) and len(value) <= 255:
        parts = value.split('|')
        if (len(parts) == 9 and parts[0] == 'replace'
                and all(re.fullmatch(r'[A-Z][A-Z0-9_]*', v) for v in parts[1:7])
                and all(re.fullmatch(r'0|[1-9][0-9]*', v) for v in parts[7:])
                and int(parts[7]) in (0, 20, 30, 40, 50, 60, 80, 100, 120, 150, 180, 210, 240)
                and (int(parts[8]) == 0 or 180 <= int(parts[8]) <= 1140 and int(parts[8]) % 30 == 0)):
            return value
    raise ValueError('replace|코스|건조정도|절약모드|물기알림|스팀|구김방지|시간건조분|예약분 순서로 '
                     '전체 값을 입력해 주세요. 시작 명령은 보내지 않아요.')


def is_canonical_program(value):
    try:
        return canonical_program(value) == value
    except ValueError:
        return False
