"""파이프라인 스테이지 공통 베이스."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel

from ..core.config import ChannelConfig
from ..core.cost_tracker import CostTracker
from ..core.exceptions import StageError
from ..core.models import Stage, VideoProduction
from ..core.state import StateManager

logger = logging.getLogger(__name__)


class BaseStage(ABC):
    """모든 파이프라인 스테이지의 공통 베이스.

    Template Method 패턴:
        execute() → validate() → run() → save
    """

    stage: Stage  # 서브클래스에서 지정

    def __init__(
        self,
        production_id: str,
        state_manager: StateManager,
        cost_tracker: CostTracker,
        channel: ChannelConfig,
        dry_run: bool = False,
    ) -> None:
        self.production_id = production_id
        self.state = state_manager
        self.cost = cost_tracker
        self.channel = channel
        self.dry_run = dry_run

        # production 디렉토리
        from ..core.config import DATA_DIR
        self.prod_dir = DATA_DIR / "productions" / production_id
        self.stage_dir = self.prod_dir / self.stage.value

    async def execute(self, **kwargs: Any) -> BaseModel:
        """스테이지 실행 (공통 흐름)."""
        stage_name = self.stage.value
        logger.info(f"[{stage_name}] START (production={self.production_id}, dry_run={self.dry_run})")

        # 상태 기록: running
        self.state.advance_stage(self.production_id, self.stage)

        try:
            result = await self.run(**kwargs)

            # 결과 저장
            self.state.save_stage_output(
                self.production_id, self.stage, result.model_dump()
            )
            logger.info(f"[{stage_name}] DONE")
            return result

        except Exception as e:
            error_msg = f"[{stage_name}] {type(e).__name__}: {e}"
            logger.error(error_msg)
            self.state.mark_failed(self.production_id, error_msg)
            raise StageError(stage_name, self.production_id, cause=e) from e

    @abstractmethod
    async def run(self, **kwargs: Any) -> BaseModel:
        """실제 스테이지 로직 (서브클래스 구현)."""
        ...

    # ── 헬퍼 ──

    def load_previous(self, stage: Stage) -> dict:
        """이전 스테이지 출력 로드."""
        data = self.state.load_stage_output(self.production_id, stage)
        if data is None:
            raise StageError(
                self.stage.value,
                self.production_id,
                cause=ValueError(f"Missing output from stage: {stage.value}"),
            )
        return data

    def record_cost(
        self, provider: str, operation: str, units: float = 1, unit_cost: float = 0.0
    ) -> None:
        """비용 기록 헬퍼."""
        self.cost.record(
            self.production_id, self.stage, provider, operation,
            units=units, unit_cost=unit_cost,
        )

    def is_completed(self) -> bool:
        """이 스테이지가 이미 완료되었는지 확인."""
        return self.state.load_stage_output(self.production_id, self.stage) is not None
