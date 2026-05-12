"""비주얼 제목 유틸.

예전에는 이 모듈이 Pillow 도형 기반 배경 이미지까지 생성했지만,
실영상 품질이 낮아져 런타임 렌더링 코드는 제거했다.
"""

from __future__ import annotations

import re


# 기존 호출부/테스트 호환을 위한 표준 캔버스 상수 (롱폼 16:9).
W, H = 1920, 1080
STANDARD_WIDTH, STANDARD_HEIGHT = W, H

# 쇼츠 캔버스 상수 (9:16) — S5 Pillow 카드 렌더에서 사용.
SHORTS_WIDTH = 1080
SHORTS_HEIGHT = 1920
SHORTS_SUBTITLE_SAFE_MARGIN = 280  # 세로 화면은 하단 자막 영역을 더 넓게


_INTENT_FALLBACK_TITLE: dict[str, str] = {
    "chart": "데이터 분석",
    "checklist": "확인 사항",
    "comparison_card": "비교 분석",
    "infographic": "핵심 정보",
    "emphasis_caption": "핵심 포인트",
    "real_broll": "현장 화면",
    "map": "지역 분석",
    "talking_head_style": "전문가 분석",
    "closing_cta": "구독 안내",
}


def derive_scene_title(
    narration: str = "",
    vis_desc: str = "",
    intent: str = "",
    max_len: int = 24,
) -> str:
    """씬별 meaningful 제목 생성. 빈 문자열은 반환하지 않는다."""
    if vis_desc and len(vis_desc.strip()) >= 5:
        title = _extract_first_phrase(vis_desc, max_len)
        if title:
            return title

    if narration and len(narration.strip()) >= 5:
        title = _extract_first_phrase(narration, max_len)
        if title:
            return title

    return _INTENT_FALLBACK_TITLE.get(intent, "핵심 내용")


def _extract_first_phrase(text: str, max_len: int = 24) -> str:
    """텍스트에서 첫 의미 있는 구절을 max_len 이내로 추출."""
    text = text.strip()
    if not text:
        return ""

    match = re.match(r"^(.+?[.!?다요죠까니])\s", text)
    first = match.group(1) if match else text

    if len(first) <= max_len:
        return first

    cut = first[:max_len]
    last_space = cut.rfind(" ")
    last_comma = max(cut.rfind(","), cut.rfind("，"))
    best = max(last_space, last_comma)
    if best > max_len * 0.4:
        cut = cut[:best].rstrip(",，. ")

    if len(cut) < 5:
        cut = first[:max_len]

    return cut
