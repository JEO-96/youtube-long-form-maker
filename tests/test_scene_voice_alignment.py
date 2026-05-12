"""씬-음성 매칭 좌표 보정 회귀 테스트."""

from src.core.models import MediaType, Scene, TimedSegment, VisualIntent, VoiceResult
from src.pipeline.s6_editing import S6Editing


class _MockEditing:
    MIN_SCENE_DURATION = 1.0
    MAX_SCENE_DURATION = 20.0
    MAX_CTA_DURATION = 12.0
    production_id = "test"


def _scene(scene_number: int, text: str) -> Scene:
    return Scene(
        scene_number=scene_number,
        start_time=0,
        end_time=0,
        duration=0,
        narration_text=text,
        visual_intent=VisualIntent.REAL_BROLL,
        media_type=MediaType.AI_IMAGE,
    )


def test_normalized_match_position_maps_back_to_original_time():
    """정규화 문자열 위치를 원문 transcript 시간으로 되돌려 씬 시작점을 잡는다."""
    stage = _MockEditing()
    voice = VoiceResult(
        total_duration_seconds=2.0,
        segments=[TimedSegment(text="hello, friend", start=0.0, end=2.0)],
    )
    durations, drift = S6Editing._align_scenes_to_voice(
        stage,
        [_scene(1, "friend")],
        voice,
        target_duration=2.0,
    )

    assert durations
    assert drift[0]["matched_start"] >= 1.0


def test_gap_between_matched_scenes_extends_previous_scene():
    """중간에 매칭되지 않은 음성 구간이 있으면 다음 씬을 앞당기지 않는다."""
    stage = _MockEditing()
    voice = VoiceResult(
        total_duration_seconds=7.0,
        segments=[
            TimedSegment(text="front point", start=0.0, end=2.0),
            TimedSegment(text="middle aside", start=3.0, end=5.0),
            TimedSegment(text="back point", start=5.0, end=7.0),
        ],
    )
    scenes = [_scene(1, "front point"), _scene(2, "back point")]

    durations, drift = S6Editing._align_scenes_to_voice(
        stage,
        scenes,
        voice,
        target_duration=7.0,
    )

    assert len(durations) == 2
    assert durations[0] >= 4.5
    assert durations[1] <= 2.5
    assert drift[1]["assigned_start"] >= 4.5
