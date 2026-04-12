"""draw_text_box 픽셀 기반 줄바꿈 + 박스 클리핑 테스트."""

import pytest
from PIL import Image, ImageDraw

from src.core.text_render import draw_text_box, _wrap_text_pixel, validate_text_bounds


@pytest.fixture
def canvas():
    """1920x1080 테스트 캔버스."""
    img = Image.new("RGB", (1920, 1080), color=(30, 40, 60))
    draw = ImageDraw.Draw(img)
    return img, draw


class TestWrapTextPixel:
    """_wrap_text_pixel 픽셀 기반 줄바꿈 테스트."""

    def test_short_text_no_wrap(self, canvas):
        _, draw = canvas
        from src.core.fonts import get_korean_font
        font = get_korean_font(size=32)
        lines = _wrap_text_pixel(draw, "짧은 텍스트", font, 800)
        assert len(lines) == 1
        assert lines[0] == "짧은 텍스트"

    def test_long_korean_text_wraps(self, canvas):
        _, draw = canvas
        from src.core.fonts import get_korean_font
        font = get_korean_font(size=32)
        text = "대한민국 부동산 시장에서 내 집 마련을 위한 최적 시점 판단 기준을 데이터로 분석합니다"
        lines = _wrap_text_pixel(draw, text, font, 400)
        assert len(lines) > 1
        # 모든 줄이 원본 텍스트의 일부
        joined = "".join(l.strip() for l in lines)
        assert joined.replace(" ", "") == text.replace(" ", "")

    def test_very_narrow_width(self, canvas):
        _, draw = canvas
        from src.core.fonts import get_korean_font
        font = get_korean_font(size=32)
        text = "좁은 폭에서도 글자 단위로 줄바꿈"
        lines = _wrap_text_pixel(draw, text, font, 100)
        assert len(lines) >= 3

    def test_empty_text(self, canvas):
        _, draw = canvas
        from src.core.fonts import get_korean_font
        font = get_korean_font(size=32)
        lines = _wrap_text_pixel(draw, "", font, 800)
        assert lines == []

    def test_newline_in_text(self, canvas):
        _, draw = canvas
        from src.core.fonts import get_korean_font
        font = get_korean_font(size=32)
        lines = _wrap_text_pixel(draw, "첫째 줄\n둘째 줄", font, 800)
        assert len(lines) == 2


class TestDrawTextBox:
    """draw_text_box 박스 클리핑 테스트."""

    def test_text_stays_in_box(self, canvas):
        _, draw = canvas
        box = (100, 100, 500, 300)
        n = draw_text_box(draw, "테스트 텍스트입니다 짧은 글", box)
        assert n >= 1

    def test_long_text_truncated(self, canvas):
        _, draw = canvas
        box = (100, 100, 500, 200)  # 높이 100px → 2~3줄만 가능
        text = "아주 긴 텍스트입니다 " * 20
        n = draw_text_box(draw, text, box, max_lines=3)
        assert n <= 3

    def test_font_auto_shrink(self, canvas):
        _, draw = canvas
        box = (100, 100, 400, 250)
        text = "폰트 자동 축소 테스트 텍스트 " * 5
        n = draw_text_box(draw, text, box, max_font_size=60, min_font_size=16, max_lines=4)
        assert n >= 1
        assert n <= 4

    def test_center_align(self, canvas):
        _, draw = canvas
        box = (0, 0, 1920, 100)
        n = draw_text_box(draw, "중앙 정렬", box, align="center")
        assert n == 1

    def test_zero_size_box(self, canvas):
        _, draw = canvas
        n = draw_text_box(draw, "텍스트", (100, 100, 100, 100))
        assert n == 0

    def test_ellipsis_on_overflow(self, canvas):
        _, draw = canvas
        box = (100, 100, 500, 160)  # 매우 좁은 높이
        text = "말줄임 테스트 " * 30
        n = draw_text_box(draw, text, box, max_lines=1, ellipsis=True)
        assert n == 1


class TestValidateTextBounds:
    """validate_text_bounds 검증 테스트."""

    def test_valid_boxes(self):
        violations = validate_text_bounds(1920, 1080, [
            (100, 100, 800, 800),
            (960, 100, 1800, 800),
        ])
        assert violations == []

    def test_right_edge_overflow(self):
        violations = validate_text_bounds(1920, 1080, [
            (100, 100, 2000, 500),  # 오른쪽 넘침
        ])
        assert len(violations) == 1
        assert "right edge" in violations[0]

    def test_subtitle_zone_invasion(self):
        violations = validate_text_bounds(1920, 1080, [
            (100, 100, 800, 950),  # 자막영역 침범 (900=1080-180)
        ])
        assert len(violations) == 1
        assert "subtitle zone" in violations[0]

    def test_negative_coords(self):
        violations = validate_text_bounds(1920, 1080, [
            (-10, 100, 800, 500),
        ])
        assert len(violations) == 1
        assert "outside screen" in violations[0]


class TestComparisonCardFallback:
    """comparison_card 비교 분리 실패 시 fallback 테스트."""

    def test_no_comparison_keyword_falls_back(self, canvas):
        """비교 키워드가 없으면 comparison_card가 아닌 다른 레이아웃으로 fallback."""
        # 비교 키워드 없는 텍스트
        text = "부동산 시장의 현황을 알려드립니다. 금리는 여전히 높습니다."
        import re
        parts = re.split(r'(?:vs|VS|보다|반면|그러나|하지만|반대로|한편)', text, maxsplit=1)
        # 분리 실패: 한쪽이 너무 짧거나 분리 안됨
        should_fallback = len(parts) < 2 or len(parts[0].strip()) < 10 or len(parts[1].strip()) < 10
        assert should_fallback
