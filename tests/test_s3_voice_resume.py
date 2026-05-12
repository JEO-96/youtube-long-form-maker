"""S3 음성 산출물 재사용 테스트."""

from src.pipeline.s3_voice import S3Voice


def test_load_srt_segments_for_resume(tmp_path):
    """이미 생성된 SRT에서 음성 세그먼트를 복구한다."""
    srt = tmp_path / "narration.srt"
    srt.write_text(
        "\n".join([
            "1",
            "00:00:01,000 --> 00:00:03,500",
            "첫 번째 문장입니다.",
            "",
            "2",
            "00:00:04,000 --> 00:00:06,250",
            "두 번째",
            "문장입니다.",
            "",
        ]),
        encoding="utf-8",
    )

    segments = S3Voice._load_srt_segments(srt)

    assert len(segments) == 2
    assert segments[0].start == 1.0
    assert segments[0].end == 3.5
    assert segments[1].text == "두 번째 문장입니다."
