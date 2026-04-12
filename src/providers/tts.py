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
from ..core.korean_number import preprocess_korean_numbers
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

    MAX_CHARS = 4800  # ElevenLabs 5000자 제한, 안전 마진

    async def synthesize(
        self,
        text: str,
        voice_id: str = "",
        output_path: Path | None = None,
        **kwargs: Any,
    ) -> Path:
        """텍스트→음성 합성. 5000자 초과 시 자동 청킹 + 결합."""
        voice_id = voice_id or self.default_voice_id
        if not voice_id:
            raise ProviderError("elevenlabs", "No voice_id provided", retryable=False)

        output_path = output_path or Path("output.mp3")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # ═══ 한국어 숫자 발음 전처리 (3가지→세 가지 등) ═══
        text = preprocess_korean_numbers(text)

        if len(text) <= self.MAX_CHARS:
            return await self._synthesize_single(text, voice_id, output_path, **kwargs)

        # ═══ 청킹: 문장 경계에서 분할 → raw MP3 바이트 연결 ═══
        chunks = self._split_text_for_tts(text, self.MAX_CHARS)
        logger.info(f"Text too long ({len(text)} chars), splitting into {len(chunks)} chunks")

        mp3_parts: list[bytes] = []
        for i, chunk in enumerate(chunks):
            chunk_path = output_path.parent / f"_tts_chunk_{i:03d}.mp3"
            await self._synthesize_single(chunk, voice_id, chunk_path, **kwargs)
            mp3_parts.append(chunk_path.read_bytes())
            chunk_path.unlink(missing_ok=True)

        # MP3는 스트리밍 포맷이므로 raw bytes 연결이 가능
        output_path.write_bytes(b"".join(mp3_parts))
        logger.info(f"ElevenLabs TTS: {len(text)} chars ({len(chunks)} chunks) → {output_path}")
        return output_path

    @retry(max_attempts=3, base_delay=2.0)
    async def _synthesize_single(
        self,
        text: str,
        voice_id: str,
        output_path: Path,
        **kwargs: Any,
    ) -> Path:
        """단일 청크 합성."""
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
        logger.info(f"ElevenLabs TTS chunk: {len(text)} chars → {output_path} ({len(resp.content)} bytes)")
        return output_path

    @staticmethod
    def _split_text_for_tts(text: str, max_chars: int) -> list[str]:
        """문장 경계에서 텍스트를 max_chars 이하 청크로 분할."""
        import re
        sentences = re.split(r'(?<=[.!?다요죠까니])\s+', text)
        chunks: list[str] = []
        current = ""
        for sent in sentences:
            if len(current) + len(sent) + 1 > max_chars and current:
                chunks.append(current.strip())
                current = sent
            else:
                current = f"{current} {sent}" if current else sent
        if current.strip():
            chunks.append(current.strip())
        return chunks

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
            # ElevenLabs는 quota 초과도 401로 반환하는 경우가 있음
            try:
                body = resp.json()
                detail = body.get("detail", {})
                if isinstance(detail, dict) and detail.get("status") == "quota_exceeded":
                    raise QuotaExhaustedError(
                        "elevenlabs",
                        message=detail.get("message", "Quota exceeded"),
                    )
            except (QuotaExhaustedError,):
                raise
            except Exception:
                pass
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

TYPECAST_API_BASE = "https://api.typecast.ai"


class TypeCastTTS(TTSProvider):
    """TypeCast v1 TTS 프로바이더 - 한국어 특화 20+ 음성.

    API: POST https://api.typecast.ai/v1/text-to-speech
    인증: X-API-KEY 헤더
    응답: 오디오 바이너리 (WAV/MP3)
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
        """TypeCast v1 API로 텍스트→음성 합성.

        API 스펙:
        - POST /v1/text-to-speech
        - 인증: X-API-KEY 헤더
        - 텍스트 한도: 1~2000자
        - 응답: 오디오 바이너리
        """
        voice_id = voice_id or self.default_voice_id
        if not voice_id:
            raise ProviderError("typecast", "No voice_id provided", retryable=False)

        output_path = output_path or Path("output.wav")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if not self.api_key:
            raise ProviderError("typecast", "Authentication failed: no API key configured", retryable=False)

        headers = {
            "X-API-KEY": self.api_key,
            "Content-Type": "application/json",
        }

        # 텍스트 2000자 제한 — 초과 시 분할 합성
        if len(text) > 2000:
            return await self._synthesize_chunked(text, voice_id, output_path, headers, **kwargs)

        payload: dict[str, Any] = {
            "text": text,
            "voice_id": voice_id,
            "model": self.model,
            "output": {
                "format": "wav",
                "tempo": kwargs.get("speed", self.speed),
                "pitch": kwargs.get("pitch", self.pitch),
            },
        }

        # 감정 설정
        emotion = kwargs.get("emotion", self.default_emotion)
        if emotion and emotion != "neutral":
            payload["prompt"] = {"emotion": emotion}

        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                resp = await client.post(
                    f"{TYPECAST_API_BASE}/v1/text-to-speech",
                    headers=headers,
                    json=payload,
                )
            except httpx.TimeoutException as e:
                raise ProviderTimeoutError("typecast", timeout_seconds=120) from e
            except httpx.ConnectError as e:
                raise ProviderError("typecast", f"Connection failed: {e}", retryable=True) from e

            self._handle_error(resp)

            # 응답이 JSON이면 비동기 폴링, 바이너리면 직접 저장
            content_type = resp.headers.get("content-type", "")
            if "application/json" in content_type:
                result = resp.json()
                audio_url = result.get("audio_url") or result.get("result", {}).get("url")
                speak_id = result.get("speak_id")

                if not audio_url and speak_id:
                    audio_url = await self._poll_result(client, headers, speak_id)

                if not audio_url:
                    raise ProviderError("typecast", f"No audio URL in response: {result}", retryable=True)

                try:
                    audio_resp = await client.get(audio_url, timeout=60.0)
                except httpx.TimeoutException as e:
                    raise ProviderTimeoutError("typecast", timeout_seconds=60) from e

                if audio_resp.status_code != 200:
                    raise ProviderError("typecast", f"Audio download failed: {audio_resp.status_code}", retryable=True)

                output_path.write_bytes(audio_resp.content)
            else:
                # 바이너리 오디오 직접 반환
                output_path.write_bytes(resp.content)

        logger.info(f"TypeCast TTS: {len(text)} chars → {output_path}")
        return output_path

    async def _synthesize_chunked(
        self,
        text: str,
        voice_id: str,
        output_path: Path,
        headers: dict,
        **kwargs: Any,
    ) -> Path:
        """2000자 초과 텍스트를 분할 합성 후 이어붙이기."""
        import re

        # 문장 경계에서 분할
        chunks: list[str] = []
        current = ""
        sentences = re.split(r'(?<=[.!?다요죠까니])\s+', text)
        for sent in sentences:
            if len(current) + len(sent) > 1800 and current:
                chunks.append(current.strip())
                current = sent
            else:
                current = f"{current} {sent}" if current else sent
        if current.strip():
            chunks.append(current.strip())

        # 각 chunk 합성
        chunk_paths: list[Path] = []
        for i, chunk in enumerate(chunks):
            chunk_path = output_path.with_name(f"{output_path.stem}_chunk{i:03d}.wav")
            # 재귀 호출 (2000자 이하이므로 chunked 분기 안 탐)
            await self.synthesize(chunk, voice_id=voice_id, output_path=chunk_path, **kwargs)
            chunk_paths.append(chunk_path)

        # pydub로 이어붙이기
        from pydub import AudioSegment
        combined = AudioSegment.empty()
        for cp in chunk_paths:
            combined += AudioSegment.from_file(str(cp))
            cp.unlink(missing_ok=True)

        combined.export(str(output_path), format="wav")
        logger.info(f"TypeCast TTS chunked: {len(text)} chars, {len(chunks)} chunks → {output_path}")
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
                    f"{TYPECAST_API_BASE}/v1/text-to-speech/{speak_id}",
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
        if resp.status_code in (402, 403):
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
