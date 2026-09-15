"""Exact form vocabulary only; appliance bytes and date preservation live in the producer."""
import re

MODEL = '1WPD4CMIDR__3'
PRESETS = 'water.amount_presets_ml'
STERILIZATION = 'water.sterilization_calendar'
SCHEMAS = {PRESETS: 'water-amount-presets-ml-v1', STERILIZATION: 'water-sterilization-calendar-v1'}

def canonical_parameter(capability, value):
    if not isinstance(value, str):
        raise ValueError('설정값을 문자열로 입력해 주세요.')
    if capability == PRESETS and re.fullmatch(r'[0-9]+,[0-9]+,[0-9]+,[0-9]+', value):
        values = [int(v) for v in value.split(',')]
        if ','.join(str(v) for v in values) == value and all(120 <= n <= 1000 and n % 10 == 0 for n in values):
            return value
    if capability == STERILIZATION and re.fullmatch(r'[0-9]{2}-[0-9]{2} (?:[01][0-9]|2[0-3]):[0-5][0-9]', value):
        month, day = int(value[:2]), int(value[3:5])
        if 1 <= month <= 12 and 1 <= day <= (31,29,31,30,31,30,31,31,30,31,30,31)[month-1]:
            return value
    raise ValueError('프리셋은 120,250,500,1000 형식, 살균 예약은 현재 날짜를 유지한 MM-DD HH:MM 형식이에요.')

def is_canonical_parameter(capability, value):
    try:
        return canonical_parameter(capability, value) == value
    except ValueError:
        return False
