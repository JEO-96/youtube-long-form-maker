"""Whisper STT 프로바이더 (로컬 실행)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..core.config import load_providers
from ..core.exceptions import ProviderError
from .base import STTProvider

logger = logging.getLogger(__name__)


class WhisperSTT(STTProvider):
    """OpenAI Whisper 로컬 STT 프로바이더."""

    def __init__(self) -> None:
        providers = load_providers()
        whisper_cfg = providers.get("stt", {}).get("whisper", {})

        self.model_name = whisper_cfg.get("model", "large-v3")
        self.language = whisper_cfg.get("language", "ko")
        self.device = whisper_cfg.get("device", "cpu")
        self._model = None

    def _load_model(self) -> Any:
        """Whisper 모델 지연 로딩."""
        if self._model is None:
            try:
                import whisper
            except ImportError as e:
                raise ProviderError(
                    "whisper",
                    "whisper not installed. Run: pip install openai-whisper",
                    retryable=False,
                ) from e

            logger.info(f"Loading Whisper model: {self.model_name} on {self.device}")
            self._model = whisper.load_model(self.model_name, device=self.device)
        return self._model

    def transcribe(
        self, audio_path: Path, language: str = ""
    ) -> list[dict[str, Any]]:
        """음성→텍스트. [{text, start, end, confidence}, ...] 반환."""
        language = language or self.language

        if not audio_path.exists():
            raise ProviderError("whisper", f"Audio file not found: {audio_path}", retryable=False)

        model = self._load_model()

        try:
            result = model.transcribe(
                str(audio_path),
                language=language,
                word_timestamps=True,
                verbose=False,
            )
        except Exception as e:
            raise ProviderError("whisper", f"Transcription failed: {e}", retryable=False) from e

        segments = []
        for seg in result.get("segments", []):
            segments.append({
                "text": seg["text"].strip(),
                "start": seg["start"],
                "end": seg["end"],
                "confidence": seg.get("avg_logprob", 0.0),
            })

        logger.info(
            f"Whisper STT: {audio_path.name} → {len(segments)} segments, "
            f"total {result.get('segments', [{}])[-1].get('end', 0):.1f}s"
            if segments else f"Whisper STT: {audio_path.name} → 0 segments"
        )
        return segments

    def transcribe_to_srt(self, audio_path: Path, output_path: Path, language: str = "") -> Path:
        """음성→SRT 파일 생성."""
        segments = self.transcribe(audio_path, language)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        lines = []
        for i, seg in enumerate(segments, 1):
            start = self._format_srt_time(seg["start"])
            end = self._format_srt_time(seg["end"])
            lines.append(f"{i}")
            lines.append(f"{start} --> {end}")
            lines.append(seg["text"])
            lines.append("")

        output_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"SRT written: {output_path} ({len(segments)} entries)")
        return output_path

    @staticmethod
    def _format_srt_time(seconds: float) -> str:
        """초→SRT 타임코드 (HH:MM:SS,mmm)."""
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds % 1) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
