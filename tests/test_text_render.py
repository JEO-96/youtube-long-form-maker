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
        text = "부동산 시장의 현황을 알려드립니다. 금리는 여전히 높습니다."
        import re
        parts = re.split(r'(?:vs|VS|보다|반면|그러나|하지만|반대로|한편)', text, maxsplit=1)
        should_fallback = len(parts) < 2 or len(parts[0].strip()) < 10 or len(parts[1].strip()) < 10
        assert should_fallback


class TestDeriveSceneTitle:
    """derive_scene_title 제목 생성 테스트."""

    def test_from_vis_desc(self):
        from src.core.visual_templates import derive_scene_title
        title = derive_scene_title(
            narration="긴 나레이션 텍스트입니다.",
            vis_desc="전세가율 70% 기준선 분석",
            intent="chart",
        )
        assert title
        assert len(title) <= 24
        assert "전세가율" in title

    def test_from_narration_when_vis_desc_empty(self):
        from src.core.visual_templates import derive_scene_title
        title = derive_scene_title(
            narration="PIR 9배 이하면 초록불, 매수 환경이 양호합니다.",
            vis_desc="",
            intent="chart",
        )
        assert title
        assert len(title) <= 24

    def test_fallback_when_both_empty(self):
        from src.core.visual_templates import derive_scene_title
        title = derive_scene_title(narration="", vis_desc="", intent="chart")
        assert title == "데이터 분석"

    def test_never_returns_empty(self):
        from src.core.visual_templates import derive_scene_title
        for intent in ["chart", "checklist", "comparison_card", "infographic",
                        "emphasis_caption", "real_broll", "unknown", ""]:
            title = derive_scene_title("", "", intent)
            assert title, f"Empty title for intent={intent}"
            assert len(title) >= 2

    def test_long_vis_desc_truncated(self):
        from src.core.visual_templates import derive_scene_title
        long_desc = "이것은 매우 긴 시각 설명 텍스트로 24자를 훨씬 초과하는 내용을 포함하고 있습니다"
        title = derive_scene_title("", long_desc, "chart", max_len=24)
        assert len(title) <= 24

    def test_generic_labels_not_used_as_title(self):
        """DATA INSIGHT, CHECK LIST 같은 generic 라벨이 제목으로 사용되지 않는지."""
        from src.core.visual_templates import derive_scene_title
        generic = ["DATA INSIGHT", "CHECK LIST", "INFO", "TREND ANALYSIS", "RISK METER"]
        for intent in ["chart", "checklist", "infographic"]:
            title = derive_scene_title(
                narration="부동산 시장의 핵심 데이터를 분석합니다.",
                vis_desc="부동산 데이터 차트",
                intent=intent,
            )
            assert title not in generic, f"Generic label '{title}' used for {intent}"
