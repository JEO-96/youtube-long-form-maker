"""Video Generation 프로바이더 - Grok Imagine + Kling 3.0."""

from __future__ import annotations

import asyncio
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
from .base import VideoGenProvider

logger = logging.getLogger(__name__)

XAI_API_BASE = "https://api.x.ai/v1"


class GrokVideoGen(VideoGenProvider):
    """Grok Imagine Video 프로바이더 (xAI API)."""

    def __init__(self) -> None:
        env = load_env()
        providers = load_providers()
        grok_cfg = providers.get("video_gen", {}).get("grok", {})

        self.api_key = env.xai_api_key
        self.model = grok_cfg.get("model", "grok-imagine-video")
        self.default_duration = grok_cfg.get("duration_seconds", 10)
        self.resolution = grok_cfg.get("resolution", "720p")
        self.include_audio = grok_cfg.get("include_audio", True)
        self.pricing = grok_cfg.get("pricing", {})

    @retry(max_attempts=3, base_delay=5.0, max_delay=120.0)
    async def generate(
        self,
        prompt: str,
        output_path: Path | None = None,
        reference_image: Path | None = None,
        duration_seconds: int = 0,
        **kwargs: Any,
    ) -> Path:
        """Grok API로 영상 생성."""
        output_path = output_path or Path("output.mp4")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        duration_seconds = duration_seconds or self.default_duration

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "duration_seconds": duration_seconds,
            "resolution": self.resolution,
            "include_audio": kwargs.get("include_audio", self.include_audio),
        }

        # Image-to-Video: reference_image가 있으면 base64 인코딩
        if reference_image and reference_image.exists():
            import base64
            img_bytes = reference_image.read_bytes()
            payload["reference_image"] = base64.b64encode(img_bytes).decode()

        async with httpx.AsyncClient(timeout=300.0) as client:
            # 1) 생성 요청
            try:
                resp = await client.post(
                    f"{XAI_API_BASE}/video/generations",
                    headers=headers,
                    json=payload,
                )
            except httpx.TimeoutException as e:
                raise ProviderTimeoutError("grok", timeout_seconds=300) from e
            except httpx.ConnectError as e:
                raise ProviderError("grok", f"Connection failed: {e}", retryable=True) from e

            self._handle_error(resp, "generate")
            result = resp.json()

            # 동기 응답인 경우 (직접 URL 반환)
            video_url = None
            if "url" in result:
                video_url = result["url"]
            elif "data" in result and result["data"]:
                video_url = result["data"][0].get("url")

            # 비동기 응답인 경우 (task_id로 폴링)
            task_id = result.get("id") or result.get("task_id")
            if not video_url and task_id:
                video_url = await self._poll_result(client, headers, task_id)

            if not video_url:
                raise ProviderError("grok", "No video URL in response", retryable=True)

            # 2) 비디오 다운로드
            try:
                video_resp = await client.get(video_url, timeout=120.0)
            except httpx.TimeoutException as e:
                raise ProviderTimeoutError("grok", timeout_seconds=120) from e

            if video_resp.status_code != 200:
                raise ProviderError("grok", f"Video download failed: {video_resp.status_code}", retryable=True)

            output_path.write_bytes(video_resp.content)
            logger.info(f"Grok video: {output_path} ({len(video_resp.content)} bytes)")
            return output_path

    async def _poll_result(
        self, client: httpx.AsyncClient, headers: dict, task_id: str
    ) -> str:
        """비동기 작업 결과 폴링."""
        for _ in range(60):
            await asyncio.sleep(5)
            try:
                poll_resp = await client.get(
                    f"{XAI_API_BASE}/video/generations/{task_id}",
                    headers=headers,
                )
            except httpx.TimeoutException:
                continue

            if poll_resp.status_code != 200:
                continue

            data = poll_resp.json()
            status = data.get("status", "").lower()

            if status in ("completed", "ready", "succeeded"):
                url = data.get("url") or data.get("result", {}).get("url")
                if data.get("data"):
                    url = url or data["data"][0].get("url")
                if url:
                    return url

            if status in ("failed", "error"):
                error_msg = data.get("error", "Unknown error")
                if "content" in str(error_msg).lower() or "safety" in str(error_msg).lower():
                    raise ContentFilterError("grok", str(error_msg))
                raise ProviderError("grok", f"Generation failed: {error_msg}", retryable=False)

        raise ProviderTimeoutError("grok", timeout_seconds=300)

    def estimate_cost(self, duration_seconds: int = 0) -> float:
        """비용 추정."""
        return self.pricing.get("per_video", 0.15)

    @staticmethod
    def _handle_error(resp: httpx.Response, operation: str) -> None:
        """HTTP 응답 에러 처리."""
        if 200 <= resp.status_code < 300:
            return
        if resp.status_code == 401:
            raise ProviderError("grok", "Authentication failed: invalid API key", retryable=False)
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("retry-after", "60"))
            raise RateLimitError("grok", retry_after_seconds=retry_after)
        if resp.status_code == 402 or resp.status_code == 403:
            raise QuotaExhaustedError("grok")

        try:
            body = resp.json()
        except Exception:
            body = {"error": resp.text}

        error_msg = str(body)
        if "content" in error_msg.lower() or "safety" in error_msg.lower():
            raise ContentFilterError("grok", error_msg)

        raise ProviderError(
            "grok",
            f"HTTP {resp.status_code} on {operation}: {body}",
            retryable=resp.status_code >= 500,
        )


# ═══ Kling 3.0 ═══

KLING_API_BASE = "https://api.klingai.com/v1"


class KlingVideoGen(VideoGenProvider):
    """Kling 3.0 Video 프로바이더 - 시네마틱 고품질 영상.

    특징:
        - 시네마틱 스타일 우수
        - 1080p 지원
        - Image-to-Video, Text-to-Video 모두 지원
        - Grok 대비 느리지만 품질 높음
    """

    def __init__(self) -> None:
        env = load_env()
        providers = load_providers()
        kling_cfg = providers.get("video_gen", {}).get("kling", {})

        self.api_key = env.kling_api_key
        self.model = kling_cfg.get("model", "kling-v3")
        self.default_duration = kling_cfg.get("duration_seconds", 10)
        self.resolution = kling_cfg.get("resolution", "1080p")
        self.mode = kling_cfg.get("mode", "standard")  # standard, professional
        self.pricing = kling_cfg.get("pricing", {})

    @retry(max_attempts=3, base_delay=5.0, max_delay=120.0)
    async def generate(
        self,
        prompt: str,
        output_path: Path | None = None,
        reference_image: Path | None = None,
        duration_seconds: int = 0,
        **kwargs: Any,
    ) -> Path:
        """Kling API로 영상 생성."""
        output_path = output_path or Path("output.mp4")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        duration_seconds = duration_seconds or self.default_duration

        if not self.api_key:
            raise ProviderError("kling", "Authentication failed: no API key configured", retryable=False)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # Image-to-Video vs Text-to-Video
        if reference_image and reference_image.exists():
            endpoint = f"{KLING_API_BASE}/videos/image2video"
            import base64
            img_bytes = reference_image.read_bytes()
            payload: dict[str, Any] = {
                "model_name": self.model,
                "prompt": prompt,
                "image": base64.b64encode(img_bytes).decode(),
                "duration": str(duration_seconds),
                "mode": kwargs.get("mode", self.mode),
            }
        else:
            endpoint = f"{KLING_API_BASE}/videos/text2video"
            payload = {
                "model_name": self.model,
                "prompt": prompt,
                "duration": str(duration_seconds),
                "mode": kwargs.get("mode", self.mode),
                "aspect_ratio": kwargs.get("aspect_ratio", "16:9"),
            }

        async with httpx.AsyncClient(timeout=300.0) as client:
            # 1) 생성 요청
            try:
                resp = await client.post(endpoint, headers=headers, json=payload)
            except httpx.TimeoutException as e:
                raise ProviderTimeoutError("kling", timeout_seconds=300) from e
            except httpx.ConnectError as e:
                raise ProviderError("kling", f"Connection failed: {e}", retryable=True) from e

            self._handle_error(resp, "generate")
            result = resp.json()

            # task_id 추출
            task_id = (
                result.get("data", {}).get("task_id")
                or result.get("task_id")
                or result.get("id")
            )
            if not task_id:
                raise ProviderError("kling", "No task_id in response", retryable=True)

            # 2) 폴링
            video_url = await self._poll_result(client, headers, task_id)

            # 3) 비디오 다운로드
            try:
                video_resp = await client.get(video_url, timeout=120.0)
            except httpx.TimeoutException as e:
                raise ProviderTimeoutError("kling", timeout_seconds=120) from e

            if video_resp.status_code != 200:
                raise ProviderError(
                    "kling", f"Video download failed: {video_resp.status_code}", retryable=True
                )

        output_path.write_bytes(video_resp.content)
        logger.info(f"Kling video: {output_path} ({len(video_resp.content)} bytes)")
        return output_path

    async def _poll_result(
        self, client: httpx.AsyncClient, headers: dict, task_id: str
    ) -> str:
        """비동기 작업 결과 폴링."""
        for _ in range(90):  # Kling은 Grok보다 느림 → 더 긴 폴링
            await asyncio.sleep(5)
            try:
                poll_resp = await client.get(
                    f"{KLING_API_BASE}/videos/{task_id}",
                    headers=headers,
                )
            except httpx.TimeoutException:
                continue

            if poll_resp.status_code != 200:
                continue

            data = poll_resp.json()
            task_data = data.get("data", data)
            status = task_data.get("task_status", "").lower()

            if status in ("succeed", "completed", "done"):
                # Kling 응답 구조: data.works[0].resource.resource
                works = task_data.get("works") or task_data.get("task_result", {}).get("videos", [])
                if works:
                    url = (
                        works[0].get("resource", {}).get("resource")
                        or works[0].get("url")
                    )
                    if url:
                        return url
                # 직접 URL
                url = task_data.get("video_url") or task_data.get("url")
                if url:
                    return url

                raise ProviderError("kling", "Completed but no video URL found", retryable=True)

            if status in ("failed", "error"):
                error_msg = task_data.get("task_status_msg", "Unknown error")
                if "content" in str(error_msg).lower() or "safety" in str(error_msg).lower():
                    raise ContentFilterError("kling", str(error_msg))
                raise ProviderError("kling", f"Generation failed: {error_msg}", retryable=False)

        raise ProviderTimeoutError("kling", timeout_seconds=450)

    def estimate_cost(self, duration_seconds: int = 0) -> float:
        """비용 추정."""
        # Kling은 duration 기반 과금
        duration_seconds = duration_seconds or self.default_duration
        per_second = self.pricing.get("per_second", 0.03)
        return duration_seconds * per_second

    @staticmethod
    def _handle_error(resp: httpx.Response, operation: str) -> None:
        """HTTP 응답 에러 처리."""
        if 200 <= resp.status_code < 300:
            return
        if resp.status_code == 401:
            raise ProviderError("kling", "Authentication failed: invalid API key", retryable=False)
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("retry-after", "60"))
            raise RateLimitError("kling", retry_after_seconds=retry_after)
        if resp.status_code in (402, 403):
            raise QuotaExhaustedError("kling")

        try:
            body = resp.json()
        except Exception:
            body = {"error": resp.text}

        error_msg = str(body)
        if "content" in error_msg.lower() or "safety" in error_msg.lower():
            raise ContentFilterError("kling", error_msg)

        raise ProviderError(
            "kling",
            f"HTTP {resp.status_code} on {operation}: {body}",
            retryable=resp.status_code >= 500,
        )
