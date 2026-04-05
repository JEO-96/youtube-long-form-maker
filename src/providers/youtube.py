"""YouTube Data API v3 프로바이더."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from ..core.config import load_env, load_providers
from ..core.exceptions import (
    ProviderError,
    ProviderTimeoutError,
    QuotaExhaustedError,
    RateLimitError,
)
from ..core.retry import retry

logger = logging.getLogger(__name__)

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"


class YouTubeProvider:
    """YouTube Data API v3 - 검색, 메타데이터, 업로드."""

    def __init__(self) -> None:
        env = load_env()
        providers = load_providers()
        yt_cfg = providers.get("youtube", {})

        self.api_key = env.youtube_api_key
        self.api_version = yt_cfg.get("api_version", "v3")
        self.scopes = yt_cfg.get("scopes", [])

    @retry(max_attempts=3, base_delay=2.0)
    async def search_videos(
        self,
        query: str,
        max_results: int = 10,
        order: str = "viewCount",
        published_after: str = "",
        region_code: str = "KR",
        relevance_language: str = "ko",
    ) -> list[dict[str, Any]]:
        """YouTube 검색 API로 영상 검색."""
        params: dict[str, Any] = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": max_results,
            "order": order,
            "regionCode": region_code,
            "relevanceLanguage": relevance_language,
            "key": self.api_key,
        }
        if published_after:
            params["publishedAfter"] = published_after

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.get(f"{YOUTUBE_API_BASE}/search", params=params)
            except httpx.TimeoutException as e:
                raise ProviderTimeoutError("youtube", timeout_seconds=30) from e
            except httpx.ConnectError as e:
                raise ProviderError("youtube", f"Connection failed: {e}", retryable=True) from e

        self._handle_error(resp)

        data = resp.json()
        results = []
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            results.append({
                "video_id": item.get("id", {}).get("videoId", ""),
                "title": snippet.get("title", ""),
                "description": snippet.get("description", ""),
                "channel_title": snippet.get("channelTitle", ""),
                "published_at": snippet.get("publishedAt", ""),
                "thumbnail_url": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
            })

        logger.info(f"YouTube search: '{query}' → {len(results)} results")
        return results

    @retry(max_attempts=3, base_delay=2.0)
    async def get_video_stats(self, video_ids: list[str]) -> list[dict[str, Any]]:
        """영상 통계 조회 (조회수, 좋아요, 댓글)."""
        if not video_ids:
            return []

        params = {
            "part": "statistics,contentDetails",
            "id": ",".join(video_ids[:50]),
            "key": self.api_key,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.get(f"{YOUTUBE_API_BASE}/videos", params=params)
            except httpx.TimeoutException as e:
                raise ProviderTimeoutError("youtube", timeout_seconds=30) from e

        self._handle_error(resp)

        data = resp.json()
        results = []
        for item in data.get("items", []):
            stats = item.get("statistics", {})
            details = item.get("contentDetails", {})
            results.append({
                "video_id": item.get("id", ""),
                "view_count": int(stats.get("viewCount", 0)),
                "like_count": int(stats.get("likeCount", 0)),
                "comment_count": int(stats.get("commentCount", 0)),
                "duration": details.get("duration", ""),
            })

        logger.info(f"YouTube stats: {len(results)} videos")
        return results

    @retry(max_attempts=2, base_delay=5.0)
    async def upload_video(
        self,
        video_path: Path,
        title: str,
        description: str = "",
        tags: list[str] | None = None,
        category_id: str = "22",
        privacy_status: str = "private",
        access_token: str = "",
    ) -> dict[str, str]:
        """YouTube 영상 업로드 (OAuth2 access_token 필요)."""
        if not access_token:
            raise ProviderError("youtube", "OAuth2 access_token required for upload", retryable=False)

        if not video_path.exists():
            raise ProviderError("youtube", f"Video file not found: {video_path}", retryable=False)

        metadata = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags or [],
                "categoryId": category_id,
            },
            "status": {
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": False,
            },
        }

        import json
        boundary = "ytmaker_upload_boundary"
        video_bytes = video_path.read_bytes()

        body = (
            f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata)}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: video/*\r\n\r\n"
        ).encode("utf-8") + video_bytes + f"\r\n--{boundary}--".encode("utf-8")

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        }

        upload_url = "https://www.googleapis.com/upload/youtube/v3/videos"
        params = {"uploadType": "multipart", "part": "snippet,status"}

        async with httpx.AsyncClient(timeout=600.0) as client:
            try:
                resp = await client.post(
                    upload_url, headers=headers, params=params, content=body
                )
            except httpx.TimeoutException as e:
                raise ProviderTimeoutError("youtube", timeout_seconds=600) from e

        self._handle_error(resp)

        data = resp.json()
        video_id = data.get("id", "")
        logger.info(f"YouTube upload: video_id={video_id}, privacy={privacy_status}")
        return {
            "video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "status": "uploaded",
        }

    @staticmethod
    def _handle_error(resp: httpx.Response) -> None:
        """HTTP 응답 에러 처리."""
        if 200 <= resp.status_code < 300:
            return

        try:
            body = resp.json()
            error = body.get("error", {})
            errors = error.get("errors", [{}])
            reason = errors[0].get("reason", "") if errors else ""
            message = error.get("message", resp.text)
        except Exception:
            reason = ""
            message = resp.text

        if resp.status_code == 401:
            raise ProviderError("youtube", f"Authentication failed: {message}", retryable=False)
        if resp.status_code == 403:
            if reason == "quotaExceeded":
                raise QuotaExhaustedError("youtube")
            if reason == "rateLimitExceeded":
                raise RateLimitError("youtube", retry_after_seconds=60)
            raise ProviderError("youtube", f"Forbidden: {message}", retryable=False)
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("retry-after", "60"))
            raise RateLimitError("youtube", retry_after_seconds=retry_after)

        raise ProviderError(
            "youtube",
            f"HTTP {resp.status_code}: {message}",
            retryable=resp.status_code >= 500,
        )
