"""AI 프로바이더 추상 인터페이스 - 모든 어댑터가 구현."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class LLMProvider(ABC):
    """LLM 프로바이더 (Claude, GPT 등)."""

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.7,
        response_format: str | None = None,  # "json" for JSON mode
    ) -> str:
        """텍스트 생성."""
        ...

    @abstractmethod
    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """비용 추정."""
        ...


class TTSProvider(ABC):
    """TTS 프로바이더 (ElevenLabs, TypeCast 등)."""

    @abstractmethod
    async def synthesize(
        self,
        text: str,
        voice_id: str,
        output_path: Path,
        **kwargs: Any,
    ) -> Path:
        """텍스트→음성 합성. 출력 파일 경로 반환."""
        ...

    @abstractmethod
    def estimate_cost(self, text: str) -> float:
        """비용 추정."""
        ...


class STTProvider(ABC):
    """STT 프로바이더 (Whisper 등)."""

    @abstractmethod
    def transcribe(
        self, audio_path: Path, language: str = "ko"
    ) -> list[dict[str, Any]]:
        """음성→텍스트. [{text, start, end, confidence}, ...] 반환."""
        ...


class ImageGenProvider(ABC):
    """이미지 생성 프로바이더 (FLUX.2 등)."""

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        output_path: Path,
        width: int = 1920,
        height: int = 1080,
        **kwargs: Any,
    ) -> Path:
        """이미지 생성. 출력 파일 경로 반환."""
        ...

    @abstractmethod
    def estimate_cost(self) -> float:
        """이미지 1장 비용 추정."""
        ...


class VideoGenProvider(ABC):
    """영상 생성 프로바이더 (Grok Imagine, Kling 등)."""

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        output_path: Path,
        reference_image: Path | None = None,
        duration_seconds: int = 10,
        **kwargs: Any,
    ) -> Path:
        """영상 생성. 출력 파일 경로 반환."""
        ...

    @abstractmethod
    def estimate_cost(self, duration_seconds: int) -> float:
        """비용 추정."""
        ...


class StockMediaProvider(ABC):
    """스톡 미디어 프로바이더 (Pexels, Pixabay 등)."""

    @abstractmethod
    async def search_videos(
        self,
        query: str,
        min_duration: int = 5,
        orientation: str = "landscape",
        per_page: int = 5,
    ) -> list[dict[str, Any]]:
        """스톡 영상 검색. [{url, duration, width, height}, ...] 반환."""
        ...

    @abstractmethod
    async def download(self, url: str, output_path: Path) -> Path:
        """스톡 영상 다운로드."""
        ...
