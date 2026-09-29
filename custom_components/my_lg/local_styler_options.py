"""Desired start form only. Editing never sends an appliance command."""
import re

MODEL = 'ST_R_ETH01Y_'
CAPABILITY = 'styler.operation.start_with_options'
SCHEMA = 'styler-start-option-program-v1'

def canonical_program(value):
    if isinstance(value, str) and len(value) <= 160:
        p = value.split('|')
        if (len(p) == 4 and re.fullmatch(r'[A-Z][A-Z0-9_]*_[0-9]+', p[0]) and p[1] in ('on', 'off')
                and all(re.fullmatch(r'0|[1-9][0-9]*', v) for v in p[2:])
                and 0 <= int(p[2]) <= 1140 and int(p[2]) % 60 == 0
                and int(p[3]) in (0, 30, 40, 50, 60, 70, 80, 100, 120, 150, 180, 240)):
            return value
    raise ValueError('코스|야간건조 on/off|예약분|건조분 형식이에요. 입력은 대기 선택이며 시작 버튼을 눌러야 가동돼요.')

def is_canonical_program(value):
    try:
        return canonical_program(value) == value
    except ValueError:
        return False
