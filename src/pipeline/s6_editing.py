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
        duration, file_size, subtitle_count = self._compose_video_ffmpeg(
            media, voice, storyboard, script, output_path,
            mixed_audio_path if audio_ducked else None,
        )
        applied_effects.append("renderer:ffmpeg_native")

        self.record_cost("system", "editing", units=1, unit_cost=0.0)

        # ═══ 장면 관련도 품질 게이트 로그 ═══
        self._log_scene_relevance_report(storyboard, media)

        # Optional Enhancers
        final_path = await self._apply_enhancers(output_path, applied_effects)

        return EditingResult(
            output_path=str(final_path),
            duration_seconds=duration,
            resolution=[1920, 1080],
            file_size_mb=round(file_size, 2),
            applied_effects=applied_effects,
            pattern_interrupts_count=pi_timeline.event_count,
            subtitle_count=subtitle_count,
            quality_gate_passed=True,
        )

    # ═══════════════════════════════════════════════════
    # ffmpeg 네이티브 합성 (MoviePy 제거)
    # ═══════════════════════════════════════════════════

    def _compose_video_ffmpeg(
        self,
        media: MediaResult,
        voice: VoiceResult,
        storyboard: StoryboardResult,
        script: ScriptResult,
        output_path: Path,
        mixed_audio_path: Path | None = None,
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

        # ═══ Step 3: 씬별 클립을 ffmpeg로 개별 생성 ═══
        tmp_dir = self.stage_dir / "_tmp_clips"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        asset_map = {a.scene_number: a for a in media.assets}
        clip_paths: list[Path] = []

        for i, scene in enumerate(sorted_scenes):
            scene_dur = aligned_durations[i] if i < len(aligned_durations) else scene.duration
            if scene_dur <= 0:
                scene_dur = 10.0

            asset = asset_map.get(scene.scene_number)
            clip_path = tmp_dir / f"clip_{i:03d}.mp4"

            self._prepare_scene_clip(asset, scene, scene_dur, target_size, clip_path)
            clip_paths.append(clip_path)

            logger.info(
                f"Scene {scene.scene_number}: {scene_dur:.1f}s "
                f"(aligned from {scene.duration:.1f}s, {scene.media_type.value})"
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

        no_audio_path = self.stage_dir / "no_audio.mp4"
        self._run_ffmpeg([
            "-f", "concat", "-safe", "0",
            "-i", str(concat_path),
            "-c:v", "copy",
            "-an",
            str(no_audio_path),
        ], "concat")

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

        # ═══ 품질 게이트 ═══
        duration = self._ffprobe_duration(output_path)
        if not self.dry_run and duration < target_duration * 0.80:
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
                if asset.media_type in (MediaType.AI_VIDEO, MediaType.STOCK_VIDEO):
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

    def _burn_subtitles_ffmpeg(
        self, video_path: Path, srt_path: Path, output_path: Path,
    ) -> int:
        """ffmpeg subtitles 필터로 SRT 자막 번인."""
        srt_content = srt_path.read_text(encoding="utf-8")
        subtitle_count = srt_content.count("\n\n")

        srt_escaped = str(srt_path).replace("\\", "/").replace(":", "\\:")

        style = (
            "FontName=Malgun Gothic,FontSize=22,PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00000000,BackColour=&H80000000,"
            "Outline=2,Shadow=0,MarginV=60,Alignment=2,BorderStyle=4"
        )

        self._run_ffmpeg([
            "-i", str(video_path),
            "-vf", f"subtitles='{srt_escaped}':force_style='{style}'",
            "-c:v", "libx264", "-preset", "medium", "-crf", "23",
            "-c:a", "copy",
            str(output_path),
        ], "subtitles", timeout=300)

        logger.info(f"ffmpeg 자막 번인 완료: {subtitle_count}개 자막")
        return subtitle_count

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
            return durations

        storyboard_durations = [max(s.duration, self.MIN_SCENE_DURATION) for s in scenes]
        sb_total = sum(storyboard_durations)
        if abs(sb_total - target_duration) < 2.0:
            return storyboard_durations

        if sb_total > 0:
            scale = target_duration / sb_total
            return [round(d * scale, 2) for d in storyboard_durations]

        return storyboard_durations

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
        self, storyboard: StoryboardResult, media: MediaResult,
    ) -> None:
        """각 씬의 narration-visual 매핑을 로그 + JSON으로 남겨 사람이 검수 가능."""
        import json

        asset_map = {a.scene_number: a for a in media.assets}
        report_rows: list[dict[str, Any]] = []

        for scene in storyboard.scenes:
            intent = getattr(scene, "visual_intent", "unknown")
            if hasattr(intent, "value"):
                intent = intent.value
            asset = asset_map.get(scene.scene_number)

            row = {
                "scene": scene.scene_number,
                "duration": scene.duration,
                "visual_intent": intent,
                "narration_text": scene.narration_text[:120],
                "visual_description": scene.visual_description[:100],
                "stock_search_query": scene.stock_search_query,
                "visual_keywords": scene.visual_keywords[:5],
                "media_type": scene.media_type.value,
                "media_provider": asset.provider if asset else "missing",
                "media_file": Path(asset.file_path).name if asset else "none",
            }
            report_rows.append(row)

            # 개별 씬 로그
            logger.info(
                f"[RELEVANCE] Scene {scene.scene_number}: "
                f"intent={intent}, "
                f"narration=\"{scene.narration_text[:50]}...\", "
                f"visual=\"{scene.visual_description[:50]}\", "
                f"stock_q=\"{scene.stock_search_query}\""
            )

        # intent 분포 로그
        from collections import Counter
        intent_dist = Counter(r["visual_intent"] for r in report_rows)
        logger.info(
            f"[RELEVANCE SUMMARY] {len(report_rows)} scenes, "
            f"intent distribution: {dict(intent_dist.most_common())}"
        )

        # JSON 리포트 저장
        report = {
            "total_scenes": len(report_rows),
            "intent_distribution": dict(intent_dist),
            "scenes": report_rows,
        }
        report_path = self.stage_dir / "scene_relevance_report.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"Scene relevance report: {report_path}")

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
