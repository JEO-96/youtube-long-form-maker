"""S4 스토리보드 — LLM 기반 의미 단위 씬 분해 + 시각 의도 매핑.

기존: 섹션 1개 = 씬 1개 (시각-내레이션 관련도 낮음)
개선: 문장/의미 단위로 잘게 분리, 각 씬에 visual_intent 강제 지정,
      채널 니치별 시각 매핑 규칙 적용, 5-10초마다 재미 요소 배치.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..core.models import (
    Stage, ScriptResult, VoiceResult, StoryboardResult, Scene,
    MediaType, TransitionType, VisualIntent,
)
from ..core.config import load_settings
from .base_stage import BaseStage

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════
# 채널 니치별 시각 매핑 규칙
# ═══════════════════════════════════════════════════

# 키워드 → visual_intent 매핑 (니치별)
NICHE_VISUAL_RULES: dict[str, list[tuple[list[str], VisualIntent]]] = {
    "real_estate": [
        # 비교 패턴을 우선 (전세 vs 매매 등 비교 문맥 먼저 검출)
        (["비교", "차이", "vs", "장단점", "유리하지만", "반면"], VisualIntent.COMPARISON_CARD),
        (["금리", "대출", "이자", "원리금", "DTI", "LTV", "DSR"], VisualIntent.CHART),
        (["비용", "가격", "세금", "취득세", "양도세", "중개수수료"], VisualIntent.CHART),
        (["매수", "매도", "타이밍", "상승", "하락", "시세", "거래량", "추세"], VisualIntent.CHART),
        (["체크", "확인", "주의", "유의", "포인트", "점검", "필수"], VisualIntent.CHECKLIST),
        (["첫째", "둘째", "셋째", "단계", "1.", "2.", "3."], VisualIntent.CHECKLIST),
        (["사례", "실제", "경험", "이야기", "케이스", "예를 들어"], VisualIntent.REAL_BROLL),
        (["핵심", "결론", "요약", "기억", "정리"], VisualIntent.EMPHASIS_CAPTION),
        (["청약", "분양", "계약", "등기"], VisualIntent.INFOGRAPHIC),
        (["지역", "입지", "역세권", "강남", "서울", "수도권"], VisualIntent.MAP),
        (["아파트", "빌라", "오피스텔", "주택", "건물"], VisualIntent.REAL_BROLL),
    ],
    "finance": [
        (["수익률", "금리", "이자", "배당", "복리", "연평균"], VisualIntent.CHART),
        (["포트폴리오", "분산투자", "자산배분", "ETF"], VisualIntent.INFOGRAPHIC),
        (["비교", "차이", "vs", "장단점"], VisualIntent.COMPARISON_CARD),
        (["체크", "주의", "실수", "함정"], VisualIntent.CHECKLIST),
        (["핵심", "결론", "요약"], VisualIntent.EMPHASIS_CAPTION),
        (["사례", "실제", "경험"], VisualIntent.REAL_BROLL),
        (["절세", "세금", "연말정산"], VisualIntent.INFOGRAPHIC),
    ],
    "health": [
        (["수치", "혈당", "혈압", "콜레스테롤", "BMI"], VisualIntent.CHART),
        (["비교", "전후", "before", "after"], VisualIntent.COMPARISON_CARD),
        (["체크", "증상", "진단", "확인"], VisualIntent.CHECKLIST),
        (["운동", "스트레칭", "동작"], VisualIntent.REAL_BROLL),
        (["핵심", "결론", "요약"], VisualIntent.EMPHASIS_CAPTION),
    ],
}

# visual_intent별 media_type 매핑
INTENT_TO_MEDIA_TYPE: dict[VisualIntent, MediaType] = {
    VisualIntent.REAL_BROLL: MediaType.STOCK_VIDEO,
    VisualIntent.MAP: MediaType.STOCK_VIDEO,
    VisualIntent.CHART: MediaType.AI_IMAGE,
    VisualIntent.INFOGRAPHIC: MediaType.AI_IMAGE,
    VisualIntent.CHECKLIST: MediaType.AI_IMAGE,
    VisualIntent.COMPARISON_CARD: MediaType.AI_IMAGE,
    VisualIntent.EMPHASIS_CAPTION: MediaType.AI_IMAGE,
    VisualIntent.TALKING_HEAD_STYLE: MediaType.STOCK_VIDEO,
    VisualIntent.CLOSING_CTA: MediaType.AI_IMAGE,
}

# visual_intent별 영문 stock_search_query 폴백 힌트
# 주의: 이 힌트는 _generate_stock_query에서 키워드 매칭 실패 시에만 사용.
# 가능한 한 문장 의미 기반 쿼리가 우선.
INTENT_STOCK_HINTS: dict[VisualIntent, str] = {
    VisualIntent.REAL_BROLL: "real life scene lifestyle",
    VisualIntent.MAP: "aerial city map drone",
    VisualIntent.CHART: "chart graph data visualization",
    VisualIntent.INFOGRAPHIC: "infographic diagram process flow",
    VisualIntent.CHECKLIST: "checklist clipboard planning document",
    VisualIntent.COMPARISON_CARD: "comparison side by side split",
    VisualIntent.EMPHASIS_CAPTION: "bold text highlight emphasis",
    VisualIntent.TALKING_HEAD_STYLE: "expert speaker explaining concept",
    VisualIntent.CLOSING_CTA: "subscribe button youtube ending card",
}


class S4Storyboard(BaseStage):
    """S4: 대본 → 의미 단위 씬 분해 + 시각 의도 매핑.

    LLM 기반: 문장/의미 단위로 잘게 분리하여 내레이션-화면 관련도를 극대화.
    폴백: 규칙 기반 문장 분할 + 키워드 매핑.
    """

    stage = Stage.STORYBOARD

    async def run(self, **kwargs: Any) -> StoryboardResult:
        script_data = self.load_previous(Stage.SCRIPT)
        script = ScriptResult(**script_data)

        voice: VoiceResult | None = None
        voice_data = self.state.load_stage_output(self.production_id, Stage.VOICE)
        if voice_data:
            voice = VoiceResult(**voice_data)

        # ═══ 씬 분해: LLM 우선, 실패 시 규칙 기반 폴백 ═══
        if self.dry_run:
            raw_scenes = self._rule_based_split(script)
        else:
            try:
                raw_scenes = await self._llm_scene_split(script)
            except Exception as e:
                logger.warning(f"LLM 씬 분해 실패: {e}, 규칙 기반 폴백")
                raw_scenes = self._rule_based_split(script)

        # ═══ 채널 니치별 visual_intent 보정 ═══
        niche = getattr(self.channel, "niche", "")
        self._apply_niche_rules(raw_scenes, niche)

        # ═══ 재미 요소 다양성 검증 + 강제 삽입 ═══
        self._enforce_visual_variety(raw_scenes)

        # ═══ generic query 후처리 — 일반적 쿼리를 니치 기반으로 재작성 ═══
        self._fix_generic_queries(raw_scenes, niche)

        # ═══ 타이밍 계산 (음성 세그먼트 기반) ═══
        self._apply_timing(raw_scenes, voice, script)

        # ═══ scene_number 재할당 + 통계 ═══
        for i, sc in enumerate(raw_scenes):
            sc.scene_number = i + 1

        ai_video = sum(1 for s in raw_scenes if s.media_type == MediaType.AI_VIDEO)
        stock_video = sum(1 for s in raw_scenes if s.media_type == MediaType.STOCK_VIDEO)
        ai_image = sum(1 for s in raw_scenes if s.media_type == MediaType.AI_IMAGE)

        logger.info(
            f"Storyboard: {len(raw_scenes)} scenes "
            f"(AI_IMG={ai_image}, STOCK={stock_video}, AI_VID={ai_video}), "
            f"intents: {self._intent_summary(raw_scenes)}"
        )

        self.record_cost("system", "storyboard_planning", units=len(raw_scenes), unit_cost=0.0)

        return StoryboardResult(
            scenes=raw_scenes,
            total_scenes=len(raw_scenes),
            ai_video_count=ai_video,
            stock_video_count=stock_video,
            ai_image_count=ai_image,
        )

    # ═══════════════════════════════════════════════════
    # LLM 기반 씬 분해
    # ═══════════════════════════════════════════════════

    async def _llm_scene_split(self, script: ScriptResult) -> list[Scene]:
        """LLM으로 대본을 의미 단위 씬으로 분해."""
        from ..providers.factory import create_llm
        from ..templates import render_prompt

        llm = create_llm(self.channel.providers.llm, fallback="claude")
        niche = getattr(self.channel, "niche", "general")

        # 니치별 시각 규칙 텍스트 생성
        niche_rules_text = self._build_niche_rules_text(niche)

        # 전체 내레이션 텍스트 구성
        narration_blocks = []
        narration_blocks.append(f"[HOOK] {script.hook}")
        if script.intro:
            narration_blocks.append(f"[INTRO] {script.intro}")
        for i, sec in enumerate(script.sections):
            narration_blocks.append(f"[SECTION {i+1}: {sec.header}] {sec.body}")
        narration_blocks.append(f"[CTA] {script.cta}")
        if script.outro:
            narration_blocks.append(f"[OUTRO] {script.outro}")

        full_narration = "\n\n".join(narration_blocks)

        # visual_intent 선택지 목록
        intent_options = ", ".join(v.value for v in VisualIntent)

        system_prompt = f"""당신은 YouTube 영상 스토리보드 감독입니다.
대본을 읽고 "의미 단위"로 씬을 분해하세요.

## 핵심 원칙
1. 섹션 = 씬이 아닙니다. 한 섹션 안에서도 의미가 바뀌면 씬을 나누세요.
   - 문제 제기 / 사례 / 기준 / 경고 / 결론 → 각각 별개 씬
   - 목표: 씬 1개 = 5~15초 분량 (내레이션 1~3문장)
   - 최소 10개 이상의 씬으로 분해하세요. 6개 이하는 절대 안 됩니다.
   - 긴 설명형 주제라면 15~25개 씬이 적절합니다.

2. 각 씬의 visual_intent를 반드시 아래 중 하나로 지정하세요:
   {intent_options}
   - real_broll: 도시/생활/현장 실사 영상
   - map: 지도, 지역 항공샷
   - chart: 차트, 그래프, 숫자 카드
   - infographic: 데이터 시각화, 프로세스 도해
   - checklist: 체크리스트 카드
   - comparison_card: A vs B 비교, before/after
   - emphasis_caption: 핵심 문장/큰 숫자 풀스크린
   - talking_head_style: 전문가 발언 느낌
   - closing_cta: 구독/좋아요 CTA 엔딩

3. stock_search_query는 반드시 영문이고, 문장 의미에 직접 연결하세요.
   나쁜 예: "부동산 분석" → "real estate analysis"
   나쁜 예: "cinematic urban lifestyle" → 너무 일반적
   좋은 예: "청년 첫 집 구매 시점" → "young couple apartment viewing seoul"
   좋은 예: "금리 하락기 매수" → "mortgage rate chart house buying concept"
   좋은 예: "체크리스트" → "home buying checklist clipboard planning"
   좋은 예: "구독 CTA" → "clean subscribe card minimal youtube ending"

4. 5~10초마다 시각적 재미 요소를 넣으세요:
   - 큰 숫자 강조 (emphasis_caption)
   - 비교 카드 (comparison_card)
   - 체크리스트 (checklist)
   - 차트/지도 인서트 (chart, map)
   - 같은 유형이 3번 연속 반복되면 안 됩니다.

{niche_rules_text}

## 출력 형식
JSON으로만 응답하세요:
{{
  "scenes": [
    {{
      "narration_text": "이 씬의 나레이션 텍스트 (원문 그대로)",
      "visual_intent": "chart",
      "visual_description": "이 장면에 어울리는 구체적 시각 묘사 (한국어)",
      "stock_search_query": "english semantic search query for this scene",
      "visual_keywords": ["화면에 표시할", "키워드"],
      "is_hook": false
    }}
  ]
}}"""

        user_prompt = f"아래 대본을 씬으로 분해하세요:\n\n{full_narration}"

        provider_name = self.channel.providers.llm
        raw = await llm.generate(user_prompt, system=system_prompt, temperature=0.4)
        self.record_cost(provider_name, "storyboard_scene_split", units=1,
                         unit_cost=llm.estimate_cost(2000, 3000))

        # JSON 파싱
        raw = raw.strip()
        # ```json ... ``` 제거
        if raw.startswith("```"):
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
        data = json.loads(raw)

        scenes_data = data.get("scenes", data) if isinstance(data, dict) else data

        scenes: list[Scene] = []
        transitions = [TransitionType.CUT, TransitionType.DISSOLVE,
                       TransitionType.FADE, TransitionType.SLIDE, TransitionType.ZOOM]

        for i, sd in enumerate(scenes_data):
            intent_str = sd.get("visual_intent", "real_broll")
            try:
                intent = VisualIntent(intent_str)
            except ValueError:
                intent = VisualIntent.REAL_BROLL

            media_type = INTENT_TO_MEDIA_TYPE.get(intent, MediaType.AI_IMAGE)
            narration = sd.get("narration_text", "")
            vis_desc = sd.get("visual_description", "")
            stock_q = sd.get("stock_search_query", "")
            is_hook = sd.get("is_hook", False)
            vis_kw = sd.get("visual_keywords", [])

            # image_prompt / video_prompt 자동 생성
            image_prompt = self._build_image_prompt(intent, vis_desc, stock_q)
            video_prompt = self._build_video_prompt(intent, vis_desc, stock_q)

            scenes.append(Scene(
                scene_number=i + 1,
                start_time=0.0,
                end_time=0.0,
                duration=0.0,
                narration_text=narration,
                visual_description=vis_desc,
                visual_intent=intent,
                media_type=media_type,
                image_prompt=image_prompt,
                video_prompt=video_prompt,
                stock_search_query=stock_q or INTENT_STOCK_HINTS.get(intent, ""),
                transition=transitions[i % len(transitions)],
                is_hook=is_hook or (i == 0),
                visual_keywords=vis_kw[:5],
            ))

        if not scenes:
            raise ValueError("LLM이 빈 씬 목록을 반환")

        # 최소 씬 수 검증 — 6개 이하면 규칙 기반 보강
        if len(scenes) < 8 and len(script.sections) >= 2:
            logger.warning(
                f"LLM 씬 분해 결과가 너무 적음: {len(scenes)}개 → "
                f"규칙 기반 보강 시도"
            )
            # LLM 결과는 유지하되, 긴 narration을 가진 씬을 추가 분할
            expanded: list[Scene] = []
            for sc in scenes:
                if len(sc.narration_text) > 150:
                    # 긴 내레이션을 2개로 분할
                    mid = len(sc.narration_text) // 2
                    for offset in range(50):
                        pos = mid + offset
                        if pos < len(sc.narration_text) and sc.narration_text[pos] in '.!?다요죠까니':
                            mid = pos + 1
                            break
                        pos = mid - offset
                        if pos > 0 and sc.narration_text[pos] in '.!?다요죠까니':
                            mid = pos + 1
                            break
                    niche = getattr(self.channel, "niche", "")
                    part1 = sc.narration_text[:mid].strip()
                    part2 = sc.narration_text[mid:].strip()
                    if part1 and part2:
                        sc.narration_text = part1
                        expanded.append(sc)
                        expanded.append(self._make_scene_from_text(part2, "", niche))
                        continue
                expanded.append(sc)
            scenes = expanded
            logger.info(f"보강 후 씬 수: {len(scenes)}")

        logger.info(f"LLM 씬 분해: {len(scenes)} scenes from {len(script.sections)} sections")
        return scenes

    # ═══════════════════════════════════════════════════
    # 규칙 기반 씬 분해 (폴백 / dry_run)
    # ═══════════════════════════════════════════════════

    def _rule_based_split(self, script: ScriptResult) -> list[Scene]:
        """규칙 기반 문장 단위 씬 분해 — LLM 없이 동작."""
        scenes: list[Scene] = []
        niche = getattr(self.channel, "niche", "")
        transitions = [TransitionType.CUT, TransitionType.DISSOLVE,
                       TransitionType.FADE, TransitionType.SLIDE, TransitionType.ZOOM]

        # Hook 씬 — 내레이션 의미 반영 query 생성
        hook_query = self._generate_stock_query(script.hook, VisualIntent.EMPHASIS_CAPTION)
        scenes.append(Scene(
            scene_number=1,
            start_time=0.0, end_time=0.0, duration=0.0,
            narration_text=script.hook,
            visual_description=f"강렬한 오프닝 — {script.hook[:60]}",
            visual_intent=VisualIntent.EMPHASIS_CAPTION,
            media_type=MediaType.AI_IMAGE,
            image_prompt=self._build_image_prompt(
                VisualIntent.EMPHASIS_CAPTION,
                f"강렬한 오프닝 — {script.hook[:60]}",
                hook_query,
            ),
            video_prompt=f"Cinematic opening shot, dramatic lighting",
            stock_search_query=hook_query,
            transition=TransitionType.FADE,
            is_hook=True,
            visual_keywords=self._extract_keywords(script.hook)[:3],
        ))

        # Intro 씬 (있으면) — 내레이션 의미 반영
        if script.intro and len(script.intro) > 20:
            intro_query = self._generate_stock_query(script.intro, VisualIntent.TALKING_HEAD_STYLE)
            scenes.append(Scene(
                scene_number=2,
                start_time=0.0, end_time=0.0, duration=0.0,
                narration_text=script.intro,
                visual_description=f"도입 — {script.intro[:60]}",
                visual_intent=VisualIntent.TALKING_HEAD_STYLE,
                media_type=MediaType.STOCK_VIDEO,
                image_prompt="Professional presenter speaking to camera",
                video_prompt="Professional speaker presentation style",
                stock_search_query=intro_query,
                transition=TransitionType.DISSOLVE,
                visual_keywords=self._extract_keywords(script.intro)[:3],
            ))

        # 본문 섹션별 → 문장 단위 분할
        for sec in script.sections:
            sub_scenes = self._split_section_to_scenes(sec.body, sec.header, niche)
            for ss in sub_scenes:
                ss.scene_number = len(scenes) + 1
                ss.transition = transitions[len(scenes) % len(transitions)]
                scenes.append(ss)

        # CTA 씬
        scenes.append(Scene(
            scene_number=len(scenes) + 1,
            start_time=0.0, end_time=0.0, duration=0.0,
            narration_text=script.cta,
            visual_description="구독 유도 엔딩 — CTA 카드",
            visual_intent=VisualIntent.CLOSING_CTA,
            media_type=MediaType.AI_IMAGE,
            image_prompt=self._build_image_prompt(
                VisualIntent.CLOSING_CTA,
                "구독 유도 엔딩 — CTA 카드",
                "subscribe button youtube ending card",
            ),
            stock_search_query="subscribe button youtube ending card",
            transition=TransitionType.FADE,
            visual_keywords=["구독", "좋아요", "알림"],
        ))

        logger.info(f"규칙 기반 씬 분해: {len(scenes)} scenes from {len(script.sections)} sections")
        return scenes

    def _split_section_to_scenes(
        self, body: str, header: str, niche: str,
    ) -> list[Scene]:
        """섹션 본문을 의미 단위(1~2문장)로 분할하여 씬 목록 반환.

        개선점:
        - 그룹핑 기준을 150자 → 100자로 축소하여 더 잘게 분할
        - 의미 전환 키워드(하지만, 반면, 그런데 등)에서 강제 분할
        - 최소 그룹 2개 보장
        """
        # 문장 분리 (한국어 종결어미 + 마침표/물음표/느낌표)
        sentences = re.split(r'(?<=[.!?다요죠까니])\s+', body.strip())
        sentences = [s.strip() for s in sentences if s.strip() and len(s.strip()) > 5]

        if not sentences:
            return [self._make_scene_from_text(body, header, niche)]

        # 의미 전환 패턴 — 이 키워드가 문장 시작에 오면 강제 분할
        _SPLIT_TRIGGERS = [
            "하지만", "반면", "그런데", "그러나", "한편", "반대로",
            "예를 들어", "실제로", "결론적으로", "핵심은", "정리하면",
            "첫째", "둘째", "셋째", "넷째", "1.", "2.", "3.", "4.",
            "주의할", "중요한", "문제는",
        ]

        # 1~2문장씩 그룹핑 (100자 기준 + 의미 전환 시 강제 분할)
        groups: list[str] = []
        current = ""
        for sent in sentences:
            # 의미 전환 키워드로 시작하면 현재 그룹 확정
            is_split_trigger = any(sent.startswith(t) for t in _SPLIT_TRIGGERS)

            if (len(current) + len(sent) > 100 and current) or \
               (is_split_trigger and current):
                groups.append(current.strip())
                current = sent
            else:
                current = f"{current} {sent}" if current else sent

        if current.strip():
            groups.append(current.strip())

        # 그룹이 1개면 최소 2개로 분할 시도
        if len(groups) == 1 and len(body) > 80:
            mid = len(body) // 2
            # 가장 가까운 문장 경계 찾기
            for offset in range(50):
                if mid + offset < len(body) and body[mid + offset] in '.!?다요죠까니':
                    mid = mid + offset + 1
                    break
                if mid - offset > 0 and body[mid - offset] in '.!?다요죠까니':
                    mid = mid - offset + 1
                    break
            groups = [body[:mid].strip(), body[mid:].strip()]
            groups = [g for g in groups if g]

        scenes: list[Scene] = []
        for group_text in groups:
            scenes.append(self._make_scene_from_text(group_text, header, niche))

        return scenes

    def _make_scene_from_text(
        self, text: str, section_header: str, niche: str,
    ) -> Scene:
        """텍스트 chunk에서 visual_intent를 추론하여 Scene 생성."""
        intent = self._infer_intent_from_text(text, niche)
        media_type = INTENT_TO_MEDIA_TYPE.get(intent, MediaType.AI_IMAGE)
        stock_query = self._generate_stock_query(text, intent)
        vis_desc = self._generate_visual_description(text, intent, section_header)
        keywords = self._extract_keywords(text)

        return Scene(
            scene_number=0,  # 나중에 재할당
            start_time=0.0, end_time=0.0, duration=0.0,
            narration_text=text[:300],
            visual_description=vis_desc,
            visual_intent=intent,
            media_type=media_type,
            image_prompt=self._build_image_prompt(intent, vis_desc, stock_query),
            video_prompt=self._build_video_prompt(intent, vis_desc, stock_query),
            stock_search_query=stock_query,
            visual_keywords=keywords[:5],
        )

    # ═══════════════════════════════════════════════════
    # Generic query 후처리
    # ═══════════════════════════════════════════════════

    _GENERIC_PATTERNS = [
        "modern concept illustration",
        "cinematic urban lifestyle",
        "professional modern",
        "dramatic opening",
        "concept illustration",
    ]

    def _fix_generic_queries(self, scenes: list, niche: str) -> None:
        """generic stock_search_query를 내레이션 키워드 기반으로 재작성."""
        fixed = 0
        for scene in scenes:
            q = scene.stock_search_query.lower().strip()
            is_generic = any(p in q for p in self._GENERIC_PATTERNS)
            if not is_generic:
                continue

            # 내레이션에서 키워드 재추출하여 쿼리 재생성
            new_query = self._generate_stock_query(
                scene.narration_text, scene.visual_intent,
            )
            # 재생성된 것도 여전히 generic이면 AI 이미지 유형으로 전환
            still_generic = any(p in new_query.lower() for p in self._GENERIC_PATTERNS)
            if still_generic:
                scene.media_type = MediaType.AI_IMAGE
                scene.stock_search_query = ""  # AI 생성이므로 스톡 쿼리 불필요
                logger.info(
                    f"Scene {scene.scene_number}: generic query → AI_IMAGE fallback"
                )
            else:
                scene.stock_search_query = new_query
                logger.info(
                    f"Scene {scene.scene_number}: generic query → '{new_query}'"
                )
            fixed += 1

        if fixed:
            logger.info(f"Fixed {fixed} generic stock queries")

    # ═══════════════════════════════════════════════════
    # 시각 의도 추론 (키워드 기반)
    # ═══════════════════════════════════════════════════

    def _infer_intent_from_text(self, text: str, niche: str) -> VisualIntent:
        """텍스트 내용에서 visual_intent 추론."""
        rules = NICHE_VISUAL_RULES.get(niche, [])

        # 니치별 규칙 우선 적용
        for keywords, intent in rules:
            if any(kw in text for kw in keywords):
                return intent

        # 범용 휴리스틱
        if re.search(r'\d+[%만억원]', text):
            return VisualIntent.CHART
        if any(kw in text for kw in ["비교", "차이", "vs", "반면", "그러나"]):
            return VisualIntent.COMPARISON_CARD
        if any(kw in text for kw in ["첫째", "둘째", "셋째", "1.", "2.", "3.", "단계"]):
            return VisualIntent.CHECKLIST
        if any(kw in text for kw in ["핵심", "결론", "기억", "중요"]):
            return VisualIntent.EMPHASIS_CAPTION
        if any(kw in text for kw in ["지역", "동네", "도시", "아파트", "건물"]):
            return VisualIntent.REAL_BROLL
        if any(kw in text for kw in ["데이터", "통계", "조사", "연구"]):
            return VisualIntent.INFOGRAPHIC

        return VisualIntent.REAL_BROLL

    # ═══════════════════════════════════════════════════
    # 영문 stock_search_query 생성 (의미 기반)
    # ═══════════════════════════════════════════════════

    # 한국어 키워드 → 영문 스톡 검색어 매핑
    _KO_TO_EN_MAP: dict[str, str] = {
        "아파트": "apartment building", "전세": "rental deposit contract",
        "매매": "property purchase", "월세": "monthly rent",
        "금리": "interest rate", "대출": "mortgage loan",
        "청약": "housing subscription lottery", "분양": "new apartment sales",
        "재개발": "urban redevelopment", "재건축": "building reconstruction",
        "부동산": "real estate property", "투자": "investment concept",
        "주택": "residential house", "빌라": "multi-family housing",
        "시세": "market price trend", "거래량": "transaction volume chart",
        "세금": "tax calculation", "취득세": "acquisition tax",
        "절세": "tax saving strategy", "수익률": "return on investment chart",
        "자산": "asset management", "포트폴리오": "investment portfolio",
        "주식": "stock market trading", "ETF": "ETF index fund",
        "적금": "savings account bank", "보험": "insurance policy",
        "건강": "health wellness", "운동": "fitness exercise",
        "식단": "healthy diet food", "수면": "sleep quality bedroom",
        "서울": "seoul city skyline", "강남": "gangnam district seoul",
        "경기": "gyeonggi province korea", "수도권": "seoul metropolitan area",
    }

    def _generate_stock_query(self, text: str, intent: VisualIntent) -> str:
        """한국어 텍스트에서 영문 스톡 검색 쿼리 생성.

        원칙: generic 쿼리("cinematic urban lifestyle") 대신
        문장 의미에 직접 연결되는 구체적 검색어를 생성한다.
        """
        parts: list[str] = []

        # 키워드 매칭 (문장 의미 기반)
        for ko, en in self._KO_TO_EN_MAP.items():
            if ko in text:
                parts.append(en)
                if len(parts) >= 3:
                    break

        # intent별 구체적 컨텍스트 추가 (generic 힌트 대신)
        intent_context: dict[VisualIntent, str] = {
            VisualIntent.CHART: "chart graph data visualization",
            VisualIntent.INFOGRAPHIC: "infographic diagram process",
            VisualIntent.CHECKLIST: "checklist clipboard planning",
            VisualIntent.COMPARISON_CARD: "comparison side by side",
            VisualIntent.EMPHASIS_CAPTION: "bold text highlight concept",
            VisualIntent.REAL_BROLL: "",  # 키워드만으로 충분
            VisualIntent.MAP: "aerial map city view",
            VisualIntent.TALKING_HEAD_STYLE: "expert speaker explaining",
            VisualIntent.CLOSING_CTA: "subscribe button youtube ending card",
        }
        ctx = intent_context.get(intent, "")

        if parts:
            # 키워드 매칭 결과 + intent 컨텍스트 결합
            if ctx and not any(c in " ".join(parts) for c in ctx.split()[:2]):
                parts.append(ctx.split()[0])  # 핵심 단어 1개만 추가
            return " ".join(parts[:4])

        # 키워드 매칭 실패 → intent 기반 + 텍스트에서 숫자/핵심어 추출
        if ctx:
            # 숫자가 포함된 문맥 추가
            numbers = re.findall(r'\d+[%만억원]?', text)
            if numbers:
                return f"{ctx} {numbers[0]}"
            return ctx

        # 최종 폴백: 채널 니치 키워드 기반으로 구체화
        niche = getattr(self, "channel", None)
        niche_name = getattr(niche, "niche", "") if niche else ""
        niche_fallback: dict[str, str] = {
            "real_estate": "apartment building real estate korea",
            "finance": "financial data investment banking",
            "health": "health wellness medical concept",
            "business": "business office meeting professional",
            "ai": "artificial intelligence technology data",
        }
        fallback_query = niche_fallback.get(niche_name, "")

        if re.search(r'\d+', text):
            return f"data statistics number {fallback_query}".strip()[:60]
        if fallback_query:
            return fallback_query
        return "professional data analysis concept"

    # ═══════════════════════════════════════════════════
    # 프롬프트 빌더
    # ═══════════════════════════════════════════════════

    @staticmethod
    def _build_image_prompt(intent: VisualIntent, desc: str, stock_q: str) -> str:
        """visual_intent별 최적화된 image_prompt 생성."""
        intent_prefix = {
            VisualIntent.CHART: "Clean professional chart/graph visualization,",
            VisualIntent.INFOGRAPHIC: "Modern infographic design, data visualization,",
            VisualIntent.CHECKLIST: "Clean checklist card design, checkbox items,",
            VisualIntent.COMPARISON_CARD: "Side-by-side comparison card, split layout,",
            VisualIntent.EMPHASIS_CAPTION: "Bold large text on dramatic background,",
            VisualIntent.REAL_BROLL: "Professional photography,",
            VisualIntent.MAP: "Aerial city map view,",
            VisualIntent.TALKING_HEAD_STYLE: "Professional presenter style,",
            VisualIntent.CLOSING_CTA: "Subscribe CTA card, channel branding,",
        }
        prefix = intent_prefix.get(intent, "Professional photography,")
        desc_part = desc[:150] if desc else stock_q[:100]
        return f"{prefix} {desc_part}, high quality, 16:9"

    @staticmethod
    def _build_video_prompt(intent: VisualIntent, desc: str, stock_q: str) -> str:
        """visual_intent별 video_prompt 생성."""
        intent_prefix = {
            VisualIntent.REAL_BROLL: "Cinematic B-roll,",
            VisualIntent.MAP: "Aerial drone shot,",
            VisualIntent.CHART: "Animated chart/graph reveal,",
            VisualIntent.TALKING_HEAD_STYLE: "Professional speaker closeup,",
        }
        prefix = intent_prefix.get(intent, "Cinematic footage,")
        return f"{prefix} {desc[:100] if desc else stock_q[:80]}"

    def _generate_visual_description(
        self, text: str, intent: VisualIntent, header: str,
    ) -> str:
        """씬의 시각 설명 생성 (한국어)."""
        intent_desc = {
            VisualIntent.CHART: "데이터 차트/그래프로 핵심 수치 시각화",
            VisualIntent.INFOGRAPHIC: "인포그래픽으로 정보 구조화",
            VisualIntent.CHECKLIST: "체크리스트 카드로 핵심 포인트 정리",
            VisualIntent.COMPARISON_CARD: "비교 카드로 차이점 시각화",
            VisualIntent.EMPHASIS_CAPTION: "핵심 메시지를 크게 강조",
            VisualIntent.REAL_BROLL: "실제 현장/생활 장면",
            VisualIntent.MAP: "지역/위치 지도 시각화",
            VisualIntent.TALKING_HEAD_STYLE: "전문가 발언 스타일",
            VisualIntent.CLOSING_CTA: "구독 유도 엔딩 카드",
        }
        base = intent_desc.get(intent, "관련 시각 자료")
        # 내레이션에서 핵심 내용 요약 추가
        key_part = text[:60].strip() if text else header[:40]
        return f"{base} — {key_part}"

    # ═══════════════════════════════════════════════════
    # 니치별 규칙 적용 + 재미 요소 강제
    # ═══════════════════════════════════════════════════

    def _apply_niche_rules(self, scenes: list[Scene], niche: str) -> None:
        """채널 니치별 시각 매핑 규칙 보정 (LLM 결과 후처리).

        비교 문맥 감지를 우선 적용 (전세+매매 동시 등장 = comparison_card).
        """
        rules = NICHE_VISUAL_RULES.get(niche, [])
        if not rules:
            return

        # 복합 비교 패턴 (두 키워드가 동시 존재하면 comparison_card 강제)
        comparison_pairs = [
            ("전세", "매매"), ("매수", "매도"), ("상승", "하락"),
            ("장점", "단점"), ("이전", "이후"), ("과거", "현재"),
        ]

        corrections = 0
        for scene in scenes:
            text = scene.narration_text

            # 복합 비교 패턴 우선 검사
            is_comparison = any(
                a in text and b in text for a, b in comparison_pairs
            ) or "vs" in text.lower()

            if is_comparison and scene.visual_intent != VisualIntent.COMPARISON_CARD:
                scene.visual_intent = VisualIntent.COMPARISON_CARD
                scene.media_type = INTENT_TO_MEDIA_TYPE[VisualIntent.COMPARISON_CARD]
                corrections += 1
                continue

            # 단일 키워드 규칙 적용
            for keywords, expected_intent in rules:
                if any(kw in text for kw in keywords):
                    if scene.visual_intent != expected_intent:
                        scene.visual_intent = expected_intent
                        scene.media_type = INTENT_TO_MEDIA_TYPE.get(expected_intent, scene.media_type)
                        corrections += 1
                    break

        if corrections:
            logger.info(f"니치 규칙 보정: {corrections}건 ({niche})")

    def _enforce_visual_variety(self, scenes: list[Scene]) -> None:
        """재미 요소 다양성 강제 — 같은 intent 3연속 방지."""
        if len(scenes) < 3:
            return

        corrections = 0
        for i in range(2, len(scenes)):
            if (scenes[i].visual_intent == scenes[i-1].visual_intent ==
                    scenes[i-2].visual_intent):
                # 3연속 동일 → 다른 재미 요소로 교체
                current = scenes[i].visual_intent
                alternatives = [
                    VisualIntent.EMPHASIS_CAPTION,
                    VisualIntent.COMPARISON_CARD,
                    VisualIntent.CHECKLIST,
                    VisualIntent.CHART,
                    VisualIntent.REAL_BROLL,
                ]
                for alt in alternatives:
                    if alt != current:
                        scenes[i].visual_intent = alt
                        scenes[i].media_type = INTENT_TO_MEDIA_TYPE.get(alt, scenes[i].media_type)
                        corrections += 1
                        break

        if corrections:
            logger.info(f"시각 다양성 보정: {corrections}건 (3연속 방지)")

    # ═══════════════════════════════════════════════════
    # 타이밍 계산
    # ═══════════════════════════════════════════════════

    # ═══ intent별 권장 duration 범위 (초) ═══
    _INTENT_DURATION_RANGE: dict[VisualIntent, tuple[float, float]] = {
        VisualIntent.EMPHASIS_CAPTION: (3.0, 8.0),     # 짧고 임팩트 있게
        VisualIntent.CHART: (6.0, 15.0),               # 수치 읽을 시간 필요
        VisualIntent.INFOGRAPHIC: (6.0, 15.0),         # 정보 소화 시간
        VisualIntent.CHECKLIST: (6.0, 15.0),           # 항목 읽기
        VisualIntent.COMPARISON_CARD: (5.0, 12.0),     # 비교 소화
        VisualIntent.REAL_BROLL: (4.0, 12.0),          # 분위기 장면
        VisualIntent.MAP: (4.0, 10.0),                 # 지도 확인
        VisualIntent.TALKING_HEAD_STYLE: (5.0, 20.0),  # 설명 호흡 길게
        VisualIntent.CLOSING_CTA: (3.0, 8.0),          # CTA는 간결하게
    }

    def _apply_timing(
        self, scenes: list[Scene], voice: VoiceResult | None, script: ScriptResult,
    ) -> None:
        """씬별 start_time / end_time / duration 계산.

        보장: 모든 씬이 total_dur 안에 들어옴 (S6 의존 없음).
        방식: 글자수 비례 → intent 클램핑 → 강제 스케일링 → 타임라인 배치.
        """
        if not scenes:
            return

        total_dur = 0.0
        if voice and voice.total_duration_seconds > 0:
            total_dur = voice.total_duration_seconds
        else:
            total_dur = script.estimated_duration_seconds or 600.0

        # ═══ Phase 1: 글자수 비례 + intent 클램핑으로 raw duration 계산 ═══
        char_counts = [max(len(s.narration_text), 10) for s in scenes]
        total_chars = sum(char_counts)

        raw_durations: list[float] = []
        for i, scene in enumerate(scenes):
            raw_dur = (char_counts[i] / total_chars) * total_dur
            intent = getattr(scene, "visual_intent", VisualIntent.REAL_BROLL)
            min_dur, max_dur = self._INTENT_DURATION_RANGE.get(intent, (4.0, 15.0))
            raw_dur = max(min_dur, min(max_dur, raw_dur))
            raw_durations.append(raw_dur)

        # ═══ Phase 2: 강제 스케일링 — 합산이 total_dur과 정확히 일치하도록 ═══
        # 핵심: min_dur도 scale과 함께 축소해야 악순환 방지
        raw_total = sum(raw_durations)
        if raw_total > 0 and abs(raw_total - total_dur) > 0.1:
            scale = total_dur / raw_total
            # 절대 최소값: 씬당 1초 (더 줄이면 화면이 깜빡임)
            abs_min = max(1.0, total_dur / len(scenes) * 0.3)
            for i in range(len(raw_durations)):
                raw_durations[i] = max(abs_min, round(raw_durations[i] * scale, 2))

        # 스케일링 후에도 합산 오차가 있으면 마지막 씬으로 보정
        scaled_total = sum(raw_durations)
        diff = round(total_dur - scaled_total, 2)
        if abs(diff) > 0.01 and raw_durations:
            raw_durations[-1] = max(1.0, round(raw_durations[-1] + diff, 2))

        # ═══ Phase 3: voice segment boundary에 스냅 (optional, total_dur 깨지지 않게) ═══
        seg_ends: list[float] = []
        if voice and voice.segments:
            seg_ends = sorted(set(seg.end for seg in voice.segments if seg.end > 0))

        if seg_ends:
            abs_min_snap = max(1.0, total_dur / len(scenes) * 0.3)
            cursor = 0.0
            for i in range(len(raw_durations) - 1):
                target_end = cursor + raw_durations[i]
                snapped = self._find_nearest_seg_end(target_end, seg_ends)
                if snapped > cursor + abs_min_snap and snapped < total_dur:
                    snapped_dur = round(snapped - cursor, 2)
                    delta = snapped_dur - raw_durations[i]
                    raw_durations[i] = snapped_dur
                    raw_durations[i + 1] = max(abs_min_snap, round(raw_durations[i + 1] - delta, 2))
                cursor += raw_durations[i]
            remaining = round(total_dur - sum(raw_durations[:-1]), 2)
            raw_durations[-1] = max(1.0, remaining)

        # ═══ Phase 4: 타임라인 배치 ═══
        current_time = 0.0
        for i, scene in enumerate(scenes):
            scene.duration = raw_durations[i]
            scene.start_time = round(current_time, 2)
            scene.end_time = round(current_time + scene.duration, 2)
            current_time = scene.end_time

        # ═══ Phase 5: 불변 조건 보장 — 반복 검증 ═══
        # 불변: 모든 duration > 0, sum(duration) == total_dur, 타임라인 연속
        abs_floor = max(0.5, total_dur / len(scenes) * 0.1)

        for _pass in range(3):  # 최대 3회 보정 패스
            has_problem = False

            # 5a: 음수/0 duration 수정 — 앞뒤 씬에서 시간을 빌려옴
            for i, scene in enumerate(scenes):
                if scene.duration <= 0:
                    has_problem = True
                    steal = abs_floor - scene.duration
                    scene.duration = abs_floor
                    # 가장 긴 인접 씬에서 빌려옴
                    donor = max(
                        [j for j in range(len(scenes)) if j != i],
                        key=lambda j: scenes[j].duration,
                    )
                    scenes[donor].duration = max(
                        abs_floor, round(scenes[donor].duration - steal, 2)
                    )

            # 5b: 합산 보정 — 비례 축소/확대 (마지막 씬 몰아주기 금지)
            actual_total = sum(s.duration for s in scenes)
            if abs(actual_total - total_dur) > 0.05:
                has_problem = True
                scale = total_dur / actual_total if actual_total > 0 else 1.0
                for scene in scenes:
                    scene.duration = max(abs_floor, round(scene.duration * scale, 2))
                # 스케일 후 잔여 오차 → 가장 긴 씬에서 보정
                residual = round(total_dur - sum(s.duration for s in scenes), 2)
                if abs(residual) > 0.01:
                    longest = max(range(len(scenes)), key=lambda i: scenes[i].duration)
                    scenes[longest].duration = round(scenes[longest].duration + residual, 2)

            # 5c: 타임라인 재배치
            cursor = 0.0
            for scene in scenes:
                scene.start_time = round(cursor, 2)
                scene.end_time = round(cursor + scene.duration, 2)
                cursor = scene.end_time

            if not has_problem:
                break

        # 최종 불변 조건 검증
        actual_total = sum(s.duration for s in scenes)
        neg_count = sum(1 for s in scenes if s.duration <= 0)
        if neg_count > 0:
            logger.error(f"S4 timing INVARIANT VIOLATED: {neg_count} scenes with duration<=0")
        logger.info(
            f"S4 timing: {len(scenes)} scenes, "
            f"total={actual_total:.1f}s (target={total_dur:.1f}s)"
        )

    @staticmethod
    def _find_nearest_seg_end(target: float, seg_ends: list[float]) -> float:
        """가장 가까운 세그먼트 경계 탐색 (0.8초 이내)."""
        best = target
        best_dist = float("inf")
        for end in seg_ends:
            dist = abs(end - target)
            if dist < best_dist:
                best_dist = dist
                best = end
        return best if best_dist <= 0.8 else target

    # ═══════════════════════════════════════════════════
    # 유틸리티
    # ═══════════════════════════════════════════════════

    @staticmethod
    def _extract_keywords(text: str) -> list[str]:
        """텍스트에서 핵심 키워드 추출 (간단 휴리스틱)."""
        # 숫자+단위 패턴
        numbers = re.findall(r'\d+[\d,.]*\s*[%만억원배]?', text)
        # 따옴표 안 텍스트
        quoted = re.findall(r'["\u201C](.+?)["\u201D]', text)
        # 긴 명사구 (2글자 이상 한글 단어)
        nouns = re.findall(r'[가-힣]{2,6}', text)
        # 중복 제거하고 상위 5개
        seen: set[str] = set()
        result: list[str] = []
        for kw in numbers + quoted + nouns:
            if kw not in seen and len(kw) >= 2:
                seen.add(kw)
                result.append(kw)
                if len(result) >= 5:
                    break
        return result

    def _build_niche_rules_text(self, niche: str) -> str:
        """니치별 LLM 프롬프트용 규칙 텍스트."""
        if niche == "real_estate":
            return """## 부동산 채널 시각 규칙 (반드시 따르세요)
- 금리/대출/비용/세금 문장 → chart 또는 infographic
- 지역/입지/역세권 문장 → map 또는 real_broll (도시 항공샷)
- 매수/매도 타이밍/시세 → chart (상승하락 그래프)
- 체크포인트/필수확인 → checklist
- 비교(전세 vs 매매 등) → comparison_card
- 사례/실제 경험 → real_broll (라이프스타일 장면)
- 핵심 결론/경고 → emphasis_caption (큰 글자 강조)
- 청약/분양/계약 프로세스 → infographic"""
        elif niche == "finance":
            return """## 금융 채널 시각 규칙
- 수익률/금리/수치 → chart
- 포트폴리오/자산배분 → infographic
- 비교/장단점 → comparison_card
- 주의사항/실수 → checklist
- 핵심 결론 → emphasis_caption"""
        elif niche == "health":
            return """## 건강 채널 시각 규칙
- 수치/혈당/혈압 → chart
- 전후 비교 → comparison_card
- 체크리스트/증상 확인 → checklist
- 운동/동작 → real_broll
- 핵심 결론 → emphasis_caption"""
        return ""

    @staticmethod
    def _intent_summary(scenes: list[Scene]) -> str:
        """씬 목록의 visual_intent 분포 요약."""
        from collections import Counter
        counts = Counter(s.visual_intent.value for s in scenes)
        return ", ".join(f"{k}={v}" for k, v in counts.most_common())
