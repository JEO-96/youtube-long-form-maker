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

# visual_intent별 영문 stock_search_query 프리셋 (B-roll 힌트)
INTENT_STOCK_HINTS: dict[VisualIntent, str] = {
    VisualIntent.REAL_BROLL: "cinematic urban lifestyle",
    VisualIntent.MAP: "aerial city drone shot",
    VisualIntent.CHART: "financial data dashboard screen",
    VisualIntent.INFOGRAPHIC: "infographic presentation screen",
    VisualIntent.CHECKLIST: "person checking list clipboard",
    VisualIntent.COMPARISON_CARD: "comparison split screen concept",
    VisualIntent.EMPHASIS_CAPTION: "dramatic spotlight text reveal",
    VisualIntent.TALKING_HEAD_STYLE: "professional speaker presentation",
    VisualIntent.CLOSING_CTA: "subscribe notification bell animation",
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
   좋은 예: "청년 첫 집 구매 시점" → "young couple apartment viewing seoul"
   좋은 예: "금리 하락기 매수" → "mortgage rate chart house buying concept"

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

        # Hook 씬
        scenes.append(Scene(
            scene_number=1,
            start_time=0.0, end_time=0.0, duration=0.0,
            narration_text=script.hook,
            visual_description="강렬한 오프닝 — 시청자 이목을 끄는 장면",
            visual_intent=VisualIntent.EMPHASIS_CAPTION,
            media_type=MediaType.AI_IMAGE,
            image_prompt=f"Bold dramatic opening, large text emphasis, {script.hook[:50]}",
            video_prompt=f"Cinematic opening shot, dramatic lighting",
            stock_search_query="dramatic opening cinematic reveal",
            transition=TransitionType.FADE,
            is_hook=True,
            visual_keywords=self._extract_keywords(script.hook)[:3],
        ))

        # Intro 씬 (있으면)
        if script.intro and len(script.intro) > 20:
            scenes.append(Scene(
                scene_number=2,
                start_time=0.0, end_time=0.0, duration=0.0,
                narration_text=script.intro,
                visual_description="도입 — 오늘 주제를 간결하게 소개",
                visual_intent=VisualIntent.TALKING_HEAD_STYLE,
                media_type=MediaType.STOCK_VIDEO,
                image_prompt="Professional presenter speaking to camera",
                video_prompt="Professional speaker presentation style",
                stock_search_query="professional speaker presentation studio",
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
            image_prompt="Subscribe button animation, channel CTA card, professional ending",
            stock_search_query="subscribe notification bell youtube",
            transition=TransitionType.FADE,
            visual_keywords=["구독", "좋아요", "알림"],
        ))

        logger.info(f"규칙 기반 씬 분해: {len(scenes)} scenes from {len(script.sections)} sections")
        return scenes

    def _split_section_to_scenes(
        self, body: str, header: str, niche: str,
    ) -> list[Scene]:
        """섹션 본문을 의미 단위(2~3문장)로 분할하여 씬 목록 반환."""
        # 문장 분리 (한국어 종결어미 + 마침표/물음표/느낌표)
        sentences = re.split(r'(?<=[.!?다요죠까니])\s+', body.strip())
        sentences = [s.strip() for s in sentences if s.strip() and len(s.strip()) > 5]

        if not sentences:
            return [self._make_scene_from_text(body, header, niche)]

        # 2~3문장씩 그룹핑 (너무 짧으면 합치기)
        groups: list[str] = []
        current = ""
        for sent in sentences:
            if len(current) + len(sent) > 150 and current:
                groups.append(current.strip())
                current = sent
            else:
                current = f"{current} {sent}" if current else sent

        if current.strip():
            groups.append(current.strip())

        # 그룹이 1개면 최소 2개로 분할 시도
        if len(groups) == 1 and len(body) > 100:
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
        """한국어 텍스트에서 영문 스톡 검색 쿼리 생성."""
        parts: list[str] = []

        # 키워드 매칭
        for ko, en in self._KO_TO_EN_MAP.items():
            if ko in text:
                parts.append(en)
                if len(parts) >= 3:
                    break

        # intent 기반 힌트 추가
        hint = INTENT_STOCK_HINTS.get(intent, "")
        if hint and hint not in " ".join(parts):
            parts.append(hint)

        if parts:
            return " ".join(parts[:3])

        # 폴백: 숫자가 있으면 chart, 아니면 generic
        if re.search(r'\d+', text):
            return "data chart statistics concept"
        return "professional modern office concept"

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

    def _apply_timing(
        self, scenes: list[Scene], voice: VoiceResult | None, script: ScriptResult,
    ) -> None:
        """씬별 start_time / end_time / duration 계산."""
        if not scenes:
            return

        total_dur = 0.0
        if voice and voice.total_duration_seconds > 0:
            total_dur = voice.total_duration_seconds
        else:
            total_dur = script.estimated_duration_seconds or 600.0

        # 글자수 비례 분배
        char_counts = [max(len(s.narration_text), 10) for s in scenes]
        total_chars = sum(char_counts)

        # voice segments 기반 스냅 시도
        seg_ends: list[float] = []
        if voice and voice.segments:
            seg_ends = sorted(set(
                seg.end for seg in voice.segments if seg.end > 0
            ))

        current_time = 0.0
        for i, scene in enumerate(scenes):
            raw_dur = (char_counts[i] / total_chars) * total_dur
            raw_dur = max(3.0, min(30.0, raw_dur))  # 3~30초 범위

            # segment boundary에 스냅
            target_end = current_time + raw_dur
            if seg_ends:
                snapped = self._find_nearest_seg_end(target_end, seg_ends)
                if snapped > current_time + 2.0:  # 최소 2초 보장
                    target_end = snapped

            scene.start_time = round(current_time, 2)
            scene.end_time = round(target_end, 2)
            scene.duration = round(target_end - current_time, 2)
            current_time = target_end

        # 마지막 씬은 오디오 끝까지 연장
        if scenes:
            scenes[-1].end_time = round(total_dur, 2)
            scenes[-1].duration = round(total_dur - scenes[-1].start_time, 2)

        # 스케일링 보정 (오차 1초 이상이면)
        actual_total = sum(s.duration for s in scenes)
        if abs(actual_total - total_dur) > 1.0 and actual_total > 0:
            scale = total_dur / actual_total
            current_time = 0.0
            for scene in scenes:
                scene.duration = round(scene.duration * scale, 2)
                scene.duration = max(2.0, scene.duration)
                scene.start_time = round(current_time, 2)
                scene.end_time = round(current_time + scene.duration, 2)
                current_time = scene.end_time

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
