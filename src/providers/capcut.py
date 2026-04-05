"""CapCut API 후처리 프로바이더 - 선택적 영상 품질 향상.

원칙:
    "S6의 기준 렌더는 MoviePy/FFmpeg가 담당하고,
     CapCut은 선택적 포스트 프로세싱만 수행한다."

    - MoviePy: 타임라인 조립, 오디오 믹싱, 기본 컷 편집, 자막 합성
    - CapCut: 필터, 전환 효과, AI 자막 스타일 (실패해도 MoviePy 결과물로 진행)

제약:
    - S6 기본 파이프라인을 대체하지 않음
    - 실패해도 원본 MoviePy 결과물 보존
    - 켜면 적용, 끄면 기존과 동일 동작
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx

from ..core.exceptions import ProviderError, ProviderTimeoutError

logger = logging.getLogger(__name__)

CAPCUT_API_BASE = "https://open.capcut.com/api/v1"


class CapCutProvider:
    """CapCut API 포스트 프로세서.

    적용 범위 (포스트 프로세싱만):
        - 필터 적용 (cinematic, warm, cool 등)
        - 전환 효과 (dissolve, slide, zoom 등)
        - AI 자막 스타일링 (트렌디 자막)
        - 색보정 (color grading)

    미적용 (MoviePy 영역):
        - 타임라인 조립
        - 오디오 믹싱
        - 기본 컷 편집
    """

    def __init__(self, api_key: str = "", api_base: str = "") -> None:
        from ..core.config import load_env
        env = load_env()
        self.api_key = api_key or getattr(env, "capcut_api_key", "")
        self.api_base = api_base or CAPCUT_API_BASE

    def is_available(self) -> bool:
        """CapCut API 사용 가능 여부."""
        return bool(self.api_key)

    async def enhance(
        self,
        input_path: Path,
        output_path: Path | None = None,
        filters: list[str] | None = None,
        transitions: bool = True,
        subtitle_style: str = "modern",
        color_grade: str | None = None,
        **kwargs: Any,
    ) -> Path:
        """영상 후처리 적용.

        Args:
            input_path: MoviePy 결과 영상
            output_path: 후처리 결과 경로
            filters: 적용할 필터 목록 ["cinematic", "warm"]
            transitions: 전환 효과 자동 적용
            subtitle_style: 자막 스타일 ("modern", "bold", "minimal")
            color_grade: 색보정 프리셋

        Returns:
            후처리된 영상 경로
        """
        if not self.is_available():
            raise ProviderError(
                "capcut", "API key not configured", retryable=False
            )

        if not input_path.exists():
            raise ProviderError(
                "capcut", f"Input file not found: {input_path}", retryable=False
            )

        if output_path is None:
            output_path = input_path.parent / f"{input_path.stem}_enhanced{input_path.suffix}"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=300.0) as client:
            # Step 1: 영상 업로드
            upload_url = await self._upload_video(client, headers, input_path)

            # Step 2: 후처리 작업 생성
            task_payload: dict[str, Any] = {
                "video_url": upload_url,
                "effects": {
                    "filters": filters or [],
                    "transitions": transitions,
                    "subtitle_style": subtitle_style,
                },
            }
            if color_grade:
                task_payload["effects"]["color_grade"] = color_grade

            try:
                resp = await client.post(
                    f"{self.api_base}/video/enhance",
                    headers=headers,
                    json=task_payload,
                )
            except httpx.TimeoutException as e:
                raise ProviderTimeoutError("capcut", timeout_seconds=300) from e

            self._handle_error(resp)
            result = resp.json()
            task_id = result.get("data", {}).get("task_id") or result.get("task_id")

            if not task_id:
                raise ProviderError("capcut", "No task_id in response", retryable=True)

            # Step 3: 결과 폴링 + 다운로드
            result_url = await self._poll_result(client, headers, task_id)

            try:
                dl_resp = await client.get(result_url, timeout=120.0)
            except httpx.TimeoutException as e:
                raise ProviderTimeoutError("capcut", timeout_seconds=120) from e

            if dl_resp.status_code != 200:
                raise ProviderError(
                    "capcut",
                    f"Download failed: {dl_resp.status_code}",
                    retryable=True,
                )

        output_path.write_bytes(dl_resp.content)
        size_mb = output_path.stat().st_size / (1024 * 1024)
        logger.info(f"CapCut enhance: {output_path} ({size_mb:.1f} MB)")
        return output_path

    async def _upload_video(
        self, client: httpx.AsyncClient, headers: dict, video_path: Path
    ) -> str:
        """영상 업로드 → URL 반환."""
        # 업로드 URL 요청
        try:
            init_resp = await client.post(
                f"{self.api_base}/upload/init",
                headers=headers,
                json={"file_name": video_path.name, "file_size": video_path.stat().st_size},
            )
        except httpx.TimeoutException as e:
            raise ProviderTimeoutError("capcut", timeout_seconds=60) from e

        self._handle_error(init_resp)
        init_data = init_resp.json().get("data", {})
        upload_url = init_data.get("upload_url")
        video_url = init_data.get("video_url", "")

        if upload_url:
            # 파일 업로드
            content = video_path.read_bytes()
            try:
                up_resp = await client.put(
                    upload_url,
                    content=content,
                    headers={"Content-Type": "video/mp4"},
                    timeout=180.0,
                )
            except httpx.TimeoutException as e:
                raise ProviderTimeoutError("capcut", timeout_seconds=180) from e

            if up_resp.status_code not in (200, 201):
                raise ProviderError(
                    "capcut", f"Upload failed: {up_resp.status_code}", retryable=True
                )

        return video_url or upload_url or ""

    async def _poll_result(
        self, client: httpx.AsyncClient, headers: dict, task_id: str
    ) -> str:
        """후처리 결과 폴링."""
        for _ in range(60):
            await asyncio.sleep(5)
            try:
                poll_resp = await client.get(
                    f"{self.api_base}/video/enhance/{task_id}",
                    headers=headers,
                )
            except httpx.TimeoutException:
                continue

            if poll_resp.status_code != 200:
                continue

            data = poll_resp.json().get("data", poll_resp.json())
            status = data.get("status", "").lower()

            if status in ("completed", "done", "ready"):
                url = data.get("result_url") or data.get("video_url")
                if url:
                    return url

            if status in ("failed", "error"):
                raise ProviderError(
                    "capcut",
                    f"Enhancement failed: {data.get('error', 'Unknown')}",
                    retryable=False,
                )

        raise ProviderTimeoutError("capcut", timeout_seconds=300)

    async def enhance_safe(
        self,
        input_path: Path,
        output_path: Path | None = None,
        **kwargs: Any,
    ) -> Path:
        """Graceful enhance — 실패 시 원본 반환.

        파이프라인에서 사용하는 안전 래퍼.
        MoviePy 결과물이 항상 보존됨.
        """
        if not self.is_available():
            logger.info("CapCut not available, using MoviePy output as-is")
            return input_path

        try:
            return await self.enhance(input_path, output_path, **kwargs)
        except Exception as e:
            logger.warning(f"CapCut enhancement failed, using original: {e}")
            return input_path

    @staticmethod
    def _handle_error(resp: httpx.Response) -> None:
        """HTTP 응답 에러 처리."""
        if 200 <= resp.status_code < 300:
            return
        if resp.status_code == 401:
            raise ProviderError("capcut", "Authentication failed", retryable=False)
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("retry-after", "60"))
            from ..core.exceptions import RateLimitError
            raise RateLimitError("capcut", retry_after_seconds=retry_after)

        try:
            body = resp.json()
        except Exception:
            body = {"error": resp.text}

        raise ProviderError(
            "capcut",
            f"HTTP {resp.status_code}: {body}",
            retryable=resp.status_code >= 500,
        )
