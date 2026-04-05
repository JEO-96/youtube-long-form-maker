"""S7 썸네일 + 메타데이터 생성."""

from __future__ import annotations

import logging
from typing import Any

from PIL import Image, ImageDraw

from ..core.models import (
    Stage, BenchmarkResult, ScriptResult, ThumbnailResult,
)
from ..core.config import load_settings
from .base_stage import BaseStage

logger = logging.getLogger(__name__)


class S7Thumbnail(BaseStage):
    """S7: 썸네일 이미지 + YouTube 메타데이터."""

    stage = Stage.THUMBNAIL

    async def run(self, **kwargs: Any) -> ThumbnailResult:
        benchmark_data = self.load_previous(Stage.BENCHMARK)
        script_data = self.load_previous(Stage.SCRIPT)

        benchmark = BenchmarkResult(**benchmark_data)
        script = ScriptResult(**script_data)

        self.stage_dir.mkdir(parents=True, exist_ok=True)
        thumb_path = self.stage_dir / "thumbnail.png"

        settings = load_settings()
        thumb_cfg = settings.thumbnail

        # 썸네일 생성
        self._generate_thumbnail(
            thumb_path, script.title, script.hook,
            thumb_cfg.width, thumb_cfg.height,
            thumb_cfg.safe_zone.bottom_margin,
        )

        # YouTube 메타데이터 생성
        title = script.title
        description = self._generate_description(script, benchmark)
        tags = self._generate_tags(benchmark, script)
        chapters = self._generate_chapters(script)

        self.record_cost("system", "thumbnail_generation", units=1, unit_cost=0.0)

        return ThumbnailResult(
            thumbnail_path=str(thumb_path),
            thumbnail_candidates=[str(thumb_path)],
            youtube_title=title,
            youtube_description=description,
            youtube_tags=tags,
            chapter_timestamps=chapters,
        )

    def _generate_thumbnail(
        self,
        path, title: str, hook: str,
        width: int, height: int,
        bottom_margin: int,
    ) -> None:
        """Pillow로 썸네일 생성."""
        # 배경 (채널 색상 기반 그래디언트 시뮬레이션)
        primary = self.channel.visual.primary_color
        r, g, b = self._hex_to_rgb(primary)
        img = Image.new("RGB", (width, height), color=(r, g, b))
        draw = ImageDraw.Draw(img)

        # 텍스트 영역 (safe zone 고려)
        safe_top = 80
        safe_bottom = height - bottom_margin
        text_area_height = safe_bottom - safe_top

        # 제목 텍스트 (간단한 방식)
        # 실제 프로덕션에서는 폰트 파일 필요
        display_title = title[:30] if len(title) > 30 else title
        display_hook = hook[:40] if len(hook) > 40 else hook

        # 배경 오버레이 (텍스트 가독성)
        overlay_y = safe_top + text_area_height // 3
        draw.rectangle(
            [50, overlay_y, width - 50, overlay_y + 200],
            fill=(0, 0, 0, 180),
        )

        # 텍스트
        try:
            draw.text(
                (100, overlay_y + 20), display_title,
                fill="white",
            )
            draw.text(
                (100, overlay_y + 100), display_hook,
                fill=(255, 255, 0),
            )
        except Exception:
            pass

        img.save(str(path), "PNG")
        logger.info(f"Thumbnail: {path} ({width}x{height})")

    @staticmethod
    def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
        hex_color = hex_color.lstrip("#")
        return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))

    @staticmethod
    def _generate_description(script: ScriptResult, benchmark: BenchmarkResult) -> str:
        lines = [
            script.title,
            "",
            f"이 영상에서는 {benchmark.topic}에 대해 알아봅니다.",
            "",
            "📌 주요 내용:",
        ]
        for sec in script.sections:
            lines.append(f"• {sec.header}")
        lines.extend([
            "",
            f"키워드: {', '.join(benchmark.keywords[:5])}",
            "",
            "#shorts #youtube",
        ])
        return "\n".join(lines)

    @staticmethod
    def _generate_tags(benchmark: BenchmarkResult, script: ScriptResult) -> list[str]:
        tags = list(benchmark.keywords[:10])
        for sec in script.sections:
            for word in sec.header.split()[:2]:
                if word not in tags:
                    tags.append(word)
        return tags[:15]

    @staticmethod
    def _generate_chapters(script: ScriptResult) -> list[dict]:
        chapters = [{"time": "0:00", "title": "인트로"}]
        t = 5  # hook 이후
        for sec in script.sections:
            m, s = divmod(int(t), 60)
            chapters.append({"time": f"{m}:{s:02d}", "title": sec.header})
            t += sec.estimated_duration_seconds or 60
        return chapters
