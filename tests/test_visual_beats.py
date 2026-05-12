"""멘트 단위 visual beat 분해 회귀 테스트."""

from src.core.models import VisualIntent
from src.pipeline.s4_storyboard import S4Storyboard


def test_money_subtraction_gets_money_decrease_cue():
    """돈을 빼고 잔액이 남는 멘트는 money_decrease cue로 잡는다."""
    cue = S4Storyboard._infer_visual_cue(
        "월급에서 50만원을 빼면 125만원이 남습니다.",
        "finance",
    )

    assert cue == "money_decrease"
    assert S4Storyboard._intent_for_visual_cue(cue) == VisualIntent.INFOGRAPHIC


def test_visual_beat_split_keeps_money_math_as_own_beat():
    """금액 계산 멘트가 별도 beat가 되어 배경 전환 타이밍을 잡을 수 있다."""
    text = (
        "월급이 들어오면 먼저 고정비를 봅니다. "
        "여기서 50만원을 빼면 125만원이 남습니다. "
        "그다음 저축 통장으로 옮깁니다."
    )

    beats = S4Storyboard._split_text_to_visual_beats(text, max_chars=80)

    assert len(beats) >= 3
    assert any("50만원" in beat and "125만원" in beat for beat in beats)
    assert any(S4Storyboard._infer_visual_cue(beat, "finance") == "money_decrease" for beat in beats)


def test_visual_beat_split_ignores_weak_connectors():
    """약한 접속어나 사례 표현만으로 화면을 불필요하게 쪼개지 않는다."""
    text = (
        "예를 들어 2025년 기준으로 통장 조합을 최적화하는 방법을 설명하겠습니다. "
        "그런데 여기서 중요한 것은 처음부터 너무 많이 바꾸지 않는 것입니다."
    )

    beats = S4Storyboard._split_text_to_visual_beats(text, max_chars=140)

    assert len(beats) == 2


def test_visual_beat_split_merges_short_account_labels():
    """통장명 같은 짧은 라벨만으로 별도 화면을 만들지 않는다."""
    text = (
        "유튜브에서 통장 쪼개기를 검색하면 대부분 이렇게 말합니다. "
        "급여 통장. 생활비 통장. 저축 통장. 투자 통장 비상금 통장. "
        "최소 4개에서 5개예요."
    )

    beats = S4Storyboard._split_text_to_visual_beats(text, max_chars=220)

    assert len(beats) <= 3
    assert all(len(beat) >= 24 for beat in beats)


def test_non_money_visual_cues_are_inferred():
    """돈 외 멘트도 배경 구도용 cue로 세분화한다."""
    examples = {
        "통장을 생활비, 저축, 비상금으로 나눠서 자동이체하세요.": "account_split",
        "가장 큰 실수는 리스크 관리를 무시하는 것입니다.": "risk_warning",
        "첫째, 목표를 먼저 정하고 둘째, 실행 순서를 만드세요.": "target_goal",
        "실제로 많은 사회초년생이 카드값에서 막힙니다.": "expense_breakdown",
        "6개월 전부터 매달 점검하면 흐름이 보입니다.": "timeline",
        "거래량이 늘어나는 구간에서는 판단이 쉬워집니다.": "growth_trend",
        "수익률이 하락하면 비중을 다시 봐야 합니다.": "decline_trend",
    }

    for text, expected in examples.items():
        assert S4Storyboard._infer_visual_cue(text, "finance") == expected
