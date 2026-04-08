"""한글 폰트 로더 — 시스템 폰트 자동 탐색."""

from __future__ import annotations

import os
import logging
from functools import lru_cache
from PIL import ImageFont

logger = logging.getLogger(__name__)

# 한글 지원 폰트 우선순위 (Windows)
_KOREAN_FONT_CANDIDATES = [
    "C:/Windows/Fonts/malgunbd.ttf",   # 맑은 고딕 Bold
    "C:/Windows/Fonts/malgun.ttf",     # 맑은 고딕
    "C:/Windows/Fonts/NanumGothicBold.ttf",
    "C:/Windows/Fonts/NanumGothic.ttf",
    "C:/Windows/Fonts/gulim.ttc",
    "C:/Windows/Fonts/HANDotumB.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",  # Linux
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",  # macOS
]


@lru_cache(maxsize=4)
def get_korean_font(size: int = 40, bold: bool = True) -> ImageFont.FreeTypeFont:
    """한글 지원 폰트 로드. 실패 시 ValueError raise (조용히 넘어가지 않음)."""
    for path in _KOREAN_FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, size)
                logger.info(f"Korean font loaded: {path} (size={size})")
                return font
            except Exception:
                continue

    # Last resort: try common names without path
    for name in ["malgun.ttf", "malgunbd.ttf", "NanumGothicBold.ttf", "arial.ttf"]:
        try:
            font = ImageFont.truetype(name, size)
            logger.info(f"Korean font loaded by name: {name} (size={size})")
            return font
        except Exception:
            continue

    raise ValueError(
        "No Korean font found on this system. "
        "Install Malgun Gothic or NanumGothic, or add font path to _KOREAN_FONT_CANDIDATES"
    )


@lru_cache(maxsize=1)
def get_korean_font_path() -> str:
    """한글 폰트 파일 경로 반환."""
    for path in _KOREAN_FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return ""
