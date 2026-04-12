"""S2 대본 생성 - Claude 기반 구조화 대본."""

from __future__ import annotations

import json
import logging
import re
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
        raw = await llm.generate(prompt, system=system_prompt, temperature=0.7, max_tokens=16384)
        self.record_cost(provider_name, "script_generation", units=1, unit_cost=llm.estimate_cost(3000, 8000))

        raw_clean = raw.strip()
        if raw_clean.startswith("```"):
            raw_clean = re.sub(r'^```(?:json)?\s*', '', raw_clean)
            raw_clean = re.sub(r'\s*```$', '', raw_clean)
        data = json.loads(raw_clean)

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
        topic = benchmark.topic
        sections = [
            ScriptSection(
                header=f"{topic}란 무엇인가?",
                body=(
                    f"{topic}의 기본 개념을 쉽게 설명드리겠습니다. "
                    f"많은 분들이 어렵게 생각하시지만 핵심만 알면 간단합니다. "
                    f"실제로 2024년 기준 대한민국 가구의 약 56%가 자가 보유율을 기록하고 있습니다. "
                    f"하지만 수도권은 45% 수준에 불과합니다. "
                    f"반면 지방 광역시는 60%를 넘는 경우도 많습니다. "
                    f"이 차이를 이해하면 시장의 큰 그림이 보이기 시작합니다."
                ),
                visual_prompt="차트와 그래프를 활용한 개념 설명 인포그래픽",
                estimated_duration_seconds=120,
            ),
            ScriptSection(
                header="금리와 시장 타이밍의 관계",
                body=(
                    f"금리는 부동산 시장의 핵심 변수입니다. "
                    f"한국은행 기준금리가 0.25% 내려가면 매수심리가 약 15% 올라간다는 연구가 있습니다. "
                    f"예를 들어 2023년 초 금리 동결 기간에 거래량이 30% 반등했습니다. "
                    f"하지만 금리만 보면 안 됩니다. 공급 물량도 함께 봐야 합니다. "
                    f"2025년 서울 입주 물량은 약 2만 8천 가구로 전년 대비 20% 감소했습니다. "
                    f"공급이 줄면 가격이 오를 가능성이 높아집니다."
                ),
                visual_prompt="금리 변동 그래프와 거래량 비교 차트",
                estimated_duration_seconds=150,
            ),
            ScriptSection(
                header="초보자가 저지르는 실수 3가지",
                body=(
                    f"첫 번째 실수는 준비 없이 시작하는 것입니다. "
                    f"최소 6개월 전부터 시세를 모니터링해야 합니다. "
                    f"두 번째는 리스크 관리를 무시하는 것입니다. "
                    f"대출 원리금이 월 소득의 30%를 넘으면 위험합니다. "
                    f"세 번째는 단기 수익만 쫓는 것입니다. "
                    f"실제로 2~3년 보유한 투자자의 70%가 손실을 경험했습니다. "
                    f"반면 7년 이상 보유한 경우 수익률은 평균 40%를 넘었습니다."
                ),
                visual_prompt="실수 사례를 시각적으로 보여주는 비교 차트",
                estimated_duration_seconds=150,
            ),
            ScriptSection(
                header="실전 전략 단계별 가이드",
                body=(
                    f"이제 실전 전략을 알려드리겠습니다. "
                    f"1단계는 목표 설정입니다. 투자용인지 실거주용인지 명확히 해야 합니다. "
                    f"2단계는 입지 분석입니다. 역세권, 학군, 재개발 호재 등을 체크하세요. "
                    f"3단계는 자금 계획입니다. DSR 규제를 반드시 확인하세요. "
                    f"예를 들어 연소득 5천만 원이면 대출 한도가 약 2억 5천만 원 수준입니다. "
                    f"4단계는 실행입니다. 매물을 최소 20곳 이상 직접 방문하시길 추천합니다. "
                    f"체크리스트를 만들어서 비교하면 판단이 훨씬 쉬워집니다."
                ),
                visual_prompt="단계별 가이드 플로우차트",
                estimated_duration_seconds=180,
            ),
            ScriptSection(
                header="전세 vs 매매, 지금 어떤 선택이 유리한가",
                body=(
                    f"마지막으로 전세와 매매를 비교해 보겠습니다. "
                    f"전세는 초기 자본 부담이 적지만, 2년마다 이사 리스크가 있습니다. "
                    f"반면 매매는 대출 이자 부담이 크지만, 자산 축적 효과가 있습니다. "
                    f"결론적으로 금리 3% 이하, 전세가율 70% 이상이면 매매가 유리합니다. "
                    f"핵심은 본인의 자금 상황과 거주 기간을 기준으로 판단하는 것입니다."
                ),
                visual_prompt="전세 vs 매매 비교 인포그래픽",
                estimated_duration_seconds=120,
            ),
        ]
        full_text = (
            f"혹시 {topic}이 어렵다고 느끼시나요?\n"
            + "\n".join(s.body for s in sections)
            + "\n구독과 좋아요, 알림 설정까지 부탁드립니다!"
        )
        return ScriptResult(
            title=f"{topic} 완벽 가이드: 초보자도 쉽게",
            hook=f"대한민국 가구 절반이 내 집 마련 시점을 잘못 판단하고 있습니다.",
            intro=f"오늘은 {topic}에 대해 처음부터 끝까지 알려드리겠습니다.",
            sections=sections,
            cta="구독과 좋아요, 알림 설정까지 부탁드립니다!",
            outro="다음 영상에서 더 심화된 내용으로 만나겠습니다.",
            full_text=full_text,
            word_count=len(full_text.split()),
            estimated_duration_seconds=600,
        )
