"""S3 음성 합성 + 자막 생성."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydub import AudioSegment
from pydub.generators import Sine

from ..core.models import Stage, ScriptResult, VoiceResult, TimedSegment
from .base_stage import BaseStage

logger = logging.getLogger(__name__)


class S3Voice(BaseStage):
    """S3: TTS 음성 합성 + Whisper 자막."""

    stage = Stage.VOICE

    async def run(self, **kwargs: Any) -> VoiceResult:
        script_data = self.load_previous(Stage.SCRIPT)
        script = ScriptResult(**script_data)

        self.stage_dir.mkdir(parents=True, exist_ok=True)
        audio_path = self.stage_dir / "narration.mp3"
        srt_path = self.stage_dir / "narration.srt"

        if self.dry_run:
            return self._mock_result(script, audio_path, srt_path)

        # 채널 설정 기반 TTS 선택 (기본: elevenlabs)
        from ..providers.factory import create_tts
        tts_provider_name = self.channel.providers.tts
        tts = create_tts(tts_provider_name, fallback="elevenlabs")

        await tts.synthesize(script.full_text, output_path=audio_path)
        char_count = len(script.full_text)
        self.record_cost(tts_provider_name, "synthesize", units=char_count, unit_cost=tts.estimate_cost("x"))

        # Whisper STT → 타임스탬프
        segments: list[TimedSegment] = []
        total_duration = 0.0
        try:
            from ..providers.stt import WhisperSTT
            stt = WhisperSTT()
            raw_segments = stt.transcribe(audio_path)
            stt.transcribe_to_srt(audio_path, srt_path)

            for seg in raw_segments:
                segments.append(TimedSegment(
                    text=seg["text"], start=seg["start"], end=seg["end"],
                    confidence=seg.get("confidence", 0.0),
                ))
            if segments:
                total_duration = segments[-1].end
        except Exception as e:
            logger.warning(f"Whisper STT failed (non-critical): {e}")
            # 음성 파일의 길이로 대체
            audio = AudioSegment.from_file(str(audio_path))
            total_duration = len(audio) / 1000.0

        self.record_cost("whisper", "transcribe", units=total_duration / 60.0, unit_cost=0.0)

        return VoiceResult(
            audio_path=str(audio_path),
            srt_path=str(srt_path) if srt_path.exists() else "",
            total_duration_seconds=total_duration,
            segments=segments,
            tts_provider="elevenlabs",
            voice_id=tts.default_voice_id,
        )

    def _mock_result(
        self, script: ScriptResult, audio_path: Path, srt_path: Path
    ) -> VoiceResult:
        """Mock: 10초짜리 테스트 오디오 + 가상 타임스탬프."""
        # 실제 wav 파일 생성 (테스트용 1초짜리 무음 + 톤)
        duration_ms = int(script.estimated_duration_seconds * 1000) if script.estimated_duration_seconds > 0 else 10000
        duration_ms = min(duration_ms, 10000)  # mock은 최대 10초

        audio = AudioSegment.silent(duration=500)
        audio += Sine(440).to_audio_segment(duration=duration_ms - 1000).apply_gain(-10)
        audio += AudioSegment.silent(duration=500)
        audio.export(str(audio_path), format="mp3")

        total_dur = duration_ms / 1000.0

        # 가상 세그먼트
        segments = []
        t = 0.5
        for sec in script.sections:
            seg_dur = min(sec.estimated_duration_seconds, (total_dur - t) * 0.9)
            if seg_dur <= 0:
                break
            segments.append(TimedSegment(
                text=sec.body[:50], start=round(t, 2), end=round(t + seg_dur, 2),
            ))
            t += seg_dur + 0.3

        # SRT 생성
        srt_lines = []
        for i, seg in enumerate(segments, 1):
            start_ts = self._srt_time(seg.start)
            end_ts = self._srt_time(seg.end)
            srt_lines.extend([str(i), f"{start_ts} --> {end_ts}", seg.text, ""])
        srt_path.write_text("\n".join(srt_lines), encoding="utf-8")

        self.record_cost("elevenlabs", "synthesize_mock", units=len(script.full_text), unit_cost=0.0)

        return VoiceResult(
            audio_path=str(audio_path),
            srt_path=str(srt_path),
            total_duration_seconds=total_dur,
            segments=segments,
            tts_provider="mock",
            voice_id="mock_voice",
        )

    @staticmethod
    def _srt_time(seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds % 1) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
