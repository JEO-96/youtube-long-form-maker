"""한국어 숫자 읽기 전처리 — TTS 발음 교정.

ElevenLabs 등 TTS 엔진이 "3가지"를 "삼가지"로 잘못 읽는 문제를 해결.
숫자+조수사 패턴을 올바른 한국어 읽기로 변환한다.

규칙:
  - 고유어 수사 (한/두/세/네/다섯...): 개, 가지, 명, 살, 시, 마리 등
  - 한자어 수사 (일/이/삼/사/오...): 원, 월, 일, 년, 퍼센트 등
  - 숫자만 단독: 변환하지 않음 (TTS가 맥락에 맞게 읽도록)
"""

from __future__ import annotations

import re
import logging

logger = logging.getLogger(__name__)

# ═══ 고유어 수 (native Korean numbers) ═══

# 관형사형 (조수사 앞에서 쓰는 형태)
_NATIVE_COUNTER_FORM: dict[int, str] = {
    1: "한", 2: "두", 3: "세", 4: "네", 5: "다섯",
    6: "여섯", 7: "일곱", 8: "여덟", 9: "아홉", 10: "열",
    11: "열한", 12: "열두", 13: "열세", 14: "열네", 15: "열다섯",
    16: "열여섯", 17: "열일곱", 18: "열여덟", 19: "열아홉", 20: "스무",
    21: "스물한", 22: "스물두", 23: "스물세", 24: "스물네", 25: "스물다섯",
    26: "스물여섯", 27: "스물일곱", 28: "스물여덟", 29: "스물아홉",
    30: "서른", 40: "마흔", 50: "쉰", 60: "예순", 70: "일흔",
    80: "여든", 90: "아흔",
}

# 30~99 범위의 합성어 생성 (31~39, 41~49, ... 91~99)
_ONES_COUNTER = {
    1: "한", 2: "두", 3: "세", 4: "네", 5: "다섯",
    6: "여섯", 7: "일곱", 8: "여덟", 9: "아홉",
}
_TENS_COUNTER = {
    3: "서른", 4: "마흔", 5: "쉰", 6: "예순",
    7: "일흔", 8: "여든", 9: "아흔",
}

for _t in range(3, 10):
    for _o in range(1, 10):
        _NATIVE_COUNTER_FORM[_t * 10 + _o] = f"{_TENS_COUNTER[_t]}{_ONES_COUNTER[_o]}"


def _native_korean_counter(n: int) -> str | None:
    """정수 → 고유어 관형사형. 1~99 범위만 지원, 범위 밖이면 None."""
    return _NATIVE_COUNTER_FORM.get(n)


# ═══ 고유어 조수사 목록 (숫자 뒤에 올 때 고유어로 읽어야 하는 단위) ═══

_NATIVE_COUNTERS: set[str] = {
    # 일반
    "개", "가지", "명", "분", "살", "마리", "번", "잔",
    "병", "송이", "그루", "벌", "채", "대", "켤레",
    "장", "권", "자루", "줄", "쌍", "통", "포기",
    "곳", "군데", "달", "시간", "가구",
    "갑", "곡", "그릇", "꼬치", "다발", "도막", "뭉치",
    "바퀴", "봉지", "사람", "알", "접시", "조각",
    "토막", "판", "포대", "몫", "뿌리", "박스", "봉",
    "컵", "그릇", "끼", "종류",
    # 시간 관련
    "시", "시간",
}

# ═══ 한자어 조수사 목록 (숫자를 한자어로 읽어야 하는 단위) ═══
# → 이 경우에는 아라비아 숫자를 그대로 두면 TTS가 한자어로 읽으므로 변환 불필요
# 참고용으로만 나열
_SINO_COUNTERS: set[str] = {
    "원", "월", "일", "년", "초", "층", "호",
    "번지", "퍼센트", "프로", "도", "인분",
    "학년", "회", "주", "세기", "주년", "기", "차",
    "세대", "평", "조", "억", "만", "천", "백",
}


# ═══ 메인 전처리 함수 ═══

# 패턴: 숫자 + (선택적 공백) + 고유어 조수사
_COUNTER_PATTERN = re.compile(
    r'(\d{1,2})\s*(' + '|'.join(sorted(_NATIVE_COUNTERS, key=len, reverse=True)) + r')(?=[\s,.\n!?다요죠까니고는을를이가의에도]|$)'
)


def preprocess_korean_numbers(text: str) -> str:
    """TTS 전처리: 숫자+고유어조수사 패턴을 올바른 한국어 읽기로 변환.

    예시:
        "3가지" → "세 가지"
        "5개"   → "다섯 개"
        "10명"  → "열 명"
        "24시간" → "스물네 시간"
        "1000원" → "1000원" (한자어 → 변환 없음)
        "2025년" → "2025년" (한자어 → 변환 없음)

    Args:
        text: 원본 텍스트

    Returns:
        전처리된 텍스트
    """
    replacements: list[tuple[str, str]] = []

    def _replace_match(m: re.Match) -> str:
        num_str = m.group(1)
        counter = m.group(2)
        n = int(num_str)

        native = _native_korean_counter(n)
        if native is None:
            # 99 초과: 변환하지 않음 (TTS가 적절히 읽음)
            return m.group(0)

        result = f"{native} {counter}"
        replacements.append((m.group(0), result))
        return result

    result = _COUNTER_PATTERN.sub(_replace_match, text)

    if replacements:
        samples = replacements[:5]
        sample_str = ", ".join(f'"{old}"→"{new}"' for old, new in samples)
        logger.info(f"한국어 숫자 전처리: {len(replacements)}건 변환 ({sample_str})")

    return result
