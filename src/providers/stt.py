"""Whisper STT 프로바이더 — faster-whisper (CTranslate2) 백엔드 우선."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..core.config import load_providers
from ..core.exceptions import ProviderError
from .base import STTProvider

logger = logging.getLogger(__name__)


class WhisperSTT(STTProvider):
    """faster-whisper 기반 STT 프로바이더.

    CTranslate2 백엔드로 openai-whisper 대비 3~5배 빠르고 메모리 사용량도 적음.
    faster-whisper 미설치 시 openai-whisper로 자동 폴백.
    """

    def __init__(self) -> None:
        providers = load_providers()
        whisper_cfg = providers.get("stt", {}).get("whisper", {})

        self.model_name = whisper_cfg.get("model", "large-v3")
        self.language = whisper_cfg.get("language", "ko")
        self.device = whisper_cfg.get("device", "cuda")
        self._model = None
        self._backend = ""  # "faster-whisper" 또는 "openai-whisper"
        self._cache: dict[str, list[dict[str, Any]]] = {}

    def _load_model(self) -> Any:
        """모델 지연 로딩 — faster-whisper 우선, 실패 시 openai-whisper 폴백."""
        if self._model is not None:
            return self._model

        # 1순위: faster-whisper (CTranslate2)
        try:
            from faster_whisper import WhisperModel

            # faster-whisper compute_type 결정
            compute_type = "float16" if self.device == "cuda" else "int8"
            logger.info(
                f"Loading faster-whisper: {self.model_name} on "
                f"{self.device} (compute_type={compute_type})"
            )
            self._model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=compute_type,
            )
            self._backend = "faster-whisper"
            logger.info("faster-whisper 로드 완료 (CTranslate2 백엔드)")
            return self._model
        except ImportError:
            logger.info("faster-whisper 미설치, openai-whisper로 폴백")
        except Exception as e:
            logger.warning(f"faster-whisper 로드 실패 ({e}), openai-whisper로 폴백")

        # 2순위: openai-whisper (원본)
        try:
            import whisper
        except ImportError as e:
            raise ProviderError(
                "whisper",
                "whisper not installed. Run: pip install faster-whisper",
                retryable=False,
            ) from e

        logger.info(f"Loading openai-whisper: {self.model_name} on {self.device}")
        self._model = whisper.load_model(self.model_name, device=self.device)
        self._backend = "openai-whisper"
        return self._model

    def transcribe(
        self, audio_path: Path, language: str = ""
    ) -> list[dict[str, Any]]:
        """음성→텍스트. [{text, start, end, confidence}, ...] 반환."""
        language = language or self.language
        cache_key = f"{audio_path}:{language}"

        # 캐시 히트
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            logger.info(
                f"Whisper STT 캐시 히트: {audio_path.name} → "
                f"{len(cached)} segments (재전사 생략)"
            )
            return cached

        if not audio_path.exists():
            raise ProviderError(
                "whisper",
                f"Audio file not found: {audio_path}",
                retryable=False,
            )

        model = self._load_model()

        if self._backend == "faster-whisper":
            segments = self._transcribe_faster(model, audio_path, language)
        else:
            segments = self._transcribe_openai(model, audio_path, language)

        # 캐시 저장
        self._cache[cache_key] = segments
        return segments

    def _transcribe_faster(
        self, model: Any, audio_path: Path, language: str
    ) -> list[dict[str, Any]]:
        """faster-whisper 전사."""
        try:
            raw_segments, info = model.transcribe(
                str(audio_path),
                language=language,
                word_timestamps=True,
                vad_filter=True,  # VAD로 무음 구간 스킵 → 추가 속도 향상
            )

            segments = []
            for seg in raw_segments:
                segments.append({
                    "text": seg.text.strip(),
                    "start": seg.start,
                    "end": seg.end,
                    "confidence": seg.avg_logprob,
                })

            total_dur = segments[-1]["end"] if segments else 0.0
            logger.info(
                f"faster-whisper STT: {audio_path.name} → "
                f"{len(segments)} segments, {total_dur:.1f}s "
                f"(lang={info.language}, prob={info.language_probability:.2f})"
            )
            return segments

        except Exception as e:
            raise ProviderError(
                "whisper",
                f"faster-whisper transcription failed: {e}",
                retryable=False,
            ) from e

    def _transcribe_openai(
        self, model: Any, audio_path: Path, language: str
    ) -> list[dict[str, Any]]:
        """openai-whisper 전사 (폴백)."""
        try:
            result = model.transcribe(
                str(audio_path),
                language=language,
                word_timestamps=True,
                verbose=False,
            )
        except Exception as e:
            raise ProviderError(
                "whisper",
                f"Transcription failed: {e}",
                retryable=False,
            ) from e

        segments = []
        for seg in result.get("segments", []):
            segments.append({
                "text": seg["text"].strip(),
                "start": seg["start"],
                "end": seg["end"],
                "confidence": seg.get("avg_logprob", 0.0),
            })

        total_dur = segments[-1]["end"] if segments else 0.0
        logger.info(
            f"openai-whisper STT: {audio_path.name} → "
            f"{len(segments)} segments, {total_dur:.1f}s"
        )
        return segments

    def transcribe_to_srt(
        self,
        audio_path: Path,
        output_path: Path,
        language: str = "",
        segments: list[dict[str, Any]] | None = None,
    ) -> Path:
        """음성→SRT 파일 생성.

        segments를 외부에서 전달하면 재전사 없이 SRT만 생성.
        """
        if segments is None:
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
