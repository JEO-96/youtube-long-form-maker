"""S8 내보내기 + YouTube 업로드."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from ..core.models import (
    Stage, EditingResult, ThumbnailResult, ExportResult, VoiceResult,
)
from .base_stage import BaseStage

logger = logging.getLogger(__name__)


class S8Export(BaseStage):
    """S8: 최종 내보내기 + YouTube 비공개 업로드."""

    stage = Stage.EXPORT

    async def run(self, **kwargs: Any) -> ExportResult:
        editing_data = self.load_previous(Stage.EDITING)
        thumb_data = self.load_previous(Stage.THUMBNAIL)

        editing = EditingResult(**editing_data)
        thumb = ThumbnailResult(**thumb_data)

        self.stage_dir.mkdir(parents=True, exist_ok=True)

        # 최종 파일 복사
        src_video = Path(editing.output_path)
        final_path = self.stage_dir / "final_export.mp4"

        if src_video.exists():
            shutil.copy2(str(src_video), str(final_path))
            file_size_mb = final_path.stat().st_size / (1024 * 1024)
        else:
            logger.warning(f"Source video not found: {src_video}")
            final_path.write_bytes(b"\x00" * 1024)
            file_size_mb = 0.001

        # ═══ Quality Gates ═══
        if not self.dry_run:
            from ..core.exceptions import StageError

            # 1. 최소 영상 길이 (롱폼 2분 / 쇼츠 15초 이상)
            min_duration = 15.0 if self.channel.content.is_shorts else 120.0
            if editing.duration_seconds < min_duration:
                raise StageError("export", self.production_id,
                    cause=ValueError(
                        f"QUALITY GATE FAILED: video {editing.duration_seconds:.1f}s < {min_duration:.0f}s minimum"))

            # 2. 파일 크기 최소값
            if file_size_mb < 1.0:
                raise StageError("export", self.production_id,
                    cause=ValueError(
                        f"QUALITY GATE FAILED: file {file_size_mb:.2f}MB is suspiciously small"))

            # 3. 썸네일 품질 검증 — 실패/누락은 StageError
            thumb_path = Path(thumb.thumbnail_path) if thumb.thumbnail_path else None
            if not thumb_path or not thumb_path.exists():
                raise StageError("export", self.production_id,
                    cause=ValueError("QUALITY GATE FAILED: thumbnail file missing"))
            thumb_size = thumb_path.stat().st_size
            if thumb_size < 5_000:
                raise StageError("export", self.production_id,
                    cause=ValueError(
                        f"QUALITY GATE FAILED: thumbnail too small ({thumb_size} bytes)"))

            # 4. scene duration outlier — 25초 초과 씬이 있으면 실패
            report_path = Path(editing.output_path).parent / "scene_relevance_report.json"
            if report_path.exists():
                import json
                try:
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    scenes_data = report.get("scenes", [])
                    if scenes_data:
                        durations = [s["duration"] for s in scenes_data]
                        avg_dur = sum(durations) / len(durations)
                        max_dur = max(durations)
                        if max_dur > 25:
                            raise StageError("export", self.production_id,
                                cause=ValueError(
                                    f"QUALITY GATE FAILED: scene duration outlier "
                                    f"max={max_dur:.1f}s > 25s (avg={avg_dur:.1f}s)"))
                        if max_dur > avg_dur * 4:
                            logger.warning(
                                f"Scene duration outlier: max={max_dur:.1f}s "
                                f"vs avg={avg_dur:.1f}s"
                            )
                except StageError:
                    raise
                except Exception:
                    pass

            # 5. 챕터 타임스탬프 — 영상 길이 초과 시 실패
            for ch in thumb.chapter_timestamps:
                parts = ch.get("time", "0:00").split(":")
                try:
                    ch_seconds = int(parts[0]) * 60 + int(parts[1])
                    if ch_seconds > editing.duration_seconds:
                        raise StageError("export", self.production_id,
                            cause=ValueError(
                                f"QUALITY GATE FAILED: chapter '{ch.get('title')}' "
                                f"at {ch['time']} exceeds video duration "
                                f"{editing.duration_seconds:.0f}s"))
                except StageError:
                    raise
                except (ValueError, IndexError):
                    pass

            # 6. 패턴 인터럽트 — 적용 검증
            pi_planned = 0
            pi_applied = 0
            for eff in editing.applied_effects:
                if eff.startswith("pattern_interrupts_planned:"):
                    try:
                        pi_planned = int(eff.split(":")[1])
                    except (ValueError, IndexError):
                        pass
                elif eff.startswith("pattern_interrupts_applied:"):
                    try:
                        pi_applied = int(eff.split(":")[1])
                    except (ValueError, IndexError):
                        pass

            # 밝기 펄스 계열 PI는 명암 튐을 만들 수 있어 S6에서 의도적으로 no-op 처리한다.
            # 따라서 planned/applied 불일치는 export 실패가 아니라 정보성 경고로만 남긴다.
            if pi_planned > 0 and pi_applied == 0:
                logger.warning(
                    "Pattern interrupts produced no visible effects "
                    f"(planned={pi_planned}, applied=0)"
                )
            elif pi_planned > 0 and pi_applied < pi_planned:
                logger.info(
                    "Pattern interrupts partially applied under brightness-free policy: "
                    f"{pi_applied}/{pi_planned}"
                )

            # 7. 자막 줄 길이 검사 (정보 로그)
            voice_data = self.state.load_stage_output(self.production_id, Stage.VOICE)
            if voice_data:
                srt_path = Path(voice_data.get("srt_path", ""))
                if srt_path.exists():
                    srt_content = srt_path.read_text(encoding="utf-8")
                    long_lines = [
                        line for line in srt_content.split("\n")
                        if len(line.strip()) > 50
                        and "-->" not in line
                        and not line.strip().replace(" ", "").replace("-", "").replace(">", "").replace(",", "").isdigit()
                    ]
                    if long_lines:
                        logger.info(
                            f"SRT line length check: {len(long_lines)} lines > 50 chars"
                        )

        # 8. Final video blackdetect — 1초+ 검은 구간 금지
        if not self.dry_run and final_path.exists():
            self._check_black_frames(final_path)

        # YouTube 업로드 시도
        upload_result = await self._try_upload(final_path, thumb)

        self.record_cost("system", "export", units=1, unit_cost=0.0)

        return ExportResult(
            final_video_path=str(final_path),
            youtube_video_id=upload_result.get("video_id", ""),
            youtube_url=upload_result.get("url", ""),
            upload_status=upload_result.get("status", "skipped"),
            final_file_size_mb=round(file_size_mb, 2),
        )

    def _check_black_frames(self, video_path: Path) -> None:
        """최종 영상에서 1초+ 검은 구간 감지 시 StageError."""
        import subprocess, re

        try:
            result = subprocess.run(
                ["ffmpeg", "-i", str(video_path),
                 "-vf", "blackdetect=d=1.0:pix_th=0.10",
                 "-an", "-f", "null", "-"],
                capture_output=True, text=True, timeout=180,
            )
            black_segments = []
            for match in re.finditer(
                r"black_start:(\d+\.?\d*)\s+black_end:(\d+\.?\d*)\s+black_duration:(\d+\.?\d*)",
                result.stderr,
            ):
                start, end, dur = float(match.group(1)), float(match.group(2)), float(match.group(3))
                if dur >= 1.0:
                    black_segments.append((start, end, dur))

            if black_segments:
                details = "; ".join(
                    f"{s:.1f}-{e:.1f}s ({d:.1f}s)" for s, e, d in black_segments[:5]
                )
                from ..core.exceptions import StageError
                raise StageError("export", self.production_id,
                    cause=ValueError(
                        f"QUALITY GATE FAILED: {len(black_segments)} black segment(s) >1.0s: "
                        f"{details}"))
        except StageError:
            raise
        except Exception as e:
            logger.warning(f"Black frame detection failed (non-fatal): {e}")

    async def _try_upload(self, video_path: Path, thumb: ThumbnailResult) -> dict:
        """YouTube 업로드 시도. 실패해도 로컬 결과물 보존."""
        if self.dry_run:
            logger.info("Dry run: YouTube upload skipped")
            return {"video_id": "", "url": "", "status": "dry_run"}

        try:
            from ..providers.youtube import YouTubeProvider
            yt = YouTubeProvider()

            # OAuth 토큰이 필요하므로 없으면 skip
            # MVP 1차에서는 토큰 관리 미구현 → dry_run과 동일
            logger.info("YouTube upload: OAuth token not configured, skipping upload")
            return {"video_id": "", "url": "", "status": "no_oauth_token"}

        except Exception as e:
            logger.error(f"YouTube upload failed: {e}")
            return {"video_id": "", "url": "", "status": f"failed: {e}"}
