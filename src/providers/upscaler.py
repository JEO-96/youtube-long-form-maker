"""Topaz Video AI CLI 업스케일러 - 선택적 영상 품질 향상.

제약:
    - S6 기본 파이프라인을 대체하지 않음
    - MoviePy 결과물에 대한 optional enhancer
    - 실패해도 원본 유지 (graceful fallback)
    - Topaz Video AI가 설치되어 있어야 동작
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

from ..core.exceptions import ProviderError

logger = logging.getLogger(__name__)

# Topaz Video AI CLI 기본 경로 (Windows)
DEFAULT_TOPAZ_PATHS = [
    r"C:\Program Files\Topaz Labs LLC\Topaz Video AI\ffmpeg.exe",
    r"C:\Program Files\Topaz Labs\Topaz Video AI\ffmpeg.exe",
]


class TopazUpscaler:
    """Topaz Video AI CLI 업스케일러.

    특징:
        - 단순 업스케일이 아닌 디테일 복원 (환각 기반)
        - 720p → 1080p+ 디테일 복원
        - CLI 기반이므로 Topaz 설치 필수
        - 실패 시 원본 파일 보존 (graceful fallback)
    """

    def __init__(self, topaz_path: str | None = None) -> None:
        self.topaz_path = self._find_topaz(topaz_path)
        self.available = self.topaz_path is not None

    @staticmethod
    def _find_topaz(custom_path: str | None = None) -> str | None:
        """Topaz Video AI CLI 경로 탐색."""
        if custom_path and Path(custom_path).exists():
            return custom_path

        for path in DEFAULT_TOPAZ_PATHS:
            if Path(path).exists():
                return path

        # PATH에서 검색
        topaz_in_path = shutil.which("tvai")
        if topaz_in_path:
            return topaz_in_path

        return None

    def is_available(self) -> bool:
        """Topaz CLI 사용 가능 여부."""
        return self.available

    async def upscale(
        self,
        input_path: Path,
        output_path: Path | None = None,
        target_resolution: str = "1080p",
        model: str = "prob-4",
        gpu_id: int = 0,
        **kwargs: Any,
    ) -> Path:
        """영상 업스케일링.

        Args:
            input_path: 입력 영상 경로
            output_path: 출력 경로 (None이면 input_upscaled.mp4)
            target_resolution: 목표 해상도 ("1080p", "1440p", "4k")
            model: Topaz AI 모델 (prob-4, ahq-12 등)
            gpu_id: GPU ID

        Returns:
            업스케일된 영상 경로

        Raises:
            ProviderError: Topaz 미설치 또는 실행 실패
        """
        if not self.available:
            raise ProviderError(
                "topaz", "Topaz Video AI not installed", retryable=False
            )

        if not input_path.exists():
            raise ProviderError(
                "topaz", f"Input file not found: {input_path}", retryable=False
            )

        if output_path is None:
            output_path = input_path.parent / f"{input_path.stem}_upscaled{input_path.suffix}"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # 해상도 매핑
        res_map = {
            "1080p": (1920, 1080),
            "1440p": (2560, 1440),
            "4k": (3840, 2160),
        }
        width, height = res_map.get(target_resolution, (1920, 1080))

        # Topaz CLI 명령 구성
        cmd = [
            str(self.topaz_path),
            "-i", str(input_path),
            "-o", str(output_path),
            "-model", model,
            "-w", str(width),
            "-h", str(height),
            "-gpu", str(gpu_id),
        ]

        logger.info(f"Topaz upscale: {input_path} → {target_resolution}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=kwargs.get("timeout", 600)
            )

            if proc.returncode != 0:
                error = stderr.decode(errors="replace")
                raise ProviderError(
                    "topaz",
                    f"Upscale failed (exit {proc.returncode}): {error[:200]}",
                    retryable=False,
                )

            if not output_path.exists():
                raise ProviderError(
                    "topaz", "Output file not created", retryable=False
                )

            size_mb = output_path.stat().st_size / (1024 * 1024)
            logger.info(f"Topaz upscale done: {output_path} ({size_mb:.1f} MB)")
            return output_path

        except asyncio.TimeoutError:
            raise ProviderError(
                "topaz", "Upscale timed out", retryable=False
            )
        except FileNotFoundError:
            raise ProviderError(
                "topaz",
                f"Topaz executable not found: {self.topaz_path}",
                retryable=False,
            )

    async def upscale_safe(
        self,
        input_path: Path,
        output_path: Path | None = None,
        **kwargs: Any,
    ) -> Path:
        """Graceful upscale — 실패 시 원본 반환.

        파이프라인에서 사용하는 안전 래퍼.
        """
        if not self.available:
            logger.info("Topaz not available, using original file")
            return input_path

        try:
            return await self.upscale(input_path, output_path, **kwargs)
        except Exception as e:
            logger.warning(f"Topaz upscale failed, using original: {e}")
            return input_path
