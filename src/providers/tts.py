"""TTS 프로바이더 - ElevenLabs + TypeCast."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from ..core.config import load_env, load_providers
from ..core.exceptions import (
    ContentFilterError,
    ProviderError,
    ProviderTimeoutError,
    QuotaExhaustedError,
    RateLimitError,
)
from ..core.retry import retry
from .base import TTSProvider

logger = logging.getLogger(__name__)

ELEVENLABS_API_BASE = "https://api.elevenlabs.io/v1"


class ElevenLabsTTS(TTSProvider):
    """ElevenLabs TTS 프로바이더."""

    def __init__(self) -> None:
        env = load_env()
        providers = load_providers()
        el_cfg = providers.get("tts", {}).get("elevenlabs", {})

        self.api_key = env.elevenlabs_api_key
        self.model = el_cfg.get("model", "eleven_multilingual_v3")
        self.default_voice_id = env.elevenlabs_voice_id or el_cfg.get("default_voice_id", "")
        self.stability = el_cfg.get("stability", 0.5)
        self.similarity_boost = el_cfg.get("similarity_boost", 0.75)
        self.style = el_cfg.get("style", 0.5)
        self.pricing = el_cfg.get("pricing", {})

    @retry(max_attempts=3, base_delay=2.0)
    async def synthesize(
        self,
        text: str,
        voice_id: str = "",
        output_path: Path | None = None,
        **kwargs: Any,
    ) -> Path:
        """텍스트→음성 합성."""
        voice_id = voice_id or self.default_voice_id
        if not voice_id:
            raise ProviderError("elevenlabs", "No voice_id provided", retryable=False)

        output_path = output_path or Path("output.mp3")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        url = f"{ELEVENLABS_API_BASE}/text-to-speech/{voice_id}"
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        payload = {
            "text": text,
            "model_id": self.model,
            "voice_settings": {
                "stability": kwargs.get("stability", self.stability),
                "similarity_boost": kwargs.get("similarity_boost", self.similarity_boost),
                "style": kwargs.get("style", self.style),
            },
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                resp = await client.post(url, headers=headers, json=payload)
            except httpx.TimeoutException as e:
                raise ProviderTimeoutError("elevenlabs", timeout_seconds=120) from e
            except httpx.ConnectError as e:
                raise ProviderError("elevenlabs", f"Connection failed: {e}", retryable=True) from e

        self._handle_error(resp)

        output_path.write_bytes(resp.content)
        logger.info(f"ElevenLabs TTS: {len(text)} chars → {output_path} ({len(resp.content)} bytes)")
        return output_path

    def estimate_cost(self, text: str) -> float:
        """비용 추정."""
        per_char = self.pricing.get("per_character", 0.00003)
        return len(text) * per_char

    @staticmethod
    def _handle_error(resp: httpx.Response) -> None:
        """HTTP 응답 에러 처리."""
        if resp.status_code == 200:
            return
        if resp.status_code == 401:
            raise ProviderError("elevenlabs", "Authentication failed: invalid API key", retryable=False)
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("retry-after", "60"))
            raise RateLimitError("elevenlabs", retry_after_seconds=retry_after)
        if resp.status_code == 422:
            raise ProviderError("elevenlabs", f"Invalid request: {resp.text}", retryable=False)

        try:
            body = resp.json()
        except Exception:
            body = {"detail": resp.text}

        detail = body.get("detail", {})
        status = detail.get("status") if isinstance(detail, dict) else str(detail)

        if status == "quota_exceeded":
            raise QuotaExhaustedError("elevenlabs")
        if "content" in str(detail).lower() and "filter" in str(detail).lower():
            raise ContentFilterError("elevenlabs", str(detail))

        raise ProviderError(
            "elevenlabs",
            f"HTTP {resp.status_code}: {body}",
            retryable=resp.status_code >= 500,
        )


# ═══ TypeCast TTS ═══

TYPECAST_API_BASE = "https://typecast.ai/api/speak"


class TypeCastTTS(TTSProvider):
    """TypeCast ssfm-v30 TTS 프로바이더 - 한국어 특화 20+ 음성.

    특징:
        - 한국어 자연스러움 최적화
        - 20개 이상의 한국어 음성 지원
        - 감정 파라미터 (happy, sad, angry 등)
    """

    def __init__(self) -> None:
        env = load_env()
        providers = load_providers()
        tc_cfg = providers.get("tts", {}).get("typecast", {})

        self.api_key = env.typecast_api_key
        self.model = tc_cfg.get("model", "ssfm-v30")
        self.default_voice_id = tc_cfg.get("default_voice_id", "")
        self.default_emotion = tc_cfg.get("default_emotion", "neutral")
        self.speed = tc_cfg.get("speed", 1.0)
        self.pitch = tc_cfg.get("pitch", 0)
        self.pricing = tc_cfg.get("pricing", {})

    @retry(max_attempts=3, base_delay=2.0)
    async def synthesize(
        self,
        text: str,
        voice_id: str = "",
        output_path: Path | None = None,
        **kwargs: Any,
    ) -> Path:
        """TypeCast API로 텍스트→음성 합성."""
        voice_id = voice_id or self.default_voice_id
        if not voice_id:
            raise ProviderError("typecast", "No voice_id provided", retryable=False)

        output_path = output_path or Path("output.wav")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if not self.api_key:
            raise ProviderError("typecast", "Authentication failed: no API key configured", retryable=False)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "text": text,
            "model_id": self.model,
            "actor_id": voice_id,
            "emotion": kwargs.get("emotion", self.default_emotion),
            "speed": kwargs.get("speed", self.speed),
            "pitch": kwargs.get("pitch", self.pitch),
            "format": "wav",
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            # Step 1: 합성 요청
            try:
                resp = await client.post(
                    f"{TYPECAST_API_BASE}/generate",
                    headers=headers,
                    json=payload,
                )
            except httpx.TimeoutException as e:
                raise ProviderTimeoutError("typecast", timeout_seconds=120) from e
            except httpx.ConnectError as e:
                raise ProviderError("typecast", f"Connection failed: {e}", retryable=True) from e

            self._handle_error(resp)
            result = resp.json()

            # Step 2: 비동기 결과 다운로드
            audio_url = result.get("audio_url") or result.get("result", {}).get("url")
            speak_id = result.get("speak_id")

            if not audio_url and speak_id:
                audio_url = await self._poll_result(client, headers, speak_id)

            if not audio_url:
                raise ProviderError("typecast", "No audio URL in response", retryable=True)

            # Step 3: 오디오 다운로드
            try:
                audio_resp = await client.get(audio_url, timeout=60.0)
            except httpx.TimeoutException as e:
                raise ProviderTimeoutError("typecast", timeout_seconds=60) from e

            if audio_resp.status_code != 200:
                raise ProviderError(
                    "typecast",
                    f"Audio download failed: {audio_resp.status_code}",
                    retryable=True,
                )

        output_path.write_bytes(audio_resp.content)
        logger.info(f"TypeCast TTS: {len(text)} chars → {output_path} ({len(audio_resp.content)} bytes)")
        return output_path

    async def _poll_result(
        self, client: httpx.AsyncClient, headers: dict, speak_id: str
    ) -> str:
        """비동기 합성 결과 폴링."""
        import asyncio

        for _ in range(30):
            await asyncio.sleep(2)
            try:
                poll_resp = await client.get(
                    f"{TYPECAST_API_BASE}/status/{speak_id}",
                    headers=headers,
                )
            except httpx.TimeoutException:
                continue

            if poll_resp.status_code != 200:
                continue

            data = poll_resp.json()
            status = data.get("status", "").lower()

            if status in ("done", "completed", "ready"):
                url = data.get("audio_url") or data.get("result", {}).get("url")
                if url:
                    return url

            if status in ("failed", "error"):
                raise ProviderError(
                    "typecast",
                    f"Synthesis failed: {data.get('error', 'Unknown')}",
                    retryable=False,
                )

        raise ProviderTimeoutError("typecast", timeout_seconds=60)

    def estimate_cost(self, text: str) -> float:
        """비용 추정."""
        per_char = self.pricing.get("per_character", 0.00005)
        return len(text) * per_char

    @staticmethod
    def _handle_error(resp: httpx.Response) -> None:
        """HTTP 응답 에러 처리."""
        if 200 <= resp.status_code < 300:
            return
        if resp.status_code == 401:
            raise ProviderError("typecast", "Authentication failed: invalid API key", retryable=False)
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("retry-after", "60"))
            raise RateLimitError("typecast", retry_after_seconds=retry_after)
        if resp.status_code == 402 or resp.status_code == 403:
            raise QuotaExhaustedError("typecast")
        if resp.status_code == 422:
            raise ProviderError("typecast", f"Invalid request: {resp.text}", retryable=False)

        try:
            body = resp.json()
        except Exception:
            body = {"error": resp.text}

        raise ProviderError(
            "typecast",
            f"HTTP {resp.status_code}: {body}",
            retryable=resp.status_code >= 500,
        )
