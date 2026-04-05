"""Pexels 스톡 미디어 프로바이더 (MVP 1차)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from ..core.config import load_env, load_providers
from ..core.exceptions import (
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
)
from ..core.retry import retry
from .base import StockMediaProvider

logger = logging.getLogger(__name__)

PEXELS_API_BASE = "https://api.pexels.com"


class PexelsStockMedia(StockMediaProvider):
    """Pexels 스톡 영상 검색/다운로드 프로바이더."""

    def __init__(self) -> None:
        env = load_env()
        providers = load_providers()
        pexels_cfg = providers.get("stock_media", {}).get("pexels", {})

        self.api_key = env.pexels_api_key
        self.min_width = pexels_cfg.get("min_width", 1920)
        self.min_duration = pexels_cfg.get("min_duration", 5)
        self.orientation = pexels_cfg.get("orientation", "landscape")
        self.per_page = pexels_cfg.get("per_page", 10)

    @retry(max_attempts=3, base_delay=2.0)
    async def search_videos(
        self,
        query: str,
        min_duration: int = 0,
        orientation: str = "",
        per_page: int = 0,
    ) -> list[dict[str, Any]]:
        """Pexels API로 스톡 영상 검색."""
        min_duration = min_duration or self.min_duration
        orientation = orientation or self.orientation
        per_page = per_page or self.per_page

        headers = {"Authorization": self.api_key}
        params = {
            "query": query,
            "orientation": orientation,
            "per_page": per_page,
            "size": "large",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.get(
                    f"{PEXELS_API_BASE}/videos/search",
                    headers=headers,
                    params=params,
                )
            except httpx.TimeoutException as e:
                raise ProviderTimeoutError("pexels", timeout_seconds=30) from e
            except httpx.ConnectError as e:
                raise ProviderError("pexels", f"Connection failed: {e}", retryable=True) from e

        self._handle_error(resp)

        data = resp.json()
        videos = []
        for v in data.get("videos", []):
            duration = v.get("duration", 0)
            if duration < min_duration:
                continue

            # 최고 품질 비디오 파일 선택
            best_file = self._select_best_file(v.get("video_files", []))
            if not best_file:
                continue

            videos.append({
                "id": v.get("id"),
                "url": best_file["link"],
                "duration": duration,
                "width": best_file.get("width", 0),
                "height": best_file.get("height", 0),
                "quality": best_file.get("quality", ""),
                "photographer": v.get("user", {}).get("name", ""),
                "pexels_url": v.get("url", ""),
            })

        logger.info(f"Pexels search: '{query}' → {len(videos)} results (filtered from {len(data.get('videos', []))})")
        return videos

    @retry(max_attempts=3, base_delay=2.0)
    async def download(self, url: str, output_path: Path | None = None) -> Path:
        """스톡 영상 다운로드."""
        output_path = output_path or Path("stock_video.mp4")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            try:
                resp = await client.get(url)
            except httpx.TimeoutException as e:
                raise ProviderTimeoutError("pexels", timeout_seconds=120) from e
            except httpx.ConnectError as e:
                raise ProviderError("pexels", f"Download failed: {e}", retryable=True) from e

        if resp.status_code != 200:
            raise ProviderError("pexels", f"Download HTTP {resp.status_code}", retryable=True)

        output_path.write_bytes(resp.content)
        logger.info(f"Pexels download: {output_path} ({len(resp.content)} bytes)")
        return output_path

    def _select_best_file(self, files: list[dict]) -> dict | None:
        """가장 높은 해상도의 비디오 파일 선택."""
        candidates = [f for f in files if f.get("width", 0) >= self.min_width]
        if not candidates:
            candidates = sorted(files, key=lambda f: f.get("width", 0), reverse=True)
        if not candidates:
            return None
        return candidates[0]

    @staticmethod
    def _handle_error(resp: httpx.Response) -> None:
        """HTTP 응답 에러 처리."""
        if 200 <= resp.status_code < 300:
            return
        if resp.status_code == 401:
            raise ProviderError("pexels", "Authentication failed: invalid API key", retryable=False)
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("retry-after", "60"))
            raise RateLimitError("pexels", retry_after_seconds=retry_after)

        raise ProviderError(
            "pexels",
            f"HTTP {resp.status_code}: {resp.text}",
            retryable=resp.status_code >= 500,
        )
