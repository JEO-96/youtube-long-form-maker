"""S1 벤치마킹 - 토픽/키워드/경쟁 분석."""

from __future__ import annotations

import json
import logging
from typing import Any

from ..core.models import Stage, BenchmarkResult, CompetitorVideo
from .base_stage import BaseStage

logger = logging.getLogger(__name__)

BENCHMARK_SYSTEM_PROMPT_FALLBACK = """당신은 YouTube 콘텐츠 전략가입니다.
주어진 주제에 대해 YouTube 영상 기획을 위한 벤치마크 분석을 수행하세요.

반드시 아래 JSON 형식으로만 응답하세요:
{
  "topic": "분석한 주제",
  "keywords": ["키워드1", "키워드2", "키워드3", "키워드4", "키워드5"],
  "trend_velocity": 0.0,
  "content_gaps": ["기존 영상에서 다루지 않는 관점1", "관점2"],
  "suggested_angle": "이 채널만의 차별화된 앵글",
  "analysis_summary": "분석 요약 (2~3문장)"
}"""


class S1Benchmark(BaseStage):
    """S1: 토픽 벤치마킹."""

    stage = Stage.BENCHMARK

    async def run(self, topic: str = "", **kwargs: Any) -> BenchmarkResult:
        if not topic:
            raise ValueError("topic은 필수입니다")

        if self.dry_run:
            return self._mock_result(topic)

        # 채널 설정 기반 LLM 선택 (기본: claude)
        from ..providers.factory import create_llm
        from ..templates import render_prompt
        llm = create_llm(self.channel.providers.llm, fallback="claude")

        # Jinja2 템플릿 기반 시스템 프롬프트 (채널 DNA 반영)
        system_prompt = render_prompt(
            "benchmark_analysis.j2",
            channel_name=self.channel.channel_name,
            niche=self.channel.niche,
            target_audience=self.channel.target_audience,
            unique_angle=self.channel.identity.unique_angle,
            forbidden_topics=getattr(self.channel.identity, 'forbidden_topics', []),
            topic=topic,
        )
        # 렌더링 실패 시 fallback
        if "Template" in system_prompt and "not found" in system_prompt:
            system_prompt = BENCHMARK_SYSTEM_PROMPT_FALLBACK

        prompt = (
            f"주제: {topic}\n"
            f"채널: {self.channel.channel_name}\n"
            f"니치: {self.channel.niche}\n"
            f"타겟: {self.channel.target_audience}\n"
            f"채널 앵글: {self.channel.identity.unique_angle}\n\n"
            f"위 정보를 바탕으로 YouTube 영상 벤치마크 분석을 JSON으로 수행하세요."
        )

        provider_name = self.channel.providers.llm
        raw = await llm.generate(prompt, system=system_prompt, temperature=0.5)
        self.record_cost(provider_name, "benchmark_analysis", units=1, unit_cost=llm.estimate_cost(2000, 1500))

        data = json.loads(raw.strip().strip("```json").strip("```"))
        return BenchmarkResult(
            topic=data.get("topic", topic),
            keywords=data.get("keywords", []),
            trend_velocity=float(data.get("trend_velocity", 70.0)),
            content_gaps=data.get("content_gaps", []),
            suggested_angle=data.get("suggested_angle", ""),
            analysis_summary=data.get("analysis_summary", ""),
        )

    @staticmethod
    def _mock_result(topic: str) -> BenchmarkResult:
        return BenchmarkResult(
            topic=topic,
            keywords=[topic, "투자", "재테크", "입문", "전략"],
            trend_velocity=78.5,
            competitor_videos=[
                CompetitorVideo(video_id="mock_001", title=f"{topic} 완벽 가이드", view_count=125000),
                CompetitorVideo(video_id="mock_002", title=f"{topic} 초보 필수", view_count=89000),
            ],
            content_gaps=[f"{topic} 실전 사례가 부족", f"초보자 관점 {topic} 설명 부재"],
            suggested_angle=f"전문가도 놓치는 {topic}의 핵심 3가지",
            analysis_summary=f"'{topic}' 주제는 트렌드 속도 78.5로 상승 중. 초보자 타겟 실전 콘텐츠 부족.",
        )
