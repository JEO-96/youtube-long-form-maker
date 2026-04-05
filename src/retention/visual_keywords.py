"""Visual Key Keyword - 화면 텍스트/아이콘 팝업 자동화.

대본의 [Visual_Key_Keyword] 태그를 감지하여
화면에 키워드, 숫자, 아이콘을 팝업으로 표시하는 지시 생성.

목적: 시각적 강조로 시청자 주의 유지 + 핵심 정보 강화
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class VisualType(str, Enum):
    """시각적 강조 유형."""
    TEXT_POPUP = "text_popup"  # 핵심 키워드 텍스트
    NUMBER_HIGHLIGHT = "number_highlight"  # 숫자/통계 강조
    ICON_POPUP = "icon_popup"  # 이모지/아이콘
    COMPARISON = "comparison"  # 비교 (A vs B)
    LIST_ITEM = "list_item"  # 번호 목록
    QUOTE = "quote"  # 인용/강조문


@dataclass
class VisualKeyword:
    """단일 시각적 키워드."""
    visual_type: VisualType
    text: str
    timestamp: float = 0.0  # 표시 시작 시점 (초)
    duration: float = 2.0  # 표시 지속 시간 (초)
    position: str = "center"  # center, top, bottom, left, right
    style: str = "default"  # default, bold, accent, minimal
    icon: str = ""  # 이모지/아이콘 (있으면)
    animation: str = "fade_in"  # fade_in, slide_up, pop, zoom


@dataclass
class VisualKeywordTimeline:
    """영상 전체의 시각적 키워드 타임라인."""
    keywords: list[VisualKeyword] = field(default_factory=list)
    total_count: int = 0

    def add(self, kw: VisualKeyword) -> None:
        self.keywords.append(kw)
        self.total_count = len(self.keywords)


class VisualKeywordEngine:
    """시각적 키워드 자동 추출 + 타임라인 생성."""

    # 숫자 패턴
    NUMBER_PATTERN = re.compile(r'\b(\d+[\d,.]*\s*[%만억원달러배]?)\b')
    # 강조 패턴 (따옴표, 큰 따옴표)
    QUOTE_PATTERN = re.compile(r'["\u201C\u201D"](.*?)["\u201C\u201D"]')
    # 비교 패턴
    COMPARE_PATTERN = re.compile(r'(.+?)\s*(?:vs|VS|대|보다|와|과)\s*(.+)')
    # 리스트 패턴
    LIST_PATTERN = re.compile(r'(?:첫째|둘째|셋째|1\.|2\.|3\.|①|②|③)')

    # 강조할 키워드 (도메인별)
    EMPHASIS_KEYWORDS = {
        "finance": ["수익률", "절세", "복리", "분산투자", "리스크", "포트폴리오", "ETF", "적금"],
        "health": ["면역력", "혈당", "콜레스테롤", "칼로리", "단백질", "수면"],
        "tech": ["AI", "자동화", "생산성", "클라우드", "보안", "API"],
        "general": ["핵심", "비밀", "방법", "전략", "실수", "주의"],
    }

    def __init__(self, niche: str = "general") -> None:
        self.niche = niche
        self.emphasis_words = (
            self.EMPHASIS_KEYWORDS.get(niche, [])
            + self.EMPHASIS_KEYWORDS["general"]
        )

    def extract_from_script(
        self,
        sections: list[dict[str, Any]],
        segments: list[dict[str, Any]] | None = None,
    ) -> VisualKeywordTimeline:
        """대본 섹션에서 시각적 키워드 자동 추출.

        Args:
            sections: ScriptResult.sections (list of dicts)
            segments: VoiceResult.segments (타임스탬프, optional)

        Returns:
            VisualKeywordTimeline
        """
        timeline = VisualKeywordTimeline()
        current_time = 5.0  # 훅 이후 시작

        for sec in sections:
            body = sec.get("body", "")
            header = sec.get("header", "")
            duration = sec.get("estimated_duration_seconds", 60)

            # 섹션 헤더 강조
            timeline.add(VisualKeyword(
                visual_type=VisualType.TEXT_POPUP,
                text=header,
                timestamp=current_time,
                duration=2.5,
                position="top",
                style="bold",
                animation="slide_up",
            ))

            # 본문에서 키워드 추출
            section_keywords = self._extract_from_text(body, current_time + 3.0)
            for kw in section_keywords:
                timeline.add(kw)

            current_time += duration

        logger.info(
            f"Visual keywords: {timeline.total_count} keywords "
            f"from {len(sections)} sections"
        )
        return timeline

    def _extract_from_text(
        self, text: str, base_time: float
    ) -> list[VisualKeyword]:
        """텍스트에서 시각적 키워드 추출."""
        keywords: list[VisualKeyword] = []
        offset = 0.0

        # 1) 숫자/통계 강조
        for match in self.NUMBER_PATTERN.finditer(text):
            num_text = match.group(1)
            keywords.append(VisualKeyword(
                visual_type=VisualType.NUMBER_HIGHLIGHT,
                text=num_text,
                timestamp=base_time + offset,
                duration=2.0,
                position="center",
                style="accent",
                animation="pop",
            ))
            offset += 3.0

        # 2) 인용문 강조
        for match in self.QUOTE_PATTERN.finditer(text):
            quote_text = match.group(1)
            if len(quote_text) < 30:
                keywords.append(VisualKeyword(
                    visual_type=VisualType.QUOTE,
                    text=f'"{quote_text}"',
                    timestamp=base_time + offset,
                    duration=2.5,
                    position="center",
                    style="bold",
                    animation="fade_in",
                ))
                offset += 3.5

        # 3) 도메인 키워드 강조
        for word in self.emphasis_words:
            if word in text:
                keywords.append(VisualKeyword(
                    visual_type=VisualType.TEXT_POPUP,
                    text=word,
                    timestamp=base_time + offset,
                    duration=1.5,
                    position="bottom",
                    style="accent",
                    icon=self._get_icon(word),
                    animation="pop",
                ))
                offset += 2.5

        # 4) 리스트 아이템
        if self.LIST_PATTERN.search(text):
            sentences = [s.strip() for s in text.split(".") if s.strip()]
            for i, sent in enumerate(sentences[:3]):
                if any(p in sent for p in ["첫째", "둘째", "셋째", "1.", "2.", "3."]):
                    keywords.append(VisualKeyword(
                        visual_type=VisualType.LIST_ITEM,
                        text=sent[:30],
                        timestamp=base_time + offset,
                        duration=2.0,
                        position="left",
                        style="default",
                        animation="slide_up",
                    ))
                    offset += 3.0

        return keywords

    @staticmethod
    def _get_icon(keyword: str) -> str:
        """키워드에 맞는 이모지/아이콘."""
        icon_map = {
            "수익률": "📈", "절세": "💰", "복리": "🔄", "리스크": "⚠️",
            "면역력": "🛡️", "칼로리": "🔥", "수면": "😴",
            "AI": "🤖", "자동화": "⚡", "보안": "🔒",
            "핵심": "⭐", "비밀": "🔑", "실수": "❌", "주의": "⚠️",
        }
        return icon_map.get(keyword, "💡")
