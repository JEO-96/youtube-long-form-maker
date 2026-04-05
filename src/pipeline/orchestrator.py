"""파이프라인 오케스트레이터 - S1~S8 순차 실행/재개."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..core.config import ChannelConfig, load_channel
from ..core.cost_tracker import CostTracker
from ..core.models import Stage, VideoProduction, ProductionStatus
from ..core.state import StateManager

from .s1_benchmark import S1Benchmark
from .s2_script import S2Script
from .s3_voice import S3Voice
from .s4_storyboard import S4Storyboard
from .s5_media import S5Media
from .s6_editing import S6Editing
from .s7_thumbnail import S7Thumbnail
from .s8_export import S8Export

logger = logging.getLogger(__name__)

# 스테이지 실행 순서
STAGE_ORDER = [
    Stage.BENCHMARK,
    Stage.SCRIPT,
    Stage.VOICE,
    Stage.STORYBOARD,
    Stage.MEDIA,
    Stage.EDITING,
    Stage.THUMBNAIL,
    Stage.EXPORT,
]

STAGE_CLASSES = {
    Stage.BENCHMARK: S1Benchmark,
    Stage.SCRIPT: S2Script,
    Stage.VOICE: S3Voice,
    Stage.STORYBOARD: S4Storyboard,
    Stage.MEDIA: S5Media,
    Stage.EDITING: S6Editing,
    Stage.THUMBNAIL: S7Thumbnail,
    Stage.EXPORT: S8Export,
}


class Orchestrator:
    """파이프라인 오케스트레이터."""

    def __init__(
        self,
        state_manager: StateManager,
        cost_tracker: CostTracker,
        dry_run: bool = False,
    ) -> None:
        self.state = state_manager
        self.cost = cost_tracker
        self.dry_run = dry_run

    async def produce(
        self,
        channel_id: str,
        topic: str,
        start_stage: Stage | None = None,
        **kwargs: Any,
    ) -> str:
        """새 프로덕션 시작.

        Returns:
            production_id
        """
        channel = load_channel(channel_id)
        production_id = self._generate_id(channel_id)

        production = VideoProduction(
            production_id=production_id,
            channel_id=channel_id,
            topic=topic,
        )
        self.state.create_production(production)
        logger.info(f"Production created: {production_id} (topic={topic})")

        await self._run_stages(
            production_id, channel, topic=topic,
            start_stage=start_stage, **kwargs,
        )
        return production_id

    async def resume(self, production_id: str, **kwargs: Any) -> str:
        """실패한 프로덕션 재개.

        Returns:
            production_id
        """
        row = self.state.get_production(production_id)
        if not row:
            raise ValueError(f"Production not found: {production_id}")

        channel = load_channel(row["channel_id"])
        current_stage = Stage(row["current_stage"])

        logger.info(f"Resuming {production_id} from stage={current_stage.value}")

        await self._run_stages(
            production_id, channel,
            topic=row["topic"],
            start_stage=current_stage,
            **kwargs,
        )
        return production_id

    async def _run_stages(
        self,
        production_id: str,
        channel: ChannelConfig,
        topic: str = "",
        start_stage: Stage | None = None,
        **kwargs: Any,
    ) -> None:
        """스테이지 순차 실행."""
        started = start_stage is None

        for stage in STAGE_ORDER:
            if not started:
                if stage == start_stage:
                    started = True
                else:
                    continue

            # 이미 완료된 스테이지 스킵
            stage_cls = STAGE_CLASSES[stage]
            instance = stage_cls(
                production_id=production_id,
                state_manager=self.state,
                cost_tracker=self.cost,
                channel=channel,
                dry_run=self.dry_run,
            )

            if instance.is_completed():
                logger.info(f"[{stage.value}] SKIP (already completed)")
                continue

            # 실행
            extra = {}
            if stage == Stage.BENCHMARK:
                extra["topic"] = topic

            try:
                await instance.execute(**extra, **kwargs)
            except Exception as e:
                logger.error(f"Pipeline stopped at {stage.value}: {e}")
                raise

        # 전체 완료
        self.state.mark_completed(production_id)
        total_cost = self.cost.get_production_cost(production_id)
        logger.info(f"Production {production_id} COMPLETED (total cost: ${total_cost:.4f})")

    @staticmethod
    def _generate_id(channel_id: str) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{channel_id}_{ts}"
