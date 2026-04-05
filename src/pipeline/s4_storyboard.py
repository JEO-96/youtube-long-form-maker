"""S4 스토리보드 - 씬 분해 + 미디어 계획."""

from __future__ import annotations

import logging
from typing import Any

from ..core.models import (
    Stage, ScriptResult, VoiceResult, StoryboardResult, Scene,
    MediaType, TransitionType,
)
from ..core.config import load_settings
from .base_stage import BaseStage

logger = logging.getLogger(__name__)


class S4Storyboard(BaseStage):
    """S4: 대본 → 씬 단위 스토리보드."""

    stage = Stage.STORYBOARD

    async def run(self, **kwargs: Any) -> StoryboardResult:
        script_data = self.load_previous(Stage.SCRIPT)
        script = ScriptResult(**script_data)

        # VoiceResult는 선택적 (타이밍 정보)
        voice: VoiceResult | None = None
        voice_data = self.state.load_stage_output(self.production_id, Stage.VOICE)
        if voice_data:
            voice = VoiceResult(**voice_data)

        settings = load_settings()
        stock_ratio = settings.media_generation.stock_mix_ratio  # 0.2

        scenes: list[Scene] = []
        current_time = 0.0
        scene_num = 0

        # Hook 씬
        scene_num += 1
        scenes.append(Scene(
            scene_number=scene_num,
            start_time=0.0,
            end_time=5.0,
            duration=5.0,
            narration_text=script.hook,
            visual_description="강렬한 오프닝 화면",
            media_type=MediaType.AI_VIDEO,
            video_prompt=f"Cinematic opening shot, dramatic lighting, {script.hook[:50]}",
            transition=TransitionType.FADE,
            is_hook=True,
            visual_keywords=script.hook.split()[:3],
        ))
        current_time = 5.0

        # 본문 섹션별 씬
        for i, section in enumerate(script.sections):
            scene_num += 1
            dur = section.estimated_duration_seconds or 60.0

            # stock_ratio에 따라 미디어 타입 결정
            if i % 5 < int(5 * stock_ratio):
                media_type = MediaType.STOCK_VIDEO
            elif i % 2 == 0:
                media_type = MediaType.AI_IMAGE
            else:
                media_type = MediaType.AI_VIDEO

            # 전환 효과 다양화
            transitions = [TransitionType.CUT, TransitionType.DISSOLVE,
                           TransitionType.FADE, TransitionType.SLIDE]
            transition = transitions[i % len(transitions)]

            scenes.append(Scene(
                scene_number=scene_num,
                start_time=round(current_time, 1),
                end_time=round(current_time + dur, 1),
                duration=round(dur, 1),
                narration_text=section.body[:200],
                visual_description=section.visual_prompt or section.header,
                media_type=media_type,
                image_prompt=f"Professional infographic style, {section.visual_prompt or section.header}",
                video_prompt=f"Cinematic B-roll, {section.visual_prompt or section.header}",
                stock_search_query=section.header,
                transition=transition,
                visual_keywords=section.header.split()[:3],
            ))
            current_time += dur

        # CTA 씬
        scene_num += 1
        scenes.append(Scene(
            scene_number=scene_num,
            start_time=round(current_time, 1),
            end_time=round(current_time + 15.0, 1),
            duration=15.0,
            narration_text=script.cta,
            visual_description="구독 유도 엔딩 화면",
            media_type=MediaType.AI_IMAGE,
            image_prompt="Subscribe button animation, channel branding, call to action",
            transition=TransitionType.FADE,
        ))

        # 통계
        ai_video = sum(1 for s in scenes if s.media_type == MediaType.AI_VIDEO)
        stock_video = sum(1 for s in scenes if s.media_type == MediaType.STOCK_VIDEO)
        ai_image = sum(1 for s in scenes if s.media_type == MediaType.AI_IMAGE)

        self.record_cost("system", "storyboard_planning", units=len(scenes), unit_cost=0.0)

        return StoryboardResult(
            scenes=scenes,
            total_scenes=len(scenes),
            ai_video_count=ai_video,
            stock_video_count=stock_video,
            ai_image_count=ai_image,
        )
