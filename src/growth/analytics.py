"""성과 분석 - YouTube 데이터 수집 + 인사이트 도출.

Phase 4 파이프라인 결과를 입력으로 사용.
독립 실행 가능 구조 (옵션 기능).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class VideoMetrics:
    """단일 영상 성과 지표."""
    video_id: str
    title: str = ""
    published_at: str = ""
    # 핵심 지표
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    # CTR & Retention
    impressions: int = 0
    ctr: float = 0.0  # Click-Through Rate (%)
    average_view_duration_seconds: float = 0.0
    average_view_percentage: float = 0.0  # 평균 시청 비율 (%)
    # 이탈 데이터
    retention_curve: list[float] = field(default_factory=list)  # 초별 잔존율
    drop_off_points: list[float] = field(default_factory=list)  # 급락 시점 (초)
    # 수익
    estimated_revenue: float = 0.0
    rpm: float = 0.0  # Revenue per Mille

    @property
    def engagement_rate(self) -> float:
        """참여율 = (좋아요 + 댓글) / 조회수."""
        if self.views == 0:
            return 0.0
        return ((self.likes + self.comments) / self.views) * 100


@dataclass
class AnalyticsResult:
    """분석 결과."""
    channel_id: str
    analysis_date: str = ""
    videos_analyzed: int = 0
    metrics: list[VideoMetrics] = field(default_factory=list)
    # 집계
    avg_ctr: float = 0.0
    avg_view_duration: float = 0.0
    avg_retention_rate: float = 0.0
    top_performing_video: str = ""
    # 인사이트
    insights: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)


class AnalyticsEngine:
    """YouTube 성과 데이터 수집 + 분석."""

    def __init__(self, youtube_api_key: str = "") -> None:
        from ..core.config import load_env
        env = load_env()
        self.api_key = youtube_api_key or env.youtube_api_key

    async def collect_metrics(
        self,
        video_ids: list[str],
        channel_id: str = "",
    ) -> AnalyticsResult:
        """영상 성과 데이터 수집.

        Args:
            video_ids: 분석할 YouTube 영상 ID 목록
            channel_id: 채널 ID

        Returns:
            AnalyticsResult
        """
        if not self.api_key:
            logger.warning("YouTube API key not set, returning empty analytics")
            return AnalyticsResult(
                channel_id=channel_id,
                analysis_date=datetime.now().isoformat(),
                insights=["YouTube API key not configured"],
            )

        metrics_list: list[VideoMetrics] = []

        for vid_id in video_ids:
            try:
                m = await self._fetch_video_metrics(vid_id)
                metrics_list.append(m)
            except Exception as e:
                logger.warning(f"Failed to fetch metrics for {vid_id}: {e}")

        result = AnalyticsResult(
            channel_id=channel_id,
            analysis_date=datetime.now().isoformat(),
            videos_analyzed=len(metrics_list),
            metrics=metrics_list,
        )

        # 집계 계산
        if metrics_list:
            result.avg_ctr = sum(m.ctr for m in metrics_list) / len(metrics_list)
            result.avg_view_duration = (
                sum(m.average_view_duration_seconds for m in metrics_list) / len(metrics_list)
            )
            result.avg_retention_rate = (
                sum(m.average_view_percentage for m in metrics_list) / len(metrics_list)
            )
            best = max(metrics_list, key=lambda m: m.views)
            result.top_performing_video = best.video_id

        # 인사이트 도출
        result.insights = self._generate_insights(metrics_list)
        result.recommendations = self._generate_recommendations(metrics_list)

        logger.info(
            f"Analytics: {len(metrics_list)} videos, "
            f"avg CTR={result.avg_ctr:.1f}%, "
            f"avg retention={result.avg_retention_rate:.1f}%"
        )
        return result

    async def _fetch_video_metrics(self, video_id: str) -> VideoMetrics:
        """YouTube Data API로 영상 지표 수집."""
        import httpx

        url = "https://www.googleapis.com/youtube/v3/videos"
        params = {
            "part": "statistics,snippet,contentDetails",
            "id": video_id,
            "key": self.api_key,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                raise RuntimeError(f"YouTube API error: {resp.status_code}")

            data = resp.json()
            items = data.get("items", [])
            if not items:
                raise RuntimeError(f"Video not found: {video_id}")

            item = items[0]
            stats = item.get("statistics", {})
            snippet = item.get("snippet", {})

            return VideoMetrics(
                video_id=video_id,
                title=snippet.get("title", ""),
                published_at=snippet.get("publishedAt", ""),
                views=int(stats.get("viewCount", 0)),
                likes=int(stats.get("likeCount", 0)),
                comments=int(stats.get("commentCount", 0)),
            )

    @staticmethod
    def _generate_insights(metrics: list[VideoMetrics]) -> list[str]:
        """성과 데이터에서 인사이트 도출."""
        if not metrics:
            return ["데이터 부족 - 분석 불가"]

        insights = []
        avg_views = sum(m.views for m in metrics) / len(metrics)
        avg_ctr = sum(m.ctr for m in metrics) / len(metrics)

        # CTR 분석
        if avg_ctr < 4.0:
            insights.append(f"CTR 평균 {avg_ctr:.1f}% → 썸네일/제목 개선 필요")
        elif avg_ctr > 8.0:
            insights.append(f"CTR 평균 {avg_ctr:.1f}% → 우수. 현재 패턴 유지")

        # 이탈 분석
        for m in metrics:
            if m.drop_off_points:
                early_drops = [p for p in m.drop_off_points if p < 30]
                if early_drops:
                    insights.append(
                        f"'{m.title[:20]}...' 30초 이내 이탈 감지 → 훅 강화 필요"
                    )

        # 참여율 분석
        high_engagement = [m for m in metrics if m.engagement_rate > 5.0]
        if high_engagement:
            topics = [m.title[:20] for m in high_engagement]
            insights.append(f"높은 참여율 주제: {', '.join(topics)}")

        return insights or ["분석 완료 - 특이사항 없음"]

    @staticmethod
    def _generate_recommendations(metrics: list[VideoMetrics]) -> list[str]:
        """다음 영상을 위한 추천."""
        if not metrics:
            return ["더 많은 데이터 필요"]

        recs = []
        avg_ctr = sum(m.ctr for m in metrics) / len(metrics)
        avg_duration = sum(m.average_view_duration_seconds for m in metrics) / len(metrics)
        avg_retention = sum(m.average_view_percentage for m in metrics) / len(metrics)

        # S9 자동 반영 룰
        if avg_ctr < 4.0:
            recs.append("썸네일 텍스트 길이 축소 (CTR < 4%)")
        if avg_retention < 40.0:
            recs.append("패턴 인터럽트 간격 1초 단축 (retention < 40%)")
        if avg_duration < 180:
            recs.append("영상 길이 15% 감소 검토 (avg watch < 3분)")

        # 성공 패턴 박제
        best = max(metrics, key=lambda m: m.views) if metrics else None
        if best and best.views > sum(m.views for m in metrics) / len(metrics) * 1.5:
            recs.append(f"'{best.title[:20]}...' 구조를 recurring_pattern으로 박제")

        return recs or ["현재 전략 유지"]
