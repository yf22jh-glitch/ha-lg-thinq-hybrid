"""Form validation only; the producer owns UTC conversion and device protocol."""
import re

MODEL = '1WPD4CMIDR__3'
WINDOW = 'water.do_not_disturb.window_kst'
SCHEMA = 'water-dnd-window-kst-v1'

def canonical_window(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]0-(?:[01]\d|2[0-3]):[0-5]0', value):
        raise ValueError('한국 시각으로 22:10-05:20처럼 입력해 주세요. 10분 간격이에요.')
    start=int(value[:2])*60+int(value[3:5]); end=int(value[6:8])*60+int(value[9:11])
    duration=(end-start)%1440
    if not 0 < duration < 720:
        raise ValueError('방해 금지 구간은 0시간보다 길고 12시간보다 짧아야 해요.')
    return value

def is_canonical_window(value: object) -> bool:
    try:
        return isinstance(value,str) and canonical_window(value)==value
    except ValueError:
        return False
