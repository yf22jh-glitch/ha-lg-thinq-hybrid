"""Reservation form vocabulary only. The producer owns protocol/state validation."""
import re

MODEL = 'HWWA9X3C_F2U'
ENABLED = 'vacuum.dust_emptying_reservation.enabled'
SCHEDULE = 'vacuum.dust_emptying_reservation.schedule'
SCHEMA = 'vacuum-reservation-schedule-v1'
DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
KO_DAYS = ('월', '화', '수', '목', '금', '토', '일')


def canonical_schedule(value: str) -> str:
    """Accept HH:MM|한번 or HH:MM|월,수,금; send canonical ordered vocabulary."""
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('예약은 21:30|월,수,금 또는 21:30|한번 형식이에요.')
    parts = value.strip().split('|')
    if len(parts) != 2 or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', parts[0]):
        raise ValueError('예약 시간·요일 형식이 올바르지 않아요.')
    if parts[1] in ('once', '한번'):
        return parts[0] + '|once'
    aliases = dict(zip(KO_DAYS, DAYS))
    days = [aliases.get(day, day) for day in parts[1].split(',')]
    if not days or len(days) != len(set(days)) or any(day not in DAYS for day in days):
        raise ValueError('예약 요일은 월~일 중 중복 없이 선택해 주세요.')
    return parts[0] + '|' + ','.join(day for day in DAYS if day in days)


def is_canonical_schedule(value: object) -> bool:
    try:
        return isinstance(value, str) and canonical_schedule(value) == value
    except ValueError:
        return False


def display_schedule(value: str) -> str:
    if value == 'unset':
        return ''
    value = canonical_schedule(value)
    time, days = value.split('|')
    aliases = dict(zip(DAYS, KO_DAYS))
    return time + '|' + ('한번' if days == 'once' else ','.join(aliases[day] for day in days.split(',')))
