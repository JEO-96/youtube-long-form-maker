"""A/B 테스팅 - 썸네일/제목 후보 생성 + 성과 비교.

실험 대상: 제목, 썸네일, 훅 오프닝, 영상 길이, 자막 스타일
판정 시점: 6시간, 24시간, 72시간
승자 기준: CTR, 30초 유지율, 평균 시청 지속 시간
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ABVariant:
    """A/B 테스트 변수."""
    variant_id: str
    variant_type: str  # title, thumbnail, hook, length, subtitle_style
    value: Any = None
    description: str = ""
    # 성과 데이터
    impressions: int = 0
    clicks: int = 0
    ctr: float = 0.0
    avg_watch_seconds: float = 0.0
    retention_30s: float = 0.0  # 30초 유지율 (%)

    @property
    def is_measured(self) -> bool:
        return self.impressions > 0


@dataclass
class ABExperiment:
    """단일 A/B 실험."""
    experiment_id: str
    video_id: str = ""
    channel_id: str = ""
    experiment_type: str = ""  # title, thumbnail, hook
    variants: list[ABVariant] = field(default_factory=list)
    created_at: str = ""
    status: str = "pending"  # pending, running, concluded
    winner_id: str = ""
    conclusion_reason: str = ""
    judged_at: str = ""


class ABTestingEngine:
    """A/B 테스팅 엔진."""

    # 판정 기준
    MIN_IMPRESSIONS = 100  # 최소 노출수
    CONFIDENCE_THRESHOLD = 0.1  # CTR 차이 최소 10% relative

    def __init__(self, storage_path: Path | None = None) -> None:
        from ..core.config import DATA_DIR
        self.storage_path = storage_path or DATA_DIR / "ab_tests"
        self.storage_path.mkdir(parents=True, exist_ok=True)

    def create_experiment(
        self,
        video_id: str,
        channel_id: str,
        experiment_type: str,
        variants: list[dict[str, Any]],
    ) -> ABExperiment:
        """새 A/B 실험 생성.

        Args:
            video_id: YouTube 영상 ID
            channel_id: 채널 ID
            experiment_type: 실험 유형 (title, thumbnail, hook)
            variants: 변수 목록 [{"id": "A", "value": "...", "description": "..."}]

        Returns:
            ABExperiment
        """
        exp_id = f"ab_{channel_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        ab_variants = [
            ABVariant(
                variant_id=v.get("id", f"V{i}"),
                variant_type=experiment_type,
                value=v.get("value"),
                description=v.get("description", ""),
            )
            for i, v in enumerate(variants)
        ]

        experiment = ABExperiment(
            experiment_id=exp_id,
            video_id=video_id,
            channel_id=channel_id,
            experiment_type=experiment_type,
            variants=ab_variants,
            created_at=datetime.now().isoformat(),
            status="pending",
        )

        self._save_experiment(experiment)
        logger.info(f"A/B experiment created: {exp_id} ({experiment_type}, {len(ab_variants)} variants)")
        return experiment

    def update_metrics(
        self,
        experiment_id: str,
        variant_id: str,
        metrics: dict[str, Any],
    ) -> None:
        """변수의 성과 데이터 업데이트."""
        exp = self._load_experiment(experiment_id)
        if not exp:
            raise ValueError(f"Experiment not found: {experiment_id}")

        for v in exp.variants:
            if v.variant_id == variant_id:
                v.impressions = metrics.get("impressions", v.impressions)
                v.clicks = metrics.get("clicks", v.clicks)
                v.ctr = metrics.get("ctr", v.ctr)
                v.avg_watch_seconds = metrics.get("avg_watch_seconds", v.avg_watch_seconds)
                v.retention_30s = metrics.get("retention_30s", v.retention_30s)
                break

        if exp.status == "pending":
            exp.status = "running"

        self._save_experiment(exp)

    def judge(self, experiment_id: str) -> ABExperiment:
        """실험 판정 — 승자 결정.

        승자 기준 (가중 점수):
            CTR: 40%
            30초 유지율: 35%
            평균 시청 시간: 25%
        """
        exp = self._load_experiment(experiment_id)
        if not exp:
            raise ValueError(f"Experiment not found: {experiment_id}")

        measured = [v for v in exp.variants if v.is_measured]
        if len(measured) < 2:
            logger.warning(f"Not enough measured variants for {experiment_id}")
            return exp

        # 최소 노출수 체크
        if any(v.impressions < self.MIN_IMPRESSIONS for v in measured):
            logger.info(f"Waiting for more data (min {self.MIN_IMPRESSIONS} impressions)")
            return exp

        # 가중 점수 계산
        scores = {}
        for v in measured:
            score = (
                v.ctr * 0.4
                + v.retention_30s * 0.35
                + (v.avg_watch_seconds / 60.0) * 0.25  # 분 단위 정규화
            )
            scores[v.variant_id] = score

        # 승자 결정
        winner_id = max(scores, key=scores.get)  # type: ignore[arg-type]
        runner_up = sorted(scores.values(), reverse=True)

        # 유의미한 차이 확인
        if len(runner_up) >= 2 and runner_up[0] > 0:
            diff = (runner_up[0] - runner_up[1]) / runner_up[0]
            if diff < self.CONFIDENCE_THRESHOLD:
                exp.conclusion_reason = f"차이 미미 ({diff:.1%}), 추가 데이터 필요"
                self._save_experiment(exp)
                return exp

        exp.winner_id = winner_id
        exp.status = "concluded"
        exp.judged_at = datetime.now().isoformat()
        exp.conclusion_reason = (
            f"Winner: {winner_id} (score={scores[winner_id]:.2f}), "
            f"scores={scores}"
        )

        self._save_experiment(exp)
        logger.info(f"A/B concluded: {experiment_id} → winner={winner_id}")
        return exp

    def list_experiments(
        self, channel_id: str | None = None, status: str | None = None
    ) -> list[ABExperiment]:
        """실험 목록 조회."""
        experiments = []
        for f in self.storage_path.glob("ab_*.json"):
            exp = self._load_experiment_from_file(f)
            if exp:
                if channel_id and exp.channel_id != channel_id:
                    continue
                if status and exp.status != status:
                    continue
                experiments.append(exp)
        return sorted(experiments, key=lambda e: e.created_at, reverse=True)

    def _save_experiment(self, exp: ABExperiment) -> None:
        """실험 저장."""
        path = self.storage_path / f"{exp.experiment_id}.json"
        data = {
            "experiment_id": exp.experiment_id,
            "video_id": exp.video_id,
            "channel_id": exp.channel_id,
            "experiment_type": exp.experiment_type,
            "variants": [
                {
                    "variant_id": v.variant_id,
                    "variant_type": v.variant_type,
                    "value": v.value,
                    "description": v.description,
                    "impressions": v.impressions,
                    "clicks": v.clicks,
                    "ctr": v.ctr,
                    "avg_watch_seconds": v.avg_watch_seconds,
                    "retention_30s": v.retention_30s,
                }
                for v in exp.variants
            ],
            "created_at": exp.created_at,
            "status": exp.status,
            "winner_id": exp.winner_id,
            "conclusion_reason": exp.conclusion_reason,
            "judged_at": exp.judged_at,
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_experiment(self, experiment_id: str) -> ABExperiment | None:
        """실험 로드."""
        path = self.storage_path / f"{experiment_id}.json"
        return self._load_experiment_from_file(path)

    @staticmethod
    def _load_experiment_from_file(path: Path) -> ABExperiment | None:
        """파일에서 실험 로드."""
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        variants = [
            ABVariant(**v) for v in data.get("variants", [])
        ]
        return ABExperiment(
            experiment_id=data["experiment_id"],
            video_id=data.get("video_id", ""),
            channel_id=data.get("channel_id", ""),
            experiment_type=data.get("experiment_type", ""),
            variants=variants,
            created_at=data.get("created_at", ""),
            status=data.get("status", "pending"),
            winner_id=data.get("winner_id", ""),
            conclusion_reason=data.get("conclusion_reason", ""),
            judged_at=data.get("judged_at", ""),
        )
