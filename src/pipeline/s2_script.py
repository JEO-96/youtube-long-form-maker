"""S2 대본 생성 - Claude 기반 구조화 대본."""

from __future__ import annotations

import json
import logging
from typing import Any

from ..core.models import Stage, BenchmarkResult, ScriptResult, ScriptSection
from .base_stage import BaseStage

logger = logging.getLogger(__name__)

SCRIPT_SYSTEM_PROMPT_FALLBACK = """당신은 YouTube 롱폼 영상 대본 작가입니다.
주어진 벤치마크 분석 결과를 바탕으로 10분 분량 영상 대본을 작성하세요.

반드시 아래 JSON 형식으로만 응답하세요:
{
  "title": "영상 제목",
  "hook": "첫 5초 후킹 멘트 (시청자 이탈 방지용)",
  "intro": "도입부 (30초 이내)",
  "sections": [
    {
      "header": "섹션 제목",
      "body": "본문 내용 (나레이션 대본)",
      "visual_prompt": "이 섹션에 맞는 영상 프롬프트",
      "estimated_duration_seconds": 120
    }
  ],
  "cta": "마무리 Call to Action",
  "outro": "아웃트로",
  "word_count": 0,
  "estimated_duration_seconds": 600
}

sections는 최소 3개 이상 작성하세요."""


class S2Script(BaseStage):
    """S2: 구조화 대본 생성."""

    stage = Stage.SCRIPT

    async def run(self, **kwargs: Any) -> ScriptResult:
        benchmark_data = self.load_previous(Stage.BENCHMARK)
        benchmark = BenchmarkResult(**benchmark_data)

        if self.dry_run:
            return self._mock_result(benchmark)

        # 채널 설정 기반 LLM 선택 (기본: claude)
        from ..providers.factory import create_llm
        from ..templates import render_prompt
        llm = create_llm(self.channel.providers.llm, fallback="claude")

        target_duration = getattr(self.channel.content, 'target_duration_minutes', 10) if hasattr(self.channel, 'content') else 10

        # Jinja2 템플릿 기반 시스템 프롬프트 (채널 DNA + safety_policy 반영)
        system_prompt = render_prompt(
            "script_body.j2",
            channel_name=self.channel.channel_name,
            tone=self.channel.tone,
            target_audience=self.channel.target_audience,
            narrative_style=self.channel.identity.narrative_style,
            recurring_pattern=getattr(self.channel.identity, 'recurring_pattern', ''),
            forbidden_topics=getattr(self.channel.identity, 'forbidden_topics', []),
            hook_style=getattr(self.channel.content, 'hook_style', 'question') if hasattr(self.channel, 'content') else 'question',
            cta_style=getattr(self.channel.content, 'cta_style', 'subscribe_and_next') if hasattr(self.channel, 'content') else 'subscribe_and_next',
            safety_policy=getattr(self.channel, 'safety_policy', None),
            topic=benchmark.topic,
            keywords=benchmark.keywords,
            suggested_angle=benchmark.suggested_angle,
            content_gaps=benchmark.content_gaps,
            target_duration=target_duration,
        )
        # 렌더링 실패 시 fallback
        if "Template" in system_prompt and "not found" in system_prompt:
            system_prompt = SCRIPT_SYSTEM_PROMPT_FALLBACK

        prompt = (
            f"벤치마크 분석 결과:\n"
            f"- 주제: {benchmark.topic}\n"
            f"- 키워드: {', '.join(benchmark.keywords)}\n"
            f"- 차별화 앵글: {benchmark.suggested_angle}\n"
            f"- 콘텐츠 갭: {', '.join(benchmark.content_gaps)}\n\n"
            f"채널 정보:\n"
            f"- 채널명: {self.channel.channel_name}\n"
            f"- 톤: {self.channel.tone}\n"
            f"- 타겟: {self.channel.target_audience}\n"
            f"- 내러티브 스타일: {self.channel.identity.narrative_style}\n\n"
            f"위 정보를 바탕으로 {target_duration}분 분량 YouTube 대본을 JSON으로 작성하세요."
        )

        provider_name = self.channel.providers.llm
        raw = await llm.generate(prompt, system=system_prompt, temperature=0.7)
        self.record_cost(provider_name, "script_generation", units=1, unit_cost=llm.estimate_cost(3000, 4000))

        data = json.loads(raw.strip().strip("```json").strip("```"))

        sections = [
            ScriptSection(
                header=s.get("header", ""),
                body=s.get("body", ""),
                visual_prompt=s.get("visual_prompt", ""),
                estimated_duration_seconds=float(s.get("estimated_duration_seconds", 60)),
            )
            for s in data.get("sections", [])
        ]

        full_text = data.get("hook", "") + "\n" + data.get("intro", "")
        for sec in sections:
            full_text += f"\n{sec.body}"
        full_text += "\n" + data.get("cta", "")

        return ScriptResult(
            title=data.get("title", benchmark.topic),
            hook=data.get("hook", ""),
            intro=data.get("intro", ""),
            sections=sections,
            cta=data.get("cta", ""),
            outro=data.get("outro", ""),
            full_text=full_text,
            word_count=len(full_text.split()),
            estimated_duration_seconds=float(data.get("estimated_duration_seconds", 600)),
        )

    @staticmethod
    def _mock_result(benchmark: BenchmarkResult) -> ScriptResult:
        sections = [
            ScriptSection(
                header=f"{benchmark.topic}란 무엇인가?",
                body=f"{benchmark.topic}의 기본 개념을 쉽게 설명드리겠습니다. "
                     f"많은 분들이 어렵게 생각하시지만 핵심만 알면 간단합니다.",
                visual_prompt="차트와 그래프를 활용한 개념 설명 인포그래픽",
                estimated_duration_seconds=120,
            ),
            ScriptSection(
                header="초보자가 저지르는 실수 3가지",
                body=f"첫 번째 실수는 준비 없이 시작하는 것입니다. "
                     f"두 번째는 리스크 관리를 무시하는 것이고, "
                     f"세 번째는 단기 수익만 쫓는 것입니다.",
                visual_prompt="실수 사례를 시각적으로 보여주는 비교 차트",
                estimated_duration_seconds=150,
            ),
            ScriptSection(
                header="실전 전략 단계별 가이드",
                body=f"이제 실전 전략을 알려드리겠습니다. "
                     f"1단계는 목표 설정, 2단계는 분석, 3단계는 실행입니다.",
                visual_prompt="단계별 가이드 플로우차트",
                estimated_duration_seconds=180,
            ),
        ]
        full_text = (
            f"혹시 {benchmark.topic}이 어렵다고 느끼시나요?\n"
            + "\n".join(s.body for s in sections)
            + "\n구독과 좋아요, 알림 설정까지 부탁드립니다!"
        )
        return ScriptResult(
            title=f"{benchmark.topic} 완벽 가이드: 초보자도 쉽게",
            hook=f"혹시 {benchmark.topic}이 어렵다고 느끼시나요?",
            intro=f"오늘은 {benchmark.topic}에 대해 처음부터 끝까지 알려드리겠습니다.",
            sections=sections,
            cta="구독과 좋아요, 알림 설정까지 부탁드립니다!",
            outro="다음 영상에서 더 심화된 내용으로 만나겠습니다.",
            full_text=full_text,
            word_count=len(full_text.split()),
            estimated_duration_seconds=600,
        )
