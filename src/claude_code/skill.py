"""Claude Code 스킬 진입점 - 자연어 → 파이프라인 실행.

사용자가 Claude Code에게 자연어로 명령하면
이 스킬이 적절한 파이프라인 동작으로 변환하여 실행.

예:
    "재테크 채널에 사회초년생 통장관리 영상 만들어줘"
    "AI 채널에 2026 업무 자동화 도구 비교 영상 만들어줘"
    "finance_20260405_001 영상 대본만 다시 써줘"
    "이번 달 비용 얼마나 썼어?"
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..core.config import list_channels, load_channel
from ..core.cost_tracker import CostTracker
from ..core.state import StateManager
from ..pipeline.orchestrator import Orchestrator
from .handlers import CommandParser, ParsedCommand

logger = logging.getLogger(__name__)


class YTMakerSkill:
    """Claude Code 통합 스킬.

    자연어 명령을 받아 적절한 파이프라인 동작 실행.
    """

    def __init__(self, dry_run: bool = False) -> None:
        self.parser = CommandParser()
        self.dry_run = dry_run

    async def execute(self, user_input: str) -> dict[str, Any]:
        """자연어 명령 실행.

        Args:
            user_input: 사용자 자연어 입력

        Returns:
            실행 결과 dict
        """
        cmd = self.parser.parse(user_input)

        if cmd.confidence < 0.3:
            return {
                "status": "clarification_needed",
                "message": "명령을 이해하지 못했습니다. 좀 더 구체적으로 말씀해주세요.",
                "parsed": {
                    "action": cmd.action,
                    "topic": cmd.topic,
                    "channel": cmd.channel_id,
                },
                "examples": [
                    "재테크 채널에 사회초년생 통장관리 영상 만들어줘",
                    "상태 확인해줘",
                    "이번 달 비용 알려줘",
                ],
            }

        handlers = {
            "produce": self._handle_produce,
            "resume": self._handle_resume,
            "status": self._handle_status,
            "costs": self._handle_costs,
            "analyze": self._handle_analyze,
            "shorts": self._handle_shorts,
        }

        handler = handlers.get(cmd.action, self._handle_unknown)
        return await handler(cmd)

    async def _handle_produce(self, cmd: ParsedCommand) -> dict[str, Any]:
        """새 영상 제작."""
        # 채널 검증
        if not cmd.channel_id:
            available = list_channels()
            return {
                "status": "missing_channel",
                "message": f"어떤 채널에서 만들까요? 사용 가능: {available}",
            }

        available = list_channels()
        if cmd.channel_id not in available:
            return {
                "status": "invalid_channel",
                "message": f"채널 '{cmd.channel_id}' 없음. 사용 가능: {available}",
            }

        if not cmd.topic:
            return {
                "status": "missing_topic",
                "message": "어떤 주제로 영상을 만들까요?",
            }

        # 실행
        sm = StateManager()
        ct = CostTracker(state_manager=sm)
        orch = Orchestrator(state_manager=sm, cost_tracker=ct, dry_run=self.dry_run or cmd.dry_run)

        ch = load_channel(cmd.channel_id)
        production_id = await orch.produce(
            channel_id=cmd.channel_id, topic=cmd.topic
        )
        total_cost = ct.get_production_cost(production_id)

        return {
            "status": "completed",
            "production_id": production_id,
            "channel": ch.channel_name,
            "topic": cmd.topic,
            "total_cost": f"${total_cost:.4f}",
            "message": f"영상 제작 완료! ID: {production_id}",
        }

    async def _handle_resume(self, cmd: ParsedCommand) -> dict[str, Any]:
        """실패한 프로덕션 재개."""
        if not cmd.production_id:
            sm = StateManager()
            resumable = sm.get_resumable()
            if resumable:
                ids = [r["production_id"] for r in resumable[:5]]
                return {
                    "status": "select_production",
                    "message": f"어떤 프로덕션을 재개할까요? {ids}",
                    "resumable": resumable[:5],
                }
            return {
                "status": "none_resumable",
                "message": "재개 가능한 프로덕션이 없습니다.",
            }

        sm = StateManager()
        ct = CostTracker(state_manager=sm)
        orch = Orchestrator(state_manager=sm, cost_tracker=ct, dry_run=self.dry_run or cmd.dry_run)

        await orch.resume(cmd.production_id)
        total_cost = ct.get_production_cost(cmd.production_id)

        return {
            "status": "completed",
            "production_id": cmd.production_id,
            "total_cost": f"${total_cost:.4f}",
            "message": f"재개 완료! ID: {cmd.production_id}",
        }

    async def _handle_status(self, cmd: ParsedCommand) -> dict[str, Any]:
        """프로덕션 상태 조회."""
        sm = StateManager()
        prods = sm.list_productions(channel_id=cmd.channel_id or None)

        return {
            "status": "ok",
            "productions": [
                {
                    "id": p["production_id"],
                    "channel": p["channel_id"],
                    "topic": p["topic"],
                    "stage": p["current_stage"],
                    "status": p["status"],
                }
                for p in prods
            ],
            "total": len(prods),
            "message": f"총 {len(prods)}개 프로덕션",
        }

    async def _handle_costs(self, cmd: ParsedCommand) -> dict[str, Any]:
        """비용 조회."""
        sm = StateManager()
        month = cmd.month or None
        total = sm.get_total_cost(month=month)

        return {
            "status": "ok",
            "month": month or "전체",
            "total_cost": f"${total:.4f}",
            "message": f"{'이번 달' if month else '전체'} 비용: ${total:.4f}",
        }

    async def _handle_analyze(self, cmd: ParsedCommand) -> dict[str, Any]:
        """성과 분석."""
        return {
            "status": "info",
            "message": "성과 분석은 YouTube API 키와 영상 ID가 필요합니다.",
            "usage": "analyze --video-ids VID1,VID2 --channel finance",
        }

    async def _handle_shorts(self, cmd: ParsedCommand) -> dict[str, Any]:
        """Shorts 생성."""
        if not cmd.production_id:
            return {
                "status": "missing_production",
                "message": "어떤 영상에서 Shorts를 만들까요? 프로덕션 ID를 알려주세요.",
            }

        return {
            "status": "info",
            "production_id": cmd.production_id,
            "message": f"Shorts 생성: {cmd.production_id} (구현 준비 완료)",
        }

    async def _handle_unknown(self, cmd: ParsedCommand) -> dict[str, Any]:
        """알 수 없는 명령."""
        return {
            "status": "unknown",
            "message": "이해하지 못한 명령입니다.",
            "available_commands": [
                "영상 만들기 (produce)",
                "재개 (resume)",
                "상태 확인 (status)",
                "비용 조회 (costs)",
                "성과 분석 (analyze)",
                "Shorts 생성 (shorts)",
            ],
        }
