"""자연어 명령 → 파이프라인 매핑 핸들러.

사용자의 자연어 명령을 분석하여 적절한 파이프라인 동작으로 변환.

예:
    "재테크 채널에 사회초년생 통장관리 영상 만들어줘"
    → produce(channel="finance", topic="사회초년생 통장관리")

    "finance_20260405_001 영상 대본만 다시 써줘"
    → resume(production_id="finance_20260405_001", start_stage="script")

    "이번 달 비용 얼마나 썼어?"
    → costs(month="2026-04")
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ParsedCommand:
    """파싱된 명령어."""
    action: str  # produce, resume, status, costs, analyze, shorts
    channel_id: str = ""
    topic: str = ""
    production_id: str = ""
    stage: str = ""
    month: str = ""
    dry_run: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0  # 파싱 확신도 (0~1)
    raw_input: str = ""


# 채널 이름 → ID 매핑
CHANNEL_ALIASES: dict[str, str] = {
    "재테크": "finance",
    "투자": "finance",
    "금융": "finance",
    "비즈니스": "business",
    "창업": "business",
    "ai": "ai",
    "인공지능": "ai",
    "테크": "ai",
    "건강": "health",
    "웰빙": "health",
    "부동산": "realestate",
}

# 액션 키워드 매핑
ACTION_PATTERNS = {
    "produce": [
        r"영상\s*(?:만들어|제작|생성)",
        r"새\s*영상",
        r"만들어\s*(?:줘|주세요)",
        r"produce",
    ],
    "resume": [
        r"재개|다시\s*시작|이어|resume",
        r"대본.*다시\s*(?:써|작성)",
        r"실패.*복구",
    ],
    "status": [
        r"상태|현황|목록|status",
        r"어떻게\s*되고\s*있",
    ],
    "costs": [
        r"비용|돈|얼마|cost",
        r"이번\s*달",
    ],
    "analyze": [
        r"분석|성과|analytics",
        r"조회수|CTR|시청",
    ],
    "shorts": [
        r"쇼츠|shorts",
        r"짧은\s*영상",
    ],
}

# 프로덕션 ID 패턴
PRODUCTION_ID_PATTERN = re.compile(r'([a-z]+_\d{8}_\d{6})')


class CommandParser:
    """자연어 명령을 ParsedCommand로 변환."""

    def parse(self, text: str) -> ParsedCommand:
        """자연어 텍스트 파싱.

        Args:
            text: 사용자 입력

        Returns:
            ParsedCommand
        """
        text = text.strip()
        cmd = ParsedCommand(action="unknown", raw_input=text)

        # 1. 액션 감지
        cmd.action = self._detect_action(text)

        # 2. 채널 감지
        cmd.channel_id = self._detect_channel(text)

        # 3. 프로덕션 ID 감지
        cmd.production_id = self._detect_production_id(text)

        # 4. 주제 추출
        cmd.topic = self._extract_topic(text, cmd.channel_id)

        # 5. 스테이지 감지 (resume용)
        cmd.stage = self._detect_stage(text)

        # 6. 월 감지 (costs용)
        cmd.month = self._detect_month(text)

        # 7. dry_run 감지
        cmd.dry_run = any(w in text.lower() for w in ["테스트", "dry", "시험"])

        # 8. 확신도 계산
        cmd.confidence = self._calculate_confidence(cmd)

        logger.info(
            f"Parsed: action={cmd.action}, channel={cmd.channel_id}, "
            f"topic='{cmd.topic[:30]}', confidence={cmd.confidence:.2f}"
        )
        return cmd

    def _detect_action(self, text: str) -> str:
        """액션 감지."""
        for action, patterns in ACTION_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, text, re.IGNORECASE):
                    return action
        return "produce"  # 기본값

    @staticmethod
    def _detect_channel(text: str) -> str:
        """채널 감지."""
        text_lower = text.lower()
        for alias, channel_id in CHANNEL_ALIASES.items():
            if alias in text_lower:
                return channel_id
        # channel_XXX 패턴 직접 매칭
        match = re.search(r'channel[_\s]*(\w+)', text_lower)
        if match:
            return match.group(1)
        return ""

    @staticmethod
    def _detect_production_id(text: str) -> str:
        """프로덕션 ID 감지."""
        match = PRODUCTION_ID_PATTERN.search(text)
        return match.group(1) if match else ""

    @staticmethod
    def _extract_topic(text: str, channel_id: str) -> str:
        """주제 추출 - 채널/액션 키워드 제거 후 핵심 추출."""
        # 불필요한 부분 제거
        cleaned = text
        remove_patterns = [
            r"영상\s*(?:만들어|제작|생성).*$",
            r"만들어\s*(?:줘|주세요|봐).*$",
            r"(?:재테크|투자|건강|AI|테크|비즈니스)\s*채널에?\s*",
            r"^(?:새\s*)?영상\s*",
        ]
        for pattern in remove_patterns:
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE).strip()

        # 조사 제거
        cleaned = re.sub(r'\s*(?:에|를|을|에서|으로|이|가)\s*$', '', cleaned)

        return cleaned.strip() if len(cleaned) > 2 else ""

    @staticmethod
    def _detect_stage(text: str) -> str:
        """스테이지 감지."""
        stage_map = {
            "대본": "script",
            "스크립트": "script",
            "음성": "voice",
            "tts": "voice",
            "미디어": "media",
            "영상": "media",
            "편집": "editing",
            "썸네일": "thumbnail",
        }
        text_lower = text.lower()
        for keyword, stage in stage_map.items():
            if keyword in text_lower:
                return stage
        return ""

    @staticmethod
    def _detect_month(text: str) -> str:
        """월 감지."""
        from datetime import datetime
        if "이번 달" in text or "이번달" in text:
            return datetime.now().strftime("%Y-%m")
        match = re.search(r'(\d{4})[.-](\d{1,2})', text)
        if match:
            return f"{match.group(1)}-{int(match.group(2)):02d}"
        return ""

    @staticmethod
    def _calculate_confidence(cmd: ParsedCommand) -> float:
        """파싱 확신도 계산."""
        score = 0.0

        if cmd.action != "unknown":
            score += 0.3

        if cmd.action == "produce":
            if cmd.channel_id:
                score += 0.3
            if cmd.topic:
                score += 0.3
        elif cmd.action == "resume":
            if cmd.production_id:
                score += 0.5
        elif cmd.action == "status":
            score += 0.4
        elif cmd.action == "costs":
            score += 0.4

        return min(score + 0.1, 1.0)
