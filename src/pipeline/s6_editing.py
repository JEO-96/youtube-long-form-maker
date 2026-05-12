"""S6 편집 - ffmpeg 네이티브 합성 + Retention 엔진."""

from __future__ import annotations

import logging
import os
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
    MIN_ALIGNMENT_MATCH_RATE = 0.5
    MAX_ACCEPTED_ALIGNMENT_DRIFT = 6.0

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

        # alignment 검증 — pre-cap drift 기준 (cap은 duration만 조정하므로 pre-cap이 정확)
        max_drift = 0.0
        align_warnings: list[str] = []
        if self._last_alignment_info:
            for info in self._last_alignment_info:
                if info['confidence'] >= 0.25:
                    drift = abs(info['drift'])
                    max_drift = max(max_drift, drift)
                    if drift > 3.0:
                        align_warnings.append(
                            f"Scene {info['scene']}: drift {info['drift']:.1f}s"
                        )

            # alignment hard gate
            match_count = sum(1 for d in self._last_alignment_info if d['confidence'] >= 0.25)
            total_scenes = len(self._last_alignment_info)
            match_rate = match_count / total_scenes if total_scenes else 0
            if not self.dry_run:
                if max_drift > 10.0:
                    raise StageError("editing", self.production_id,
                        cause=ValueError(
                            f"Alignment drift too large: max {max_drift:.1f}s > 10s"))
                if match_rate < 0.5 and total_scenes > 5:
                    raise StageError("editing", self.production_id,
                        cause=ValueError(
                            f"Alignment match rate too low: {match_count}/{total_scenes} "
                            f"({match_rate:.0%} < 50%)"))

        # 쇼츠 모드: 9:16 해상도 기록
        is_shorts = getattr(self.channel.content, "is_shorts", False) if hasattr(self.channel, "content") else False
        resolution = [1080, 1920] if is_shorts else [1920, 1080]

        return EditingResult(
            output_path=str(final_path),
            duration_seconds=duration,
            resolution=resolution,
            file_size_mb=round(file_size, 2),
            applied_effects=applied_effects,
            pattern_interrupts_count=self._last_pi_success_count,
            subtitle_count=subtitle_count,
            quality_gate_passed=True,
            alignment_max_drift=round(max_drift, 1),
            alignment_warnings=align_warnings,
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
        # 쇼츠 모드: 9:16 세로 해상도
        is_shorts = getattr(self.channel.content, "is_shorts", False) if hasattr(self.channel, "content") else False
        target_size = (1080, 1920) if is_shorts else (1920, 1080)
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
            applied_scene_pi_count = 0
            if pi_events and clip_path.exists():
                enhanced_path = tmp_dir / f"clip_{i:03d}_pi.mp4"
                try:
                    cumulative_start = sum(aligned_durations[:i])
                    applied_pi_count = self._apply_pattern_interrupt(
                        clip_path, enhanced_path, pi_events,
                        scene_dur, w, h, cumulative_start,
                    )
                    if enhanced_path.exists() and enhanced_path.stat().st_size > 0:
                        # PI 적용 후 blackdetect hard gate (검증 실패 시에도 원본 유지)
                        blacks = self._detect_black_frames(enhanced_path, min_duration=1.0, fail_safe=True)
                        if blacks:
                            logger.warning(
                                f"PI blackdetect: scene {i+1} has {len(blacks)} "
                                f"black segment(s) after PI — reverting to original")
                            enhanced_path.unlink(missing_ok=True)
                        else:
                            clip_path.unlink(missing_ok=True)
                            enhanced_path.rename(clip_path)
                            self._last_pi_success_count += applied_pi_count
                            applied_scene_pi_count = applied_pi_count
                    else:
                        logger.warning(f"PI effect produced empty output for scene {i+1}")
                except Exception as e:
                    logger.warning(f"PI effect failed for scene {i+1}: {e}")
                    enhanced_path.unlink(missing_ok=True)

            clip_paths.append(clip_path)

            logger.info(
                f"Scene {scene.scene_number}: {scene_dur:.1f}s "
                f"(aligned from {scene.duration:.1f}s, {scene.media_type.value})"
                f"{f' +{applied_scene_pi_count}PI' if applied_scene_pi_count else ''}"
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

        # ═══ Step 6: SRT 품질 게이트 + 자막 번인 ═══
        subtitle_count = 0
        srt_path = Path(voice.srt_path) if voice.srt_path else None
        if srt_path and srt_path.exists():
            # SRT-오디오 duration 불일치 검증
            srt_end, srt_last_text = self._parse_srt_last_cue(srt_path)
            if srt_end > 0 and not self.dry_run:
                srt_gap = abs(srt_end - target_duration)
                if srt_gap > 5.0:
                    raise StageError("editing", self.production_id,
                        cause=ValueError(
                            f"SRT-audio duration mismatch: SRT ends at {srt_end:.1f}s, "
                            f"audio={target_duration:.1f}s (gap={srt_gap:.1f}s > 5s)"))

            # SRT 마지막 cue 문장 절단 검증
            if srt_last_text and not self.dry_run:
                sentence_endings = ('.', '!', '?', '~', '…', '"', "'", ')', '」')
                if not srt_last_text.rstrip().endswith(sentence_endings):
                    if len(srt_last_text) < 5:
                        logger.warning(
                            f"SRT last cue truncated (too short): '{srt_last_text}' "
                            f"— STT 마지막 분절 이슈, 영상 품질에 큰 영향 없음")
                    else:
                        logger.warning(
                            f"SRT last cue may be truncated (no sentence ending): "
                            f"'{srt_last_text[-30:]}'")

            try:
                subtitle_count = self._burn_subtitles_ffmpeg(
                    no_sub_path, srt_path, output_path,
                )
                no_sub_path.unlink(missing_ok=True)
            except Exception as e:
                if not self.dry_run:
                    raise StageError("editing", self.production_id,
                        cause=RuntimeError(f"자막 burn-in 실패: {e}")) from e
                logger.warning(f"ffmpeg 자막 번인 실패 (dry_run): {e}, 자막 없이 진행")
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

        # ═══ Blackdetect soft warning ═══
        if not self.dry_run and output_path.exists():
            self._warn_black_frames(output_path)

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
        - 에셋 없음 → dry-run에서만 placeholder 이미지 → 영상
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

        if not self.dry_run:
            raise StageError(
                "editing",
                self.production_id,
                cause=ValueError(
                    f"Scene {getattr(scene, 'scene_number', '?')}: media asset missing. "
                    "저품질 placeholder/도형 배경을 넣지 않고 렌더를 중단합니다."
                ),
            )

        # dry-run 에셋 없음 → placeholder 생성
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

        # 쇼츠 모드: 세로 화면은 자막 더 크게, 하단 마진 더 넉넉히
        is_shorts = getattr(self.channel.content, "is_shorts", False) if hasattr(self.channel, "content") else False
        if is_shorts:
            style = (
                "FontName=Malgun Gothic,FontSize=28,PrimaryColour=&H00FFFFFF,"
                "OutlineColour=&H00000000,BackColour=&HA0000000,"
                "Outline=2,Shadow=1,MarginV=180,MarginL=60,MarginR=60,"
                "Alignment=2,BorderStyle=3,Bold=1"
            )
        else:
            # 자막 스타일: 작은 폰트 + 하단 배치 + 얇은 테두리 (화면 가림 최소화)
            style = (
                "FontName=Malgun Gothic,FontSize=18,PrimaryColour=&H00FFFFFF,"
                "OutlineColour=&H00000000,BackColour=&H60000000,"
                "Outline=1,Shadow=1,MarginV=30,MarginL=80,MarginR=80,"
                "Alignment=2,BorderStyle=3"
            )

        # temp 파일에 먼저 쓰고, 검증 후 교체 (moov atom 보호)
        tmp_output = output_path.with_suffix(".tmp.mp4")
        try:
            self._run_ffmpeg([
                "-i", str(video_path),
                "-vf", f"subtitles='{srt_escaped}':force_style='{style}'",
                "-c:v", "libx264", "-preset", "medium", "-crf", "23",
                "-c:a", "copy",
                str(tmp_output),
            ], "subtitles", timeout=300)

            # ffprobe로 moov atom / duration 검증
            tmp_dur = self._ffprobe_duration(tmp_output)
            if tmp_dur <= 0:
                raise RuntimeError(
                    f"Subtitle burn-in produced invalid file (duration={tmp_dur})")

            os.replace(str(tmp_output), str(output_path))
        except Exception:
            tmp_output.unlink(missing_ok=True)
            raise
        finally:
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
    # 씬-나레이션 싱크 정렬 (voice segment fuzzy matching)
    # ═══════════════════════════════════════════════════

    _last_alignment_info: list[dict] = []  # 마지막 alignment drift 정보

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

        # 1순위: voice segments fuzzy alignment (핵심 경로)
        if voice.segments and len(voice.segments) >= 3:
            durations, drift_info = self._align_scenes_to_voice(
                scenes, voice, target_duration,
            )
            match_count = sum(1 for d in drift_info if d.get('confidence', 0.0) >= 0.25)
            match_rate = match_count / n_scenes if n_scenes else 0.0
            max_drift = max((abs(d.get('drift', 0.0)) for d in drift_info), default=0.0)

            if (
                durations
                and match_rate >= self.MIN_ALIGNMENT_MATCH_RATE
                and max_drift <= self.MAX_ACCEPTED_ALIGNMENT_DRIFT
            ):
                self._last_alignment_info = drift_info
                logger.info(
                    f"Voice-aligned: {len(durations)} scenes, "
                    f"total={sum(durations):.1f}s, target={target_duration:.1f}s"
                )
                durations = self._cap_scene_durations(durations, scenes, target_duration)
                return durations
            if durations:
                logger.warning(
                    f"Voice alignment rejected: match_rate={match_rate:.0%} "
                    f"({match_count}/{n_scenes}), max_drift={max_drift:.1f}s. "
                    "Falling back to storyboard timing."
                )

        # 2순위: section_timings 매칭 (유지)
        self._last_alignment_info = []
        if voice.section_timings and len(voice.section_timings) == n_scenes:
            durations = self._snap_to_sentence_boundaries(
                voice.section_timings, voice.segments, target_duration
            )
        else:
            # 3순위: proportional fallback
            storyboard_durations = [max(s.duration, self.MIN_SCENE_DURATION) for s in scenes]
            sb_total = sum(storyboard_durations)
            if abs(sb_total - target_duration) < 2.0:
                durations = storyboard_durations
            elif sb_total > 0:
                scale = target_duration / sb_total
                durations = [max(round(d * scale, 2), self.MIN_SCENE_DURATION) for d in storyboard_durations]
            else:
                durations = storyboard_durations

        durations = self._cap_scene_durations(durations, scenes, target_duration)
        return durations

    # ── fuzzy alignment 구현 ──

    @staticmethod
    def _normalize_for_match(text: str) -> str:
        """텍스트 정규화 — 공백/구두점 통일."""
        normalized, _ = S6Editing._normalize_with_map(text)
        return normalized

    @staticmethod
    def _normalize_with_map(text: str) -> tuple[str, list[int]]:
        """정규화된 문자열과 원문 char offset 매핑을 함께 만든다.

        씬 매칭은 정규화 텍스트에서 수행하지만, 시간 변환은 원문 transcript
        offset이 필요하다. 둘을 섞으면 장면 시작점이 밀리므로 매핑을 보존한다.
        """
        punct = set(',.!?;:·…"“”\'"()（）[]{}')
        chars: list[str] = []
        index_map: list[int] = []
        last_was_space = True

        for idx, ch in enumerate(text):
            if ch.isspace():
                if chars and not last_was_space:
                    chars.append(" ")
                    index_map.append(idx)
                    last_was_space = True
                continue
            if ch in punct:
                continue
            chars.append(ch.lower())
            index_map.append(idx)
            last_was_space = False

        while chars and chars[-1] == " ":
            chars.pop()
            index_map.pop()

        return "".join(chars), index_map

    def _align_scenes_to_voice(
        self,
        scenes: list[Any],
        voice: VoiceResult,
        target_duration: float,
    ) -> tuple[list[float], list[dict]]:
        """각 scene.narration_text를 voice segments에 fuzzy match하여 정확한 타이밍 결정.

        Returns: (durations, drift_info)
        """
        from difflib import SequenceMatcher

        segments = voice.segments
        if not segments:
            return [], []

        # ── 전체 transcript 구축 + char offset → time 매핑 ──
        transcript_parts: list[str] = []
        # 각 segment의 시작 char offset 기록
        seg_char_starts: list[int] = []
        cursor = 0
        for seg in segments:
            seg_char_starts.append(cursor)
            text = seg.text.strip()
            transcript_parts.append(text)
            cursor += len(text) + 1  # +1 for space
        full_transcript = " ".join(transcript_parts)
        norm_transcript, norm_to_orig = S6Editing._normalize_with_map(full_transcript)

        def _char_to_time(char_pos: int) -> float:
            """transcript char position → audio time (초)."""
            # 해당 char가 어느 segment에 속하는지 찾기
            for i in range(len(seg_char_starts) - 1, -1, -1):
                if char_pos >= seg_char_starts[i]:
                    seg = segments[i]
                    seg_text_len = len(seg.text.strip())
                    if seg_text_len == 0:
                        return seg.start
                    local_pos = char_pos - seg_char_starts[i]
                    ratio = min(local_pos / seg_text_len, 1.0)
                    return seg.start + ratio * (seg.end - seg.start)
            return 0.0

        def _norm_char_to_time(norm_pos: int, *, is_end: bool = False) -> float:
            """정규화 transcript 위치를 실제 transcript 시간으로 변환."""
            if not norm_to_orig:
                return 0.0
            if is_end:
                map_idx = min(max(norm_pos - 1, 0), len(norm_to_orig) - 1)
                orig_pos = norm_to_orig[map_idx] + 1
            else:
                map_idx = min(max(norm_pos, 0), len(norm_to_orig) - 1)
                orig_pos = norm_to_orig[map_idx]
            return _char_to_time(orig_pos)

        # ── 각 scene을 순차 fuzzy match ──
        matched_spans: list[tuple[float, float, float]] = []  # (audio_start, audio_end, confidence)
        search_from = 0  # 순차 제약

        for scene in scenes:
            narr = getattr(scene, 'narration_text', '') or ''
            norm_narr = S6Editing._normalize_for_match(narr)

            if len(norm_narr) < 5:
                # 너무 짧은 텍스트 → 매치 불가
                matched_spans.append((-1, -1, 0.0))
                continue

            # sliding window: narr 길이의 0.7~1.3배 범위에서 탐색
            best_ratio = 0.0
            best_start = search_from
            best_end = min(search_from + len(norm_narr), len(norm_transcript))
            window_min = max(int(len(norm_narr) * 0.5), 10)
            window_max = min(int(len(norm_narr) * 1.5), len(norm_transcript) - search_from)

            # 성능을 위해 탐색 범위 제한 (norm_narr 길이의 3배까지만)
            search_end = min(search_from + len(norm_narr) * 4, len(norm_transcript))

            for win_size in [len(norm_narr), window_min, window_max]:
                if win_size <= 0 or win_size > search_end - search_from:
                    continue
                for pos in range(search_from, search_end - win_size + 1, max(1, win_size // 10)):
                    candidate = norm_transcript[pos:pos + win_size]
                    sm = SequenceMatcher(None, norm_narr[:200], candidate[:200], autojunk=False)
                    ratio = sm.ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_start = pos
                        best_end = pos + win_size

            if best_ratio >= 0.25:
                audio_start = _norm_char_to_time(best_start)
                audio_end = _norm_char_to_time(best_end, is_end=True)
                matched_spans.append((audio_start, audio_end, best_ratio))
                search_from = best_end  # 순차 제약 유지
            else:
                matched_spans.append((-1, -1, best_ratio))

        # ── 매치 실패 씬 보간을 위해 다음 매치 성공 인덱스 사전 계산 ──
        next_match: list[int] = [len(matched_spans)] * len(matched_spans)
        for i in range(len(matched_spans) - 1, -1, -1):
            if matched_spans[i][2] >= 0.25:
                next_match[i] = i
            elif i + 1 < len(matched_spans):
                next_match[i] = next_match[i + 1]

        # ── 매치 결과 → duration 변환 (cursor_time 스냅 + 보간) ──
        durations: list[float] = []
        drift_info: list[dict] = []
        cursor_time = 0.0
        last_matched_end = 0.0  # 마지막 매치 성공 씬의 audio_end

        for i, (audio_s, audio_e, conf) in enumerate(matched_spans):
            scene = scenes[i]
            scene_num = getattr(scene, 'scene_number', i + 1)

            if audio_s >= 0 and audio_e > audio_s and conf >= 0.25:
                drift = round(cursor_time - audio_s, 2)

                if audio_s > cursor_time + 0.05 and durations:
                    # 현재 씬 앞에 남은 음성 구간은 이전 씬이 더 자연스럽게 담당한다.
                    gap = round(audio_s - cursor_time, 2)
                    durations[-1] = round(durations[-1] + gap, 2)
                    prev = drift_info[-1]
                    prev['duration'] = round(prev.get('duration', 0.0) + gap, 2)
                    cursor_time = audio_s

                scene_start = cursor_time if durations else min(cursor_time, audio_s)
                if audio_s > scene_start + 0.05 and not durations:
                    # 앞쪽 무음/호흡 구간은 첫 씬에 포함한다.
                    scene_start = cursor_time

                dur = round(audio_e - scene_start, 2)
                dur = max(self.MIN_SCENE_DURATION, dur)
                # cursor를 실제 음성 끝으로 스냅 → drift 누적 차단
                cursor_time = round(scene_start + dur, 3)
                last_matched_end = audio_e
            else:
                # 매치 실패 → 인접 매치 기반 보간 시도
                nm_idx = next_match[i] if i + 1 < len(next_match) else len(matched_spans)
                nm_idx = next_match[min(i + 1, len(next_match) - 1)]

                if nm_idx < len(matched_spans):
                    # 다음 매치 성공 씬까지의 간격을 실패 씬들이 나눠 가짐
                    next_audio_s = matched_spans[nm_idx][0]
                    gap = max(next_audio_s - cursor_time, 0)
                    # 이 gap 안에 있는 미매치 씬 수
                    unmatched_count = sum(
                        1 for j in range(i, nm_idx)
                        if matched_spans[j][2] < 0.25
                    )
                    dur = round(gap / max(unmatched_count, 1), 2)
                    dur = max(self.MIN_SCENE_DURATION, dur)
                else:
                    # 마지막까지 매치 실패 → 나머지 시간 균등 분배
                    remaining = max(target_duration - cursor_time, 0)
                    unmatched_count = sum(
                        1 for j in range(i, len(matched_spans))
                        if matched_spans[j][2] < 0.25
                    )
                    dur = round(remaining / max(unmatched_count, 1), 2)
                    dur = max(self.MIN_SCENE_DURATION, dur)

                drift = 0.0
                audio_s = cursor_time
                cursor_time += dur

            durations.append(dur)
            drift_info.append({
                'scene': scene_num,
                'matched_start': round(audio_s, 1),
                'assigned_start': round(cursor_time - dur, 1),
                'drift': drift,
                'confidence': round(conf, 2),
                'duration': round(dur, 2),
            })

        # ── 합계 보정 ──
        total = sum(durations)
        if total > 0 and abs(total - target_duration) > 0.1:
            scale = target_duration / total
            abs_min = max(1.0, target_duration / len(durations) * 0.3)
            durations = [max(abs_min, round(d * scale, 2)) for d in durations]
            diff = round(target_duration - sum(durations), 2)
            if abs(diff) > 0.01 and durations:
                longest = max(range(len(durations)), key=lambda i: durations[i])
                durations[longest] = round(durations[longest] + diff, 2)

        # ── drift 경고 ──
        max_drift = max((abs(d['drift']) for d in drift_info), default=0)
        if max_drift > 3.0:
            warn_scenes = [d for d in drift_info if abs(d['drift']) > 3.0]
            logger.warning(
                f"Alignment drift >3s: {len(warn_scenes)} scenes, "
                f"max={max_drift:.1f}s"
            )

        logger.info(
            f"Fuzzy alignment: {len(durations)} scenes, "
            f"matched={sum(1 for s in matched_spans if s[2] >= 0.25)}/{len(scenes)}, "
            f"max_drift={max_drift:.1f}s"
        )

        return durations, drift_info

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
    ) -> int:
        """씬 클립에 패턴 인터럽트 효과 적용 (ffmpeg filter_complex).

        화면 명암이 튀지 않도록 밝기 펄스 계열은 적용하지 않고,
        필요한 경우에만 짧은 중앙 줌을 적용한다.
        """
        from ..retention.pattern_interrupt import InterruptType

        filters: list[str] = []
        applied_count = 0
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
                applied_count += 1

        if not filters:
            import shutil
            shutil.copy2(str(clip_path), str(out_path))
            return 0

        vf = ",".join(filters)
        self._run_ffmpeg([
            "-i", str(clip_path),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-an",
            str(out_path),
        ], "pi_effect", timeout=60)
        return applied_count

    # ═══════════════════════════════════════════════════
    # Optional Enhancers (기존 유지)
    # ═══════════════════════════════════════════════════

    def _parse_srt_last_cue(self, srt_path: Path) -> tuple[float, str]:
        """SRT 파일의 마지막 cue end_time(초)과 텍스트를 반환."""
        import re
        content = srt_path.read_text(encoding="utf-8")
        blocks = [b.strip() for b in re.split(r"\n\n+", content.strip()) if b.strip()]
        if not blocks:
            return 0.0, ""

        last_block = blocks[-1]
        lines = last_block.split("\n")
        if len(lines) < 3:
            return 0.0, ""

        # 타임코드 파싱: "HH:MM:SS,mmm --> HH:MM:SS,mmm"
        tc_match = re.match(
            r"\d+:\d+:\d+[,.]\d+\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)",
            lines[1],
        )
        if not tc_match:
            return 0.0, ""

        h, m, s, ms = (int(tc_match.group(i)) for i in range(1, 5))
        end_sec = h * 3600 + m * 60 + s + ms / 1000.0
        text = " ".join(lines[2:]).strip()
        return end_sec, text

    def _detect_black_frames(
        self, video_path: Path, min_duration: float = 1.0, *, fail_safe: bool = False,
    ) -> list[tuple[float, float, float]]:
        """영상에서 검은 구간을 감지하여 (start, end, duration) 리스트 반환.

        fail_safe=True면 ffmpeg 실패 시 sentinel [(-1,-1,-1)] 반환하여
        호출부가 "검증 불가 → 원본 유지" 판단 가능.
        fail_safe=False면 빈 리스트 반환 (기존 soft warning 용도).
        """
        import re as _re
        blacks: list[tuple[float, float, float]] = []
        try:
            result = subprocess.run(
                ["ffmpeg", "-i", str(video_path),
                 "-vf", f"blackdetect=d={min_duration}:pix_th=0.10",
                 "-an", "-f", "null", "-"],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                logger.debug(
                    "Black detect failed for %s: %s",
                    video_path,
                    result.stderr[-500:] if result.stderr else "unknown error",
                )
                if fail_safe:
                    return [(-1.0, -1.0, -1.0)]
                return []
            for match in _re.finditer(
                r"black_start:(\d+\.?\d*)\s+black_end:(\d+\.?\d*)\s+black_duration:(\d+\.?\d*)",
                result.stderr,
            ):
                start, end, dur = float(match.group(1)), float(match.group(2)), float(match.group(3))
                if dur >= min_duration:
                    blacks.append((start, end, dur))
        except Exception as e:
            logger.debug(f"Black detect skipped: {e}")
            if fail_safe:
                return [(-1.0, -1.0, -1.0)]
        return blacks

    def _warn_black_frames(self, video_path: Path) -> None:
        """합성 영상에서 1초+ 검은 구간 감지 시 경고 로그."""
        for start, end, dur in self._detect_black_frames(video_path):
            logger.warning(f"BLACKDETECT: {dur:.1f}s black at {start:.1f}-{end:.1f}s")

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
