"""S5 미디어 생성 - 씬별 이미지/영상/스톡."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from ..core.models import (
    Stage, StoryboardResult, Scene, MediaResult, MediaAsset, MediaType,
)
from ..core.config import load_settings
from .base_stage import BaseStage

logger = logging.getLogger(__name__)


class S5Media(BaseStage):
    """S5: 씬별 미디어 자산 생성."""

    stage = Stage.MEDIA

    async def run(self, **kwargs: Any) -> MediaResult:
        sb_data = self.load_previous(Stage.STORYBOARD)
        storyboard = StoryboardResult(**sb_data)

        settings = load_settings()
        concurrency = settings.media_generation.concurrency_limit
        semaphore = asyncio.Semaphore(concurrency)

        self.stage_dir.mkdir(parents=True, exist_ok=True)
        assets: list[MediaAsset] = []
        failed_scenes: list[int] = []
        total_cost = 0.0

        async def generate_scene(scene: Scene) -> MediaAsset | None:
            async with semaphore:
                try:
                    asset = await self._generate_single(scene)
                    return asset
                except Exception as e:
                    logger.error(f"Scene {scene.scene_number} failed: {e}")
                    failed_scenes.append(scene.scene_number)
                    return None

        # 병렬 생성
        tasks = [generate_scene(s) for s in storyboard.scenes]
        results = await asyncio.gather(*tasks)

        for r in results:
            if r:
                assets.append(r)
                total_cost += r.generation_cost

        return MediaResult(
            assets=assets,
            total_cost=round(total_cost, 4),
            failed_scenes=failed_scenes,
        )

    async def _generate_single(self, scene: Scene) -> MediaAsset:
        """단일 씬 미디어 생성."""
        scene_num = scene.scene_number

        if scene.media_type == MediaType.AI_IMAGE:
            return await self._generate_image(scene)
        elif scene.media_type == MediaType.AI_VIDEO:
            return await self._generate_video(scene)
        elif scene.media_type == MediaType.STOCK_VIDEO:
            return await self._generate_stock(scene)
        else:
            return await self._generate_image(scene)

    async def _generate_image(self, scene: Scene) -> MediaAsset:
        """AI 이미지 생성 (FLUX.2) 또는 mock."""
        out_path = self.stage_dir / f"scene_{scene.scene_number:03d}.png"

        if self.dry_run:
            self._create_placeholder_image(out_path, scene)
            return MediaAsset(
                scene_number=scene.scene_number, media_type=MediaType.AI_IMAGE,
                file_path=str(out_path), original_resolution=[1920, 1080],
                provider="mock_flux",
            )

        from ..providers.image_gen import FluxImageGen
        img_gen = FluxImageGen()
        await img_gen.generate(scene.image_prompt, out_path)
        cost = img_gen.estimate_cost()
        self.record_cost("flux", "generate_image", units=1, unit_cost=cost)

        return MediaAsset(
            scene_number=scene.scene_number, media_type=MediaType.AI_IMAGE,
            file_path=str(out_path), original_resolution=[1920, 1080],
            generation_cost=cost, provider="flux",
        )

    async def _generate_video(self, scene: Scene) -> MediaAsset:
        """AI 영상 생성 (Grok) 또는 mock."""
        out_path = self.stage_dir / f"scene_{scene.scene_number:03d}.mp4"

        if self.dry_run:
            self._create_placeholder_video(out_path, scene)
            return MediaAsset(
                scene_number=scene.scene_number, media_type=MediaType.AI_VIDEO,
                file_path=str(out_path), original_resolution=[1280, 720],
                provider="mock_grok",
            )

        # 채널 설정 기반 VideoGen 선택 (기본: grok)
        from ..providers.factory import create_video_gen
        vg_provider_name = self.channel.providers.video_gen
        vid_gen = create_video_gen(vg_provider_name, fallback="grok")
        duration = min(int(scene.duration), 10)
        await vid_gen.generate(scene.video_prompt, out_path, duration_seconds=duration)
        cost = vid_gen.estimate_cost(duration)
        self.record_cost(vg_provider_name, "generate_video", units=1, unit_cost=cost)

        return MediaAsset(
            scene_number=scene.scene_number, media_type=MediaType.AI_VIDEO,
            file_path=str(out_path), original_resolution=[1280, 720],
            generation_cost=cost, provider="grok",
        )

    async def _generate_stock(self, scene: Scene) -> MediaAsset:
        """스톡 영상 (Pexels) 또는 mock → 이미지 fallback."""
        out_path = self.stage_dir / f"scene_{scene.scene_number:03d}_stock.mp4"

        if self.dry_run:
            # mock: 이미지 placeholder
            img_path = self.stage_dir / f"scene_{scene.scene_number:03d}_stock.png"
            self._create_placeholder_image(img_path, scene, label="STOCK")
            return MediaAsset(
                scene_number=scene.scene_number, media_type=MediaType.STOCK_VIDEO,
                file_path=str(img_path), provider="mock_pexels",
            )

        from ..providers.stock_media import PexelsStockMedia
        stock = PexelsStockMedia()
        query = scene.stock_search_query or scene.visual_description[:30]
        results = await stock.search_videos(query)
        self.record_cost("pexels", "search", units=1, unit_cost=0.0)

        if results:
            url = results[0]["url"]
            await stock.download(url, out_path)
            return MediaAsset(
                scene_number=scene.scene_number, media_type=MediaType.STOCK_VIDEO,
                file_path=str(out_path), provider="pexels",
            )

        # fallback: AI 이미지
        logger.warning(f"Scene {scene.scene_number}: no stock found, falling back to AI image")
        return await self._generate_image(scene)

    def _create_placeholder_image(
        self, path: Path, scene: Scene, label: str = ""
    ) -> None:
        """테스트용 placeholder 이미지."""
        img = Image.new("RGB", (1920, 1080), color=(30, 60, 90))
        draw = ImageDraw.Draw(img)
        text = f"Scene {scene.scene_number}\n{scene.media_type.value}\n{label}"
        try:
            draw.text((100, 400), text, fill="white")
        except Exception:
            pass
        img.save(str(path))

    def _create_placeholder_video(self, path: Path, scene: Scene) -> None:
        """테스트용 placeholder 비디오 (짧은 mp4)."""
        try:
            from moviepy import ColorClip
            duration = min(scene.duration, 3.0)
            clip = ColorClip(size=(1280, 720), color=(30, 60, 90), duration=duration)
            clip.write_videofile(
                str(path), fps=24, codec="libx264",
                audio=False, logger=None,
            )
            clip.close()
        except Exception as e:
            # moviepy 실패 시 빈 파일
            logger.warning(f"Placeholder video creation failed: {e}")
            path.write_bytes(b"\x00" * 1024)
