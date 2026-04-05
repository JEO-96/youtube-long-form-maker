"""Audio Ducking 기반 음성+BGM 믹싱."""

from __future__ import annotations

import logging
from pathlib import Path

from pydub import AudioSegment
from pydub.silence import detect_nonsilent

from ..core.config import load_settings
from ..core.exceptions import ProviderError

logger = logging.getLogger(__name__)


class AudioMixer:
    """음성+BGM Audio Ducking 믹서."""

    def __init__(self) -> None:
        settings = load_settings()
        ducking_cfg = settings.retention.audio_ducking

        self.bgm_volume_during_speech = ducking_cfg.bgm_volume_during_speech
        self.fade_duration_ms = ducking_cfg.fade_duration_ms

    def mix(
        self,
        voice_path: Path,
        bgm_path: Path,
        output_path: Path,
        bgm_base_volume_db: float = -12.0,
        duck_amount_db: float = 0.0,
        fade_ms: int = 0,
    ) -> Path:
        """음성+BGM 믹싱 with Audio Ducking.

        Args:
            voice_path: 음성 오디오 파일
            bgm_path: BGM 오디오 파일
            output_path: 출력 파일 경로
            bgm_base_volume_db: BGM 기본 볼륨 조절 (dB)
            duck_amount_db: 음성 구간 BGM 감소량 (dB). 0이면 config에서 계산.
            fade_ms: 페이드 시간 (ms). 0이면 config 값 사용.
        """
        # 입력 검증
        if not voice_path.exists():
            raise ProviderError("audio_mixer", f"Voice file not found: {voice_path}", retryable=False)
        if not bgm_path.exists():
            raise ProviderError("audio_mixer", f"BGM file not found: {bgm_path}", retryable=False)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        fade_ms = fade_ms or self.fade_duration_ms

        # duck_amount_db 계산: bgm_volume_during_speech(0.15) → 약 -16dB
        if duck_amount_db == 0.0:
            import math
            duck_amount_db = 20 * math.log10(max(self.bgm_volume_during_speech, 0.01))

        # 1. 오디오 로드
        voice = AudioSegment.from_file(str(voice_path))
        bgm = AudioSegment.from_file(str(bgm_path))

        # 2. BGM 기본 볼륨 조절
        bgm = bgm + bgm_base_volume_db

        # 3. BGM을 음성 길이에 맞추기 (loop 또는 trim)
        bgm = self._fit_bgm_to_duration(bgm, len(voice))

        # 4. 음성 구간 감지
        speech_ranges = detect_nonsilent(
            voice,
            min_silence_len=300,
            silence_thresh=voice.dBFS - 16,
            seek_step=10,
        )

        logger.info(f"Detected {len(speech_ranges)} speech segments in {len(voice)}ms audio")

        # 5. Audio Ducking 적용
        ducked_bgm = self._apply_ducking(bgm, speech_ranges, duck_amount_db, fade_ms)

        # 6. 최종 믹싱
        mixed = voice.overlay(ducked_bgm)

        # 7. 출력
        output_format = output_path.suffix.lstrip(".") or "mp3"
        mixed.export(str(output_path), format=output_format)

        logger.info(
            f"Audio mixed: voice={len(voice)}ms + bgm={len(bgm)}ms → "
            f"{output_path} ({output_path.stat().st_size} bytes)"
        )
        return output_path

    @staticmethod
    def _fit_bgm_to_duration(bgm: AudioSegment, target_ms: int) -> AudioSegment:
        """BGM을 target 길이에 맞추기 (loop 또는 trim)."""
        if len(bgm) >= target_ms:
            # Trim: fade out at the end
            return bgm[:target_ms].fade_out(min(2000, target_ms // 4))

        # Loop: BGM 반복
        loops_needed = (target_ms // len(bgm)) + 1
        looped = bgm * loops_needed
        return looped[:target_ms].fade_out(min(2000, target_ms // 4))

    @staticmethod
    def _apply_ducking(
        bgm: AudioSegment,
        speech_ranges: list[list[int]],
        duck_db: float,
        fade_ms: int,
    ) -> AudioSegment:
        """음성 구간에서 BGM 볼륨을 줄이는 ducking 적용."""
        if not speech_ranges:
            return bgm

        # 구간별 볼륨 조절을 위해 밀리초 단위 볼륨 맵 생성
        # 효율적인 방식: 구간을 나눠서 overlay
        result = bgm

        # speech 구간을 병합 (겹치거나 fade_ms 이내로 가까운 구간)
        merged = _merge_ranges(speech_ranges, margin_ms=fade_ms)

        for start_ms, end_ms in merged:
            # 범위 클리핑
            start_ms = max(0, start_ms - fade_ms)
            end_ms = min(len(bgm), end_ms + fade_ms)

            if start_ms >= end_ms:
                continue

            # 해당 구간의 BGM을 duck
            segment = bgm[start_ms:end_ms]
            ducked_segment = segment + duck_db

            # 페이드 적용
            actual_fade = min(fade_ms, len(ducked_segment) // 3)
            if actual_fade > 0:
                ducked_segment = ducked_segment.fade_in(actual_fade).fade_out(actual_fade)

            # 원본에서 해당 구간을 교체
            before = result[:start_ms]
            after = result[end_ms:]
            result = before + ducked_segment + after

        return result


def _merge_ranges(ranges: list[list[int]], margin_ms: int = 0) -> list[list[int]]:
    """겹치거나 가까운 구간 병합."""
    if not ranges:
        return []
    sorted_ranges = sorted(ranges, key=lambda r: r[0])
    merged = [list(sorted_ranges[0])]

    for start, end in sorted_ranges[1:]:
        if start <= merged[-1][1] + margin_ms:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    return merged
