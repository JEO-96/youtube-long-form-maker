"""API 사용량/비용 추적."""

from __future__ import annotations

import logging
from datetime import datetime

from .models import CostEntry, Stage
from .state import StateManager

logger = logging.getLogger(__name__)


class CostTracker:
    """API 호출 비용을 기록하고 리포트."""

    def __init__(self, state_manager: StateManager):
        self.state = state_manager

    def record(
        self,
        production_id: str,
        stage: Stage,
        provider: str,
        operation: str,
        units: float,
        unit_cost: float,
    ) -> CostEntry:
        """비용 기록."""
        entry = CostEntry(
            stage=stage,
            provider=provider,
            operation=operation,
            units=units,
            unit_cost=unit_cost,
            total_cost=units * unit_cost,
            timestamp=datetime.now(),
        )
        self.state.record_cost(entry, production_id)
        logger.info(
            f"Cost: {provider}/{operation} = ${entry.total_cost:.4f} "
            f"({units:.1f} x ${unit_cost:.6f})"
        )
        return entry

    def get_production_cost(self, production_id: str) -> float:
        """프로덕션별 총 비용."""
        return self.state.get_total_cost(production_id=production_id)

    def get_monthly_cost(self, month: str) -> float:
        """월별 총 비용 (예: "2026-04")."""
        return self.state.get_total_cost(month=month)

    def get_cost_breakdown(self, production_id: str) -> dict[str, float]:
        """프로바이더별 비용 분석."""
        costs = self.state.get_costs(production_id=production_id)
        breakdown: dict[str, float] = {}
        for c in costs:
            provider = c["provider"]
            breakdown[provider] = breakdown.get(provider, 0) + c["total_cost"]
        return breakdown
