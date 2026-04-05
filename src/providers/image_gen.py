"""FLUX.2 이미지 생성 프로바이더 (MVP 1차)."""

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
from .base import ImageGenProvider

logger = logging.getLogger(__name__)

FLUX_API_BASE = "https://api.bfl.ml/v1"


class FluxImageGen(ImageGenProvider):
    """FLUX.2 Pro 이미지 생성 프로바이더."""

    def __init__(self) -> None:
        env = load_env()
        providers = load_providers()
        flux_cfg = providers.get("image_gen", {}).get("flux", {})

        self.api_key = env.flux_api_key
        self.model = flux_cfg.get("model", "flux-2-pro")
        self.default_width = flux_cfg.get("width", 1920)
        self.default_height = flux_cfg.get("height", 1080)
        self.steps = flux_cfg.get("steps", 30)
        self.guidance = flux_cfg.get("guidance", 7.5)
        self.pricing = flux_cfg.get("pricing", {})

    @retry(max_attempts=3, base_delay=3.0)
    async def generate(
        self,
        prompt: str,
        output_path: Path | None = None,
        width: int = 0,
        height: int = 0,
        **kwargs: Any,
    ) -> Path:
        """FLUX.2 API로 이미지 생성."""
        output_path = output_path or Path("output.png")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        width = width or self.default_width
        height = height or self.default_height

        headers = {
            "X-Key": self.api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "prompt": prompt,
            "width": width,
            "height": height,
            "steps": kwargs.get("steps", self.steps),
            "guidance": kwargs.get("guidance", self.guidance),
        }

        async with httpx.AsyncClient(timeout=180.0) as client:
            # 1) 생성 요청
            try:
                resp = await client.post(
                    f"{FLUX_API_BASE}/{self.model}",
                    headers=headers,
                    json=payload,
                )
            except httpx.TimeoutException as e:
                raise ProviderTimeoutError("flux", timeout_seconds=180) from e
            except httpx.ConnectError as e:
                raise ProviderError("flux", f"Connection failed: {e}", retryable=True) from e

            self._handle_error(resp, "generate")
            task_id = resp.json().get("id")

            if not task_id:
                raise ProviderError("flux", "No task_id in response", retryable=True)

            # 2) 결과 폴링
            import asyncio
            for _ in range(60):
                await asyncio.sleep(3)
                try:
                    poll_resp = await client.get(
                        f"{FLUX_API_BASE}/get_result",
                        params={"id": task_id},
                        headers=headers,
                    )
                except httpx.TimeoutException:
                    continue

                if poll_resp.status_code != 200:
                    continue

                result = poll_resp.json()
                status = result.get("status")

                if status == "Ready":
                    image_url = result.get("result", {}).get("sample")
                    if not image_url:
                        raise ProviderError("flux", "No image URL in result", retryable=True)

                    # 3) 이미지 다운로드
                    img_resp = await client.get(image_url)
                    if img_resp.status_code != 200:
                        raise ProviderError("flux", f"Image download failed: {img_resp.status_code}", retryable=True)

                    output_path.write_bytes(img_resp.content)
                    logger.info(f"FLUX.2 image: {output_path} ({len(img_resp.content)} bytes)")
                    return output_path

                if status == "Error":
                    error_msg = result.get("result", {}).get("error", "Unknown error")
                    if "content" in error_msg.lower() or "nsfw" in error_msg.lower():
                        raise ContentFilterError("flux", error_msg)
                    raise ProviderError("flux", f"Generation failed: {error_msg}", retryable=False)

            raise ProviderTimeoutError("flux", timeout_seconds=180)

    def estimate_cost(self) -> float:
        """이미지 1장 비용 추정."""
        return self.pricing.get("per_image", 0.05)

    @staticmethod
    def _handle_error(resp: httpx.Response, operation: str) -> None:
        """HTTP 응답 에러 처리."""
        if 200 <= resp.status_code < 300:
            return
        if resp.status_code == 401:
            raise ProviderError("flux", "Authentication failed: invalid API key", retryable=False)
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("retry-after", "30"))
            raise RateLimitError("flux", retry_after_seconds=retry_after)
        if resp.status_code == 402:
            raise QuotaExhaustedError("flux")

        try:
            body = resp.json()
        except Exception:
            body = {"error": resp.text}

        raise ProviderError(
            "flux",
            f"HTTP {resp.status_code} on {operation}: {body}",
            retryable=resp.status_code >= 500,
        )
