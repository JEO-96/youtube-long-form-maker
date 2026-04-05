"""루프형 영상 구조 - 끝 → 시작 재시청 유도.

원리: 영상 마지막에 시작과 연결되는 요소를 배치하여
      시청자가 다시 처음부터 보고 싶게 만드는 구조.

기법:
    - 오프닝 떡밥 회수 (Opening Callback)
    - 미완결 느낌 (Open Loop)
    - 시리즈 연결 (Series Hook)
    - 반전 클로징 (Twist Ending)
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class LoopStyle(str, Enum):
    """루프 스타일."""
    OPENING_CALLBACK = "opening_callback"  # 오프닝 떡밥 회수
    OPEN_LOOP = "open_loop"  # 미완결 느낌
    SERIES_HOOK = "series_hook"  # 시리즈 연결
    TWIST_ENDING = "twist_ending"  # 반전 클로징
    CHALLENGE = "challenge"  # 시청자 도전 유도


@dataclass
class LoopElement:
    """루프 구조 요소."""
    style: LoopStyle
    opening_text: str  # 영상 시작에 삽입할 텍스트
    closing_text: str  # 영상 끝에 삽입할 텍스트
    connection_hint: str  # 시작-끝 연결 설명
    cta_overlay: str = ""  # 화면 오버레이 텍스트


# 스타일별 템플릿 (한국어)
LOOP_TEMPLATES: dict[LoopStyle, list[dict[str, str]]] = {
    LoopStyle.OPENING_CALLBACK: [
        {
            "opening": "오늘 영상 끝까지 보시면, {topic}에 대해 완전히 다른 시각을 갖게 될 겁니다.",
            "closing": "처음에 말씀드렸듯이, 이제 {topic}이 완전히 다르게 보이시죠? 다시 처음부터 보시면 더 많은 걸 발견하실 겁니다.",
            "hint": "오프닝 약속 회수",
        },
        {
            "opening": "영상 마지막에 {topic}의 가장 중요한 비밀을 알려드립니다.",
            "closing": "처음부터 다시 보시면, 제가 곳곳에 숨겨둔 힌트를 발견하실 겁니다.",
            "hint": "숨겨진 힌트 회수",
        },
    ],
    LoopStyle.OPEN_LOOP: [
        {
            "opening": "{topic}, 정말 이게 전부일까요?",
            "closing": "하지만 아직 한 가지 더 있습니다... 다음 영상에서 공개합니다. 그 전에, 이 영상을 한 번 더 보시면 힌트를 찾으실 수 있어요.",
            "hint": "미완결 떡밥",
        },
    ],
    LoopStyle.SERIES_HOOK: [
        {
            "opening": "이 시리즈의 핵심을 이해하려면, 오늘 영상이 가장 중요합니다.",
            "closing": "다음 편이 나오기 전에, 이 영상의 핵심 포인트를 한 번 더 정리해보세요. 처음부터 다시 보시면 놓친 부분이 보일 겁니다.",
            "hint": "시리즈 연결",
        },
    ],
    LoopStyle.TWIST_ENDING: [
        {
            "opening": "{topic}에 대해 알고 계신 것, 사실은 틀렸을 수 있습니다.",
            "closing": "그래서 처음에 말씀드렸던 것이 사실은 이런 의미였습니다. 다시 처음부터 보시면 완전히 다르게 느껴지실 거예요.",
            "hint": "반전 연결",
        },
    ],
    LoopStyle.CHALLENGE: [
        {
            "opening": "오늘 {topic} 영상에서 핵심 포인트 3개를 찾아보세요.",
            "closing": "3개 다 찾으셨나요? 다시 처음부터 확인해보세요! 댓글에 답을 적어주세요.",
            "hint": "시청자 참여 루프",
        },
    ],
}


class LoopStructureEngine:
    """루프형 영상 구조 생성기."""

    def __init__(self, seed: int | None = None) -> None:
        self.rng = random.Random(seed)

    def generate(
        self,
        topic: str,
        preferred_style: LoopStyle | None = None,
        channel_tone: str = "",
    ) -> LoopElement:
        """루프 구조 요소 생성.

        Args:
            topic: 영상 주제
            preferred_style: 선호 스타일 (None이면 랜덤)
            channel_tone: 채널 톤 (참고용)

        Returns:
            LoopElement
        """
        style = preferred_style or self.rng.choice(list(LoopStyle))
        templates = LOOP_TEMPLATES.get(style, LOOP_TEMPLATES[LoopStyle.OPENING_CALLBACK])
        template = self.rng.choice(templates)

        opening = template["opening"].format(topic=topic)
        closing = template["closing"].format(topic=topic)
        hint = template["hint"]

        # CTA 오버레이
        cta = f"🔄 다시 보기 → 새로운 발견이 기다립니다"

        element = LoopElement(
            style=style,
            opening_text=opening,
            closing_text=closing,
            connection_hint=hint,
            cta_overlay=cta,
        )

        logger.info(f"Loop structure: [{style.value}] {hint}")
        return element

    def generate_multiple(
        self, topic: str, count: int = 3
    ) -> list[LoopElement]:
        """여러 스타일의 루프 후보 생성."""
        styles = list(LoopStyle)
        self.rng.shuffle(styles)
        results = []
        for style in styles[:count]:
            results.append(self.generate(topic, preferred_style=style))
        return results
