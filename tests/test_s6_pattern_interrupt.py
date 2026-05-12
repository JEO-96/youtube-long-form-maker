"""S6 패턴 인터럽트 회귀 테스트."""

from pathlib import Path

from src.pipeline.s6_editing import S6Editing
from src.retention.pattern_interrupt import InterruptEvent, InterruptType


class _Stage:
    ffmpeg_args: list[str] | None = None

    def _run_ffmpeg(self, args: list[str], _label: str, timeout: int = 60) -> None:
        self.ffmpeg_args = args
        Path(args[-1]).write_bytes(b"rendered")


def test_non_zoom_pattern_interrupt_does_not_change_brightness(tmp_path):
    """밝기 펄스 이벤트는 원본 클립을 그대로 통과시킨다."""
    clip = tmp_path / "clip.mp4"
    out = tmp_path / "out.mp4"
    clip.write_bytes(b"original")

    stage = _Stage()
    applied = S6Editing._apply_pattern_interrupt(
        stage,
        clip,
        out,
        [InterruptEvent(timestamp=0.2, interrupt_type=InterruptType.SUBTITLE_EMPHASIS)],
        scene_dur=2.0,
        w=1280,
        h=720,
        scene_start=0.0,
    )

    assert applied == 0
    assert stage.ffmpeg_args is None
    assert out.read_bytes() == b"original"


def test_zoom_pattern_interrupt_keeps_filter_brightness_free(tmp_path):
    """줌 인터럽트도 명암 필터 없이 crop/scale만 사용한다."""
    clip = tmp_path / "clip.mp4"
    out = tmp_path / "out.mp4"
    clip.write_bytes(b"original")

    stage = _Stage()
    applied = S6Editing._apply_pattern_interrupt(
        stage,
        clip,
        out,
        [InterruptEvent(timestamp=0.2, interrupt_type=InterruptType.ZOOM)],
        scene_dur=2.0,
        w=1280,
        h=720,
        scene_start=0.0,
    )

    vf = " ".join(stage.ffmpeg_args or [])
    assert applied == 1
    assert "brightness" not in vf
    assert "crop=" in vf
    assert "scale=1280:720" in vf
