"""Pillow 텍스트 렌더링 유틸 — 픽셀 기반 줄바꿈 + 박스 클리핑.

모든 visual template에서 공용으로 사용.
"""

from __future__ import annotations

import logging
from typing import Any

from PIL import ImageDraw, ImageFont

from .fonts import get_korean_font

logger = logging.getLogger(__name__)

# 자막 안전영역: 하단 180px은 SRT 자막 + 유튜브 컨트롤바 영역
SUBTITLE_SAFE_MARGIN = 180


def draw_text_box(
    draw: ImageDraw.ImageDraw,
    text: str,
    box: tuple[int, int, int, int],
    font: ImageFont.FreeTypeFont | None = None,
    fill: tuple | str = "white",
    max_lines: int = 6,
    min_font_size: int = 18,
    max_font_size: int = 44,
    align: str = "left",
    ellipsis: bool = True,
    line_spacing: float = 1.3,
) -> int:
    """텍스트를 box(x, y, x2, y2) 안에 픽셀 기반 줄바꿈으로 렌더링.

    반환: 실제 렌더링된 줄 수.

    동작 순서:
    1. max_font_size에서 시작하여 줄바꿈 시도
    2. max_lines를 초과하면 폰트 축소
    3. min_font_size에서도 초과하면 말줄임
    4. 텍스트는 절대 box 밖으로 나가지 않음
    """
    if not text or not text.strip():
        return 0

    x, y, x2, y2 = box
    box_w = x2 - x
    box_h = y2 - y

    if box_w <= 0 or box_h <= 0:
        return 0

    # 폰트 크기를 줄여가며 맞는 크기 탐색
    for size in range(max_font_size, min_font_size - 1, -2):
        font_try = get_korean_font(size=size)
        lines = _wrap_text_pixel(draw, text, font_try, box_w)

        line_h = int(size * line_spacing)
        total_h = len(lines) * line_h

        if len(lines) <= max_lines and total_h <= box_h:
            font = font_try
            break
    else:
        # min_font_size에서도 안 들어가면 말줄임
        font = get_korean_font(size=min_font_size)
        lines = _wrap_text_pixel(draw, text, font, box_w)
        line_h = int(min_font_size * line_spacing)

        if len(lines) > max_lines:
            lines = lines[:max_lines]
            if ellipsis and lines:
                last = lines[-1]
                lines[-1] = last[:-1] + "…" if len(last) > 1 else "…"

    # 렌더링
    for i, line in enumerate(lines):
        ly = y + i * line_h
        if ly + line_h > y2:
            break

        if align == "center":
            bbox = draw.textbbox((0, 0), line, font=font)
            tw = bbox[2] - bbox[0]
            lx = x + (box_w - tw) // 2
        elif align == "right":
            bbox = draw.textbbox((0, 0), line, font=font)
            tw = bbox[2] - bbox[0]
            lx = x2 - tw
        else:
            lx = x

        draw.text((lx, ly), line, fill=fill, font=font)

    return min(len(lines), max_lines)


def _wrap_text_pixel(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    """텍스트를 픽셀 폭 기준으로 줄바꿈.

    공백/조사 경계에서 우선 분할, 안 되면 글자 단위 분할.
    """
    if not text:
        return []

    # 기존 줄바꿈 문자 처리
    paragraphs = text.replace("\r\n", "\n").split("\n")
    all_lines: list[str] = []

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # 전체가 폭 안에 들어가면 그대로
        bbox = draw.textbbox((0, 0), para, font=font)
        if bbox[2] - bbox[0] <= max_width:
            all_lines.append(para)
            continue

        # 단어/글자 단위 줄바꿈
        current = ""
        for char in para:
            test = current + char
            bbox = draw.textbbox((0, 0), test, font=font)
            if bbox[2] - bbox[0] > max_width and current:
                # 공백 경계에서 분할 시도
                last_space = current.rfind(" ")
                if last_space > len(current) * 0.3:
                    all_lines.append(current[:last_space].rstrip())
                    current = current[last_space:].lstrip() + char
                else:
                    all_lines.append(current)
                    current = char
            else:
                current = test

        if current:
            all_lines.append(current)

    return all_lines


def validate_text_bounds(
    img_width: int = 1920,
    img_height: int = 1080,
    content_boxes: list[tuple[int, int, int, int]] | None = None,
) -> list[str]:
    """텍스트 바운딩 박스가 화면 안에 있고 자막 안전영역을 침범하지 않는지 검증.

    반환: 위반 메시지 목록 (빈 리스트면 통과).
    """
    violations = []
    subtitle_y = img_height - SUBTITLE_SAFE_MARGIN

    if content_boxes:
        for i, (x, y, x2, y2) in enumerate(content_boxes):
            if x < 0 or y < 0:
                violations.append(f"box {i}: starts outside screen ({x},{y})")
            if x2 > img_width:
                violations.append(f"box {i}: right edge {x2} > screen width {img_width}")
            if y2 > subtitle_y:
                violations.append(f"box {i}: bottom {y2} invades subtitle zone (>{subtitle_y})")

    return violations
