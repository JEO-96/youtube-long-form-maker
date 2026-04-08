"""OpenAI GPT Image 프로바이더 — Flux 대체/fallback 이미지 생성."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
import base64

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

OPENAI_API_BASE = "https://api.openai.com/v1"


class OpenAIImageGen(ImageGenProvider):
    """OpenAI 이미지 생성 프로바이더 (gpt-image-1 / dall-e-3)."""

    def __init__(self) -> None:
        env = load_env()
        providers = load_providers()
        openai_cfg = providers.get("image_gen", {}).get("openai", {})

        self.api_key = env.openai_api_key
        if not self.api_key:
            raise ProviderError(
                "openai_image",
                "OPENAI_API_KEY not set",
                retryable=False,
            )

        self.model = openai_cfg.get("model", "gpt-image-1")
        self.default_size = openai_cfg.get("size", "1536x1024")
        self.quality = openai_cfg.get("quality", "high")
        self.pricing = openai_cfg.get("pricing", {})

    @retry(max_attempts=2, base_delay=3.0)
    async def generate(
        self,
        prompt: str,
        output_path: Path | None = None,
        width: int = 0,
        height: int = 0,
        **kwargs: Any,
    ) -> Path:
        """OpenAI API로 이미지 생성."""
        output_path = output_path or Path("output.png")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # 크기 결정: 요청이 있으면 가장 가까운 지원 사이즈 선택
        size = self._resolve_size(width, height)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt[:4000],  # OpenAI 프롬프트 길이 제한
            "n": 1,
            "size": size,
            "quality": kwargs.get("quality", self.quality),
        }

        # gpt-image-1은 b64_json 출력 지원
        if self.model == "gpt-image-1":
            payload["output_format"] = "png"

        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                resp = await client.post(
                    f"{OPENAI_API_BASE}/images/generations",
                    headers=headers,
                    json=payload,
                )
            except httpx.TimeoutException as e:
                raise ProviderTimeoutError("openai_image", timeout_seconds=120) from e
            except httpx.ConnectError as e:
                raise ProviderError(
                    "openai_image",
                    f"Connection failed: {e}",
                    retryable=True,
                ) from e

            self._handle_error(resp, "generate")

            result = resp.json()
            data_list = result.get("data", [])
            if not data_list:
                raise ProviderError(
                    "openai_image",
                    "No image data in response",
                    retryable=True,
                )

            item = data_list[0]

            # b64_json 응답 처리
            if "b64_json" in item:
                img_bytes = base64.b64decode(item["b64_json"])
                output_path.write_bytes(img_bytes)
            elif "url" in item:
                # URL 응답 → 이미지 다운로드
                img_resp = await client.get(item["url"])
                if img_resp.status_code != 200:
                    raise ProviderError(
                        "openai_image",
                        f"Image download failed: HTTP {img_resp.status_code}",
                        retryable=True,
                    )
                output_path.write_bytes(img_resp.content)
            else:
                raise ProviderError(
                    "openai_image",
                    "Response contains neither b64_json nor url",
                    retryable=False,
                )

        logger.info(
            f"OpenAI image: {output_path} "
            f"({output_path.stat().st_size} bytes, model={self.model})"
        )
        return output_path

    def estimate_cost(self) -> float:
        """이미지 1장 비용 추정."""
        return self.pricing.get("per_image", 0.08)

    def _resolve_size(self, width: int, height: int) -> str:
        """요청 크기를 OpenAI 지원 사이즈로 변환."""
        if width <= 0 and height <= 0:
            return self.default_size

        # gpt-image-1 지원: 1024x1024, 1536x1024, 1024x1536, auto
        # dall-e-3 지원: 1024x1024, 1792x1024, 1024x1792
        if self.model == "gpt-image-1":
            sizes = ["1024x1024", "1536x1024", "1024x1536"]
        else:
            sizes = ["1024x1024", "1792x1024", "1024x1792"]

        # 가로형이면 가로 넓은 사이즈, 세로형이면 세로 넓은 사이즈
        if width > height:
            return sizes[1]  # 가로형
        elif height > width:
            return sizes[2]  # 세로형
        return sizes[0]  # 정방

    @staticmethod
    def _handle_error(resp: httpx.Response, operation: str) -> None:
        """HTTP 응답 에러 처리."""
        if 200 <= resp.status_code < 300:
            return

        if resp.status_code == 401:
            raise ProviderError(
                "openai_image",
                "Authentication failed: invalid OPENAI_API_KEY",
                retryable=False,
            )
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("retry-after", "30"))
            raise RateLimitError("openai_image", retry_after_seconds=retry_after)
        if resp.status_code == 402:
            raise QuotaExhaustedError("openai_image")

        try:
            body = resp.json()
            error_msg = body.get("error", {}).get("message", str(body))
        except Exception:
            error_msg = resp.text

        # Content policy violation
        if resp.status_code == 400 and "content_policy" in error_msg.lower():
            raise ContentFilterError("openai_image", error_msg)

        raise ProviderError(
            "openai_image",
            f"HTTP {resp.status_code} on {operation}: {error_msg}",
            retryable=resp.status_code >= 500,
        )
