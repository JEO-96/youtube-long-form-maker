"""비주얼 카드 템플릿 — 고품질 Pillow 기반 씬 이미지 생성.

chart(3종 변형), comparison_card, checklist, emphasis, infographic,
default, cta 레이아웃을 포함한다.

규칙:
- 하단 180px은 자막 안전영역으로 비움 (SUBTITLE_SAFE_MARGIN)
- narration[:N] 같은 단순 substring 금지 — draw_text_box 사용
- 화면 중앙 60%에 주요 시각 요소 최소 1개
- 텍스트만 있는 화면은 라벨/배지/KPI 카드 등 보조 요소 추가
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .fonts import get_korean_font
from .text_render import draw_text_box, SUBTITLE_SAFE_MARGIN

logger = logging.getLogger(__name__)

# 표준 캔버스
W, H = 1920, 1080
SAFE_BOTTOM = H - SUBTITLE_SAFE_MARGIN  # 900


# ═══════════════════════════════════════════════════
# 공통 유틸
# ═══════════════════════════════════════════════════

def _extract_numbers(text: str) -> list[str]:
    """텍스트에서 숫자+단위 추출."""
    return re.findall(r'(\d+[\d,.]*\s*[%만억원배조건채호세]?)', text)


def _extract_years(text: str) -> list[str]:
    """연도 추출."""
    return re.findall(r'((?:19|20)\d{2})\s*년?', text)


def _extract_comparison_titles(narration: str) -> tuple[str, str]:
    """비교 제목 추출: '수도권 / 지방', '전세 / 매매' 등."""
    # 패턴 매칭으로 비교 대상 추출
    pairs = [
        (r'수도권|서울', r'지방|비수도권'),
        (r'전세', r'매매'),
        (r'금리\s*인하|금리\s*하락', r'금리\s*인상|금리\s*동결'),
        (r'현재|올해', r'내년|2026|2027'),
        (r'장점|긍정', r'단점|부정|위험'),
        (r'매수|사는', r'매도|파는|관망'),
    ]
    for left_pat, right_pat in pairs:
        if re.search(left_pat, narration) and re.search(right_pat, narration):
            left_match = re.search(left_pat, narration)
            right_match = re.search(right_pat, narration)
            return left_match.group(0), right_match.group(0)
    return "A", "B"


def _draw_rounded_card(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    fill: tuple,
    outline: tuple | None = None,
    radius: int = 12,
) -> None:
    """둥근 모서리 카드 배경."""
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=2 if outline else 0)


def _draw_kpi_card(
    draw: ImageDraw.ImageDraw,
    x: int, y: int, w: int, h: int,
    label: str, value: str,
    accent: tuple, bg: tuple,
) -> None:
    """KPI 미니 카드 (숫자 + 라벨)."""
    _draw_rounded_card(draw, (x, y, x + w, y + h), fill=bg, outline=accent, radius=10)
    draw_text_box(draw, value, (x + 10, y + 8, x + w - 10, y + h // 2 + 5),
                   max_font_size=36, min_font_size=20, fill="white", align="center", max_lines=1)
    draw_text_box(draw, label, (x + 10, y + h // 2 + 5, x + w - 10, y + h - 5),
                   max_font_size=18, min_font_size=12, fill=(180, 180, 190), align="center", max_lines=1)


def _draw_badge(
    draw: ImageDraw.ImageDraw,
    x: int, y: int,
    text: str,
    fill: tuple,
    bg: tuple,
) -> None:
    """작은 정보 배지 (태그)."""
    font = get_korean_font(size=16)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    pad = 8
    draw.rounded_rectangle(
        (x, y, x + tw + pad * 2, y + 28),
        radius=6, fill=bg,
    )
    draw.text((x + pad, y + 4), text, fill=fill, font=font)


def _draw_section_number(
    draw: ImageDraw.ImageDraw,
    x: int, y: int,
    number: int,
    accent: tuple,
) -> None:
    """섹션 번호 원형 배지."""
    r = 22
    draw.ellipse((x, y, x + r * 2, y + r * 2), fill=accent)
    font = get_korean_font(size=22, bold=True)
    draw.text((x + r - 7, y + 5), str(number), fill="white", font=font)


def _draw_footer(
    draw: ImageDraw.ImageDraw,
    W: int,
    text: str = "데이터 기반 분석",
    accent: tuple = (200, 200, 200),
) -> None:
    """하단 출처 느낌 footer — 자막 안전영역 바로 위."""
    draw_text_box(draw, text, (60, SAFE_BOTTOM - 28, W - 60, SAFE_BOTTOM),
                   max_font_size=14, min_font_size=11, fill=(120, 120, 130),
                   align="right", max_lines=1)


# ═══════════════════════════════════════════════════
# 씬 제목 생성
# ═══════════════════════════════════════════════════

# intent → 카테고리 칩 라벨
_INTENT_CHIP: dict[str, str] = {
    "chart": "DATA",
    "checklist": "CHECK",
    "comparison_card": "COMPARE",
    "infographic": "INFO",
    "emphasis_caption": "KEY",
    "real_broll": "SCENE",
    "map": "MAP",
    "talking_head_style": "TALK",
    "closing_cta": "CTA",
}

# intent → 최종 fallback 제목 (derive_scene_title에서 모든 추출이 실패한 경우)
_INTENT_FALLBACK_TITLE: dict[str, str] = {
    "chart": "데이터 분석",
    "checklist": "확인 사항",
    "comparison_card": "비교 분석",
    "infographic": "핵심 정보",
    "emphasis_caption": "핵심 포인트",
    "real_broll": "현장 화면",
    "map": "지역 분석",
    "talking_head_style": "전문가 분석",
    "closing_cta": "구독 안내",
}


def derive_scene_title(
    narration: str = "",
    vis_desc: str = "",
    intent: str = "",
    max_len: int = 24,
) -> str:
    """씬별 meaningful 제목 생성. 빈 문자열 절대 반환하지 않음.

    우선순위:
    1. visual_description에서 핵심 제목 추출 (첫 문장, 12~24자)
    2. narration 첫 문장을 max_len 이내로 요약
    3. intent 기반 fallback 제목
    """
    # 1순위: visual_description
    if vis_desc and len(vis_desc.strip()) >= 5:
        title = _extract_first_phrase(vis_desc, max_len)
        if title:
            return title

    # 2순위: narration 첫 문장
    if narration and len(narration.strip()) >= 5:
        title = _extract_first_phrase(narration, max_len)
        if title:
            return title

    # 3순위: intent fallback
    return _INTENT_FALLBACK_TITLE.get(intent, "핵심 내용")


def _extract_first_phrase(text: str, max_len: int = 24) -> str:
    """텍스트에서 첫 의미 있는 구절을 max_len 이내로 추출."""
    text = text.strip()
    if not text:
        return ""

    # 첫 문장 추출 (한국어 어미 + 구두점 기준)
    match = re.match(r'^(.+?[.!?다요죠까니])\s', text)
    first = match.group(1) if match else text

    # max_len 이내로 자르기
    if len(first) <= max_len:
        return first

    # 자연스러운 절단: 쉼표/공백 경계
    cut = first[:max_len]
    last_space = cut.rfind(" ")
    last_comma = max(cut.rfind(","), cut.rfind("，"))
    best = max(last_space, last_comma)
    if best > max_len * 0.4:
        cut = cut[:best].rstrip(",，. ")

    if len(cut) < 5:
        cut = first[:max_len]

    return cut


def _draw_header_bar(
    draw: ImageDraw.ImageDraw,
    W: int,
    title: str,
    accent: tuple,
    height: int = 80,
    chip: str = "",
) -> None:
    """상단 색상 헤더 바 — [카테고리 칩] + 실제 제목.

    chip이 있으면 "[CHIP] 제목" 형태로 렌더링.
    chip이 없으면 제목만 렌더링.
    """
    draw.rectangle([(0, 0), (W, height)], fill=accent)

    if chip:
        # 칩 배경
        chip_font = get_korean_font(size=16, bold=True)
        chip_bbox = draw.textbbox((0, 0), chip, font=chip_font)
        chip_w = chip_bbox[2] - chip_bbox[0] + 16
        chip_h = 26
        chip_x = 40
        chip_y = (height - chip_h) // 2
        draw.rounded_rectangle(
            (chip_x, chip_y, chip_x + chip_w, chip_y + chip_h),
            radius=4, fill=(255, 255, 255, 60),
        )
        draw.text((chip_x + 8, chip_y + 3), chip, fill="white", font=chip_font)

        # 제목 (칩 오른쪽)
        title_x = chip_x + chip_w + 16
        draw_text_box(draw, title, (title_x, 12, W - 50, height - 10),
                       max_font_size=34, min_font_size=20, fill="white", max_lines=1)
    else:
        draw_text_box(draw, title, (50, 12, W - 50, height - 10),
                       max_font_size=38, min_font_size=24, fill="white", max_lines=1)


# ═══════════════════════════════════════════════════
# CHART 3종 변형
# ═══════════════════════════════════════════════════

def can_draw_chart(narration: str) -> bool:
    """narration에 차트로 표현할 숫자 데이터가 있는지 판별.

    숫자가 0개면 chart를 그리면 안 됨 → False.
    """
    numbers = _extract_numbers(narration)
    years = _extract_years(narration)
    return len(numbers) >= 1 or len(years) >= 2


def _select_chart_variant(narration: str) -> str:
    """narration 성격에 따라 chart 변형 선택."""
    numbers = _extract_numbers(narration)
    years = _extract_years(narration)

    # gauge: 위험/부담/DSR/부채 + 퍼센트가 있을 때
    if any(kw in narration for kw in ['위험', '부담', 'DSR', '부채', '리스크', '과열', '경고']):
        if any('%' in n for n in numbers):
            return "gauge"

    # line: 연도 2개 이상 또는 추세 키워드
    if len(years) >= 2 or any(kw in narration for kw in ['추이', '추세', '변화', '흐름', '전망']):
        return "line"

    # 숫자 1개만 있으면 KPI 단독
    if len(numbers) == 1 and len(years) < 2:
        return "kpi_only"

    # 단위 혼합 감지: 같은 단위끼리만 bar chart 허용
    if len(numbers) >= 2 and _has_mixed_units(numbers):
        return "kpi_only"  # 단위 섞이면 KPI 카드로

    # default: 비교 bar
    return "kpi_bar"


def _classify_unit(num_str: str) -> str:
    """숫자 문자열의 단위를 분류."""
    num_str = num_str.strip()
    if '%' in num_str:
        return "percent"
    if '배' in num_str:
        return "multiple"
    if '억' in num_str:
        return "billion"
    if '만' in num_str:
        if '호' in num_str:
            return "units"
        if '원' in num_str or '건' in num_str:
            return "money"
        return "ten_thousand"
    if '년' in num_str or re.match(r'^(19|20)\d{2}$', num_str.strip()):
        return "year"
    # 단위 없는 순수 숫자
    return "plain"


def _has_mixed_units(numbers: list[str]) -> bool:
    """숫자 목록의 단위가 섞여 있는지 판별.

    같은 단위끼리만 bar chart로 묶을 수 있음.
    예: [9배, 12배, 15배] → False (동일 단위)
    예: [2026년, 40%, 27만 호] → True (혼합)
    """
    if len(numbers) < 2:
        return False
    units = {_classify_unit(n) for n in numbers}
    # plain은 무시 (단위 없는 숫자는 어디든 호환)
    units.discard("plain")
    return len(units) > 1


def _extract_y_unit(narration: str, keywords: list[str]) -> str:
    """narration에서 Y축 단위 추출."""
    for pattern, unit in [
        (r'\d+\s*만\s*호', "만 호"), (r'\d+\s*만\s*건', "만 건"),
        (r'\d+\s*만\s*원', "만 원"), (r'\d+\s*억', "억 원"),
        (r'\d+\s*%', "%"), (r'\d+\s*배', "배"),
        (r'\d+\s*건', "건"), (r'거래량', "거래량"), (r'물량', "물량"),
    ]:
        if re.search(pattern, narration):
            return unit
    return keywords[0] if keywords else ""


def _build_bar_data(
    numbers: list[str], keywords: list[str], years: list[str],
) -> list[tuple[str, float, bool]]:
    """narration 숫자로 실제 막대 데이터 구성. [(라벨, 값, 강조여부)]."""
    bars: list[tuple[str, float, bool]] = []

    # 숫자+키워드 쌍 매칭
    for i, num in enumerate(numbers[:6]):
        clean = re.sub(r'[^\d.]', '', num)
        val = float(clean) if clean else 0
        label = keywords[i] if i < len(keywords) else num
        bars.append((str(label), val, i == 0))

    # 연도 기반 (숫자가 부족하면)
    if not bars and years:
        import random
        rng = random.Random(hash("".join(years)))
        for yr in years[:6]:
            bars.append((yr, rng.uniform(30, 100), False))
        if bars:
            max_idx = max(range(len(bars)), key=lambda i: bars[i][1])
            bars[max_idx] = (bars[max_idx][0], bars[max_idx][1], True)

    return bars


def draw_chart_kpi_bar(
    draw: ImageDraw.ImageDraw,
    narration: str, keywords: list[str],
    accent: tuple, primary: tuple,
    vis_desc: str = "",
) -> None:
    """Chart 변형 A: narration 숫자 기반 비교 막대 차트."""
    title = derive_scene_title(narration, vis_desc, "chart")
    _draw_header_bar(draw, W, title, accent, chip="DATA")

    numbers = _extract_numbers(narration)
    years = _extract_years(narration)
    y_unit = _extract_y_unit(narration, keywords)

    # KPI 카드 (우측 상단)
    kpi_x = W - 380
    if numbers:
        _draw_kpi_card(draw, kpi_x, 110, 320, 90,
                       keywords[0] if keywords else "핵심 수치",
                       numbers[0], accent, (40, 50, 70))
    if len(numbers) > 1:
        _draw_kpi_card(draw, kpi_x, 220, 320, 90,
                       keywords[1] if len(keywords) > 1 else "비교",
                       numbers[1], accent, (40, 50, 70))

    # 실제 데이터로 막대 구성
    bars = _build_bar_data(numbers, keywords, years)
    n_bars = max(len(bars), 2)
    bar_w = min(140, (W - 550) // (n_bars + 1))
    gap = bar_w // 2
    chart_left = 160
    bar_y_base = 680
    total_w = n_bars * (bar_w + gap)
    start_x = chart_left + (W - 440 - chart_left - total_w) // 2

    # Y축 기준선 + 라벨
    if bars:
        max_val = max(b[1] for b in bars) or 1
    else:
        max_val = 100
    for i, gy in enumerate([380, 530, 680]):
        draw.line([(chart_left - 10, gy), (W - 420, gy)], fill=(70, 80, 100), width=1)
    if y_unit:
        draw_text_box(draw, f"({y_unit})", (30, 350, chart_left - 15, 375),
                       max_font_size=14, fill=(150, 155, 170), align="right", max_lines=1)

    # 막대 렌더링 — 실제 값에 비례
    for i, (label, val, highlight) in enumerate(bars[:n_bars]):
        x = start_x + i * (bar_w + gap)
        bh = int((val / max_val) * 280) if max_val > 0 else 100
        bh = max(30, min(300, bh))
        bar_color = accent if highlight else (80, 95, 130)
        draw.rounded_rectangle(
            [(x, bar_y_base - bh), (x + bar_w, bar_y_base)],
            radius=4, fill=bar_color,
        )
        # 막대 위 값 표시
        val_text = numbers[i] if i < len(numbers) else f"{val:.0f}"
        draw_text_box(draw, val_text,
                       (x - 10, bar_y_base - bh - 32, x + bar_w + 10, bar_y_base - bh - 5),
                       max_font_size=18, fill="white", align="center", max_lines=1)
        # X축 라벨
        draw_text_box(draw, label,
                       (x - 10, bar_y_base + 8, x + bar_w + 10, bar_y_base + 38),
                       max_font_size=15, fill=(170, 175, 190), align="center", max_lines=1)

    # "예시 시각화" 라벨 (실제 데이터가 아님을 명시)
    _draw_badge(draw, W - 200, SAFE_BOTTOM - 55, "예시 시각화", (140, 140, 150), (45, 55, 70))

    # 나레이션 요약
    draw_text_box(draw, narration, (80, 720, W - 80, SAFE_BOTTOM - 30),
                   max_font_size=22, min_font_size=16, fill=(190, 190, 200), max_lines=3)
    _draw_footer(draw, W)


def draw_chart_kpi_only(
    draw: ImageDraw.ImageDraw,
    narration: str, keywords: list[str],
    accent: tuple, primary: tuple,
    vis_desc: str = "",
) -> None:
    """숫자 1개만 있을 때: 큰 KPI 카드 + 나레이션."""
    title = derive_scene_title(narration, vis_desc, "chart")
    _draw_header_bar(draw, W, title, accent, chip="DATA")

    numbers = _extract_numbers(narration)
    main_num = numbers[0] if numbers else "—"
    main_label = keywords[0] if keywords else "핵심 수치"

    # 큰 KPI (중앙)
    _draw_rounded_card(draw, (W // 2 - 250, 180, W // 2 + 250, 400),
                        fill=(35, 45, 65), outline=accent, radius=16)
    draw_text_box(draw, main_num, (W // 2 - 230, 200, W // 2 + 230, 320),
                   max_font_size=80, min_font_size=40, fill="white", align="center", max_lines=1)
    draw_text_box(draw, main_label, (W // 2 - 230, 330, W // 2 + 230, 390),
                   max_font_size=28, fill=(180, 185, 200), align="center", max_lines=1)

    # 나레이션
    draw_text_box(draw, narration, (100, 440, W - 100, SAFE_BOTTOM - 60),
                   max_font_size=26, min_font_size=18, fill=(190, 190, 200),
                   align="center", max_lines=6)
    _draw_badge(draw, W - 200, SAFE_BOTTOM - 55, "예시 시각화", (140, 140, 150), (45, 55, 70))
    _draw_footer(draw, W)


def draw_chart_line(
    draw: ImageDraw.ImageDraw,
    narration: str, keywords: list[str],
    accent: tuple, primary: tuple,
    vis_desc: str = "",
) -> None:
    """Chart 변형 B: 연도 기반 라인 트렌드."""
    title = derive_scene_title(narration, vis_desc, "chart")
    _draw_header_bar(draw, W, title, accent, chip="TREND")

    numbers = _extract_numbers(narration)
    years = _extract_years(narration)
    y_unit = _extract_y_unit(narration, keywords)

    if numbers:
        _draw_kpi_card(draw, W - 380, 110, 320, 90,
                       keywords[0] if keywords else "주요 지표", numbers[0],
                       accent, (40, 50, 70))

    # 차트 영역
    chart_left, chart_right = 180, W - 140
    chart_top, chart_bottom = 240, 640
    chart_w = chart_right - chart_left
    chart_h = chart_bottom - chart_top

    # Y축 기준선
    for i in range(5):
        gy = chart_top + i * (chart_h // 4)
        draw.line([(chart_left, gy), (chart_right, gy)], fill=(65, 75, 95), width=1)
    if y_unit:
        draw_text_box(draw, f"({y_unit})", (30, chart_top - 30, chart_left - 10, chart_top - 5),
                       max_font_size=14, fill=(150, 155, 170), align="right", max_lines=1)

    # 데이터 포인트 — 연도별
    n_points = max(len(years), 5)
    import random
    rng = random.Random(hash(narration) % 10000)
    points = []
    y_val = rng.randint(chart_h // 4, chart_h * 3 // 4)
    for i in range(n_points):
        x = chart_left + int(i * chart_w / max(n_points - 1, 1))
        y_val = max(30, min(chart_h - 30, y_val + rng.randint(-50, 50)))
        points.append((x, chart_top + chart_h - y_val))

    # 영역 채우기
    area_color = (accent[0] // 5, accent[1] // 5, accent[2] // 5)
    area_points = list(points) + [(points[-1][0], chart_bottom), (points[0][0], chart_bottom)]
    draw.polygon(area_points, fill=area_color)

    # 라인 + 포인트
    for i in range(len(points) - 1):
        draw.line([points[i], points[i + 1]], fill="white", width=3)
    for p in points:
        draw.ellipse((p[0] - 5, p[1] - 5, p[0] + 5, p[1] + 5), fill=accent, outline="white", width=2)

    # X축 라벨 (연도)
    x_labels = years[:n_points] if years else [str(2020 + i) for i in range(n_points)]
    while len(x_labels) < n_points:
        x_labels.append("")
    for i in range(n_points):
        x = chart_left + int(i * chart_w / max(n_points - 1, 1))
        if x_labels[i]:
            draw_text_box(draw, x_labels[i],
                           (x - 30, chart_bottom + 10, x + 30, chart_bottom + 32),
                           max_font_size=15, fill=(160, 165, 180), align="center", max_lines=1)

    _draw_badge(draw, W - 200, SAFE_BOTTOM - 55, "예시 시각화", (140, 140, 150), (45, 55, 70))
    draw_text_box(draw, narration, (80, 690, W - 80, SAFE_BOTTOM - 30),
                   max_font_size=22, min_font_size=16, fill=(190, 190, 200), max_lines=3)
    _draw_footer(draw, W)


def draw_chart_gauge(
    draw: ImageDraw.ImageDraw,
    narration: str, keywords: list[str],
    accent: tuple, primary: tuple,
    vis_desc: str = "",
) -> None:
    """Chart 변형 C: 리스크 게이지 미터."""
    title = derive_scene_title(narration, vis_desc, "chart")
    _draw_header_bar(draw, W, title, accent, chip="RISK")

    numbers = _extract_numbers(narration)

    cx, cy = W // 2, 430
    outer_r = 220
    inner_r = 160

    # 3구간 게이지 아크
    for start_a, end_a, color in [
        (-180, -108, (50, 200, 80)), (-108, -54, (230, 190, 40)), (-54, 0, accent),
    ]:
        draw.arc([(cx - outer_r, cy - outer_r), (cx + outer_r, cy + outer_r)],
                  start=start_a, end=end_a, fill=color, width=outer_r - inner_r)
    draw.ellipse((cx - inner_r + 5, cy - inner_r + 5, cx + inner_r - 5, cy + inner_r - 5),
                  fill=(35, 45, 65))

    # 게이지 값
    gauge_val = 0.6
    for num in numbers:
        clean = re.sub(r'[^\d.]', '', num)
        if clean:
            val = float(clean)
            if val <= 100:
                gauge_val = val / 100
                break

    # 바늘
    needle_angle = math.radians(-180 + gauge_val * 180)
    nl = inner_r - 20
    draw.line([(cx, cy), (cx + int(nl * math.cos(needle_angle)), cy + int(nl * math.sin(needle_angle)))],
               fill="white", width=4)
    draw.ellipse((cx - 12, cy - 12, cx + 12, cy + 12), fill="white")

    draw_text_box(draw, "안전", (cx - outer_r - 40, cy + 20, cx - outer_r + 60, cy + 50),
                   max_font_size=16, fill=(60, 180, 80), align="center", max_lines=1)
    draw_text_box(draw, "위험", (cx + outer_r - 60, cy + 20, cx + outer_r + 40, cy + 50),
                   max_font_size=16, fill=accent, align="center", max_lines=1)

    if numbers:
        draw_text_box(draw, numbers[0], (cx - 150, cy + 50, cx + 150, cy + 130),
                       max_font_size=60, fill="white", align="center", max_lines=1)

    if len(numbers) > 1:
        _draw_kpi_card(draw, 80, 150, 280, 80,
                       keywords[0] if keywords else "지표", numbers[1], accent, (40, 50, 70))

    draw_text_box(draw, narration, (80, 620, W - 80, SAFE_BOTTOM - 60),
                   max_font_size=22, min_font_size=16, fill=(190, 190, 200), max_lines=4)
    _draw_badge(draw, W - 200, SAFE_BOTTOM - 55, "예시 시각화", (140, 140, 150), (45, 55, 70))
    _draw_footer(draw, W)


# ═══════════════════════════════════════════════════
# COMPARISON CARD
# ═══════════════════════════════════════════════════

def draw_comparison_card(
    draw: ImageDraw.ImageDraw,
    narration: str, keywords: list[str],
    accent: tuple, primary: tuple, secondary: tuple,
    vis_desc: str = "",
) -> bool:
    """고도화된 비교 카드. 분리 실패 시 False 반환 → 호출측에서 fallback."""
    parts = re.split(r'(?:vs|VS|보다|반면|그러나|하지만|반대로|한편)', narration, maxsplit=1)
    if len(parts) < 2 or len(parts[0].strip()) < 10 or len(parts[1].strip()) < 10:
        return False  # fallback 필요

    left_text = parts[0].strip()
    right_text = parts[1].strip()
    left_title, right_title = _extract_comparison_titles(narration)

    mid = W // 2
    comp_title = f"{left_title} vs {right_title}"
    scene_title = derive_scene_title(narration, vis_desc, "comparison_card")
    header_text = f"{comp_title}: {scene_title}" if scene_title != comp_title else comp_title
    if len(header_text) > 30:
        header_text = comp_title
    _draw_header_bar(draw, W, header_text, accent, chip="COMPARE")

    # 좌우 카드 배경
    card_top = 110
    card_bottom = SAFE_BOTTOM - 40
    _draw_rounded_card(draw, (40, card_top, mid - 30, card_bottom), fill=(35, 45, 65), outline=(60, 70, 90))
    _draw_rounded_card(draw, (mid + 30, card_top, W - 40, card_bottom), fill=(35, 45, 65), outline=(60, 70, 90))

    # VS 뱃지 (중앙)
    draw.ellipse((mid - 30, 340, mid + 30, 400), fill=accent)
    draw_text_box(draw, "VS", (mid - 25, 350, mid + 25, 395),
                   max_font_size=28, fill="white", align="center", max_lines=1)

    # 좌측 카드 내용
    _draw_section_number(draw, 60, card_top + 20, 1, accent)
    draw_text_box(draw, left_title, (100, card_top + 20, mid - 50, card_top + 70),
                   max_font_size=30, fill=accent, max_lines=1)
    left_nums = _extract_numbers(left_text)
    if left_nums:
        draw_text_box(draw, left_nums[0], (60, card_top + 80, mid - 50, card_top + 160),
                       max_font_size=48, fill="white", align="center", max_lines=1)
    draw_text_box(draw, left_text, (60, card_top + 170, mid - 50, card_bottom - 20),
                   max_font_size=24, min_font_size=16, fill=(200, 200, 210), max_lines=8)

    # 우측 카드 내용
    _draw_section_number(draw, mid + 50, card_top + 20, 2, (80, 140, 200))
    draw_text_box(draw, right_title, (mid + 90, card_top + 20, W - 60, card_top + 70),
                   max_font_size=30, fill=(80, 140, 200), max_lines=1)
    right_nums = _extract_numbers(right_text)
    if right_nums:
        draw_text_box(draw, right_nums[0], (mid + 50, card_top + 80, W - 60, card_top + 160),
                       max_font_size=48, fill="white", align="center", max_lines=1)
    draw_text_box(draw, right_text, (mid + 50, card_top + 170, W - 60, card_bottom - 20),
                   max_font_size=24, min_font_size=16, fill=(200, 200, 210), max_lines=8)

    _draw_footer(draw, W)
    return True


# ═══════════════════════════════════════════════════
# CHECKLIST
# ═══════════════════════════════════════════════════

def draw_checklist_card(
    draw: ImageDraw.ImageDraw,
    narration: str, keywords: list[str],
    accent: tuple, primary: tuple,
    vis_desc: str = "",
) -> None:
    """고도화된 체크리스트 카드."""
    title = derive_scene_title(narration, vis_desc, "checklist")
    _draw_header_bar(draw, W, title, accent, height=70, chip="CHECK")

    # 항목 추출: 문장 경계 분리 (어미+구두점 뒤 공백 기준, 어미 자체는 보존)
    items = re.split(r'(?<=[.!?다요죠니까])\s+', narration)
    items = [it.strip() for it in items if it.strip() and len(it.strip()) > 5]

    # 항목이 부족하면 narration에서 추가 생성
    if len(items) < 3:
        extra = re.split(r'[,，]\s*', narration)
        extra = [e.strip() for e in extra if e.strip() and len(e.strip()) > 8]
        items.extend(extra)
        items = list(dict.fromkeys(items))  # 중복 제거

    items = items[:7]
    if len(items) < 3:
        items = [narration[:40], narration[40:80], narration[80:120]]
        items = [it.strip() for it in items if it.strip()]

    y = 100
    item_height = min(95, (SAFE_BOTTOM - 200 - y) // max(len(items), 1))
    checked_count = max(1, len(items) // 2)

    check_font = get_korean_font(size=32, bold=True)

    for i, item in enumerate(items):
        if y + item_height > SAFE_BOTTOM - 100:
            break

        checked = i < checked_count
        box_x = 70
        box_y = y + 10

        # 체크박스
        draw.rounded_rectangle(
            [(box_x, box_y), (box_x + 40, box_y + 40)],
            radius=6,
            outline=accent if checked else (100, 110, 120),
            width=2,
        )
        if checked:
            draw.text((box_x + 7, box_y - 2), "V", fill=accent, font=check_font)

        # 항목 텍스트
        text_color = (230, 230, 240) if checked else (150, 150, 160)
        draw_text_box(draw, item, (140, y + 5, W - 100, y + item_height - 10),
                       max_font_size=28, min_font_size=18, fill=text_color, max_lines=2)

        # 구분선
        if i < len(items) - 1:
            draw.line([(140, y + item_height - 5), (W - 100, y + item_height - 5)],
                       fill=(50, 60, 75), width=1)
        y += item_height

    # 남는 공간: 정보 배지
    if y + 60 < SAFE_BOTTOM - 40:
        badge_y = SAFE_BOTTOM - 80
        badges = ["확인 필요", "리스크 체크", "다음 행동"]
        badge_x = 80
        for badge_text in badges:
            if badge_x > W - 200:
                break
            _draw_badge(draw, badge_x, badge_y, badge_text, (180, 180, 190), (50, 60, 75))
            badge_x += 160

    _draw_footer(draw, W)


# ═══════════════════════════════════════════════════
# EMPHASIS CAPTION
# ═══════════════════════════════════════════════════

def draw_emphasis_card(
    draw: ImageDraw.ImageDraw,
    narration: str, keywords: list[str],
    accent: tuple,
    vis_desc: str = "",
) -> None:
    """핵심 강조 캡션 — 숫자 + 키워드 + 보조 + 데이터 태그."""
    # 반투명 오버레이
    draw.rectangle([(0, 0), (W, H)], fill=(*accent, 30))

    numbers = _extract_numbers(narration)

    # 큰 숫자
    num_y = 120
    if numbers:
        draw_text_box(draw, numbers[0], (100, num_y, W - 100, num_y + 180),
                       max_font_size=140, min_font_size=60,
                       fill="white", align="center", max_lines=1)
        num_y += 200

    # 핵심 키워드
    key_text = keywords[0] if keywords else narration[:30]
    draw_text_box(draw, key_text, (100, num_y, W - 100, num_y + 100),
                   max_font_size=52, min_font_size=28,
                   fill="white", align="center", max_lines=2)

    # 강조 밑줄
    draw.rectangle([(W // 4, num_y + 90), (3 * W // 4, num_y + 96)], fill=accent)

    # 보조 설명
    draw_text_box(draw, narration, (120, num_y + 130, W - 120, SAFE_BOTTOM - 80),
                   max_font_size=24, min_font_size=16,
                   fill=(200, 200, 210), align="center", max_lines=5)

    # 데이터 태그 (하단)
    tags = keywords[:3] if keywords else []
    tag_x = W // 2 - len(tags) * 80
    for tag in tags:
        _draw_badge(draw, tag_x, SAFE_BOTTOM - 60, tag, "white", (*accent, 180))
        tag_x += 160

    _draw_footer(draw, W)


# ═══════════════════════════════════════════════════
# INFOGRAPHIC
# ═══════════════════════════════════════════════════

def draw_infographic_card(
    draw: ImageDraw.ImageDraw,
    narration: str, keywords: list[str],
    accent: tuple, primary: tuple,
    vis_desc: str = "",
) -> None:
    """인포그래픽 카드 — 번호 카드 + 나레이션."""
    title = derive_scene_title(narration, vis_desc, "infographic")
    _draw_header_bar(draw, W, title, accent, height=70, chip="INFO")

    items = keywords[:6] if keywords else re.split(r'[,，.]\s*', narration[:200])
    items = [it.strip() for it in items if it.strip() and len(it.strip()) > 2][:6]

    cols = min(len(items), 3)
    rows = math.ceil(len(items) / cols)
    card_w = min(500, (W - 100) // cols - 30)
    card_h = min(180, (SAFE_BOTTOM - 200) // rows - 20)
    total_w = cols * card_w + (cols - 1) * 30
    start_x = (W - total_w) // 2
    y_start = 110

    for i, item in enumerate(items):
        row = i // cols
        col = i % cols
        x = start_x + col * (card_w + 30)
        y = y_start + row * (card_h + 20)

        if y + card_h > SAFE_BOTTOM - 80:
            break

        _draw_rounded_card(draw, (x, y, x + card_w, y + card_h),
                            fill=(35, 45, 65), outline=(60, 70, 90))
        _draw_section_number(draw, x + 15, y + 15, i + 1, accent)
        draw_text_box(draw, item, (x + 60, y + 15, x + card_w - 15, y + card_h - 15),
                       max_font_size=24, min_font_size=16, fill=(220, 225, 235), max_lines=3)

    # 하단 나레이션
    draw_text_box(draw, narration,
                   (60, SAFE_BOTTOM - 100, W - 60, SAFE_BOTTOM - 30),
                   max_font_size=20, min_font_size=14, fill=(160, 160, 170), max_lines=2)
    _draw_footer(draw, W)


# ═══════════════════════════════════════════════════
# DEFAULT / CTA
# ═══════════════════════════════════════════════════

def draw_default_card(
    draw: ImageDraw.ImageDraw,
    narration: str, vis_desc: str,
    accent: tuple, primary: tuple,
    scene_number: int = 0,
) -> None:
    """기본 카드뉴스 — 헤더 + 본문 + 섹션 번호 + 장식."""
    title = derive_scene_title(narration, vis_desc, "real_broll")
    _draw_header_bar(draw, W, title, accent, height=100, chip="SCENE")

    if scene_number > 0:
        _draw_section_number(draw, W - 80, 110, scene_number, accent)

    # 좌측 장식선
    draw.rectangle([(0, 100), (6, SAFE_BOTTOM)], fill=accent)

    # 본문
    draw_text_box(draw, narration, (60, 140, W - 100, SAFE_BOTTOM - 40),
                   max_font_size=30, min_font_size=18, fill=(220, 225, 235), max_lines=14)
    _draw_footer(draw, W)


def draw_cta_card(
    draw: ImageDraw.ImageDraw,
    accent: tuple, primary: tuple,
    channel_name: str = "",
) -> None:
    """CTA 엔딩 카드."""
    draw_text_box(draw, "SUBSCRIBE", (100, 220, W - 100, 380),
                   max_font_size=72, fill="white", align="center", max_lines=1)

    # 구독 버튼
    btn_w, btn_h = 400, 80
    btn_x = (W - btn_w) // 2
    btn_y = 420
    draw.rounded_rectangle((btn_x, btn_y, btn_x + btn_w, btn_y + btn_h), radius=10, fill=accent)
    draw_text_box(draw, "구독하기", (btn_x, btn_y + 15, btn_x + btn_w, btn_y + btn_h - 5),
                   max_font_size=36, fill="white", align="center", max_lines=1)

    draw_text_box(draw, "좋아요 & 알림 설정",
                   (W // 2 - 250, btn_y + 110, W // 2 + 250, btn_y + 160),
                   max_font_size=30, fill=(200, 200, 210), align="center", max_lines=1)

    if channel_name:
        draw_text_box(draw, channel_name,
                       (W // 2 - 200, SAFE_BOTTOM - 50, W // 2 + 200, SAFE_BOTTOM),
                       max_font_size=28, fill=(180, 180, 190), align="center", max_lines=1)
    _draw_footer(draw, W)
