"""Exact form vocabulary only; appliance bytes and date preservation live in the producer."""
import re

MODEL = '1WPD4CMIDR__3'
PRESETS = 'water.amount_presets_ml'
STERILIZATION = 'water.sterilization_calendar'
HOT_TEMPERATURE_PRESETS = 'water.hot_temperature_presets'
CUSTOM_RECIPES = tuple(f'water.custom_recipe_{slot}.program' for slot in range(1, 5))
SCHEMAS = {
    PRESETS: 'water-amount-presets-ml-v1',
    STERILIZATION: 'water-sterilization-calendar-v1',
    HOT_TEMPERATURE_PRESETS: 'water-hot-temperature-presets-v1',
    **{capability: 'water-custom-recipe-replacement-v1' for capability in CUSTOM_RECIPES},
}


def _canonical_recipe(value):
    if value == 'off':
        return value
    parts = value.split('|') if isinstance(value, str) and len(value) <= 64 else ()
    if len(parts) != 5 or parts[0] != 'replace' or parts[1] not in ('hot', 'normal', 'cold'):
        raise ValueError
    water_type, amount, temperature, timer = parts[1:]
    if amount == 'continuous':
        if water_type == 'hot':
            raise ValueError
    elif not re.fullmatch(r'0|[1-9][0-9]*', amount) or not 120 <= int(amount) <= 1000 or int(amount) % 10:
        raise ValueError
    if water_type == 'hot':
        if temperature not in ('40', '50', '60', '70', '80', '90'):
            raise ValueError
    elif temperature != '-':
        raise ValueError
    if timer != 'off':
        if not re.fullmatch(r'(?:0[0-9]|1[0-9]):[0-5][0-9]', timer) or timer == '00:00':
            raise ValueError
    return value

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
    if capability == HOT_TEMPERATURE_PRESETS and re.fullmatch(r'[0-9]+,[0-9]+,[0-9]+', value):
        values = value.split(',')
        if all(item in ('40', '50', '60', '70', '80', '90') for item in values):
            return value
    if capability in CUSTOM_RECIPES:
        try:
            return _canonical_recipe(value)
        except ValueError:
            pass
    raise ValueError(
        '출수량은 120,250,500,1000, 온수는 40,60,90, 살균은 MM-DD HH:MM, '
        '레시피는 replace|종류|양|온도|분:초 또는 off 형식이에요.'
    )

def is_canonical_parameter(capability, value):
    try:
        return canonical_parameter(capability, value) == value
    except ValueError:
        return False
