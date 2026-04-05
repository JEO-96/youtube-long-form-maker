"""피드백 루프 - 성과 → 다음 영상 전략 수정.

핵심: "조회수 학습 공장"
    영상 업로드 → 7일 후 성과 수집 → 패턴 분석 → 다음 영상 반영

자동 반영 룰:
    CTR < 4%          → 썸네일 텍스트 길이 축소
    30초 이탈 급증     → 훅 길이 20% 단축
    50% 구간 이탈      → 패턴 인터럽트 간격 1초 단축
    평균 시청 < 목표   → 영상 길이 15% 감소
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .analytics import AnalyticsResult, VideoMetrics

logger = logging.getLogger(__name__)


@dataclass
class FeedbackAdjustment:
    """단일 조정 항목."""
    parameter: str  # 조정 대상 (hook_duration, interrupt_interval, video_length 등)
    current_value: Any = None
    recommended_value: Any = None
    reason: str = ""
    confidence: float = 0.0  # 0~1
    auto_apply: bool = False  # 자동 적용 여부


@dataclass
class FeedbackReport:
    """피드백 루프 결과."""
    channel_id: str
    based_on_videos: int = 0
    adjustments: list[FeedbackAdjustment] = field(default_factory=list)
    recurring_patterns: list[str] = field(default_factory=list)
    summary: str = ""


class FeedbackLoop:
    """성과 기반 다음 영상 전략 수정 엔진."""

    # 자동 반영 임계값
    CTR_LOW_THRESHOLD = 4.0
    CTR_HIGH_THRESHOLD = 8.0
    RETENTION_LOW_THRESHOLD = 40.0
    EARLY_DROP_THRESHOLD = 30  # 초
    WATCH_TIME_MIN_MINUTES = 3

    def analyze(
        self,
        analytics: AnalyticsResult,
        current_settings: dict[str, Any] | None = None,
    ) -> FeedbackReport:
        """성과 분석 결과 → 조정 보고서 생성.

        Args:
            analytics: AnalyticsEngine 결과
            current_settings: 현재 채널/영상 설정값

        Returns:
            FeedbackReport with 조정 항목들
        """
        current = current_settings or {}
        adjustments: list[FeedbackAdjustment] = []
        patterns: list[str] = []

        metrics = analytics.metrics
        if not metrics:
            return FeedbackReport(
                channel_id=analytics.channel_id,
                summary="데이터 부족 - 최소 1개 영상 성과 필요",
            )

        # 1. CTR 분석 → 썸네일/제목 조정
        adjustments.extend(self._analyze_ctr(metrics, current))

        # 2. 이탈 분석 → 훅/인터럽트 조정
        adjustments.extend(self._analyze_retention(metrics, current))

        # 3. 시청 시간 분석 → 영상 길이 조정
        adjustments.extend(self._analyze_watch_time(metrics, current))

        # 4. 성공 패턴 식별 → recurring_pattern
        patterns = self._identify_patterns(metrics)

        # 보고서 요약
        auto_count = sum(1 for a in adjustments if a.auto_apply)
        summary = (
            f"{len(metrics)}개 영상 분석 → "
            f"{len(adjustments)}개 조정 항목 "
            f"(자동 적용: {auto_count}개)"
        )

        report = FeedbackReport(
            channel_id=analytics.channel_id,
            based_on_videos=len(metrics),
            adjustments=adjustments,
            recurring_patterns=patterns,
            summary=summary,
        )

        logger.info(f"Feedback: {summary}")
        return report

    def _analyze_ctr(
        self, metrics: list[VideoMetrics], current: dict
    ) -> list[FeedbackAdjustment]:
        """CTR 분석."""
        adjustments = []
        avg_ctr = sum(m.ctr for m in metrics) / len(metrics) if metrics else 0

        if avg_ctr < self.CTR_LOW_THRESHOLD:
            adjustments.append(FeedbackAdjustment(
                parameter="thumbnail_text_length",
                current_value=current.get("thumbnail_text_length", "normal"),
                recommended_value="shorter",
                reason=f"CTR {avg_ctr:.1f}% < {self.CTR_LOW_THRESHOLD}% → 텍스트 축소",
                confidence=0.7,
                auto_apply=True,
            ))
            adjustments.append(FeedbackAdjustment(
                parameter="title_style",
                current_value=current.get("title_style", "informative"),
                recommended_value="curiosity_gap",
                reason=f"CTR 개선 필요 → 호기심 유발형 제목 시도",
                confidence=0.5,
                auto_apply=False,
            ))

        elif avg_ctr > self.CTR_HIGH_THRESHOLD:
            adjustments.append(FeedbackAdjustment(
                parameter="thumbnail_pattern",
                current_value="current",
                recommended_value="lock_current",
                reason=f"CTR {avg_ctr:.1f}% 우수 → 현재 패턴 유지",
                confidence=0.9,
                auto_apply=True,
            ))

        return adjustments

    def _analyze_retention(
        self, metrics: list[VideoMetrics], current: dict
    ) -> list[FeedbackAdjustment]:
        """이탈 분석."""
        adjustments = []

        # 30초 이탈 체크
        early_drop_videos = [
            m for m in metrics
            if any(p < self.EARLY_DROP_THRESHOLD for p in m.drop_off_points)
        ]
        if len(early_drop_videos) > len(metrics) * 0.3:
            current_hook = current.get("hook_duration_seconds", 5)
            adjustments.append(FeedbackAdjustment(
                parameter="hook_duration_seconds",
                current_value=current_hook,
                recommended_value=max(3, int(current_hook * 0.8)),
                reason="30초 이탈 급증 → 훅 길이 20% 단축",
                confidence=0.8,
                auto_apply=True,
            ))

        # 평균 잔존율
        avg_retention = (
            sum(m.average_view_percentage for m in metrics) / len(metrics)
            if metrics else 0
        )
        if avg_retention < self.RETENTION_LOW_THRESHOLD:
            current_interval = current.get("pattern_interrupt_max_interval", 9.0)
            adjustments.append(FeedbackAdjustment(
                parameter="pattern_interrupt_max_interval",
                current_value=current_interval,
                recommended_value=max(4.0, current_interval - 1.0),
                reason=f"잔존율 {avg_retention:.0f}% → 인터럽트 간격 1초 단축",
                confidence=0.7,
                auto_apply=True,
            ))

        return adjustments

    def _analyze_watch_time(
        self, metrics: list[VideoMetrics], current: dict
    ) -> list[FeedbackAdjustment]:
        """시청 시간 분석."""
        adjustments = []

        avg_watch = (
            sum(m.average_view_duration_seconds for m in metrics) / len(metrics)
            if metrics else 0
        )
        avg_watch_min = avg_watch / 60.0

        if avg_watch_min < self.WATCH_TIME_MIN_MINUTES:
            current_target = current.get("target_duration_minutes", 10)
            adjustments.append(FeedbackAdjustment(
                parameter="target_duration_minutes",
                current_value=current_target,
                recommended_value=max(5, int(current_target * 0.85)),
                reason=f"평균 시청 {avg_watch_min:.1f}분 → 영상 길이 15% 감소",
                confidence=0.6,
                auto_apply=False,  # 길이 변경은 수동 검토
            ))

        return adjustments

    @staticmethod
    def _identify_patterns(metrics: list[VideoMetrics]) -> list[str]:
        """성공 패턴 식별."""
        if not metrics:
            return []

        patterns = []
        avg_views = sum(m.views for m in metrics) / len(metrics)

        # 조회수 1.5배 이상 영상 분석
        top_videos = [m for m in metrics if m.views > avg_views * 1.5]
        for m in top_videos:
            patterns.append(
                f"[성공] '{m.title[:30]}' (views={m.views:,}, "
                f"engagement={m.engagement_rate:.1f}%)"
            )

        return patterns

    def apply_adjustments(
        self,
        report: FeedbackReport,
        channel_yaml_path: Path | None = None,
    ) -> dict[str, Any]:
        """자동 적용 가능한 조정을 반환.

        실제 YAML 수정은 하지 않고, 적용할 값을 dict로 반환.
        사용자가 검토 후 수동으로 적용.
        """
        to_apply = {}
        for adj in report.adjustments:
            if adj.auto_apply and adj.confidence >= 0.6:
                to_apply[adj.parameter] = {
                    "value": adj.recommended_value,
                    "reason": adj.reason,
                    "confidence": adj.confidence,
                }
                logger.info(
                    f"Auto-adjust: {adj.parameter} = {adj.recommended_value} "
                    f"({adj.reason})"
                )

        return to_apply
