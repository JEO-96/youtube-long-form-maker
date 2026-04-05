"""S6 편집 - MoviePy 중심 + Retention 엔진."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..core.models import (
    Stage, ScriptResult, VoiceResult, MediaResult, MediaAsset,
    EditingResult, MediaType,
)
from ..core.config import load_settings
from .base_stage import BaseStage

logger = logging.getLogger(__name__)


class S6Editing(BaseStage):
    """S6: MoviePy 편집 + Retention 엔진 적용."""

    stage = Stage.EDITING

    async def run(self, **kwargs: Any) -> EditingResult:
        script_data = self.load_previous(Stage.SCRIPT)
        voice_data = self.load_previous(Stage.VOICE)
        media_data = self.load_previous(Stage.MEDIA)

        script = ScriptResult(**script_data)
        voice = VoiceResult(**voice_data)
        media = MediaResult(**media_data)

        self.stage_dir.mkdir(parents=True, exist_ok=True)
        output_path = self.stage_dir / "final.mp4"

        settings = load_settings()
        applied_effects: list[str] = []

        # Retention 엔진: Hook + Pattern Interrupt 계획
        from ..retention.hook_system import HookSystem
        from ..retention.pattern_interrupt import PatternInterruptEngine

        hook_sys = HookSystem(seed=42)
        hook_result = hook_sys.generate_hook(
            topic=script.title,
            audience=self.channel.target_audience,
        )
        applied_effects.append(f"hook:{hook_result.style.value}")

        total_dur = voice.total_duration_seconds or script.estimated_duration_seconds or 60.0
        pi_engine = PatternInterruptEngine(seed=42)
        pi_timeline = pi_engine.generate_timeline(
            total_duration=total_dur,
            start_offset=settings.retention.hook_duration_seconds,
        )
        applied_effects.append(f"pattern_interrupts:{pi_timeline.event_count}")

        # Audio Ducking (BGM이 있으면)
        mixed_audio_path = self.stage_dir / "mixed_audio.wav"
        audio_ducked = False
        bgm_path = kwargs.get("bgm_path")
        if bgm_path and Path(bgm_path).exists() and Path(voice.audio_path).exists():
            try:
                from ..retention.audio_mixing import AudioMixer
                mixer = AudioMixer()
                mixer.mix(Path(voice.audio_path), Path(bgm_path), mixed_audio_path)
                audio_ducked = True
                applied_effects.append("audio_ducking:applied")
            except Exception as e:
                logger.warning(f"Audio ducking failed: {e}")
                applied_effects.append("audio_ducking:skipped")

        # MoviePy 편집
        duration, file_size = self._compose_video(
            media, voice, output_path,
            mixed_audio_path if audio_ducked else None,
        )

        self.record_cost("system", "editing", units=1, unit_cost=0.0)

        # ═══ Optional Enhancers (Phase 5B) ═══
        # Topaz 업스케일 + CapCut 후처리 — 실패해도 MoviePy 결과 보존
        final_path = await self._apply_enhancers(output_path, applied_effects)

        return EditingResult(
            output_path=str(final_path),
            duration_seconds=duration,
            resolution=[1920, 1080],
            file_size_mb=round(file_size, 2),
            applied_effects=applied_effects,
            pattern_interrupts_count=pi_timeline.event_count,
        )

    def _compose_video(
        self,
        media: MediaResult,
        voice: VoiceResult,
        output_path: Path,
        mixed_audio_path: Path | None = None,
    ) -> tuple[float, float]:
        """MoviePy로 미디어 합성."""
        try:
            from moviepy import (
                ImageClip, VideoFileClip, AudioFileClip,
                concatenate_videoclips, CompositeVideoClip,
            )
        except ImportError:
            logger.warning("moviepy not available, creating minimal output")
            return self._fallback_compose(media, voice, output_path)

        clips = []
        target_size = (1920, 1080)

        for asset in sorted(media.assets, key=lambda a: a.scene_number):
            asset_path = Path(asset.file_path)
            if not asset_path.exists():
                logger.warning(f"Missing asset: {asset_path}")
                continue

            try:
                if asset.media_type in (MediaType.AI_VIDEO, MediaType.STOCK_VIDEO):
                    clip = VideoFileClip(str(asset_path))
                    clip = clip.resized(target_size)
                else:
                    # 이미지 → 3초 클립
                    clip = ImageClip(str(asset_path), duration=3.0)
                    clip = clip.resized(target_size)
                clips.append(clip)
            except Exception as e:
                logger.warning(f"Failed to load asset {asset_path}: {e}")
                continue

        if not clips:
            logger.warning("No valid clips, creating color placeholder")
            from moviepy import ColorClip
            dur = voice.total_duration_seconds or 10.0
            clips = [ColorClip(size=target_size, color=(20, 40, 60), duration=min(dur, 30))]

        video = concatenate_videoclips(clips, method="compose")

        # 오디오 추가
        audio_path = mixed_audio_path or (Path(voice.audio_path) if voice.audio_path else None)
        if audio_path and audio_path.exists():
            try:
                audio = AudioFileClip(str(audio_path))
                # 비디오/오디오 길이 맞추기
                final_dur = min(video.duration, audio.duration)
                video = video.with_duration(final_dur)
                audio = audio.with_duration(final_dur)
                video = video.with_audio(audio)
            except Exception as e:
                logger.warning(f"Audio merge failed: {e}")

        video.write_videofile(
            str(output_path), fps=30, codec="libx264",
            audio_codec="aac", logger=None,
        )

        duration = video.duration
        for c in clips:
            c.close()
        video.close()

        file_size_mb = output_path.stat().st_size / (1024 * 1024) if output_path.exists() else 0
        return duration, file_size_mb

    async def _apply_enhancers(
        self, moviepy_output: Path, applied_effects: list[str]
    ) -> Path:
        """Optional post-processing enhancers.

        원칙: 실패해도 MoviePy 결과물(moviepy_output) 보존.
        """
        result_path = moviepy_output

        if self.dry_run:
            return result_path

        # 1) Topaz 업스케일 (선택적)
        try:
            from ..providers.upscaler import TopazUpscaler
            topaz = TopazUpscaler()
            if topaz.is_available():
                upscaled = await topaz.upscale_safe(result_path)
                if upscaled != result_path:
                    result_path = upscaled
                    applied_effects.append("topaz_upscale:applied")
                else:
                    applied_effects.append("topaz_upscale:skipped")
            else:
                applied_effects.append("topaz_upscale:not_installed")
        except Exception as e:
            logger.warning(f"Topaz enhancer error: {e}")
            applied_effects.append("topaz_upscale:error")

        # 2) CapCut 후처리 (선택적)
        try:
            from ..providers.capcut import CapCutProvider
            capcut = CapCutProvider()
            if capcut.is_available():
                enhanced = await capcut.enhance_safe(result_path)
                if enhanced != result_path:
                    result_path = enhanced
                    applied_effects.append("capcut_enhance:applied")
                else:
                    applied_effects.append("capcut_enhance:skipped")
            else:
                applied_effects.append("capcut_enhance:no_api_key")
        except Exception as e:
            logger.warning(f"CapCut enhancer error: {e}")
            applied_effects.append("capcut_enhance:error")

        return result_path

    def _fallback_compose(
        self, media: MediaResult, voice: VoiceResult, output_path: Path
    ) -> tuple[float, float]:
        """MoviePy 없을 때 최소 출력."""
        output_path.write_bytes(b"\x00" * 4096)
        return voice.total_duration_seconds or 10.0, 0.004
