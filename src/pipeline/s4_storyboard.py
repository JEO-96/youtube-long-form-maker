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
    VisualIntent.CHART: "finance dashboard laptop analysis",
    VisualIntent.INFOGRAPHIC: "finance desk objects planning",
    VisualIntent.CHECKLIST: "checklist clipboard planning document",
    VisualIntent.COMPARISON_CARD: "comparison side by side split",
    VisualIntent.EMPHASIS_CAPTION: "cinematic finance object closeup",
    VisualIntent.TALKING_HEAD_STYLE: "expert speaker explaining concept",
    VisualIntent.CLOSING_CTA: "creator desk laptop camera closing shot",
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

        # ═══ 쇼츠 모드 감지 ═══
        is_shorts = getattr(self.channel.content, "is_shorts", False) if hasattr(self.channel, "content") else False
        max_scenes_cap = getattr(self.channel.content, "max_scenes", 0) if hasattr(self.channel, "content") else 0

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
        # 쇼츠는 visual beat 세분화를 스킵 — 씬 수 폭발 방지
        if load_settings().media_generation.split_visual_beats and not is_shorts:
            raw_scenes = self._refine_visual_beats(raw_scenes, niche)

        self._apply_niche_rules(raw_scenes, niche)

        # ═══ 재미 요소 다양성 검증 + 강제 삽입 ═══
        self._enforce_visual_variety(raw_scenes)

        # ═══ generic query 후처리 — 일반적 쿼리를 니치 기반으로 재작성 ═══
        self._fix_generic_queries(raw_scenes, niche)
        self._refresh_visual_metadata(raw_scenes, niche)

        # ═══ 쇼츠 강제 제약: 씬 수 cap + intent 분포 재조정 ═══
        if is_shorts:
            raw_scenes = self._apply_shorts_constraints(raw_scenes, max_scenes_cap or 12)

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

        is_shorts = getattr(self.channel.content, "is_shorts", False) if hasattr(self.channel, "content") else False
        max_scenes = getattr(self.channel.content, "max_scenes", 12) if hasattr(self.channel, "content") else 0
        target_seconds = getattr(self.channel.content, "target_duration_seconds", 55) if hasattr(self.channel, "content") else 55

        if is_shorts:
            scene_constraint_text = (
                f"1. 이 영상은 YouTube Shorts입니다. 총 재생 시간은 약 {target_seconds}초입니다.\n"
                f"   - 씬 수는 **정확히 {max_scenes}개 이하**로 제한하세요. 절대 초과 금지.\n"
                f"   - 각 씬은 **3~8초** 분량 (내레이션 1~2문장).\n"
                f"   - 너무 짧게 잘게 쪼개지 마세요. 한 씬에 1개 의미 단위만 담으세요."
            )
        else:
            scene_constraint_text = (
                "1. 섹션 = 씬이 아닙니다. 한 섹션 안에서도 의미가 바뀌면 씬을 나누세요.\n"
                "   - 문제 제기 / 사례 / 기준 / 경고 / 결론 → 각각 별개 씬\n"
                "   - 목표: 씬 1개 = 5~15초 분량 (내레이션 1~3문장)\n"
                "   - 최소 10개 이상의 씬으로 분해하세요. 6개 이하는 절대 안 됩니다.\n"
                "   - 긴 설명형 주제라면 15~25개 씬이 적절합니다."
            )

        system_prompt = f"""당신은 YouTube 영상 스토리보드 감독입니다.
대본을 읽고 "의미 단위"로 씬을 분해하세요.

## 핵심 원칙
{scene_constraint_text}

2. 각 씬의 visual_intent를 반드시 아래 중 하나로 지정하세요:
   {intent_options}
   - real_broll: 도시/생활/현장 실사 영상
   - map: 지도, 지역 항공샷
   - chart: 차트, 그래프, 숫자 카드
   - infographic: 데이터 시각화, 프로세스 도해
   - checklist: 체크리스트 카드
   - comparison_card: A vs B 비교, before/after
   - emphasis_caption: 텍스트 없는 강조 배경(빛, 실제 사물, 상징 오브젝트)
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
   - 텍스트 없는 강조 배경 (emphasis_caption)
   - 비교 카드 (comparison_card)
   - 체크리스트 (checklist)
   - 차트/지도 인서트 (chart, map)
   - 같은 유형이 3번 연속 반복되면 안 됩니다.

5. 자막은 SRT 레이어가 담당합니다.
   - 배경 이미지 안에는 읽을 수 있는 텍스트, 숫자, 퍼센트, 라벨을 넣지 않습니다.
   - visual_description에는 화면에 글자/숫자를 넣으라는 지시를 쓰지 마세요.
   - visual_description과 stock_search_query는 반드시 해당 씬 narration_text의 의미에 직접 연결하세요.

6. visual_cue는 배경 전환용 내부 단서입니다.
   - 예: "50만원 빼면 125만원"처럼 돈이 빠지는 멘트 → "money_decrease"
   - 예: 월급/입금/수입 → "money_income", 저축/모으기 → "money_saving", 남는 돈/잔액 → "remaining_balance"
   - visual_cue는 화면에 쓰는 텍스트가 아니라 이미지 구도 선택용 메타데이터입니다.

{niche_rules_text}

## 출력 형식
JSON으로만 응답하세요:
{{
  "scenes": [
    {{
      "narration_text": "이 씬의 나레이션 텍스트 (원문 그대로)",
      "visual_intent": "chart",
      "visual_description": "이 장면에 어울리는 구체적 시각 묘사 (한국어, 화면 텍스트/숫자 지시 금지)",
      "stock_search_query": "english semantic search query for this scene",
      "visual_keywords": ["검수용키워드1", "검수용키워드2"],
      "visual_cue": "money_decrease",
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
            visual_cue = sd.get("visual_cue") or self._infer_visual_cue(narration, niche)
            cue_intent = self._intent_for_visual_cue(visual_cue)
            if cue_intent:
                intent = cue_intent
                media_type = INTENT_TO_MEDIA_TYPE.get(intent, media_type)

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
                visual_cue=visual_cue,
            ))

        if not scenes:
            raise ValueError("LLM이 빈 씬 목록을 반환")

        # 최소 씬 수 검증 — 6개 이하면 규칙 기반 보강 (쇼츠는 스킵)
        if not is_shorts and len(scenes) < 8 and len(script.sections) >= 2:
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
        hook_cue = self._infer_visual_cue(script.hook, niche)
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
            visual_cue=hook_cue,
        ))

        # Intro 씬 (있으면) — 내레이션 의미 반영
        if script.intro and len(script.intro) > 20:
            intro_query = self._generate_stock_query(script.intro, VisualIntent.TALKING_HEAD_STYLE)
            intro_cue = self._infer_visual_cue(script.intro, niche)
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
                visual_cue=intro_cue,
            ))

        # 본문 섹션별 → 문장 단위 분할
        for sec in script.sections:
            sub_scenes = self._split_section_to_scenes(sec.body, sec.header, niche)
            for ss in sub_scenes:
                ss.scene_number = len(scenes) + 1
                ss.transition = transitions[len(scenes) % len(transitions)]
                scenes.append(ss)

        # CTA 씬
        cta_cue = self._infer_visual_cue(script.cta, niche)
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
            visual_cue=cta_cue or "closing_cta",
        ))

        logger.info(f"규칙 기반 씬 분해: {len(scenes)} scenes from {len(script.sections)} sections")
        return scenes

    def _split_section_to_scenes(
        self, body: str, header: str, niche: str,
    ) -> list[Scene]:
        """섹션 본문을 의미 단위(1~2문장)로 분할하여 씬 목록 반환.

        개선점:
        - 그룹핑 기준을 너무 짧게 잡지 않아 불필요한 화면 전환을 줄임
        - 강한 의미 전환 키워드(하지만, 반면 등)에서만 강제 분할
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

        # 1~3문장씩 그룹핑 (180자 기준 + 의미 전환 시 강제 분할)
        groups: list[str] = []
        current = ""
        for sent in sentences:
            # 의미 전환 키워드로 시작하면 현재 그룹 확정
            is_split_trigger = any(sent.startswith(t) for t in _SPLIT_TRIGGERS)

            if (len(current) + len(sent) > 180 and current) or \
               (is_split_trigger and current):
                groups.append(current.strip())
                current = sent
            else:
                current = f"{current} {sent}" if current else sent

        if current.strip():
            groups.append(current.strip())

        # 그룹이 1개면 최소 2개로 분할 시도
        if len(groups) == 1 and len(body) > 180:
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
        visual_cue = self._infer_visual_cue(text, niche)
        intent = self._intent_for_visual_cue(visual_cue) or self._infer_intent_from_text(text, niche)
        media_type = INTENT_TO_MEDIA_TYPE.get(intent, MediaType.AI_IMAGE)
        stock_query = self._generate_stock_query(text, intent)
        vis_desc = self._generate_visual_description(text, intent, section_header, visual_cue)
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
            visual_cue=visual_cue,
        )

    def _refine_visual_beats(self, scenes: list[Scene], niche: str) -> list[Scene]:
        """내레이션 속 시각 전환점 단위로 씬을 더 잘게 나눈다.

        SRT가 자막을 담당하므로, 배경은 "돈이 빠짐", "잔액만 남음" 같은
        내부 cue만 사용해 멘트와 더 촘촘하게 맞춘다.
        """
        if not scenes:
            return scenes

        max_chars = max(140, load_settings().media_generation.visual_beat_max_chars)
        refined: list[Scene] = []
        split_count = 0

        for scene in scenes:
            text = (scene.narration_text or "").strip()
            if not text:
                refined.append(scene)
                continue

            # CTA는 구조가 고정된 엔딩이라 과분할하지 않는다.
            if scene.visual_intent == VisualIntent.CLOSING_CTA:
                scene.visual_cue = scene.visual_cue or "closing_cta"
                refined.append(scene)
                continue

            beats = self._split_text_to_visual_beats(text, max_chars=max_chars)
            if len(beats) <= 1:
                cue = scene.visual_cue or self._infer_visual_cue(text, niche)
                cue_intent = self._intent_for_visual_cue(cue)
                if cue_intent:
                    scene.visual_intent = cue_intent
                    scene.media_type = INTENT_TO_MEDIA_TYPE.get(cue_intent, scene.media_type)
                    scene.visual_description = self._generate_visual_description(
                        text, scene.visual_intent, "", cue,
                    )
                    scene.stock_search_query = self._generate_stock_query(text, scene.visual_intent)
                    scene.image_prompt = self._build_image_prompt(
                        scene.visual_intent, scene.visual_description, scene.stock_search_query,
                    )
                    scene.video_prompt = self._build_video_prompt(
                        scene.visual_intent, scene.visual_description, scene.stock_search_query,
                    )
                scene.visual_cue = cue
                refined.append(scene)
                continue

            split_count += len(beats) - 1
            for beat_idx, beat in enumerate(beats):
                new_scene = self._copy_scene(scene)
                cue = self._infer_visual_cue(beat, niche)
                intent = self._intent_for_visual_cue(cue) or self._infer_intent_from_text(beat, niche)
                stock_query = self._generate_stock_query(beat, intent)
                vis_desc = self._generate_visual_description(beat, intent, "", cue)

                new_scene.scene_number = 0
                new_scene.start_time = 0.0
                new_scene.end_time = 0.0
                new_scene.duration = 0.0
                new_scene.narration_text = beat[:300]
                new_scene.visual_description = vis_desc
                new_scene.visual_intent = intent
                new_scene.media_type = INTENT_TO_MEDIA_TYPE.get(intent, new_scene.media_type)
                new_scene.image_prompt = self._build_image_prompt(intent, vis_desc, stock_query)
                new_scene.video_prompt = self._build_video_prompt(intent, vis_desc, stock_query)
                new_scene.stock_search_query = stock_query
                new_scene.visual_keywords = self._extract_keywords(beat)[:5]
                new_scene.visual_cue = cue
                new_scene.is_hook = scene.is_hook and beat_idx == 0
                refined.append(new_scene)

        if split_count:
            logger.info(
                f"Visual beat refinement: +{split_count} scenes "
                f"({len(scenes)} → {len(refined)})"
            )
        return refined

    @staticmethod
    def _copy_scene(scene: Scene) -> Scene:
        """Pydantic v1/v2 양쪽에서 동작하는 Scene 복사."""
        if hasattr(scene, "model_copy"):
            return scene.model_copy(deep=True)
        return scene.copy(deep=True)

    @staticmethod
    def _split_text_to_visual_beats(text: str, max_chars: int = 80) -> list[str]:
        """문장보다 작은 시각 beat 후보로 분할한다."""
        text = re.sub(r'\s+', ' ', text).strip()
        if not text:
            return []

        sentence_parts = re.split(r'(?<=[.!?다요죠까니])\s+', text)
        sentence_parts = [p.strip() for p in sentence_parts if p.strip()]
        if not sentence_parts:
            sentence_parts = [text]

        trigger = (
            r'월급|수입|입금|고정비|카드값|월세|남는|남은|잔액|'
            r'대출|이자|목표|계획|위험|리스크|실수|함정|주의|비교|차이|'
            r'자동이체|쪼개|나누|분리|옮기|옮깁|옮겨|그다음|첫째|둘째|셋째|넷째|'
            r'1단계|2단계|3단계|4단계'
        )

        raw_parts: list[str] = []
        for part in sentence_parts:
            force_split = bool(
                re.search(r'빼면|빠지|남는|남은|남습니다|잔액', part)
                and re.search(r'자동이체|옮기|옮깁|옮겨|이체|나누|쪼개|분리', part)
            )
            if len(part) <= max_chars and S4Storyboard._count_visual_cues(part) <= 2 and not force_split:
                raw_parts.append(part)
                continue

            pieces = re.split(rf'(?<=[,;])\s+|\s+(?=(?:{trigger}))', part)
            pieces = [p.strip(" ,;") for p in pieces if p.strip(" ,;")]
            if len(pieces) <= 1:
                raw_parts.append(part)
            else:
                raw_parts.extend(pieces)

        beats: list[str] = []
        min_beat_chars = 24
        for part in raw_parts:
            atomic_part = S4Storyboard._is_atomic_visual_beat(part)
            atomic_prev = S4Storyboard._is_atomic_visual_beat(beats[-1]) if beats else False
            if beats and len(part) < min_beat_chars and not atomic_part:
                beats[-1] = f"{beats[-1]} {part}".strip()
            elif beats and len(beats[-1]) < min_beat_chars and not atomic_prev and not atomic_part:
                beats[-1] = f"{beats[-1]} {part}".strip()
            else:
                beats.append(part)

        return [b for b in beats if b.strip()]

    @staticmethod
    def _is_atomic_visual_beat(text: str) -> bool:
        """짧아도 별도 화면으로 남길 만큼 구체적인 시각 beat인지 판별한다."""
        cue = S4Storyboard._infer_visual_cue(text, "")
        money_amounts = re.findall(r'\d+[\d,.]*\s*(?:만\s*)?원|\d+[\d,.]*\s*억', text)

        if cue in {"money_decrease", "remaining_balance"} and len(money_amounts) >= 2:
            return True
        if cue in {"money_income", "money_saving"} and money_amounts and len(text) >= 18:
            return True
        if cue == "account_split" and re.search(r'자동이체|옮기|옮깁|옮겨|이체|나누|쪼개|분리', text):
            return True
        if cue in {"risk_warning", "debt_pressure"} and len(text) >= 24:
            return True
        return False

    @staticmethod
    def _has_strong_visual_cue(text: str) -> bool:
        """별도 화면으로 남겨도 좋은 강한 시각 cue인지 판별한다."""
        cue = S4Storyboard._infer_visual_cue(text, "")
        return cue in {
            "money_decrease",
            "remaining_balance",
            "money_income",
            "money_saving",
            "debt_pressure",
            "expense_breakdown",
            "account_split",
            "risk_warning",
            "target_goal",
            "step_sequence",
            "comparison",
            "checklist",
        }

    @staticmethod
    def _count_visual_cues(text: str) -> int:
        """한 문장 안의 주요 시각 cue 개수를 대략 센다."""
        patterns = [
            r'\d+[\d,.]*\s*(?:만\s*)?원',
            r'\d+[\d,.]*\s*억',
            r'월급|수입|입금',
            r'빼면|빠지면|지출|고정비|생활비|카드값|월세',
            r'남는|남은|잔액',
            r'저축|모으|쌓이|투자',
            r'대출|빚|이자|부채',
            r'목표|계획|기준',
            r'위험|리스크|실수|함정|주의',
            r'비교|차이|반면|이전|이후',
            r'자동이체|쪼개|나누|분리|옮기',
            r'첫째|둘째|셋째|단계|순서',
        ]
        return sum(1 for pat in patterns if re.search(pat, text))

    @staticmethod
    def _infer_visual_cue(text: str, niche: str = "") -> str:
        """내레이션에서 배경 생성용 내부 시각 단서를 추론한다."""
        if not text:
            return ""

        has_money = bool(re.search(r'\d+[\d,.]*\s*(?:만\s*)?원|\d+[\d,.]*\s*억|돈|현금|통장|잔액', text))
        decrease = bool(re.search(r'빼면|제외하면|차감|빠지|나가|지출|고정비|생활비|카드값|월세|줄어|감소', text))
        remaining = bool(re.search(r'남는|남은|남습니다|남아요|잔액|여유', text))
        income = bool(re.search(r'월급|수입|입금|들어오|벌면|받으면', text))
        saving = bool(re.search(r'저축|모으|쌓이|적금|투자|불어', text))
        debt = bool(re.search(r'대출|빚|이자|부채|상환', text))
        account_split = bool(re.search(r'통장\s*쪼개|쪼개|나누|분리|자동이체|옮기|옮깁|옮겨|이체|분산', text))

        if account_split and (has_money or re.search(r'통장|저축|생활비|비상금', text)):
            return "account_split"
        if re.search(r'고정비|생활비|카드값|월세|예산|지출\s*항목|분류|항목별', text):
            return "expense_breakdown"

        if has_money and decrease:
            return "money_decrease"
        if has_money and remaining:
            return "remaining_balance"
        if has_money and income:
            return "money_income"
        if has_money and saving:
            return "money_saving"
        if debt:
            return "debt_pressure"
        if re.search(r'위험|리스크|실수|함정|주의|경고|망하|실패|손실|무시', text):
            return "risk_warning"
        if re.search(r'목표|계획|기준|우선순위|전략|원칙', text):
            return "target_goal"
        if re.search(r'첫째|둘째|셋째|넷째|1단계|2단계|3단계|단계|순서|프로세스', text):
            return "step_sequence"
        if re.search(r'예를 들어|실제로|사례|경험|케이스', text):
            return "case_story"
        if re.search(r'\d+\s*(?:개월|년|일)|전부터|이후|이전|기간|장기|단기|매달|매월', text):
            return "timeline"
        if re.search(r'증가|늘어|올라|상승|반등|개선|불어|높아', text):
            return "growth_trend"
        if re.search(r'감소|줄어|내려|하락|떨어|낮아', text):
            return "decline_trend"
        if re.search(r'\d+[\d,.]*\s*%|금리|수익률|비율', text):
            return "rate_chart"
        if any(kw in text for kw in ["비교", "차이", "vs", "반면", "이전", "이후"]):
            return "comparison"
        if any(kw in text for kw in ["체크", "확인", "주의", "실수", "첫째", "둘째", "셋째"]):
            return "checklist"
        return ""

    @staticmethod
    def _intent_for_visual_cue(cue: str) -> VisualIntent | None:
        """visual_cue에 가장 잘 맞는 기본 visual_intent."""
        cue_to_intent: dict[str, VisualIntent] = {
            "money_decrease": VisualIntent.INFOGRAPHIC,
            "remaining_balance": VisualIntent.INFOGRAPHIC,
            "money_income": VisualIntent.INFOGRAPHIC,
            "money_saving": VisualIntent.INFOGRAPHIC,
            "debt_pressure": VisualIntent.INFOGRAPHIC,
            "expense_breakdown": VisualIntent.INFOGRAPHIC,
            "account_split": VisualIntent.INFOGRAPHIC,
            "risk_warning": VisualIntent.EMPHASIS_CAPTION,
            "target_goal": VisualIntent.INFOGRAPHIC,
            "step_sequence": VisualIntent.CHECKLIST,
            "case_story": VisualIntent.REAL_BROLL,
            "timeline": VisualIntent.INFOGRAPHIC,
            "growth_trend": VisualIntent.CHART,
            "decline_trend": VisualIntent.CHART,
            "rate_chart": VisualIntent.CHART,
            "comparison": VisualIntent.COMPARISON_CARD,
            "checklist": VisualIntent.CHECKLIST,
        }
        return cue_to_intent.get(cue)

    @staticmethod
    def _visual_cue_description(cue: str) -> str:
        """visual_cue를 텍스트 없는 이미지 구도 설명으로 변환."""
        return {
            "money_decrease": "영수증과 지갑 옆 현금 더미가 줄어든 사실적인 장면",
            "remaining_balance": "작게 남은 현금과 모바일뱅킹 화면을 보여주는 사실적인 장면",
            "money_income": "월급 입금을 암시하는 은행 카드, 현금, 모바일뱅킹의 사실적인 장면",
            "money_saving": "현금, 동전, 저축 봉투, 가계부가 놓인 사실적인 장면",
            "debt_pressure": "대출 서류, 청구서, 계산기가 놓인 부담감 있는 책상 장면",
            "expense_breakdown": "영수증, 봉투, 카드, 계산기로 지출 항목을 나누는 사실적인 장면",
            "account_split": "여러 봉투와 은행 카드, 현금 더미로 통장 분리를 표현한 사실적인 장면",
            "risk_warning": "빈 지갑, 청구서, 계산기로 재정 위험을 보여주는 사실적인 장면",
            "target_goal": "가계부, 달력, 현금 봉투로 재정 목표를 계획하는 사실적인 장면",
            "step_sequence": "휴대폰, 카드, 봉투, 가계부가 순서대로 놓인 실행 과정 장면",
            "case_story": "실제 생활 속 재정 계획을 하는 사람과 책상 장면",
            "timeline": "달력, 가계부, 저축 봉투로 기간 흐름을 보여주는 사실적인 장면",
            "growth_trend": "정돈된 책상 위 저축 내역과 밝은 조명으로 개선 흐름을 표현한 장면",
            "decline_trend": "줄어든 현금 더미와 영수증으로 하락 흐름을 표현한 사실적인 장면",
            "rate_chart": "노트북의 라벨 없는 금융 대시보드로 금리 흐름을 보여주는 장면",
            "comparison": "두 가지 재정 선택지를 실제 물건 배치로 비교하는 사실적인 장면",
            "checklist": "가계부, 펜, 달력으로 실행 항목을 준비하는 사실적인 장면",
        }.get(cue, "")

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

    def _refresh_visual_metadata(self, scenes: list[Scene], niche: str) -> None:
        """후처리로 바뀐 intent/cue에 맞춰 프롬프트와 쿼리를 동기화한다."""
        for scene in scenes:
            cue = scene.visual_cue or self._infer_visual_cue(scene.narration_text, niche)
            cue_intent = self._intent_for_visual_cue(cue)
            if cue_intent:
                scene.visual_intent = cue_intent
                scene.media_type = INTENT_TO_MEDIA_TYPE.get(cue_intent, scene.media_type)
                scene.visual_description = self._generate_visual_description(
                    scene.narration_text, scene.visual_intent, "", cue,
                )

            scene.visual_cue = cue
            if not scene.stock_search_query:
                scene.stock_search_query = self._generate_stock_query(
                    scene.narration_text, scene.visual_intent,
                )

            scene.image_prompt = self._build_image_prompt(
                scene.visual_intent,
                scene.visual_description,
                scene.stock_search_query,
            )
            scene.video_prompt = self._build_video_prompt(
                scene.visual_intent,
                scene.visual_description,
                scene.stock_search_query,
            )

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
        "월급": "salary payment bank account", "수입": "income deposit bank account",
        "통장": "bank account balance", "잔액": "bank balance money",
        "고정비": "monthly expenses bills", "생활비": "household budget expenses",
        "카드값": "credit card bill payment", "저축": "saving money cash envelope",
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
        cue = self._infer_visual_cue(text, "")
        cue_queries: dict[str, str] = {
            "money_decrease": "cash stack decreasing monthly expense budget",
            "remaining_balance": "bank account balance cash stack budget",
            "money_income": "salary deposit bank account money",
            "money_saving": "saving money cash envelope bank card",
            "debt_pressure": "debt pressure loan payment finance",
            "expense_breakdown": "household budget expense breakdown finance",
            "account_split": "bank account budgeting envelope system",
            "risk_warning": "financial stress bills calculator wallet",
            "target_goal": "financial goal planning notebook calendar",
            "step_sequence": "banking setup steps phone card envelopes",
            "case_story": "real life financial planning scene",
            "timeline": "project timeline roadmap planning",
            "growth_trend": "upward trend financial chart",
            "decline_trend": "downward trend financial chart",
            "rate_chart": "interest rate chart financial graph",
        }
        if cue in cue_queries:
            return cue_queries[cue]

        parts: list[str] = []

        # 키워드 매칭 (문장 의미 기반)
        for ko, en in self._KO_TO_EN_MAP.items():
            if ko in text:
                parts.append(en)
                if len(parts) >= 3:
                    break

        # intent별 구체적 컨텍스트 추가 (generic 힌트 대신)
        intent_context: dict[VisualIntent, str] = {
            VisualIntent.CHART: "finance dashboard laptop analysis",
            VisualIntent.INFOGRAPHIC: "finance desk objects planning",
            VisualIntent.CHECKLIST: "checklist clipboard planning",
            VisualIntent.COMPARISON_CARD: "comparison side by side",
            VisualIntent.EMPHASIS_CAPTION: "cinematic finance object closeup",
            VisualIntent.REAL_BROLL: "",  # 키워드만으로 충분
            VisualIntent.MAP: "aerial map city view",
            VisualIntent.TALKING_HEAD_STYLE: "expert speaker explaining",
            VisualIntent.CLOSING_CTA: "creator desk laptop camera closing shot",
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
            VisualIntent.CHART: "Premium photorealistic finance dashboard scene,",
            VisualIntent.INFOGRAPHIC: "Premium editorial finance object scene,",
            VisualIntent.CHECKLIST: "Photorealistic planning desk scene,",
            VisualIntent.COMPARISON_CARD: "Cinematic side-by-side real-world comparison scene,",
            VisualIntent.EMPHASIS_CAPTION: "Dramatic cinematic real-object emphasis scene,",
            VisualIntent.REAL_BROLL: "Professional photography,",
            VisualIntent.MAP: "Aerial city map view,",
            VisualIntent.TALKING_HEAD_STYLE: "Professional presenter style,",
            VisualIntent.CLOSING_CTA: "Polished creator desk closing shot,",
        }
        prefix = intent_prefix.get(intent, "Professional photography,")
        desc_part = desc[:150] if desc else stock_q[:100]
        return (
            f"{prefix} {desc_part}, high quality, 16:9, "
            "no readable text, no Korean characters, no Latin letters, no numerals, no captions, no labels"
        )

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
        self, text: str, intent: VisualIntent, header: str, visual_cue: str = "",
    ) -> str:
        """씬의 시각 설명 생성 (한국어)."""
        cue_desc = self._visual_cue_description(visual_cue)
        if cue_desc:
            return cue_desc

        intent_desc = {
            VisualIntent.CHART: "데이터 차트/그래프로 핵심 수치 시각화",
            VisualIntent.INFOGRAPHIC: "인포그래픽으로 정보 구조화",
            VisualIntent.CHECKLIST: "체크리스트 카드로 핵심 포인트 정리",
            VisualIntent.COMPARISON_CARD: "비교 카드로 차이점 시각화",
            VisualIntent.EMPHASIS_CAPTION: "텍스트 없는 강조 배경으로 핵심 흐름 표현",
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

    # ═══ 쇼츠용 duration 범위 (초) — 모든 intent 3~8초 ═══
    _SHORTS_INTENT_DURATION_RANGE: dict[VisualIntent, tuple[float, float]] = {
        intent: (3.0, 8.0) for intent in VisualIntent
    }

    def _apply_shorts_constraints(
        self, scenes: list[Scene], max_scenes: int,
    ) -> list[Scene]:
        """쇼츠 모드 제약 적용 — 씬 수 cap + visual_intent 분포 재조정.

        규칙:
        1. 씬 수를 max_scenes 이하로 cap (초과 시 인접 씬 병합)
        2. REAL_BROLL은 최대 3개까지 (Pexels 쿼리 중복 리스크 감소)
        3. 카드 계열(CHART/CHECKLIST/COMPARISON_CARD/EMPHASIS_CAPTION) 우선 유지
        """
        if not scenes:
            return scenes

        # ═══ 1. 씬 수 cap — 초과분은 인접한 짧은 씬과 병합 ═══
        if len(scenes) > max_scenes:
            logger.info(
                f"Shorts: {len(scenes)}개 씬 → {max_scenes}개로 병합"
            )
            # 내레이션이 짧은 씬부터 병합 후보
            while len(scenes) > max_scenes:
                # 가장 짧은 narration을 가진 씬 탐색
                shortest_idx = min(
                    range(len(scenes)),
                    key=lambda i: len(scenes[i].narration_text),
                )
                # 병합 대상: 앞 또는 뒤 (더 짧은 쪽)
                if shortest_idx == 0 and len(scenes) > 1:
                    merge_target = 1
                elif shortest_idx == len(scenes) - 1:
                    merge_target = shortest_idx - 1
                else:
                    # 양쪽 중 더 짧은 쪽으로 병합
                    left_len = len(scenes[shortest_idx - 1].narration_text)
                    right_len = len(scenes[shortest_idx + 1].narration_text)
                    merge_target = shortest_idx - 1 if left_len <= right_len else shortest_idx + 1

                # 병합: narration 이어붙이기, target의 intent 유지
                scenes[merge_target].narration_text = (
                    scenes[merge_target].narration_text
                    + " "
                    + scenes[shortest_idx].narration_text
                ).strip()
                scenes.pop(shortest_idx)

        # ═══ 2. REAL_BROLL 상한 3개 — 초과분은 카드 계열로 재지정 ═══
        real_broll_indices = [
            i for i, s in enumerate(scenes)
            if s.visual_intent == VisualIntent.REAL_BROLL
        ]
        if len(real_broll_indices) > 3:
            card_alternatives = [
                VisualIntent.EMPHASIS_CAPTION,
                VisualIntent.CHART,
                VisualIntent.CHECKLIST,
                VisualIntent.COMPARISON_CARD,
            ]
            to_convert = real_broll_indices[3:]  # 4번째부터
            for cnt, idx in enumerate(to_convert):
                new_intent = card_alternatives[cnt % len(card_alternatives)]
                scenes[idx].visual_intent = new_intent
                scenes[idx].media_type = INTENT_TO_MEDIA_TYPE.get(
                    new_intent, scenes[idx].media_type,
                )
            logger.info(
                f"Shorts: REAL_BROLL {len(to_convert)}개를 카드 계열로 재지정"
            )

        # scene_number 재할당
        for i, sc in enumerate(scenes):
            sc.scene_number = i + 1

        return scenes

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

        # 쇼츠 모드: 모든 intent를 3~8초 범위로 강제
        is_shorts = getattr(self.channel.content, "is_shorts", False) if hasattr(self.channel, "content") else False
        duration_range_map = (
            self._SHORTS_INTENT_DURATION_RANGE if is_shorts
            else self._INTENT_DURATION_RANGE
        )
        default_range = (3.0, 8.0) if is_shorts else (4.0, 15.0)

        raw_durations: list[float] = []
        for i, scene in enumerate(scenes):
            raw_dur = (char_counts[i] / total_chars) * total_dur
            intent = getattr(scene, "visual_intent", VisualIntent.REAL_BROLL)
            min_dur, max_dur = duration_range_map.get(intent, default_range)
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
- 핵심 결론/경고 → emphasis_caption (텍스트 없는 강조 배경)
- 청약/분양/계약 프로세스 → infographic"""
        elif niche == "finance":
            return """## 금융 채널 시각 규칙
- 수익률/금리/수치 → chart
- 포트폴리오/자산배분 → infographic
- 월급/통장/고정비/잔액/저축 → money_* visual_cue + infographic
- "빼면", "남는 돈", "잔액"처럼 돈 흐름이 바뀌는 문장 → 별도 씬으로 분리
- 통장 쪼개기/자동이체/예산 항목 → account_split 또는 expense_breakdown
- 목표/계획/기준 → target_goal, 실수/위험/함정 → risk_warning
- 사례/실제로/예를 들어 → case_story, 단계 설명 → step_sequence
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
