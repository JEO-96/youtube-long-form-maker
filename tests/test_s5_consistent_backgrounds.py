"""S5 로컬 도형 렌더 제거 회귀 테스트."""

from src.core.config import load_settings
from src.core.models import Scene
from src.pipeline.s5_media import S5Media


def test_local_pillow_rendering_is_disabled_by_default():
    """실제 영상에는 저품질 Pillow 도형 배경을 넣지 않는다."""
    settings = load_settings()

    assert settings.media_generation.prefer_local_render is False
    assert settings.media_generation.prefer_consistent_textless_backgrounds is False
    assert not hasattr(S5Media, "_generate_fallback_image")


def test_cue_context_keeps_scene_specific_stock_hint():
    """같은 cue라도 스톡 힌트를 붙여 장면별 이미지 프롬프트가 달라진다."""
    scene = Scene(
        scene_number=1,
        start_time=0,
        end_time=3,
        duration=3,
        narration_text="저축을 시작합니다.",
        stock_search_query="cash envelope bank card desk",
        visual_cue="money_saving",
    )

    context = S5Media._sanitize_textless_image_context(scene, "finance")

    assert "cash envelope bank card desk" in context
    assert "flat icon" in context


def test_cue_context_removes_piggy_bank_stock_hint():
    """기존 스토리보드에 piggy bank 검색어가 남아도 이미지 프롬프트에서는 제거한다."""
    scene = Scene(
        scene_number=1,
        start_time=0,
        end_time=3,
        duration=3,
        narration_text="저축을 시작합니다.",
        stock_search_query="saving money piggy bank cash stack",
        visual_cue="money_saving",
    )

    context = S5Media._sanitize_textless_image_context(scene, "finance")

    assert "cash stack" in context
    assert "piggy bank" not in context.lower().replace("-", " ")
