"""S5 미디어 생성 - 씬별 이미지/영상/스톡."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from ..core.models import (
    Stage, StoryboardResult, Scene, MediaResult, MediaAsset, MediaType,
    SceneFailureRecord, VisualIntent,
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
        concurrency = min(settings.media_generation.concurrency_limit, 6)
        semaphore = asyncio.Semaphore(concurrency)

        # 프롬프트 캐시: 동일/유사 프롬프트의 중복 API 호출 방지
        self._prompt_cache: dict[str, Path] = {}  # prompt_hash → file_path

        self.stage_dir.mkdir(parents=True, exist_ok=True)
        assets: list[MediaAsset] = []
        failed_scenes: list[int] = []
        failure_records: list[SceneFailureRecord] = []
        total_cost = 0.0

        async def generate_scene(scene: Scene) -> MediaAsset | None:
            async with semaphore:
                try:
                    asset = await self._generate_single(scene)
                    return asset
                except Exception as e:
                    # ═══ 구조화된 실패 기록 + failover 메타 ═══
                    record = self._build_failure_record(scene, e)

                    # failover 추적 정보 부착 (_generate_image에서 설정)
                    fo_attempts = getattr(e, "_failover_attempts", [])
                    fo_errors = getattr(e, "_failover_errors", [])
                    record.provider_attempts = fo_attempts
                    record.failure_stage = "failover" if len(fo_attempts) > 2 else "first_try"
                    record.network_related = any(
                        "connect" in str(err).lower()
                        for _, err in fo_errors
                    )
                    # 사람이 읽을 수 있는 요약
                    if record.network_related:
                        record.human_summary = (
                            f"Scene {scene.scene_number}: 모든 이미지 provider 연결 실패 "
                            f"(시도: {', '.join(fo_attempts)}). 네트워크 문제로 추정."
                        )
                    else:
                        record.human_summary = (
                            f"Scene {scene.scene_number}: 이미지 생성 실패 "
                            f"(시도: {', '.join(fo_attempts)}). {record.error_message[:100]}"
                        )

                    failure_records.append(record)
                    failed_scenes.append(scene.scene_number)

                    logger.error(
                        f"Scene {scene.scene_number} 모든 provider 실패: "
                        f"attempts={fo_attempts}, "
                        f"network={record.network_related}, "
                        f"last_error={record.error_message[:200]}"
                    )

                    # 최종 폴백: Pillow 기반 스타일 이미지 생성
                    try:
                        out_path = (
                            self.stage_dir
                            / f"scene_{scene.scene_number:03d}_fallback.png"
                        )
                        self._generate_fallback_image(scene, out_path)
                        record.fallback_used = True
                        record.fallback_path = str(out_path)
                        record.final_provider = "fallback_pillow"
                        record.failure_stage = "fallback"
                        logger.info(
                            f"Scene {scene.scene_number}: 최종 Pillow 폴백 완료 → {out_path}"
                        )
                        return MediaAsset(
                            scene_number=scene.scene_number,
                            media_type=MediaType.AI_IMAGE,
                            file_path=str(out_path),
                            original_resolution=[1920, 1080],
                            provider="fallback_pillow",
                        )
                    except Exception as fallback_err:
                        logger.error(
                            f"Scene {scene.scene_number} Pillow 폴백도 실패: {fallback_err}"
                        )
                        return None

        # 병렬 생성
        tasks = [generate_scene(s) for s in storyboard.scenes]
        results = await asyncio.gather(*tasks)

        for r in results:
            if r:
                assets.append(r)
                total_cost += r.generation_cost

        # 실패 요약 로그 + JSON 리포트 저장
        if failure_records:
            logger.warning(
                f"미디어 생성 실패 요약: {len(failure_records)}건 — "
                + ", ".join(
                    f"Scene {r.scene_number}({r.provider}/{r.exception_type})"
                    for r in failure_records
                )
            )
            self._save_failure_report(failure_records)

        return MediaResult(
            assets=assets,
            total_cost=round(total_cost, 4),
            failed_scenes=failed_scenes,
            failure_records=failure_records,
        )

    def _save_failure_report(self, records: list[SceneFailureRecord]) -> None:
        """실패 기록을 production 디렉토리에 JSON 리포트로 저장.

        production 단위에서 Flux 실패 원인을 즉시 추적할 수 있게 함.
        """
        import json
        from datetime import datetime

        report = {
            "production_id": self.production_id,
            "timestamp": datetime.now().isoformat(),
            "total_failures": len(records),
            "failures": [],
        }
        for r in records:
            entry = {
                "scene_number": r.scene_number,
                "provider": r.provider,
                "exception_type": r.exception_type,
                "http_status": r.http_status,
                "error_message": r.error_message,
                "detail": r.detail,
                "fallback_used": r.fallback_used,
                "fallback_path": r.fallback_path,
                "provider_attempts": r.provider_attempts,
                "final_provider": r.final_provider,
                "failure_stage": r.failure_stage,
                "network_related": r.network_related,
                "human_summary": r.human_summary,
            }
            report["failures"].append(entry)

        # provider별 실패 집계
        from collections import Counter
        provider_counts = Counter(r.provider for r in records)
        error_type_counts = Counter(r.exception_type for r in records)
        network_count = sum(1 for r in records if r.network_related)

        report["summary"] = {
            "by_provider": dict(provider_counts),
            "by_error_type": dict(error_type_counts),
            "fallback_count": sum(1 for r in records if r.fallback_used),
            "network_related_count": network_count,
            "failover_reached_openai": sum(
                1 for r in records
                if any("openai" in a for a in r.provider_attempts)
            ),
        }

        # 사람이 바로 읽을 수 있는 요약
        if network_count == len(records):
            report["diagnosis"] = (
                "모든 실패가 네트워크 연결 문제. "
                "Flux/OpenAI 서버 접속 불가 또는 방화벽/DNS 문제 확인 필요."
            )
        elif network_count > 0:
            report["diagnosis"] = (
                f"네트워크 관련 실패 {network_count}건 포함. "
                "일부 provider는 연결 가능하나 일부는 불가."
            )
        else:
            report["diagnosis"] = (
                "네트워크 문제 없음. API 인증, 쿼터, 콘텐츠 필터 등 확인 필요."
            )

        report_path = self.stage_dir / "failure_report.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"실패 리포트 저장: {report_path}")

    # 로컬 Pillow 렌더 대상 intent (API 호출 없이 빠르게 생성)
    _LOCAL_RENDER_INTENTS = {
        VisualIntent.CHART,
        VisualIntent.CHECKLIST,
        VisualIntent.COMPARISON_CARD,
        VisualIntent.EMPHASIS_CAPTION,
        VisualIntent.INFOGRAPHIC,
        VisualIntent.CLOSING_CTA,
    }

    async def _generate_single(self, scene: Scene) -> MediaAsset:
        """단일 씬 미디어 생성."""
        settings = load_settings()
        prefer_local = settings.media_generation.prefer_local_render

        # 로컬 Pillow 렌더 우선: chart/card/checklist 등은 API 호출 없이 빠르게 생성
        if (prefer_local
            and scene.media_type == MediaType.AI_IMAGE
            and scene.visual_intent in self._LOCAL_RENDER_INTENTS
            and not self.dry_run):
            try:
                out_path = self.stage_dir / f"scene_{scene.scene_number:03d}.png"
                self._generate_fallback_image(scene, out_path)
                logger.info(
                    f"Scene {scene.scene_number}: local Pillow render "
                    f"(intent={scene.visual_intent.value})"
                )
                return MediaAsset(
                    scene_number=scene.scene_number,
                    media_type=MediaType.AI_IMAGE,
                    file_path=str(out_path),
                    original_resolution=[1920, 1080],
                    provider="local_pillow",
                )
            except Exception as e:
                logger.warning(
                    f"Scene {scene.scene_number}: local render failed ({e}), "
                    "falling back to AI"
                )

        if scene.media_type == MediaType.AI_IMAGE:
            return await self._generate_image(scene)
        elif scene.media_type == MediaType.AI_VIDEO:
            return await self._generate_video(scene)
        elif scene.media_type == MediaType.STOCK_VIDEO:
            return await self._generate_stock(scene)
        else:
            return await self._generate_image(scene)

    # ═══ 이미지 생성 Failover 설정 ═══
    MAX_IMAGE_PROMPT_LENGTH = 500

    async def _generate_image(self, scene: Scene) -> MediaAsset:
        """AI 이미지 생성 — 다중 provider 자동 failover.

        채널 설정의 image_gen provider를 1순위로 사용.
        기본 순서: GPT Image → GPT Image 간소화 → Flux 보조 → 예외(외부 fallback)
        """
        out_path = self.stage_dir / f"scene_{scene.scene_number:03d}.png"
        sn = scene.scene_number

        if self.dry_run:
            self._create_placeholder_image(out_path, scene)
            return MediaAsset(
                scene_number=sn, media_type=MediaType.AI_IMAGE,
                file_path=str(out_path), original_resolution=[1920, 1080],
                provider="mock",
            )

        # GPT Image 최적화 프롬프트 생성
        gpt_prompt = self._build_gpt_image_prompt(scene)
        simplified = self._simplify_prompt(scene)
        attempts: list[str] = []
        errors: list[tuple[str, Exception]] = []

        # ═══ 프롬프트 캐시 확인 ═══
        import hashlib
        prompt_hash = hashlib.md5(gpt_prompt[:200].encode()).hexdigest()[:12]
        cached_path = self._prompt_cache.get(prompt_hash)
        if cached_path and cached_path.exists():
            import shutil
            shutil.copy2(str(cached_path), str(out_path))
            logger.info(f"Scene {sn}: prompt cache hit → {cached_path.name}")
            return MediaAsset(
                scene_number=sn, media_type=MediaType.AI_IMAGE,
                file_path=str(out_path), original_resolution=[1920, 1080],
                provider="cache",
            )

        # ═══ 1단계: OpenAI GPT Image — 최적화 프롬프트 ═══
        result = await self._try_provider(
            "openai", gpt_prompt, out_path, sn, attempts, errors,
        )
        if result:
            self._prompt_cache[prompt_hash] = out_path
            return result

        # ═══ 2단계: OpenAI GPT Image — 간소화 프롬프트 ═══
        result = await self._try_provider(
            "openai", simplified, out_path, sn, attempts, errors,
            label="openai_simplified",
        )
        if result:
            return result

        # ═══ 모든 시도 실패 → 예외로 외부 Pillow fallback 트리거 ═══
        last_label, last_err = errors[-1] if errors else ("unknown", RuntimeError("No providers"))
        last_err._failover_attempts = attempts  # type: ignore[attr-defined]
        last_err._failover_errors = errors  # type: ignore[attr-defined]
        raise last_err

    async def _try_provider(
        self,
        provider_name: str,
        prompt: str,
        out_path: Path,
        scene_number: int,
        attempts: list[str],
        errors: list[tuple[str, Exception]],
        label: str = "",
    ) -> MediaAsset | None:
        """단일 provider 이미지 생성 시도. 성공 시 MediaAsset, 실패 시 None."""
        from ..providers.factory import create_image_gen

        step_label = label or provider_name
        attempts.append(step_label)

        try:
            img_gen = create_image_gen(provider_name)
        except Exception as init_err:
            logger.warning(f"Scene {scene_number} [{step_label}] 초기화 실패: {init_err}")
            errors.append((step_label, init_err))
            return None

        try:
            await img_gen.generate(prompt, out_path)
            cost = img_gen.estimate_cost()
            self.record_cost(provider_name, f"generate_image_{step_label}", units=1, unit_cost=cost)

            logger.info(
                f"Scene {scene_number} 이미지 생성 성공: provider={step_label}, "
                f"attempts={attempts}"
            )
            return MediaAsset(
                scene_number=scene_number,
                media_type=MediaType.AI_IMAGE,
                file_path=str(out_path),
                original_resolution=[1920, 1080],
                generation_cost=cost,
                provider=provider_name,
            )
        except Exception as e:
            is_network = "connection" in str(e).lower() or "connect" in type(e).__name__.lower()
            logger.warning(
                f"Scene {scene_number} [{step_label}] 실패: "
                f"{type(e).__name__}: {str(e)[:200]}"
                f"{' (네트워크)' if is_network else ''}"
            )
            errors.append((step_label, e))
            return None

    def _sanitize_image_prompt(self, prompt: str) -> str:
        """프롬프트 길이 제한 + 불필요한 제어 문자 제거."""
        prompt = prompt.strip()
        if len(prompt) > self.MAX_IMAGE_PROMPT_LENGTH:
            prompt = prompt[:self.MAX_IMAGE_PROMPT_LENGTH].rsplit(" ", 1)[0]
        return prompt

    def _build_gpt_image_prompt(self, scene: Scene) -> str:
        """GPT Image 최적화 프롬프트 — visual_intent 기반 구체적 지시.

        각 visual_intent에 맞는 구도/레이아웃/스타일을 명확히 지시.
        """
        desc = getattr(scene, "visual_description", "") or ""
        narration = getattr(scene, "narration_text", "") or ""
        intent = getattr(scene, "visual_intent", VisualIntent.REAL_BROLL)
        keywords = getattr(scene, "visual_keywords", [])

        niche = self.channel.niche
        niche_palette = {
            "real_estate": "navy blue and red accent tones",
            "finance": "deep blue and gold accent tones",
            "health": "soft green and white clean tones",
            "ai": "dark purple and cyan futuristic tones",
            "business": "charcoal and warm orange tones",
        }.get(niche, "professional muted tones")

        # visual_intent별 구도/레이아웃 지시
        intent_instructions: dict[str, str] = {
            VisualIntent.CHART: (
                "Create a clean, professional chart or graph visualization. "
                "Include a bar chart or line graph with labeled axes. "
                "Data should look realistic. Modern dashboard aesthetic."
            ),
            VisualIntent.INFOGRAPHIC: (
                "Create a modern infographic layout with icons and structured data. "
                "Use clean sections, arrows, and visual hierarchy. "
                "Flat design style, easy to read at a glance."
            ),
            VisualIntent.CHECKLIST: (
                "Create a clean checklist card with 3-5 checkbox items. "
                "Some items checked, some unchecked. "
                "Card-style layout, organized and clear."
            ),
            VisualIntent.COMPARISON_CARD: (
                "Create a side-by-side comparison card layout. "
                "Two columns with clear labels, split down the middle. "
                "Use contrasting colors for each side."
            ),
            VisualIntent.EMPHASIS_CAPTION: (
                "Create a bold, dramatic visual with large impactful text or numbers. "
                "Cinematic background with spotlight effect. "
                "The focal point should be a big, attention-grabbing element."
            ),
            VisualIntent.REAL_BROLL: (
                "Create a photorealistic lifestyle or cityscape photograph. "
                "Natural lighting, candid feel, documentary style."
            ),
            VisualIntent.MAP: (
                "Create an aerial view or stylized map visualization. "
                "City grid, neighborhood layout, or satellite perspective."
            ),
            VisualIntent.TALKING_HEAD_STYLE: (
                "Create a professional presenter/speaker setting. "
                "Clean studio background, professional lighting, confident posture."
            ),
            VisualIntent.CLOSING_CTA: (
                "Create a YouTube channel subscribe CTA card. "
                "Subscribe button, notification bell, 'Like & Subscribe' layout. "
                "Professional ending card design."
            ),
        }

        base_instruction = intent_instructions.get(intent, intent_instructions[VisualIntent.REAL_BROLL])

        # 키워드를 시각 요소로 변환
        keyword_hint = ""
        if keywords:
            keyword_hint = f"Feature these key elements: {', '.join(keywords[:3])}."

        parts = [
            base_instruction,
            f"Context: {desc[:200]}." if desc else "",
            keyword_hint,
            f"Color palette: {niche_palette}.",
            "Aspect ratio: 16:9 landscape. No watermarks.",
            "High resolution, professional quality.",
        ]

        prompt = " ".join(p for p in parts if p)
        return prompt[:2000]

    @staticmethod
    def _simplify_prompt(scene: Scene) -> str:
        """실패한 프롬프트를 간소화 — 영문 키워드 + 스타일 지시어 중심."""
        desc = getattr(scene, "visual_description", "") or ""
        import re
        english_parts = re.findall(r'[A-Za-z0-9][A-Za-z0-9\s,.-]+', desc)
        keywords = ", ".join(english_parts).strip() if english_parts else "professional infographic"

        style_suffix = (
            "cinematic lighting, high quality, 4K resolution, "
            "professional photography, clean composition"
        )
        simplified = f"{keywords}, {style_suffix}"
        return simplified[:500]

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

            # 검은 화면 검증: 50% 이상 검은 구간이면 reject
            if out_path.exists() and self._is_mostly_black(out_path):
                logger.warning(
                    f"Scene {scene.scene_number}: stock video mostly black, "
                    "falling back to AI image"
                )
                out_path.unlink(missing_ok=True)
                return await self._generate_image(scene)

            return MediaAsset(
                scene_number=scene.scene_number, media_type=MediaType.STOCK_VIDEO,
                file_path=str(out_path), provider="pexels",
            )

        # fallback: AI 이미지
        logger.warning(f"Scene {scene.scene_number}: no stock found, falling back to AI image")
        return await self._generate_image(scene)

    # ------------------------------------------------------------------
    # 실패 기록 생성
    # ------------------------------------------------------------------

    @staticmethod
    def _build_failure_record(scene: Scene, exc: Exception) -> SceneFailureRecord:
        """예외에서 구조화된 실패 기록을 추출."""
        from ..core.exceptions import (
            ProviderError, ProviderTimeoutError, RateLimitError,
            QuotaExhaustedError, ContentFilterError,
        )
        import httpx

        record = SceneFailureRecord(
            scene_number=scene.scene_number,
            exception_type=type(exc).__name__,
            error_message=str(exc)[:500],
        )

        # ProviderError 계열에서 provider 추출
        if isinstance(exc, ProviderError):
            record.provider = exc.provider
            if isinstance(exc, ProviderTimeoutError):
                record.detail = "Connection or request timeout"
            elif isinstance(exc, RateLimitError):
                record.detail = f"Rate limited, retry after {exc.retry_after_seconds}s"
                record.http_status = 429
            elif isinstance(exc, QuotaExhaustedError):
                record.detail = "Daily quota exhausted"
                record.http_status = 429
            elif isinstance(exc, ContentFilterError):
                record.detail = "Content safety filter triggered"

        # httpx 에러에서 상세 추출
        if isinstance(exc, httpx.ConnectError):
            record.exception_type = "ConnectError"
            record.detail = f"Connection failed: {exc}"
        elif isinstance(exc, httpx.TimeoutException):
            record.exception_type = "TimeoutError"
            record.detail = f"Request timeout: {exc}"
        elif isinstance(exc, httpx.HTTPStatusError):
            record.http_status = exc.response.status_code
            try:
                record.detail = exc.response.text[:500]
            except Exception:
                record.detail = f"HTTP {exc.response.status_code}"

        # ProviderError 메시지에서 HTTP 상태 코드 파싱 (fallback)
        if record.http_status is None:
            import re
            match = re.search(r'HTTP (\d{3})', str(exc))
            if match:
                record.http_status = int(match.group(1))

        # provider가 아직 비어 있으면 scene의 media_type에서 추정
        if not record.provider:
            type_to_provider = {
                MediaType.AI_IMAGE: "flux",
                MediaType.AI_VIDEO: "grok",
                MediaType.STOCK_VIDEO: "pexels",
            }
            record.provider = type_to_provider.get(scene.media_type, "unknown")

        return record

    # ------------------------------------------------------------------
    # 폴백 이미지 생성 (AI 실패 시)
    # ------------------------------------------------------------------

    @staticmethod
    def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
        """'#RRGGBB' 형식의 hex 색상을 (R, G, B) 튜플로 변환."""
        hex_color = hex_color.lstrip("#")
        if len(hex_color) != 6:
            return (20, 40, 80)  # 기본 남색
        return (
            int(hex_color[0:2], 16),
            int(hex_color[2:4], 16),
            int(hex_color[4:6], 16),
        )

    def _generate_fallback_image(self, scene: Scene, out_path: Path) -> None:
        """visual_intent 기반 고품질 Pillow 이미지 생성.

        visual_templates 모듈의 고도화된 템플릿을 사용.
        chart는 narration 성격에 따라 3종 변형 자동 선택.
        """
        from ..core import visual_templates as vt

        intent = getattr(scene, "visual_intent", VisualIntent.REAL_BROLL)
        W, H = 1920, 1080
        img = Image.new("RGB", (W, H))
        draw = ImageDraw.Draw(img)

        primary = self._hex_to_rgb(getattr(self.channel.visual, 'primary_color', '#1a2840'))
        secondary = self._hex_to_rgb(getattr(self.channel.visual, 'secondary_color', '#2d4a6f'))
        accent = self._hex_to_rgb(getattr(self.channel.visual, 'accent_color', '#EF233C'))

        # 공통: gradient background
        for y in range(H):
            ratio = y / H
            r = int(primary[0] + (secondary[0] - primary[0]) * ratio)
            g = int(primary[1] + (secondary[1] - primary[1]) * ratio)
            b = int(primary[2] + (secondary[2] - primary[2]) * ratio)
            draw.line([(0, y), (W, y)], fill=(r, g, b))

        narration = getattr(scene, "narration_text", "") or ""
        keywords = getattr(scene, "visual_keywords", []) or []
        vis_desc = getattr(scene, "visual_description", "") or ""

        # ═══ intent별 레이아웃 분기 (visual_templates 모듈 사용) ═══
        if intent == VisualIntent.CHART:
            # 숫자가 없으면 chart를 쓰지 않고 fallback
            has_data = vt.can_draw_chart(narration)
            if not has_data:
                logger.info(
                    f"Scene {getattr(scene, 'scene_number', '?')}: "
                    "no numeric data for chart → emphasis fallback"
                )
                vt.draw_emphasis_card(draw, narration, keywords, accent, vis_desc)
            else:
                variant = vt._select_chart_variant(narration)
                if variant == "gauge":
                    vt.draw_chart_gauge(draw, narration, keywords, accent, primary, vis_desc)
                elif variant == "line":
                    vt.draw_chart_line(draw, narration, keywords, accent, primary, vis_desc)
                elif variant == "kpi_only":
                    vt.draw_chart_kpi_only(draw, narration, keywords, accent, primary, vis_desc)
                else:
                    vt.draw_chart_kpi_bar(draw, narration, keywords, accent, primary, vis_desc)
        elif intent == VisualIntent.CHECKLIST:
            vt.draw_checklist_card(draw, narration, keywords, accent, primary, vis_desc)
        elif intent == VisualIntent.COMPARISON_CARD:
            ok = vt.draw_comparison_card(draw, narration, keywords, accent, primary, secondary, vis_desc)
            if not ok:
                vt.draw_checklist_card(draw, narration, keywords, accent, primary, vis_desc)
        elif intent == VisualIntent.EMPHASIS_CAPTION:
            vt.draw_emphasis_card(draw, narration, keywords, accent, vis_desc)
        elif intent == VisualIntent.INFOGRAPHIC:
            vt.draw_infographic_card(draw, narration, keywords, accent, primary, vis_desc)
        elif intent == VisualIntent.CLOSING_CTA:
            channel_name = getattr(self.channel, 'name', '')
            vt.draw_cta_card(draw, accent, primary, channel_name)
        else:
            scene_num = getattr(scene, 'scene_number', 0)
            vt.draw_default_card(draw, narration, vis_desc, accent, primary, scene_num)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(out_path), "PNG")

        # Visual QA: 기본 품질 검증
        self._validate_generated_image(img, out_path, scene)

    # ── 기존 _draw_* 메서드는 src/core/visual_templates.py로 이전됨 ──

    @staticmethod
    def _validate_generated_image(img: Image.Image, path: Path, scene: Any) -> None:
        """생성된 이미지의 기본 품질 검증."""
        from ..core.text_render import SUBTITLE_SAFE_MARGIN
        import numpy as np

        w, h = img.size
        subtitle_y = h - SUBTITLE_SAFE_MARGIN

        # 1. 전체 프레임 평균 밝기 (너무 어둡거나 너무 밝으면 경고)
        try:
            arr = np.array(img.convert("L"))
            mean_brightness = arr.mean()
            if mean_brightness < 15:
                logger.warning(
                    f"Visual QA: {path.name} scene {scene.scene_number} "
                    f"too dark (brightness={mean_brightness:.0f})"
                )
            elif mean_brightness > 245:
                logger.warning(
                    f"Visual QA: {path.name} scene {scene.scene_number} "
                    f"too bright (brightness={mean_brightness:.0f})"
                )

            # 2. 자막 안전영역 침범 검사: subtitle zone에 비배경 콘텐츠가 있는지
            # 자막 영역의 분산이 높으면 콘텐츠가 있을 가능성
            sub_zone = arr[subtitle_y:, :]
            if sub_zone.std() > 40:
                logger.info(
                    f"Visual QA: {path.name} scene {scene.scene_number} "
                    f"content near subtitle zone (std={sub_zone.std():.0f})"
                )
        except ImportError:
            pass  # numpy 없으면 스킵
        except Exception as e:
            logger.debug(f"Visual QA failed: {e}")

    @staticmethod
    def _is_mostly_black(video_path: Path, threshold: float = 0.4) -> bool:
        """비디오에서 검은 구간 비율을 검사. threshold 이상이면 True.

        ffmpeg blackdetect로 검은 구간 감지 후 전체 대비 비율 계산.
        """
        import subprocess, re

        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(video_path),
                ],
                capture_output=True, text=True, timeout=10,
            )
            total_dur = float(result.stdout.strip()) if result.stdout.strip() else 0
            if total_dur <= 0:
                return False

            result = subprocess.run(
                [
                    "ffmpeg", "-i", str(video_path),
                    "-vf", "blackdetect=d=0.5:pix_th=0.10",
                    "-an", "-f", "null", "-",
                ],
                capture_output=True, text=True, timeout=30,
            )
            black_total = 0.0
            for match in re.finditer(r"black_duration:(\d+\.?\d*)", result.stderr):
                black_total += float(match.group(1))

            ratio = black_total / total_dur
            if ratio > threshold:
                logger.info(
                    f"Black detect: {video_path.name} → "
                    f"{black_total:.1f}s/{total_dur:.1f}s ({ratio:.0%} black)"
                )
            return ratio > threshold
        except Exception as e:
            logger.debug(f"Black detect failed for {video_path}: {e}")
            return False

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
