"""S5 미디어 생성 - 씬별 이미지/영상/스톡."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

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

                    record.fallback_used = False
                    record.final_provider = ""
                    logger.error(
                        f"Scene {scene.scene_number}: Pillow 폴백 비활성화됨. "
                        "저품질 도형 이미지 대신 실패로 기록합니다."
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

    async def _generate_single(self, scene: Scene) -> MediaAsset:
        """단일 씬 미디어 생성."""
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

        쇼츠 모드: AI 이미지 생성을 완전히 스킵하고 Pillow 카드 템플릿(9:16)을 렌더.
        """
        is_shorts = getattr(self.channel.content, "is_shorts", False) if hasattr(self.channel, "content") else False

        out_path = self.stage_dir / f"scene_{scene.scene_number:03d}.png"
        sn = scene.scene_number

        if self.dry_run:
            if is_shorts:
                self._render_shorts_card(out_path, scene)
                return MediaAsset(
                    scene_number=sn, media_type=MediaType.AI_IMAGE,
                    file_path=str(out_path), original_resolution=[1080, 1920],
                    provider="shorts_card",
                )
            self._create_placeholder_image(out_path, scene)
            return MediaAsset(
                scene_number=sn, media_type=MediaType.AI_IMAGE,
                file_path=str(out_path), original_resolution=[1920, 1080],
                provider="mock",
            )

        # ═══ 쇼츠 모드: AI 이미지 생성 비활성 — Pillow 카드 렌더 ═══
        if is_shorts:
            logger.info(f"Scene {sn}: shorts mode → Pillow 9:16 카드 렌더")
            self._render_shorts_card(out_path, scene)
            return MediaAsset(
                scene_number=sn, media_type=MediaType.AI_IMAGE,
                file_path=str(out_path), original_resolution=[1080, 1920],
                provider="shorts_card",
            )

        # GPT Image 최적화 프롬프트 생성
        gpt_prompt = self._build_gpt_image_prompt(scene)
        simplified = self._simplify_prompt(scene)
        attempts: list[str] = []
        errors: list[tuple[str, Exception]] = []

        # ═══ 프롬프트 캐시 확인 ═══
        import hashlib
        prompt_hash = hashlib.md5(
            f"{sn}:{gpt_prompt}".encode("utf-8")
        ).hexdigest()[:12]
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

        # ═══ 모든 시도 실패 → 저품질 도형 폴백 없이 실패로 기록 ═══
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
        visual_cue = getattr(scene, "visual_cue", "") or ""
        render_text = load_settings().media_generation.render_text_in_background

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
                "Create a premium photorealistic finance workspace scene with a laptop "
                "or tablet showing an unlabeled analytical dashboard. Avoid flat vector "
                "illustration and generic geometric clip-art."
            ),
            VisualIntent.INFOGRAPHIC: (
                "Create a premium editorial finance image using realistic objects: bank "
                "cards, envelopes, phone banking screens, notebooks, and documents. "
                "Avoid flat vector illustration and generic geometric clip-art."
            ),
            VisualIntent.CHECKLIST: (
                "Create a photorealistic planning desk scene with a notebook, pen, "
                "calendar, and finance app screen, all without readable writing."
            ),
            VisualIntent.COMPARISON_CARD: (
                "Create a cinematic side-by-side real-world comparison scene using "
                "two realistic desk setups or lifestyle vignettes. No labels or UI text."
            ),
            VisualIntent.EMPHASIS_CAPTION: (
                "Create a cinematic symbolic photograph with dramatic lighting and "
                "a real object focal point such as a wallet, bank card, phone, or notebook. "
                "Avoid flat vector illustration and generic geometric clip-art."
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
                "Create a polished creator desk closing shot with camera gear, a laptop, "
                "and soft studio lighting. Do not include subscribe words or readable UI text."
            ),
        }

        base_instruction = intent_instructions.get(intent, intent_instructions[VisualIntent.REAL_BROLL])
        cue_instruction = self._cue_image_instruction(visual_cue)
        if cue_instruction:
            base_instruction = f"{cue_instruction} {base_instruction}"

        context = desc[:200]
        if not render_text:
            context = self._sanitize_textless_image_context(scene, niche)

        # 키워드를 시각 요소로 변환
        keyword_hint = ""
        if keywords and render_text:
            keyword_hint = f"Feature these key elements: {', '.join(keywords[:3])}."

        no_text_instruction = (
            "Do not render readable text, Korean characters, Latin letters, numerals, "
            "percent signs, captions, labels, watermarks, UI words, or subtitles inside the image. "
            "Any spoken content will be added later as a subtitle layer."
        )

        parts = [
            base_instruction,
            f"Context: {context}." if context else "",
            keyword_hint,
            f"Color palette: {niche_palette}.",
            no_text_instruction if not render_text else "",
            "Aspect ratio: 16:9 landscape. No watermarks.",
            "High resolution, professional quality.",
        ]

        prompt = " ".join(p for p in parts if p)
        return prompt[:2000]

    @staticmethod
    def _cue_image_instruction(visual_cue: str) -> str:
        """visual_cue를 이미지 모델용 텍스트 없는 구도 지시로 변환."""
        return {
            "money_decrease": (
                "Depict money decreasing with realistic objects: a wallet, cash laid out "
                "on a desk, receipts, and a visibly smaller remaining stack. "
                "Avoid flat vector illustration and generic geometric clip-art."
            ),
            "remaining_balance": (
                "Depict a remaining balance with a small cash stack beside a phone banking "
                "screen and a tidy finance desk, without readable text."
            ),
            "money_income": (
                "Depict salary income with realistic cash, bank card, and phone banking "
                "objects arranged as money arriving into an account."
            ),
            "money_saving": (
                "Depict saving money with realistic cash stacks, coins, envelopes, and a "
                "planning notebook. Avoid coin-bank animal motifs and flat icon imagery."
            ),
            "debt_pressure": (
                "Depict debt pressure with realistic bills, loan papers, a calculator, "
                "and tense desk lighting, without readable text."
            ),
            "expense_breakdown": (
                "Depict a budget breakdown with realistic receipts, envelopes, a calculator, "
                "and a phone banking screen arranged into spending categories, without labels."
            ),
            "account_split": (
                "Depict account separation with several real envelopes, bank cards, and cash "
                "stacks on a desk, arranged as separate accounts without labels."
            ),
            "risk_warning": (
                "Depict financial risk with a realistic stressed desk scene: unpaid bills, "
                "calculator, empty wallet, and moody lighting, without readable text."
            ),
            "target_goal": (
                "Depict a financial goal with a realistic planning notebook, calendar, "
                "cash envelope, and phone banking screen, without readable text."
            ),
            "step_sequence": (
                "Depict a step-by-step process with realistic objects laid out left to right: "
                "phone, bank card, envelopes, and notebook, without readable text."
            ),
            "case_story": (
                "Depict a real-life example visually: a natural lifestyle planning scene with "
                "a person at a desk, phone, and documents without readable text."
            ),
            "timeline": (
                "Depict a timeline through realistic calendar pages, a notebook, and savings "
                "envelopes on a desk, without readable dates or labels."
            ),
            "growth_trend": (
                "Depict improvement with realistic savings objects, a tidy desk, phone banking "
                "screen, and optimistic lighting, without readable text."
            ),
            "decline_trend": (
                "Depict decline with a realistic shrinking cash stack, receipts, empty wallet, "
                "and cautionary lighting, without readable text."
            ),
            "rate_chart": (
                "Depict interest-rate analysis with a realistic laptop or tablet showing an "
                "unlabeled financial dashboard, without readable text."
            ),
        }.get(visual_cue, "")

    @staticmethod
    def _sanitize_textless_image_context(scene: Scene, niche: str) -> str:
        """이미지 모델이 글자/숫자를 그리지 않도록 컨텍스트를 시각어 중심으로 정리."""
        import re

        source = (
            getattr(scene, "stock_search_query", "")
            or getattr(scene, "image_prompt", "")
            or getattr(scene, "visual_description", "")
            or ""
        )
        source = re.sub(r'\d+[\d,.\s]*[%A-Za-z가-힣]*', ' ', source)
        source = re.sub(r'[가-힣]+', ' ', source)
        source = re.sub(r'\bpiggy\s*bank\b|\bpiggy\b', ' ', source, flags=re.IGNORECASE)
        source = re.sub(r'\b(text|caption|label|number|percent|headline|title|word|letter)s?\b',
                        ' ', source, flags=re.IGNORECASE)
        source = re.sub(r'\s+', ' ', source).strip(" ,.-")

        cue_context = S5Media._cue_image_instruction(getattr(scene, "visual_cue", "") or "")
        if cue_context:
            if len(source) >= 8:
                return f"{cue_context} Scene-specific context: {source}."
            return cue_context

        niche_fallback = {
            "real_estate": "apartment building lifestyle, city street, real estate planning",
            "finance": "banking desk, savings account, phone banking, realistic finance objects",
            "health": "clean wellness scene, realistic medical and lifestyle objects",
            "ai": "realistic technology workspace, laptop, subtle neural network light patterns",
            "business": "office planning scene, realistic business documents and laptop",
        }
        return source if len(source) >= 8 else niche_fallback.get(niche, "professional cinematic visual background")

    @staticmethod
    def _simplify_prompt(scene: Scene) -> str:
        """실패한 프롬프트를 간소화 — 영문 키워드 + 스타일 지시어 중심."""
        import re
        cue_desc = S5Media._cue_image_instruction(getattr(scene, "visual_cue", "") or "")
        desc = cue_desc or getattr(scene, "stock_search_query", "") or getattr(scene, "visual_description", "") or ""
        english_parts = re.findall(r'[A-Za-z0-9][A-Za-z0-9\s,.-]+', desc)
        keywords = ", ".join(english_parts).strip() if english_parts else "professional editorial photograph"
        keywords = re.sub(r'\d+[\d,.\s]*[%A-Za-z]*', ' ', keywords)
        keywords = re.sub(r'\s+', ' ', keywords).strip(" ,.-") or "professional editorial photograph"

        style_suffix = (
            "cinematic lighting, high quality, 4K resolution, "
            "professional photography, clean composition, no readable text, "
            "no letters, no numerals, no captions, no labels"
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
            is_shorts = getattr(self.channel.content, "is_shorts", False) if hasattr(self.channel, "content") else False
            if is_shorts:
                self._render_shorts_card(img_path, scene)
            else:
                self._create_placeholder_image(img_path, scene, label="STOCK")
            return MediaAsset(
                scene_number=scene.scene_number, media_type=MediaType.STOCK_VIDEO,
                file_path=str(img_path), provider="mock_pexels",
            )

        # PexelsStockMedia는 production 전체에서 1개 인스턴스 공유 (중복 방지)
        if not hasattr(self, "_stock_media"):
            from ..providers.stock_media import PexelsStockMedia
            self._stock_media = PexelsStockMedia()
        stock = self._stock_media
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
    # 색상 유틸
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
    @staticmethod
    def _is_mostly_black(video_path: Path, threshold: float = 0.2) -> bool:
        """비디오에서 검은 구간 비율을 검사. threshold 이상이면 True.

        duration 조회: ffmpeg -i stderr의 Duration 라인 파싱 (ffprobe 비의존).
        black 감지: ffmpeg blackdetect 필터.
        파싱 실패 시 True 반환 (reject 쪽으로 안전하게 처리).
        """
        import subprocess, re

        # ── duration 조회: ffmpeg stderr에서 Duration 파싱 ──
        total_dur = 0.0
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-i", str(video_path)],
                capture_output=True, text=True, timeout=10,
            )
            match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", result.stderr)
            if match:
                h, m, s = float(match.group(1)), float(match.group(2)), float(match.group(3))
                total_dur = h * 3600 + m * 60 + s
        except Exception:
            pass

        if total_dur <= 0:
            logger.warning(f"Black detect: duration parse failed for {video_path.name} → rejecting")
            return True  # 파싱 실패 시 reject (안전)

        # ── blackdetect ──
        try:
            result = subprocess.run(
                ["ffmpeg", "-i", str(video_path),
                 "-vf", "blackdetect=d=0.5:pix_th=0.10",
                 "-an", "-f", "null", "-"],
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
            logger.warning(f"Black detect failed for {video_path.name}: {e} → rejecting")
            return True  # 실패 시 reject

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

    # ═══════════════════════════════════════════════════
    # 쇼츠 9:16 Pillow 카드 렌더링
    # ═══════════════════════════════════════════════════

    # 쇼츠 캔버스 상수
    _SHORTS_W = 1080
    _SHORTS_H = 1920
    _SHORTS_SUBTITLE_MARGIN = 280  # 하단 자막 안전영역

    def _render_shorts_card(self, path: Path, scene: Scene) -> None:
        """쇼츠용 9:16 카드 이미지 렌더링 (visual_intent별 디자인).

        모든 AI_IMAGE 씬은 이 함수로 생성된다. API 호출 없음, 로컬 Pillow만 사용.
        """
        from ..core.text_render import draw_text_box
        from ..core.fonts import get_korean_font
        from ..core.visual_templates import derive_scene_title

        W, H = self._SHORTS_W, self._SHORTS_H
        niche = getattr(self.channel, "niche", "finance")

        # 채널 색상 팔레트
        primary = self._hex_to_rgb(
            getattr(self.channel.visual, "primary_color", "#0F172A")
        )
        secondary = self._hex_to_rgb(
            getattr(self.channel.visual, "secondary_color", "#1E293B")
        )
        accent = self._hex_to_rgb(
            getattr(self.channel.visual, "accent_color", "#3B82F6")
        )

        # 세로 그라데이션 배경 (primary → secondary)
        img = Image.new("RGB", (W, H), color=primary)
        draw = ImageDraw.Draw(img)
        for y in range(H):
            ratio = y / H
            r = int(primary[0] + (secondary[0] - primary[0]) * ratio)
            g = int(primary[1] + (secondary[1] - primary[1]) * ratio)
            b = int(primary[2] + (secondary[2] - primary[2]) * ratio)
            draw.line([(0, y), (W, y)], fill=(r, g, b))

        # 상단 액센트 바 (좌측 세로)
        draw.rectangle([0, 0, 14, H], fill=accent)
        # 상단 가로 바
        draw.rectangle([0, 0, W, 10], fill=accent)

        # 헤더 — 카테고리 칩
        intent = getattr(scene, "visual_intent", VisualIntent.REAL_BROLL)
        chip_label = {
            VisualIntent.CHART: "DATA",
            VisualIntent.CHECKLIST: "CHECK",
            VisualIntent.COMPARISON_CARD: "VS",
            VisualIntent.EMPHASIS_CAPTION: "KEY",
            VisualIntent.INFOGRAPHIC: "INFO",
            VisualIntent.REAL_BROLL: "LIFE",
            VisualIntent.MAP: "MAP",
            VisualIntent.TALKING_HEAD_STYLE: "TALK",
            VisualIntent.CLOSING_CTA: "CTA",
        }.get(intent, "SCENE")

        chip_font = get_korean_font(size=36, bold=True)
        chip_x, chip_y = 60, 90
        chip_bbox = draw.textbbox((0, 0), chip_label, font=chip_font)
        chip_w = (chip_bbox[2] - chip_bbox[0]) + 40
        chip_h = (chip_bbox[3] - chip_bbox[1]) + 24
        draw.rounded_rectangle(
            [chip_x, chip_y, chip_x + chip_w, chip_y + chip_h],
            radius=18,
            fill=accent,
        )
        draw.text(
            (chip_x + 20, chip_y + 8),
            chip_label,
            fill="white",
            font=chip_font,
        )

        # 타이틀 (씬 narration에서 추출)
        title = derive_scene_title(
            narration=getattr(scene, "narration_text", ""),
            vis_desc=getattr(scene, "visual_description", ""),
            intent=intent.value if hasattr(intent, "value") else str(intent),
            max_len=18,
        )

        title_box = (60, chip_y + chip_h + 60, W - 60, chip_y + chip_h + 260)
        draw_text_box(
            draw,
            title,
            title_box,
            fill="white",
            max_lines=2,
            min_font_size=48,
            max_font_size=96,
            align="left",
        )

        # 본문 — narration 전체 (자막은 별도 번인이므로 여기 텍스트는 보조)
        narration = getattr(scene, "narration_text", "") or ""
        body_box = (
            60,
            title_box[3] + 40,
            W - 60,
            H - self._SHORTS_SUBTITLE_MARGIN - 80,
        )

        # 인텐트별 장식 블록
        if intent == VisualIntent.CHECKLIST:
            # 체크리스트 박스
            items = self._extract_short_items(narration, max_items=3)
            y_start = body_box[1]
            for i, item in enumerate(items):
                box_top = y_start + i * 180
                draw.rounded_rectangle(
                    [body_box[0], box_top, body_box[2], box_top + 150],
                    radius=18,
                    fill=(255, 255, 255, 20),
                    outline=accent,
                    width=4,
                )
                # 체크 마크
                mark_box = (
                    body_box[0] + 30,
                    box_top + 30,
                    body_box[0] + 120,
                    box_top + 120,
                )
                draw.rounded_rectangle(mark_box, radius=12, fill=accent)
                check_font = get_korean_font(size=60, bold=True)
                draw.text(
                    (mark_box[0] + 20, mark_box[1] + 8),
                    "✓",
                    fill="white",
                    font=check_font,
                )
                # 아이템 텍스트
                draw_text_box(
                    draw,
                    item,
                    (mark_box[2] + 30, box_top + 30, body_box[2] - 30, box_top + 130),
                    fill="white",
                    max_lines=2,
                    min_font_size=32,
                    max_font_size=44,
                )
        elif intent == VisualIntent.COMPARISON_CARD:
            # A vs B 좌우 분할
            mid_y = (body_box[1] + body_box[3]) // 2
            top_box = (body_box[0], body_box[1], body_box[2], mid_y - 40)
            bot_box = (body_box[0], mid_y + 40, body_box[2], body_box[3])
            draw.rounded_rectangle(top_box, radius=24, outline=accent, width=5)
            draw.rounded_rectangle(bot_box, radius=24, outline=(200, 200, 200), width=4)
            # VS 배지
            vs_font = get_korean_font(size=72, bold=True)
            vs_bbox = draw.textbbox((0, 0), "VS", font=vs_font)
            vs_w = vs_bbox[2] - vs_bbox[0]
            draw.text(
                ((W - vs_w) // 2, mid_y - 40),
                "VS",
                fill=accent,
                font=vs_font,
            )
            # 상/하단 텍스트는 narration 전체 분배
            half = len(narration) // 2 if narration else 0
            draw_text_box(
                draw,
                narration[:half] or title,
                (top_box[0] + 30, top_box[1] + 30, top_box[2] - 30, top_box[3] - 30),
                fill="white",
                max_lines=3,
                min_font_size=30,
                max_font_size=46,
            )
            draw_text_box(
                draw,
                narration[half:] or title,
                (bot_box[0] + 30, bot_box[1] + 30, bot_box[2] - 30, bot_box[3] - 30),
                fill="white",
                max_lines=3,
                min_font_size=30,
                max_font_size=46,
            )
        elif intent == VisualIntent.CHART:
            # 간단한 수평 막대 차트 시각화
            bars = 3
            bar_h = 90
            gap = 60
            start_y = body_box[1] + 60
            widths = [0.95, 0.70, 0.45]
            for i in range(bars):
                y0 = start_y + i * (bar_h + gap)
                full_w = body_box[2] - body_box[0] - 40
                bar_w = int(full_w * widths[i])
                # 배경 트랙
                draw.rounded_rectangle(
                    [body_box[0] + 20, y0, body_box[0] + 20 + full_w, y0 + bar_h],
                    radius=bar_h // 2,
                    fill=(255, 255, 255, 15),
                    outline=(255, 255, 255),
                    width=2,
                )
                # 액센트 바
                draw.rounded_rectangle(
                    [body_box[0] + 20, y0, body_box[0] + 20 + bar_w, y0 + bar_h],
                    radius=bar_h // 2,
                    fill=accent,
                )
        elif intent == VisualIntent.EMPHASIS_CAPTION:
            # 대형 강조 카드 (반투명 블록 + 큰 타이틀만)
            emp_box = (
                body_box[0],
                body_box[1] + 60,
                body_box[2],
                body_box[3] - 60,
            )
            draw.rounded_rectangle(emp_box, radius=32, outline=accent, width=6)
            draw_text_box(
                draw,
                narration or title,
                (emp_box[0] + 40, emp_box[1] + 40, emp_box[2] - 40, emp_box[3] - 40),
                fill="white",
                max_lines=5,
                min_font_size=40,
                max_font_size=72,
                align="center",
            )
        else:
            # 기본: 본문 텍스트 박스
            draw_text_box(
                draw,
                narration or title,
                body_box,
                fill=(230, 230, 230),
                max_lines=8,
                min_font_size=32,
                max_font_size=54,
            )

        # 하단 액센트 바
        draw.rectangle([0, H - 10, W, H], fill=accent)

        img.save(str(path), "PNG")
        logger.info(
            f"Scene {scene.scene_number}: shorts card rendered "
            f"(intent={intent.value if hasattr(intent, 'value') else intent}, {W}x{H})"
        )

    @staticmethod
    def _extract_short_items(text: str, max_items: int = 3) -> list[str]:
        """narration에서 체크리스트용 짧은 항목 추출."""
        import re
        if not text:
            return ["핵심 포인트 1", "핵심 포인트 2", "핵심 포인트 3"][:max_items]
        parts = re.split(r'[.!?다요죠까니]\s*', text)
        items = [p.strip() for p in parts if p.strip() and len(p.strip()) >= 4]
        if len(items) < max_items:
            items += ["핵심 포인트"] * (max_items - len(items))
        return items[:max_items]

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
