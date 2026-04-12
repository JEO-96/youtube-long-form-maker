"""S7 썸네일 + 메타데이터 생성."""

from __future__ import annotations

import logging
import textwrap
from typing import Any

from PIL import Image, ImageDraw

from ..core.models import (
    Stage, BenchmarkResult, ScriptResult, VoiceResult, StoryboardResult,
    ThumbnailResult,
)
from ..core.config import load_settings
from ..core.fonts import get_korean_font
from ..core.exceptions import StageError
from .base_stage import BaseStage

logger = logging.getLogger(__name__)

# 썸네일 검증 상수
_MIN_TEXT_AREA_RATIO = 0.02   # 텍스트 영역이 전체의 최소 2% 이상
_MIN_FILE_SIZE_BYTES = 5_000  # 최소 5KB (단색 이미지 탐지)


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

        # 썸네일 품질 검증
        self._validate_thumbnail(thumb_path, thumb_cfg.width, thumb_cfg.height)

        # YouTube 메타데이터 생성
        title = script.title
        description = self._generate_description(script, benchmark)
        tags = self._generate_tags(benchmark, script)

        # 챕터 생성 — 실제 voice/storyboard 타이밍 기반
        voice_data = self.state.load_stage_output(self.production_id, Stage.VOICE)
        storyboard_data = self.state.load_stage_output(self.production_id, Stage.STORYBOARD)
        editing_data = self.state.load_stage_output(self.production_id, Stage.EDITING)

        voice = VoiceResult(**voice_data) if voice_data else None
        storyboard = StoryboardResult(**storyboard_data) if storyboard_data else None

        # 최종 영상 duration (editing stage에서 가져옴)
        final_duration = None
        if editing_data:
            final_duration = editing_data.get("duration_seconds")

        chapters = self._generate_chapters(script, voice, final_duration)

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
        """Pillow + 한글 폰트로 고대비 썸네일 생성."""
        primary = self.channel.visual.primary_color
        secondary = getattr(self.channel.visual, "secondary_color", "#2d4a6f")
        r1, g1, b1 = self._hex_to_rgb(primary)
        r2, g2, b2 = self._hex_to_rgb(secondary)

        img = Image.new("RGB", (width, height))
        draw = ImageDraw.Draw(img)

        # 배경: 그래디언트
        for y in range(height):
            ratio = y / height
            r = int(r1 + (r2 - r1) * ratio)
            g = int(g1 + (g2 - g1) * ratio)
            b = int(b1 + (b2 - b1) * ratio)
            draw.line([(0, y), (width, y)], fill=(r, g, b))

        # 장식: 상단 액센트 바
        accent_color = (255, 200, 0)  # 골드/노란색 강조
        draw.rectangle([0, 0, width, 8], fill=accent_color)

        # 장식: 좌측 세로 바
        draw.rectangle([0, 0, 12, height], fill=accent_color)

        # safe zone 계산
        safe_left = 60
        safe_right = width - 60
        safe_top = 80
        safe_bottom = height - max(bottom_margin, 80)
        safe_width = safe_right - safe_left

        # ═══ 제목 텍스트 (큰 메인 키워드) ═══
        title_font = self._fit_font(
            draw, title, safe_width - 40, max_size=72, min_size=36,
            max_lines=3,
        )
        title_lines = self._wrap_text(draw, title, title_font, safe_width - 40)

        # 제목 위치: safe zone 상단 1/3 영역
        title_y = safe_top + 20
        title_line_height = title_font.size + 8

        # 제목 배경 오버레이 (반투명 어두운 박스)
        title_block_height = len(title_lines) * title_line_height + 40
        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rounded_rectangle(
            [safe_left - 10, title_y - 10,
             safe_right + 10, title_y + title_block_height],
            radius=16,
            fill=(0, 0, 0, 180),
        )
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(img)

        # 제목 렌더링 (흰색, 두껍게)
        for i, line in enumerate(title_lines):
            y = title_y + 10 + i * title_line_height
            # 그림자 효과
            draw.text((safe_left + 22, y + 2), line, fill=(0, 0, 0), font=title_font)
            draw.text((safe_left + 20, y), line, fill="white", font=title_font)

        # ═══ 후킹 문구 (보조 텍스트) ═══
        hook_y = title_y + title_block_height + 30
        if hook and hook_y + 60 < safe_bottom:
            hook_font = self._fit_font(
                draw, hook, safe_width - 80, max_size=36, min_size=20,
                max_lines=2,
            )
            hook_lines = self._wrap_text(draw, hook, hook_font, safe_width - 80)
            hook_line_height = hook_font.size + 6

            for i, line in enumerate(hook_lines[:2]):
                y = hook_y + i * hook_line_height
                draw.text((safe_left + 42, y + 1), line, fill=(0, 0, 0), font=hook_font)
                draw.text((safe_left + 40, y), line, fill=accent_color, font=hook_font)

        # ═══ 하단 채널 브랜딩 영역 ═══
        brand_y = safe_bottom - 40
        if brand_y > hook_y + 60:
            brand_font = get_korean_font(size=20, bold=False)
            channel_name = getattr(self.channel, "name", "")
            if channel_name:
                draw.text(
                    (safe_left + 20, brand_y),
                    channel_name,
                    fill=(200, 200, 200),
                    font=brand_font,
                )

        img.save(str(path), "PNG")
        logger.info(f"Thumbnail: {path} ({width}x{height})")

    def _fit_font(
        self, draw: ImageDraw.ImageDraw, text: str,
        max_width: int, max_size: int = 72, min_size: int = 28,
        max_lines: int = 3,
    ) -> Any:
        """텍스트가 max_width * max_lines 안에 들어가도록 폰트 크기 자동 조절."""
        for size in range(max_size, min_size - 1, -2):
            font = get_korean_font(size=size)
            lines = self._wrap_text(draw, text, font, max_width)
            if len(lines) <= max_lines:
                return font
        return get_korean_font(size=min_size)

    @staticmethod
    def _wrap_text(
        draw: ImageDraw.ImageDraw, text: str, font: Any, max_width: int,
    ) -> list[str]:
        """텍스트를 max_width 기준으로 줄바꿈."""
        if not text:
            return []

        lines: list[str] = []
        current = ""
        for char in text:
            test = current + char
            bbox = draw.textbbox((0, 0), test, font=font)
            if bbox[2] - bbox[0] > max_width and current:
                lines.append(current)
                current = char
            else:
                current = test
        if current:
            lines.append(current)
        return lines

    def _validate_thumbnail(self, path, width: int, height: int) -> None:
        """썸네일 품질 검증: 단색/텍스트 렌더 실패 감지."""
        import os
        file_size = os.path.getsize(str(path))
        if file_size < _MIN_FILE_SIZE_BYTES:
            raise StageError(
                "thumbnail", self.production_id,
                cause=ValueError(
                    f"Thumbnail file too small ({file_size} bytes) — "
                    "likely a blank or failed render"
                ),
            )

        img = Image.open(str(path))
        # 색상 다양성 검사: 상위 10 색상이 전체의 98% 이상이면 단색으로 판정
        colors = img.getcolors(maxcolors=256)
        if colors:
            total_pixels = width * height
            top_color_pixels = sum(c[0] for c in sorted(colors, key=lambda x: -x[0])[:3])
            if top_color_pixels / total_pixels > 0.97:
                logger.warning(
                    "Thumbnail nearly monochrome — text rendering may have failed"
                )

        logger.info(f"Thumbnail validation passed: {file_size} bytes")

    @staticmethod
    def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
        try:
            hex_color = hex_color.lstrip("#")
            return (int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16))
        except (ValueError, IndexError):
            return (26, 40, 64)

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
    def _generate_chapters(
        script: ScriptResult,
        voice: VoiceResult | None = None,
        final_duration: float | None = None,
    ) -> list[dict]:
        """챕터 타임스탬프 생성 — 실제 voice 타이밍 기반, 영상 길이 초과 방지."""
        chapters = [{"time": "0:00", "title": "인트로"}]

        # voice section_timings가 있으면 실제 타이밍 사용
        if voice and voice.section_timings:
            for timing in voice.section_timings:
                label = timing.section_label
                t = timing.start

                # final_duration 초과하면 생성하지 않음
                if final_duration and t >= final_duration:
                    break

                # hook은 이미 "인트로"로 추가됨, cta는 별도 처리
                if label == "hook":
                    continue
                elif label == "cta":
                    title = "마무리"
                elif label.startswith("section_"):
                    idx = int(label.split("_")[1]) - 1
                    if idx < len(script.sections):
                        title = script.sections[idx].header
                    else:
                        title = label
                else:
                    title = label

                m, s = divmod(int(t), 60)
                chapters.append({"time": f"{m}:{s:02d}", "title": title})
        else:
            # 폴백: estimated duration 기반 (기존 로직)
            t = 5  # hook 이후
            for sec in script.sections:
                # final_duration 초과 방지
                if final_duration and t >= final_duration:
                    break
                m, s = divmod(int(t), 60)
                chapters.append({"time": f"{m}:{s:02d}", "title": sec.header})
                t += sec.estimated_duration_seconds or 60

        # 최종 검증: final_duration 초과 챕터 제거
        if final_duration:
            valid_chapters = []
            for ch in chapters:
                parts = ch["time"].split(":")
                ch_seconds = int(parts[0]) * 60 + int(parts[1])
                if ch_seconds < final_duration:
                    valid_chapters.append(ch)
            chapters = valid_chapters

        return chapters
