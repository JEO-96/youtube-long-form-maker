"""S6 편집 - ffmpeg 네이티브 합성 + Retention 엔진."""

from __future__ import annotations

import logging
import subprocess
import shutil
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from ..core.models import (
    Stage, ScriptResult, VoiceResult, MediaResult, MediaAsset,
    StoryboardResult, EditingResult, MediaType, VisualIntent,
)
from ..core.config import load_settings
from ..core.exceptions import StageError
from .base_stage import BaseStage

logger = logging.getLogger(__name__)


class S6Editing(BaseStage):
    """S6: ffmpeg 네이티브 편집 + Retention 엔진 적용."""

    stage = Stage.EDITING

    # ═══ 싱크 미세 조정 상수 ═══
    SCENE_TRANSITION_MARGIN = 0.15
    MIN_SCENE_DURATION = 2.0
    MAX_SCENE_DURATION = 20.0       # 단일 씬 최대 20초
    MAX_CTA_DURATION = 12.0         # CTA/outro 씬 최대 12초
    SNAP_TO_SENTENCE_THRESHOLD = 0.8

    async def run(self, **kwargs: Any) -> EditingResult:
        script_data = self.load_previous(Stage.SCRIPT)
        voice_data = self.load_previous(Stage.VOICE)
        storyboard_data = self.load_previous(Stage.STORYBOARD)
        media_data = self.load_previous(Stage.MEDIA)

        script = ScriptResult(**script_data)
        voice = VoiceResult(**voice_data)
        storyboard = StoryboardResult(**storyboard_data)
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

        # ═══ ffmpeg 네이티브 편집 ═══
        _aligned_durations: list[float] = []
        _pi_success_count = 0  # 실제 ffmpeg PI 적용 성공 수
        duration, file_size, subtitle_count = self._compose_video_ffmpeg(
            media, voice, storyboard, script, output_path,
            mixed_audio_path if audio_ducked else None,
            out_aligned_durations=_aligned_durations,
            pi_timeline=pi_timeline,
            out_pi_success_count=_pi_success_count,
        )
        # _compose_video_ffmpeg는 int를 변이할 수 없으므로 리스트를 사용
        # → 아래에서 별도 변수로 받음
        applied_effects.append("renderer:ffmpeg_native")
        applied_effects.append(f"pattern_interrupts_planned:{pi_timeline.event_count}")
        applied_effects.append(f"pattern_interrupts_applied:{self._last_pi_success_count}")

        self.record_cost("system", "editing", units=1, unit_cost=0.0)

        # ═══ 장면 관련도 품질 게이트 로그 (실제 aligned duration 전달) ═══
        self._log_scene_relevance_report(storyboard, media, _aligned_durations)

        # Optional Enhancers
        final_path = await self._apply_enhancers(output_path, applied_effects)

        return EditingResult(
            output_path=str(final_path),
            duration_seconds=duration,
            resolution=[1920, 1080],
            file_size_mb=round(file_size, 2),
            applied_effects=applied_effects,
            pattern_interrupts_count=self._last_pi_success_count,
            subtitle_count=subtitle_count,
            quality_gate_passed=True,
        )

    # ═══════════════════════════════════════════════════
    # ffmpeg 네이티브 합성 (MoviePy 제거)
    # ═══════════════════════════════════════════════════

    _last_pi_success_count: int = 0  # 마지막 렌더링에서 실제 PI 적용 성공 수

    def _compose_video_ffmpeg(
        self,
        media: MediaResult,
        voice: VoiceResult,
        storyboard: StoryboardResult,
        script: ScriptResult,
        output_path: Path,
        mixed_audio_path: Path | None = None,
        out_aligned_durations: list[float] | None = None,
        pi_timeline: Any | None = None,
        out_pi_success_count: int = 0,
    ) -> tuple[float, float, int]:
        """ffmpeg 네이티브로 영상 합성 — 오디오가 타임라인 백본.

        흐름:
        1. 씬별 duration 계산 (싱크 정렬)
        2. 씬별 개별 클립을 ffmpeg로 생성 (이미지→영상, 영상→트림/루프/리사이즈)
        3. ffmpeg concat demuxer로 전체 이어붙이기
        4. 오디오 mux
        5. SRT 자막 번인
        """
        target_size = (1920, 1080)
        w, h = target_size

        # ═══ Step 1: 오디오 기반 target_duration 결정 ═══
        audio_path = mixed_audio_path or (Path(voice.audio_path) if voice.audio_path else None)
        target_duration = voice.total_duration_seconds or 60.0

        if audio_path and audio_path.exists():
            probe_dur = self._ffprobe_duration(audio_path)
            if probe_dur > 0:
                target_duration = probe_dur

        logger.info(f"Target duration from audio: {target_duration:.1f}s")

        # ═══ Step 2: 씬별 duration 계산 ═══
        sorted_scenes = sorted(storyboard.scenes, key=lambda s: s.scene_number)
        aligned_durations = self._compute_segment_aligned_durations(
            sorted_scenes, voice, script, target_duration,
        )

        # 음수/0 duration 방어 (절대 허용 안 함)
        for i in range(len(aligned_durations)):
            if aligned_durations[i] <= 0:
                logger.error(
                    f"Scene {i+1} aligned_duration={aligned_durations[i]:.2f}s → 강제 3.0s"
                )
                aligned_durations[i] = 3.0

        # aligned_durations 합계 검증 + 보정
        aligned_total = sum(aligned_durations)
        if abs(aligned_total - target_duration) > 0.5:
            logger.warning(
                f"Aligned durations total={aligned_total:.1f}s vs "
                f"target={target_duration:.1f}s, correcting..."
            )
            if aligned_total > 0:
                scale = target_duration / aligned_total
                aligned_durations = [
                    max(self.MIN_SCENE_DURATION, round(d * scale, 2))
                    for d in aligned_durations
                ]
                diff = round(target_duration - sum(aligned_durations), 2)
                if abs(diff) > 0.01 and aligned_durations:
                    aligned_durations[-1] = max(
                        self.MIN_SCENE_DURATION,
                        round(aligned_durations[-1] + diff, 2),
                    )

        logger.info(
            f"Aligned durations: total={sum(aligned_durations):.1f}s, "
            f"target={target_duration:.1f}s, scenes={len(aligned_durations)}"
        )

        # 외부로 aligned_durations 전달 (리포트용)
        if out_aligned_durations is not None:
            out_aligned_durations.extend(aligned_durations)

        # ═══ Step 3: 씬별 클립을 ffmpeg로 개별 생성 (캐시 지원) ═══
        tmp_dir = self.stage_dir / "_tmp_clips"
        clip_cache_dir = self.stage_dir / "_clip_cache"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        clip_cache_dir.mkdir(parents=True, exist_ok=True)

        asset_map = {a.scene_number: a for a in media.assets}
        clip_paths: list[Path] = []

        # PI 성공 카운터 초기화
        self._last_pi_success_count = 0

        # PI 이벤트를 씬별로 매핑 (타임라인 위치 → 씬 인덱스)
        scene_pi_events: dict[int, list] = {}
        if pi_timeline and pi_timeline.events:
            cumulative = 0.0
            for i_sc, dur_sc in enumerate(aligned_durations):
                scene_start = cumulative
                scene_end = cumulative + dur_sc
                for evt in pi_timeline.events:
                    if scene_start <= evt.timestamp < scene_end:
                        scene_pi_events.setdefault(i_sc, []).append(evt)
                cumulative = scene_end

        for i, scene in enumerate(sorted_scenes):
            scene_dur = aligned_durations[i] if i < len(aligned_durations) else scene.duration
            if scene_dur <= 0:
                scene_dur = 10.0

            asset = asset_map.get(scene.scene_number)
            clip_path = tmp_dir / f"clip_{i:03d}.mp4"

            # clip 캐시: asset 파일 + duration이 동일하면 재사용
            cache_key = f"clip_{i:03d}_{scene_dur:.1f}s"
            if asset:
                cache_key += f"_{Path(asset.file_path).stem}"
            cached_clip = clip_cache_dir / f"{cache_key}.mp4"
            if cached_clip.exists() and cached_clip.stat().st_size > 0:
                import shutil
                shutil.copy2(str(cached_clip), str(clip_path))
                logger.debug(f"Scene {scene.scene_number}: clip cache hit")
            else:
                self._prepare_scene_clip(asset, scene, scene_dur, target_size, clip_path)
                # 캐시에 저장
                if clip_path.exists():
                    import shutil
                    shutil.copy2(str(clip_path), str(cached_clip))

            # 패턴 인터럽트 적용 (해당 씬에 이벤트가 있으면)
            pi_events = scene_pi_events.get(i, [])
            if pi_events and clip_path.exists():
                enhanced_path = tmp_dir / f"clip_{i:03d}_pi.mp4"
                try:
                    cumulative_start = sum(aligned_durations[:i])
                    self._apply_pattern_interrupt(
                        clip_path, enhanced_path, pi_events,
                        scene_dur, w, h, cumulative_start,
                    )
                    if enhanced_path.exists() and enhanced_path.stat().st_size > 0:
                        clip_path.unlink(missing_ok=True)
                        enhanced_path.rename(clip_path)
                        self._last_pi_success_count += len(pi_events)
                    else:
                        logger.warning(f"PI effect produced empty output for scene {i+1}")
                except Exception as e:
                    logger.warning(f"PI effect failed for scene {i+1}: {e}")
                    enhanced_path.unlink(missing_ok=True)

            clip_paths.append(clip_path)

            logger.info(
                f"Scene {scene.scene_number}: {scene_dur:.1f}s "
                f"(aligned from {scene.duration:.1f}s, {scene.media_type.value})"
                f"{f' +{len(pi_events)}PI' if pi_events else ''}"
            )

        if not clip_paths:
            raise StageError("editing", self.production_id,
                             cause=ValueError("No scene clips generated"))

        # ═══ Step 4: concat demuxer로 전체 이어붙이기 ═══
        concat_path = tmp_dir / "concat.txt"
        with open(concat_path, "w", encoding="utf-8") as f:
            for cp in clip_paths:
                # ffmpeg concat은 forward slash 필요
                f.write(f"file '{str(cp).replace(chr(92), '/')}'\n")

        no_audio_raw = self.stage_dir / "no_audio_raw.mp4"
        self._run_ffmpeg([
            "-f", "concat", "-safe", "0",
            "-i", str(concat_path),
            "-c:v", "copy",
            "-an",
            str(no_audio_raw),
        ], "concat")

        # concat 결과를 target_duration에 맞춤 (프레임 소수점 누적 보정)
        no_audio_path = self.stage_dir / "no_audio.mp4"
        concat_dur = self._ffprobe_duration(no_audio_raw)
        if concat_dur > 0 and concat_dur < target_duration - 0.5:
            pad_dur = target_duration - concat_dur
            if pad_dur > 2.0:
                no_audio_raw.unlink(missing_ok=True)
                raise StageError("editing", self.production_id,
                    cause=ValueError(
                        f"Concat duration gap too large: {concat_dur:.1f}s vs "
                        f"target {target_duration:.1f}s (gap={pad_dur:.1f}s > 2s max). "
                        f"Clip duration calculation is broken."))
            logger.info(
                f"Extending concat {concat_dur:.1f}s (+{pad_dur:.1f}s freeze)"
            )
            self._run_ffmpeg([
                "-i", str(no_audio_raw),
                "-vf", f"tpad=stop_mode=clone:stop_duration={pad_dur:.3f}",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-an",
                str(no_audio_path),
            ], "duration_extend")
            no_audio_raw.unlink(missing_ok=True)
        elif concat_dur > target_duration + 1.0:
            # 영상이 길면 트림
            logger.info(f"Trimming concat {concat_dur:.1f}s → {target_duration:.1f}s")
            self._run_ffmpeg([
                "-i", str(no_audio_raw),
                "-t", f"{target_duration:.3f}",
                "-c:v", "copy",
                "-an",
                str(no_audio_path),
            ], "duration_trim")
            no_audio_raw.unlink(missing_ok=True)
        else:
            import os
            os.replace(str(no_audio_raw), str(no_audio_path))

        # ═══ Step 5: 오디오 mux ═══
        no_sub_path = self.stage_dir / "no_subtitles.mp4"
        if audio_path and audio_path.exists():
            self._run_ffmpeg([
                "-i", str(no_audio_path),
                "-i", str(audio_path),
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                "-map", "0:v:0", "-map", "1:a:0",
                "-shortest",
                str(no_sub_path),
            ], "audio_mux")
        else:
            shutil.move(str(no_audio_path), str(no_sub_path))

        # ═══ Step 6: SRT 자막 번인 ═══
        subtitle_count = 0
        srt_path = Path(voice.srt_path) if voice.srt_path else None
        if srt_path and srt_path.exists():
            try:
                subtitle_count = self._burn_subtitles_ffmpeg(
                    no_sub_path, srt_path, output_path,
                )
                no_sub_path.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"ffmpeg 자막 번인 실패: {e}, 자막 없이 진행")
                shutil.move(str(no_sub_path), str(output_path))
        else:
            shutil.move(str(no_sub_path), str(output_path))

        # 임시 파일 정리
        no_audio_path.unlink(missing_ok=True)
        shutil.rmtree(tmp_dir, ignore_errors=True)

        # ═══ 품질 게이트: 오디오/영상 싱크 ═══
        duration = self._ffprobe_duration(output_path)
        sync_diff = abs(duration - target_duration)
        if sync_diff > 2.0:
            logger.warning(
                f"Audio/video sync gap: {sync_diff:.1f}s "
                f"(video={duration:.1f}s, audio={target_duration:.1f}s)"
            )
        if not self.dry_run:
            if sync_diff > 5.0:
                raise StageError("editing", self.production_id,
                    cause=ValueError(
                        f"Audio/video sync too large: {sync_diff:.1f}s "
                        f"(video={duration:.1f}s, audio={target_duration:.1f}s)"))
            if duration < target_duration * 0.80:
                raise StageError("editing", self.production_id,
                    cause=ValueError(
                        f"Video {duration:.1f}s < 80% of audio {target_duration:.1f}s"))

        file_size_mb = output_path.stat().st_size / (1024 * 1024) if output_path.exists() else 0
        logger.info(f"Final video: {duration:.1f}s, {file_size_mb:.1f}MB, {subtitle_count} subtitles")
        return duration, file_size_mb, subtitle_count

    # ═══════════════════════════════════════════════════
    # 씬별 클립 준비 (ffmpeg)
    # ═══════════════════════════════════════════════════

    def _prepare_scene_clip(
        self,
        asset: MediaAsset | None,
        scene: Any,
        duration: float,
        target_size: tuple[int, int],
        out_path: Path,
    ) -> None:
        """단일 씬을 ffmpeg로 정규화된 클립으로 변환.

        - 이미지 → 지정 duration 영상
        - 비디오 → 리사이즈 + 트림 or 루프
        - 에셋 없음 → placeholder 이미지 → 영상
        """
        w, h = target_size

        if asset:
            asset_path = Path(asset.file_path)
            if asset_path.exists():
                # 파일 확장자로 실제 타입 판별 (dry-run에서 stock이 png일 수 있음)
                is_video_file = asset_path.suffix.lower() in (".mp4", ".webm", ".mov", ".avi", ".mkv")
                if is_video_file and asset.media_type in (MediaType.AI_VIDEO, MediaType.STOCK_VIDEO):
                    self._video_to_clip(asset_path, out_path, duration, w, h)
                    return
                else:
                    self._image_to_clip(asset_path, out_path, duration, w, h)
                    return

        # 에셋 없음 → placeholder 생성
        placeholder_path = out_path.with_suffix(".png")
        self._generate_placeholder(scene, placeholder_path, target_size)
        self._image_to_clip(placeholder_path, out_path, duration, w, h)
        placeholder_path.unlink(missing_ok=True)

    def _image_to_clip(
        self, img_path: Path, out_path: Path, duration: float, w: int, h: int
    ) -> None:
        """이미지를 지정 duration의 영상으로 변환."""
        self._run_ffmpeg([
            "-loop", "1",
            "-i", str(img_path),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-t", f"{duration:.3f}",
            "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black",
            "-pix_fmt", "yuv420p",
            "-r", "30",
            "-an",
            str(out_path),
        ], f"img2clip_{img_path.stem}")

    def _video_to_clip(
        self, video_path: Path, out_path: Path, duration: float, w: int, h: int
    ) -> None:
        """비디오를 리사이즈 + 트림/루프하여 정규화."""
        src_dur = self._ffprobe_duration(video_path)
        if src_dur <= 0:
            src_dur = duration

        if src_dur >= duration:
            # 소스가 더 길면 트림
            self._run_ffmpeg([
                "-i", str(video_path),
                "-t", f"{duration:.3f}",
                "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-r", "30",
                "-an",
                str(out_path),
            ], f"trim_{video_path.stem}")
        else:
            # 소스가 더 짧으면 루프
            loops = int(duration / src_dur) + 1
            self._run_ffmpeg([
                "-stream_loop", str(loops),
                "-i", str(video_path),
                "-t", f"{duration:.3f}",
                "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-r", "30",
                "-an",
                str(out_path),
            ], f"loop_{video_path.stem}")

    def _generate_placeholder(
        self, scene: Any, out_path: Path, target_size: tuple[int, int]
    ) -> None:
        """채널 색상 기반 placeholder 이미지 생성."""
        w, h = target_size
        img = Image.new("RGB", (w, h))
        draw = ImageDraw.Draw(img)

        c1 = self._hex_to_rgb(getattr(self.channel.visual, "primary_color", "#1a2840"))
        c2 = self._hex_to_rgb(getattr(self.channel.visual, "secondary_color", "#2d4a6f"))
        for y in range(h):
            ratio = y / h
            r = int(c1[0] + (c2[0] - c1[0]) * ratio)
            g = int(c1[1] + (c2[1] - c1[1]) * ratio)
            b = int(c1[2] + (c2[2] - c1[2]) * ratio)
            draw.line([(0, y), (w, y)], fill=(r, g, b))

        text = getattr(scene, "narration_text", "")[:60] or getattr(scene, "visual_description", "")[:60]
        if text:
            from ..core.fonts import get_korean_font
            font = get_korean_font(size=44)
            bbox = draw.textbbox((0, 0), text, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.text(((w - tw) // 2, (h - th) // 2), text, fill="white", font=font)

        img.save(str(out_path), "PNG")

    # ═══════════════════════════════════════════════════
    # 자막 번인 (ffmpeg subtitles 필터)
    # ═══════════════════════════════════════════════════

    # ═══ SRT 줄 길이 제한 상수 ═══
    MAX_SUBTITLE_LINE_CHARS = 28   # 한 줄 최대 글자 수
    MAX_SUBTITLE_LINES = 2         # 최대 줄 수

    def _burn_subtitles_ffmpeg(
        self, video_path: Path, srt_path: Path, output_path: Path,
    ) -> int:
        """ffmpeg subtitles 필터로 SRT 자막 번인.

        번인 전에 SRT를 줄 길이 기준으로 wrap하여 화면 가림을 최소화.
        """
        # SRT 줄 wrap 전처리
        wrapped_srt_path = srt_path.with_name("narration_wrapped.srt")
        subtitle_count = self._wrap_srt_lines(srt_path, wrapped_srt_path)

        srt_escaped = str(wrapped_srt_path).replace("\\", "/").replace(":", "\\:")

        # 자막 스타일: 작은 폰트 + 하단 배치 + 얇은 테두리 (화면 가림 최소화)
        style = (
            "FontName=Malgun Gothic,FontSize=18,PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00000000,BackColour=&H60000000,"
            "Outline=1,Shadow=1,MarginV=30,MarginL=80,MarginR=80,"
            "Alignment=2,BorderStyle=3"
        )

        self._run_ffmpeg([
            "-i", str(video_path),
            "-vf", f"subtitles='{srt_escaped}':force_style='{style}'",
            "-c:v", "libx264", "-preset", "medium", "-crf", "23",
            "-c:a", "copy",
            str(output_path),
        ], "subtitles", timeout=300)

        # wrap된 SRT 정리
        wrapped_srt_path.unlink(missing_ok=True)

        logger.info(f"ffmpeg 자막 번인 완료: {subtitle_count}개 자막")
        return subtitle_count

    def _wrap_srt_lines(self, src_path: Path, dst_path: Path) -> int:
        """SRT 파일의 각 자막 텍스트를 MAX_SUBTITLE_LINE_CHARS 기준 2줄 이내로 wrap.

        너무 긴 단일 라인을 자연스러운 지점에서 분할.
        """
        import re

        srt_content = src_path.read_text(encoding="utf-8")
        blocks = re.split(r"\n\n+", srt_content.strip())
        result_blocks: list[str] = []

        for block in blocks:
            lines = block.strip().split("\n")
            if len(lines) < 3:
                result_blocks.append(block.strip())
                continue

            index_line = lines[0]
            time_line = lines[1]
            text = " ".join(lines[2:]).strip()

            # 줄 wrap
            wrapped = self._wrap_subtitle_text(text)
            result_blocks.append(f"{index_line}\n{time_line}\n{wrapped}")

        subtitle_count = len(result_blocks)
        dst_path.write_text("\n\n".join(result_blocks) + "\n", encoding="utf-8")
        return subtitle_count

    def _wrap_subtitle_text(self, text: str) -> str:
        """자막 텍스트를 MAX_SUBTITLE_LINE_CHARS 기준 최대 2줄로 분할."""
        max_chars = self.MAX_SUBTITLE_LINE_CHARS
        max_lines = self.MAX_SUBTITLE_LINES

        if len(text) <= max_chars:
            return text

        # 자연스러운 분할 지점 탐색: 띄어쓰기, 조사/어미 경계
        mid = len(text) // 2
        best_pos = mid

        # 중간점 근처에서 공백 찾기
        for offset in range(min(15, mid)):
            for pos in [mid + offset, mid - offset]:
                if 0 < pos < len(text) and text[pos] == " ":
                    best_pos = pos
                    break
            else:
                continue
            break

        line1 = text[:best_pos].strip()
        line2 = text[best_pos:].strip()

        # 2줄 초과 시 line2를 자르기
        if len(line2) > max_chars * 1.5:
            line2 = line2[:max_chars] + "…"

        lines = [line1, line2]
        return "\n".join(lines[:max_lines])

    # ═══════════════════════════════════════════════════
    # ffmpeg 유틸리티
    # ═══════════════════════════════════════════════════

    @staticmethod
    def _run_ffmpeg(args: list[str], label: str = "", timeout: int = 180) -> None:
        """ffmpeg 명령 실행 + 에러 처리."""
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + args
        logger.debug(f"ffmpeg [{label}]: {' '.join(cmd[:10])}...")

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            err = result.stderr[-500:] if result.stderr else "unknown error"
            logger.error(f"ffmpeg [{label}] 실패: {err}")
            raise RuntimeError(f"ffmpeg [{label}] failed: {err}")

    @staticmethod
    def _ffprobe_duration(path: Path) -> float:
        """ffprobe로 미디어 길이(초) 조회.

        다양한 ffprobe 버전에 호환되도록 stderr 파싱 폴백 포함.
        """
        # 방법 1: -show_entries (최신 ffprobe)
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True, text=True, timeout=10,
            )
            val = result.stdout.strip()
            if val:
                return float(val)
        except Exception:
            pass

        # 방법 2: stderr에서 Duration 파싱 (모든 ffprobe 버전 호환)
        import re
        try:
            result = subprocess.run(
                ["ffprobe", "-i", str(path)],
                capture_output=True, text=True, timeout=10,
            )
            # "Duration: 00:08:50.02" 패턴 매칭
            match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", result.stderr)
            if match:
                h, m, s = float(match.group(1)), float(match.group(2)), float(match.group(3))
                return h * 3600 + m * 60 + s
        except Exception:
            pass

        return 0.0

    @staticmethod
    def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
        """hex 색상 → RGB 튜플."""
        try:
            hex_color = hex_color.lstrip("#")
            return (int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16))
        except (ValueError, IndexError):
            return (20, 40, 80)

    # ═══════════════════════════════════════════════════
    # 싱크 정렬 (기존 유지)
    # ═══════════════════════════════════════════════════

    def _compute_segment_aligned_durations(
        self,
        scenes: list[Any],
        voice: VoiceResult,
        script: ScriptResult,
        target_duration: float,
    ) -> list[float]:
        """씬별 duration을 실제 발화 타이밍에 맞춰 재계산."""
        n_scenes = len(scenes)
        if n_scenes == 0:
            return []

        if voice.section_timings and len(voice.section_timings) == n_scenes:
            durations = self._snap_to_sentence_boundaries(
                voice.section_timings, voice.segments, target_duration
            )
            logger.info(
                f"Sentence-snapped durations: {durations} "
                f"(total={sum(durations):.1f}s, audio={target_duration:.1f}s)"
            )
        else:
            storyboard_durations = [max(s.duration, self.MIN_SCENE_DURATION) for s in scenes]
            sb_total = sum(storyboard_durations)
            if abs(sb_total - target_duration) < 2.0:
                durations = storyboard_durations
            elif sb_total > 0:
                scale = target_duration / sb_total
                durations = [max(round(d * scale, 2), self.MIN_SCENE_DURATION) for d in storyboard_durations]
            else:
                durations = storyboard_durations

        # ═══ Duration capping: 과도한 단일 씬 방지 ═══
        durations = self._cap_scene_durations(durations, scenes, target_duration)

        return durations

    def _cap_scene_durations(
        self,
        durations: list[float],
        scenes: list[Any],
        target_duration: float,
    ) -> list[float]:
        """단일 씬 duration 상한 적용 + 총 길이 보존.

        반환 불변 조건 (모두 보장):
        1. 모든 duration > 0
        2. 모든 duration >= effective_min (가능한 한 MIN_SCENE_DURATION)
        3. 모든 duration <= scene별 max
        4. abs(sum - target_duration) < 0.2

        수학적 불가능 케이스 (n * min > target)는 min을 동적으로 축소.
        """
        n = len(durations)
        if n == 0:
            return durations

        d = list(durations)

        # ── scene별 max 계산 ──
        maxes: list[float] = []
        for i in range(n):
            scene = scenes[i] if i < len(scenes) else None
            intent = getattr(scene, "visual_intent", None)
            if hasattr(intent, "value"):
                intent = intent.value
            maxes.append(
                self.MAX_CTA_DURATION if intent == "closing_cta"
                else self.MAX_SCENE_DURATION
            )

        # ── effective min 결정 ──
        desired_min = self.MIN_SCENE_DURATION
        sum_of_maxes = sum(maxes)

        if n * desired_min > target_duration:
            # 수학적 불가능: min을 축소
            effective_min = max(0.5, target_duration / n * 0.4)
            logger.warning(
                f"Duration capping: {n} scenes × min {desired_min}s = "
                f"{n * desired_min:.1f}s > target {target_duration:.1f}s. "
                f"Reducing min to {effective_min:.2f}s"
            )
        else:
            effective_min = desired_min

        if sum_of_maxes < target_duration:
            raise StageError("editing", self.production_id,
                cause=ValueError(
                    f"Impossible: sum of max durations ({sum_of_maxes:.1f}s) "
                    f"< target ({target_duration:.1f}s)"))

        # ── 반복 수렴: clamp → 비례 재분배 ──
        for _pass in range(10):
            # clamp to [effective_min, max]
            for i in range(n):
                d[i] = max(effective_min, min(maxes[i], d[i]))

            total = sum(d)
            err = target_duration - total

            if abs(err) < 0.05:
                break

            # 비례 재분배: err을 조정 가능한 씬들에 분배
            if err > 0:
                # 늘려야 함 → max까지 여유 있는 씬들
                adjustable = [(i, maxes[i] - d[i]) for i in range(n) if d[i] < maxes[i] - 0.01]
            else:
                # 줄여야 함 → min까지 여유 있는 씬들
                adjustable = [(i, d[i] - effective_min) for i in range(n) if d[i] > effective_min + 0.01]

            if not adjustable:
                break

            total_room = sum(room for _, room in adjustable)
            if total_room < 0.01:
                break

            for idx, room in adjustable:
                # 여유(room)에 비례하여 분배
                share = (room / total_room) * err
                d[idx] = round(d[idx] + share, 3)

        # ── 최종 clamp (부동소수점 안전) ──
        for i in range(n):
            d[i] = max(effective_min, min(maxes[i], d[i]))

        # ── 잔여 오차: 여러 씬에 1/100초 단위로 분산 ──
        residual = round(target_duration - sum(d), 3)
        if abs(residual) > 0.01:
            if residual > 0:
                candidates = sorted(range(n), key=lambda i: maxes[i] - d[i], reverse=True)
            else:
                candidates = sorted(range(n), key=lambda i: d[i] - effective_min, reverse=True)

            step = 0.01 if residual > 0 else -0.01
            idx = 0
            while abs(residual) > 0.005 and idx < len(candidates) * 50:
                i = candidates[idx % len(candidates)]
                new_val = d[i] + step
                if effective_min <= new_val <= maxes[i]:
                    d[i] = round(new_val, 3)
                    residual = round(residual - step, 3)
                idx += 1

        # ── 최종 반올림 + 불변 조건 검증 ──
        d = [round(v, 2) for v in d]

        violations = []
        for i in range(n):
            if d[i] <= 0:
                violations.append(f"scene {i+1}: duration={d[i]}<=0")
            if d[i] < effective_min - 0.01:
                violations.append(f"scene {i+1}: duration={d[i]}<min({effective_min})")
            if d[i] > maxes[i] + 0.01:
                violations.append(f"scene {i+1}: duration={d[i]}>max({maxes[i]})")
        sum_err = abs(sum(d) - target_duration)
        if sum_err >= 0.2:
            violations.append(f"sum={sum(d):.3f} vs target={target_duration:.3f} (diff={sum_err:.3f})")

        if violations:
            raise StageError("editing", self.production_id,
                cause=ValueError(
                    f"_cap_scene_durations INVARIANT VIOLATED: {violations}"))

        return d

    def _snap_to_sentence_boundaries(
        self, section_timings: list[Any], segments: list[Any], target_duration: float,
    ) -> list[float]:
        """섹션 경계를 문장 끝 지점으로 스냅."""
        n = len(section_timings)
        if n == 0:
            return []

        if not segments:
            durations = [max(st.duration, self.MIN_SCENE_DURATION) for st in section_timings]
            total = sum(durations)
            if total > 0 and abs(total - target_duration) > 0.5:
                scale = target_duration / total
                durations = [round(d * scale, 2) for d in durations]
            return [round(d, 2) for d in durations]

        seg_ends = sorted(set(seg.end for seg in segments if seg.end > 0))

        snapped_boundaries: list[float] = [0.0]
        for i in range(n - 1):
            raw_end = section_timings[i].end
            best = self._find_nearest_boundary(raw_end, seg_ends)
            prev = snapped_boundaries[-1]
            if best <= prev + self.MIN_SCENE_DURATION:
                best = prev + max(section_timings[i].duration, self.MIN_SCENE_DURATION)
            snapped_boundaries.append(round(best, 3))
        snapped_boundaries.append(round(target_duration, 3))

        durations: list[float] = []
        for i in range(n):
            d = snapped_boundaries[i + 1] - snapped_boundaries[i]
            d = max(d, self.MIN_SCENE_DURATION)
            d = round(d + self.SCENE_TRANSITION_MARGIN, 2)
            durations.append(d)

        total = sum(durations)
        if total > 0 and abs(total - target_duration) > 0.1:
            scale = target_duration / total
            durations = [round(d * scale, 2) for d in durations]

        diff = round(target_duration - sum(durations), 2)
        if abs(diff) > 0.01 and durations:
            durations[-1] = round(durations[-1] + diff, 2)

        # 최종 안전 검증: 음수/0 duration 절대 방지
        for i in range(len(durations)):
            if durations[i] <= 0:
                logger.error(
                    f"_snap_to_sentence_boundaries: scene {i+1} "
                    f"duration={durations[i]:.2f}s → 강제 {self.MIN_SCENE_DURATION}s"
                )
                durations[i] = self.MIN_SCENE_DURATION

        return durations

    def _find_nearest_boundary(self, target: float, seg_ends: list[float]) -> float:
        if not seg_ends:
            return target
        best = target
        best_dist = float("inf")
        for end in seg_ends:
            dist = abs(end - target)
            if dist < best_dist:
                best_dist = dist
                best = end
        if best_dist <= self.SNAP_TO_SENTENCE_THRESHOLD:
            return best
        return target

    # ═══════════════════════════════════════════════════
    # 장면 관련도 품질 게이트
    # ═══════════════════════════════════════════════════

    def _log_scene_relevance_report(
        self,
        storyboard: StoryboardResult,
        media: MediaResult,
        aligned_durations: list[float] | None = None,
    ) -> None:
        """각 씬의 narration-visual 매핑을 로그 + JSON으로 남겨 사람이 검수 가능.

        개선점:
        - aligned_durations(실제 편집에 사용된 duration) 사용
        - mock vs real 자산 구분 명시
        - 음수/0 duration 검증 + 경고
        - generic query 비율 계산
        """
        import json

        asset_map = {a.scene_number: a for a in media.assets}
        sorted_scenes = sorted(storyboard.scenes, key=lambda s: s.scene_number)
        report_rows: list[dict[str, Any]] = []
        validation_warnings: list[str] = []

        # mock provider 판별 패턴
        _MOCK_PROVIDERS = {"mock", "mock_pexels", "placeholder", "fallback", "pillow"}

        # generic query 패턴 (너무 일반적인 쿼리 감지)
        _GENERIC_PATTERNS = [
            "cinematic", "professional modern", "dramatic opening",
            "urban lifestyle", "concept illustration",
        ]

        for i, scene in enumerate(sorted_scenes):
            intent = getattr(scene, "visual_intent", "unknown")
            if hasattr(intent, "value"):
                intent = intent.value
            asset = asset_map.get(scene.scene_number)

            # 실제 편집에 사용된 duration (aligned) 또는 S4 원본
            actual_duration = scene.duration
            if aligned_durations and i < len(aligned_durations):
                actual_duration = aligned_durations[i]

            # duration 검증
            if actual_duration <= 0:
                validation_warnings.append(
                    f"Scene {scene.scene_number}: duration={actual_duration}s (<=0)"
                )
                actual_duration = max(actual_duration, 3.0)  # 리포트에서 최소 보정

            # mock vs real 판별
            provider = asset.provider if asset else "missing"
            is_mock = provider.lower() in _MOCK_PROVIDERS or not asset
            asset_status = "mock" if is_mock else "real"

            # generic query 판별
            stock_q = scene.stock_search_query or ""
            is_generic_query = any(p in stock_q.lower() for p in _GENERIC_PATTERNS)

            row = {
                "scene": scene.scene_number,
                "duration": round(actual_duration, 2),
                "duration_source": "aligned" if aligned_durations else "storyboard",
                "visual_intent": intent,
                "narration_text": scene.narration_text[:120],
                "visual_description": scene.visual_description[:100],
                "stock_search_query": stock_q,
                "query_quality": "generic" if is_generic_query else "specific",
                "visual_keywords": scene.visual_keywords[:5],
                "media_type": scene.media_type.value,
                "media_provider": provider,
                "asset_status": asset_status,
                "media_file": Path(asset.file_path).name if asset else "none",
            }
            report_rows.append(row)

            logger.info(
                f"[RELEVANCE] Scene {scene.scene_number}: "
                f"intent={intent}, dur={actual_duration:.1f}s, "
                f"asset={asset_status}, query_q={'generic' if is_generic_query else 'specific'}, "
                f"narration=\"{scene.narration_text[:50]}...\""
            )

        # 품질 통계
        from collections import Counter
        intent_dist = Counter(r["visual_intent"] for r in report_rows)
        total = len(report_rows)
        mock_count = sum(1 for r in report_rows if r["asset_status"] == "mock")
        generic_count = sum(1 for r in report_rows if r["query_quality"] == "generic")

        quality_summary = {
            "total_scenes": total,
            "mock_asset_ratio": f"{mock_count}/{total}",
            "generic_query_ratio": f"{generic_count}/{total}",
            "duration_range": f"{min(r['duration'] for r in report_rows):.1f}s ~ {max(r['duration'] for r in report_rows):.1f}s",
            "avg_duration": f"{sum(r['duration'] for r in report_rows) / total:.1f}s",
        }

        logger.info(
            f"[RELEVANCE SUMMARY] {total} scenes, "
            f"mock={mock_count}/{total}, generic_q={generic_count}/{total}, "
            f"intents: {dict(intent_dist.most_common())}"
        )

        if validation_warnings:
            for w in validation_warnings:
                logger.warning(f"[RELEVANCE VALIDATION] {w}")

        # JSON 리포트 저장
        report = {
            "total_scenes": total,
            "quality_summary": quality_summary,
            "intent_distribution": dict(intent_dist),
            "validation_warnings": validation_warnings,
            "scenes": report_rows,
        }
        report_path = self.stage_dir / "scene_relevance_report.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"Scene relevance report: {report_path}")

    # ═══════════════════════════════════════════════════
    # 패턴 인터럽트 ffmpeg 적용
    # ═══════════════════════════════════════════════════

    def _apply_pattern_interrupt(
        self,
        clip_path: Path,
        out_path: Path,
        events: list,
        scene_dur: float,
        w: int, h: int,
        scene_start: float,
    ) -> None:
        """씬 클립에 패턴 인터럽트 효과 적용 (ffmpeg filter_complex).

        MVP 구현:
        - ZOOM: 1.0→1.15 줌인 후 복귀 (0.8초)
        - SUBTITLE_EMPHASIS: 화면 하단에 강조 플래시 (0.5초 밝기 부스트)
        - SCENE_CHANGE: 0.3초 페이드인 (이미 씬 전환이므로 가벼운 효과)
        - SFX_HIT: 0.2초 밝기 펄스 (효과음은 별도 오디오 믹싱 필요)
        """
        from ..retention.pattern_interrupt import InterruptType

        filters: list[str] = []
        for evt in events:
            local_t = evt.timestamp - scene_start
            if local_t < 0 or local_t >= scene_dur:
                continue

            t_s = round(local_t, 2)

            if evt.interrupt_type == InterruptType.ZOOM:
                # crop+scale 줌인: 중앙 90% 크롭 후 원본 크기로 확대 (0.6초)
                t_e = round(min(local_t + 0.6, scene_dur), 2)
                cw, ch = int(w * 0.9), int(h * 0.9)
                cx, cy = (w - cw) // 2, (h - ch) // 2
                filters.append(
                    f"crop=if(between(t\\,{t_s}\\,{t_e})\\,{cw}\\,{w})"
                    f":if(between(t\\,{t_s}\\,{t_e})\\,{ch}\\,{h})"
                    f":if(between(t\\,{t_s}\\,{t_e})\\,{cx}\\,0)"
                    f":if(between(t\\,{t_s}\\,{t_e})\\,{cy}\\,0)"
                    f",scale={w}:{h}"
                )

            elif evt.interrupt_type == InterruptType.SCENE_CHANGE:
                filters.append(f"fade=in:st={t_s}:d=0.3")

            elif evt.interrupt_type == InterruptType.SUBTITLE_EMPHASIS:
                t_e = round(min(local_t + 0.5, scene_dur), 2)
                filters.append(
                    f"eq=brightness=0.08:enable='between(t,{t_s},{t_e})'"
                )

            elif evt.interrupt_type == InterruptType.SFX_HIT:
                t_e = round(min(local_t + 0.2, scene_dur), 2)
                filters.append(
                    f"eq=brightness=0.12:enable='between(t,{t_s},{t_e})'"
                )

        if not filters:
            import shutil
            shutil.copy2(str(clip_path), str(out_path))
            return

        vf = ",".join(filters)
        self._run_ffmpeg([
            "-i", str(clip_path),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-an",
            str(out_path),
        ], "pi_effect", timeout=60)

    # ═══════════════════════════════════════════════════
    # Optional Enhancers (기존 유지)
    # ═══════════════════════════════════════════════════

    async def _apply_enhancers(
        self, output: Path, applied_effects: list[str]
    ) -> Path:
        """Topaz/CapCut 후처리 — 실패해도 원본 보존."""
        result_path = output
        if self.dry_run:
            return result_path

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
