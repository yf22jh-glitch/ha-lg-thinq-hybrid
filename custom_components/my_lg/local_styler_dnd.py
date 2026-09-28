"""Form validation for the Styler do-not-disturb reservation bundle."""

import re

MODEL = "ST_R_ETH01Y_"
RESERVATION = "styler.do_not_disturb.reservation"
SCHEMA = "styler-do-not-disturb-reservation-v1"

_RESERVATION = re.compile(
    r"^(on|off)\|((?:[01]\d|2[0-3]):[0-5]\d)-"
    r"((?:[01]\d|2[0-3]):[0-5]\d)\|(on|off)$"
)
_DISABLED_RESET = "off|00:00-00:00|off"


def canonical_reservation(value: str) -> str:
    """Return the exact producer value or reject a partial/ambiguous edit."""
    if not isinstance(value, str) or (match := _RESERVATION.fullmatch(value)) is None:
        raise ValueError(
            "on|01:00-02:00|off처럼 사용 여부|시작-종료|음소거를 함께 입력해 주세요."
        )
    enabled, start, end, _mute = match.groups()
    if enabled == "on" and start == end:
        raise ValueError("방해 금지를 켤 때 시작과 종료 시각은 달라야 해요.")
    if enabled == "off" and value != _DISABLED_RESET:
        raise ValueError("끄기는 off|00:00-00:00|off 전체 초기화만 지원해요.")
    return value


def is_canonical_reservation(value: object) -> bool:
    """Whether a cached producer value is safe to expose as editable state."""
    try:
        return isinstance(value, str) and canonical_reservation(value) == value
    except ValueError:
        return False
