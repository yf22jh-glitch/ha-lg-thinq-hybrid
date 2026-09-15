"""Form vocabulary only; exact course gating and appliance bytes live in the producer."""
import re

MODEL = 'WTL_KPK_BDH_KR_01'
CAPABILITY = 'washer.course_option_program'
SCHEMA = 'washer-course-option-program-v1'

def canonical_program(value):
    if isinstance(value, str) and len(value) <= 255:
        parts = value.split('|')
        if (len(parts) == 10 and all(re.fullmatch(r'[A-Z][A-Z0-9_]*', v) for v in parts[:9])
                and re.fullmatch(r'0|[1-9][0-9]*', parts[9]) and 0 <= int(parts[9]) <= 1140):
            return value
    raise ValueError('현재 코스|세탁강도|온도|헹굼|탈수|세제단계|유연제단계|터보|구김방지|예약분 순서로 전체 값을 입력해 주세요. 시작 명령은 보내지 않아요.')

def is_canonical_program(value):
    try:
        return canonical_program(value) == value
    except ValueError:
        return False
