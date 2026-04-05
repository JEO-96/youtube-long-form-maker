"""영상 첫 5초 Hook 시스템 - 이탈 방지."""

from __future__ import annotations

import logging
import random
from enum import Enum
from typing import Any

from pydantic import BaseModel

from ..core.config import load_settings

logger = logging.getLogger(__name__)


class HookStyle(str, Enum):
    """Hook 스타일 (최소 5개+)."""
    PROBLEM = "problem"                 # 문제 제기형: "당신의 통장이 0원인 이유"
    LOSS_AVERSION = "loss_aversion"     # 손해 회피형: "이것 모르면 매달 30만원 손해"
    CURIOSITY = "curiosity"             # 궁금증 유발형: "전문가가 절대 말 안 하는 것"
    COMPARISON = "comparison"           # 비교/대조형: "부자 vs 빈자, 단 하나의 차이"
    SHOCKING = "shocking"               # 충격/반전형: "이 사실을 알고 나면 잠이 안 옵니다"
    STATISTIC = "statistic"             # 통계 제시형: "상위 1%만 아는 방법"
    STORY = "story"                     # 스토리 시작형: "3년 전, 나는 전 재산을 잃었습니다"


# 스타일별 템플릿 (한국어)
HOOK_TEMPLATES: dict[HookStyle, list[str]] = {
    HookStyle.PROBLEM: [
        "왜 {audience}의 90%가 {topic}에서 실패할까요?",
        "{topic}, 당신이 계속 망하는 진짜 이유",
        "지금 {topic} 잘못하고 있다는 3가지 신호",
    ],
    HookStyle.LOSS_AVERSION: [
        "이것 모르면 {topic}에서 매달 돈을 잃고 있습니다",
        "{topic} 안 하면 5년 후 후회할 겁니다",
        "지금 {topic} 시작 안 하면 이미 늦습니다",
    ],
    HookStyle.CURIOSITY: [
        "전문가들이 절대 알려주지 않는 {topic}의 비밀",
        "{topic}에 대해 아무도 말 안 하는 것",
        "검색해도 안 나오는 {topic}의 진실",
    ],
    HookStyle.COMPARISON: [
        "{topic} 잘하는 사람 vs 못하는 사람, 딱 하나 차이",
        "상위 1% {audience}와 나머지의 {topic} 차이",
        "{topic}에서 성공하는 사람의 공통점 3가지",
    ],
    HookStyle.SHOCKING: [
        "이 영상을 보고 나면 {topic}이 완전히 달라집니다",
        "{topic}에 대해 충격적인 사실을 알게 됐습니다",
        "솔직히 {topic}은 다 거짓말이었습니다",
    ],
    HookStyle.STATISTIC: [
        "{audience}의 87%가 {topic}을 잘못 알고 있습니다",
        "{topic}으로 월 100만원 버는 사람이 전체의 3%인 이유",
        "데이터가 증명하는 {topic}의 최적 전략",
    ],
    HookStyle.STORY: [
        "3년 전, 저는 {topic} 때문에 전부 잃었습니다",
        "{topic}을 시작한 지 6개월, 인생이 바뀌었습니다",
        "저도 처음에는 {topic}이 무서웠습니다",
    ],
}


class HookResult(BaseModel):
    """Hook 생성 결과."""
    style: HookStyle
    hook_text: str
    duration_seconds: float
    style_description: str


class HookSystem:
    """영상 첫 5초 Hook 생성 시스템."""

    def __init__(self, seed: int | None = None) -> None:
        settings = load_settings()
        self.hook_duration = settings.retention.hook_duration_seconds
        self._rng = random.Random(seed)
        self._recent_styles: list[HookStyle] = []
        self._max_recent = 3  # 최근 3개 스타일 중복 방지

    def generate_hook(
        self,
        topic: str,
        audience: str = "",
        preferred_style: HookStyle | None = None,
        channel_tone: str = "",
    ) -> HookResult:
        """Hook 텍스트 생성.

        Args:
            topic: 영상 주제 (예: "ETF 투자")
            audience: 타겟 오디언스 (예: "사회초년생")
            preferred_style: 지정 스타일 (None이면 자동 선택)
            channel_tone: 채널 톤 (예: "신뢰감 있고 이해하기 쉬운")
        """
        if not topic:
            raise ValueError("topic은 필수입니다")

        audience = audience or "시청자"

        # 스타일 선택
        style = preferred_style or self._select_style()

        # 템플릿에서 Hook 텍스트 생성
        templates = HOOK_TEMPLATES[style]
        template = self._rng.choice(templates)
        hook_text = template.format(topic=topic, audience=audience)

        # 최근 스타일 기록
        self._recent_styles.append(style)
        if len(self._recent_styles) > self._max_recent:
            self._recent_styles.pop(0)

        result = HookResult(
            style=style,
            hook_text=hook_text,
            duration_seconds=float(self.hook_duration),
            style_description=self._style_description(style),
        )

        logger.info(f"Hook generated: [{style.value}] {hook_text}")
        return result

    def generate_multiple(
        self,
        topic: str,
        audience: str = "",
        count: int = 3,
    ) -> list[HookResult]:
        """여러 스타일의 Hook 후보 생성 (A/B 테스트용)."""
        if count < 1:
            raise ValueError("count는 1 이상이어야 합니다")

        styles = list(HookStyle)
        self._rng.shuffle(styles)
        selected = styles[:min(count, len(styles))]

        return [
            self.generate_hook(topic=topic, audience=audience, preferred_style=s)
            for s in selected
        ]

    def _select_style(self) -> HookStyle:
        """중복 방지 기반 스타일 자동 선택."""
        available = [s for s in HookStyle if s not in self._recent_styles]
        if not available:
            available = list(HookStyle)
        return self._rng.choice(available)

    @staticmethod
    def _style_description(style: HookStyle) -> str:
        """스타일 설명 (디버깅/로깅용)."""
        descriptions = {
            HookStyle.PROBLEM: "문제 제기형: 시청자의 문제를 직접 지적",
            HookStyle.LOSS_AVERSION: "손해 회피형: 안 하면 손해라는 심리 자극",
            HookStyle.CURIOSITY: "궁금증 유발형: 비밀/진실을 암시",
            HookStyle.COMPARISON: "비교/대조형: 성공 vs 실패 대비",
            HookStyle.SHOCKING: "충격/반전형: 강한 감정 유발",
            HookStyle.STATISTIC: "통계 제시형: 데이터로 신뢰 확보",
            HookStyle.STORY: "스토리 시작형: 개인 경험으로 공감",
        }
        return descriptions.get(style, style.value)
