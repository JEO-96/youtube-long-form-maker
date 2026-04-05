"""패턴 인터럽트 타임라인 생성 - 시청 이탈 방지."""

from __future__ import annotations

import logging
import random
from enum import Enum

from pydantic import BaseModel

from ..core.config import load_settings

logger = logging.getLogger(__name__)


class InterruptType(str, Enum):
    """패턴 인터럽트 이벤트 타입."""
    ZOOM = "zoom"                           # 화면 줌인/줌아웃
    SUBTITLE_EMPHASIS = "subtitle_emphasis"  # 자막 강조 (크기/색상 변화)
    SCENE_CHANGE = "scene_change"           # 장면 전환
    SFX_HIT = "sfx_hit"                     # 효과음 삽입


# 이벤트 타입별 가중치 (자연스러운 분배)
DEFAULT_WEIGHTS: dict[InterruptType, float] = {
    InterruptType.ZOOM: 0.30,
    InterruptType.SUBTITLE_EMPHASIS: 0.30,
    InterruptType.SCENE_CHANGE: 0.25,
    InterruptType.SFX_HIT: 0.15,
}


class InterruptEvent(BaseModel):
    """단일 패턴 인터럽트 이벤트."""
    timestamp: float          # 초
    interrupt_type: InterruptType
    intensity: float = 1.0    # 0.5~1.5 (강도 변동)


class InterruptTimeline(BaseModel):
    """전체 패턴 인터럽트 타임라인."""
    events: list[InterruptEvent]
    total_duration: float
    average_interval: float
    event_count: int


class PatternInterruptEngine:
    """패턴 인터럽트 타임라인 생성 엔진."""

    def __init__(self, seed: int | None = None) -> None:
        settings = load_settings()
        pi_cfg = settings.retention.pattern_interrupt
        timing_cfg = settings.retention.timing_offset

        self.min_interval = pi_cfg.min_interval
        self.max_interval = pi_cfg.max_interval
        self.timing_offset_min = timing_cfg.min
        self.timing_offset_max = timing_cfg.max
        self._rng = random.Random(seed)

    def generate_timeline(
        self,
        total_duration: float,
        start_offset: float = 0.0,
        min_interval: float = 0.0,
        max_interval: float = 0.0,
        weights: dict[InterruptType, float] | None = None,
    ) -> InterruptTimeline:
        """패턴 인터럽트 타임라인 생성.

        Args:
            total_duration: 총 영상 길이 (초)
            start_offset: 첫 인터럽트 시작점 (초). Hook 이후 시작 권장.
            min_interval: 최소 간격 (초). 0이면 settings.yaml 값 사용.
            max_interval: 최대 간격 (초). 0이면 settings.yaml 값 사용.
            weights: 이벤트 타입별 가중치. None이면 기본값 사용.
        """
        if total_duration <= 0:
            raise ValueError(f"total_duration은 양수여야 합니다: {total_duration}")

        min_iv = min_interval or self.min_interval
        max_iv = max_interval or self.max_interval

        if min_iv <= 0 or max_iv <= 0:
            raise ValueError(f"interval은 양수여야 합니다: min={min_iv}, max={max_iv}")
        if min_iv > max_iv:
            raise ValueError(f"min_interval({min_iv}) > max_interval({max_iv})")

        # start_offset 보정
        if start_offset < 0:
            start_offset = 0.0
        if start_offset >= total_duration:
            logger.warning(f"start_offset({start_offset}) >= total_duration({total_duration}), 빈 타임라인 반환")
            return InterruptTimeline(
                events=[], total_duration=total_duration,
                average_interval=0.0, event_count=0,
            )

        weights = weights or DEFAULT_WEIGHTS
        events: list[InterruptEvent] = []
        current_time = start_offset

        # 영상 끝 여유 (마지막 2초는 CTA 구간이므로 인터럽트 안 넣음)
        end_margin = min(2.0, total_duration * 0.05)

        while current_time < (total_duration - end_margin):
            # Jitter 포함 간격
            interval = self._rng.uniform(min_iv, max_iv)

            # 타이밍 오프셋 (자연스러운 느낌)
            offset = self._rng.uniform(self.timing_offset_min, self.timing_offset_max)
            offset *= self._rng.choice([-1, 1])  # 앞뒤 랜덤

            next_time = current_time + interval + offset
            next_time = max(current_time + min_iv * 0.5, next_time)  # 너무 촘촘하지 않게

            if next_time >= (total_duration - end_margin):
                break

            # 이벤트 타입 선택 (가중치 기반)
            event_type = self._weighted_choice(weights)

            # 강도 변동 (0.7~1.3 범위)
            intensity = self._rng.uniform(0.7, 1.3)

            events.append(InterruptEvent(
                timestamp=round(next_time, 1),
                interrupt_type=event_type,
                intensity=round(intensity, 2),
            ))

            current_time = next_time

        # 통계 계산
        avg_interval = 0.0
        if len(events) >= 2:
            intervals = [events[i+1].timestamp - events[i].timestamp for i in range(len(events)-1)]
            avg_interval = sum(intervals) / len(intervals)

        timeline = InterruptTimeline(
            events=events,
            total_duration=total_duration,
            average_interval=round(avg_interval, 2),
            event_count=len(events),
        )

        logger.info(
            f"Pattern interrupt: {len(events)} events over {total_duration}s "
            f"(avg interval: {avg_interval:.1f}s)"
        )
        return timeline

    def _weighted_choice(self, weights: dict[InterruptType, float]) -> InterruptType:
        """가중치 기반 랜덤 선택."""
        types = list(weights.keys())
        w = list(weights.values())
        return self._rng.choices(types, weights=w, k=1)[0]
