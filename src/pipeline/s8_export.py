"""S8 내보내기 + YouTube 업로드."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from ..core.models import (
    Stage, EditingResult, ThumbnailResult, ExportResult,
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

        # Quality gate
        if not self.dry_run:
            min_duration = 120.0  # minimum 2 minutes for any long-form content
            if editing.duration_seconds < min_duration:
                from ..core.exceptions import StageError
                raise StageError("export", self.production_id,
                    cause=ValueError(
                        f"QUALITY GATE FAILED: video {editing.duration_seconds:.1f}s < {min_duration:.0f}s minimum"))

            if file_size_mb < 1.0:
                from ..core.exceptions import StageError
                raise StageError("export", self.production_id,
                    cause=ValueError(
                        f"QUALITY GATE FAILED: file {file_size_mb:.2f}MB is suspiciously small"))

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
