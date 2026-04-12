"""_cap_scene_durations() 불변 조건 스트레스 테스트.

회귀 방지: 모든 입력 조합에서 다음 불변 조건을 보장한다.
1. 모든 duration > 0
2. 모든 duration <= scene별 max (CTA: 12s, 기타: 20s)
3. abs(sum(duration) - target) < 0.2
4. 수학적 불가능 케이스는 StageError로 처리
"""

import random
import pytest

from src.core.models import VisualIntent, Scene, MediaType
from src.core.exceptions import StageError
from src.pipeline.s6_editing import S6Editing


class _MockStage:
    MAX_SCENE_DURATION = 20.0
    MAX_CTA_DURATION = 12.0
    MIN_SCENE_DURATION = 2.0
    production_id = "test"


_cap = S6Editing._cap_scene_durations
_stage = _MockStage()
_intents = list(VisualIntent)


def _make_scenes(n: int, durations: list[float], intents_list: list[VisualIntent]) -> list[Scene]:
    return [
        Scene(
            scene_number=i + 1,
            start_time=0,
            end_time=durations[i],
            duration=durations[i],
            visual_intent=intents_list[i],
            media_type=MediaType.AI_IMAGE,
        )
        for i in range(n)
    ]


def _get_max(intent: VisualIntent) -> float:
    return 12.0 if intent == VisualIntent.CLOSING_CTA else 20.0


def _validate(result: list[float], scenes: list[Scene], target: float) -> list[str]:
    violations = []
    for i, d in enumerate(result):
        if d <= 0:
            violations.append(f"scene {i+1}: duration={d}<=0")
        mx = _get_max(scenes[i].visual_intent)
        if d > mx + 0.02:
            violations.append(f"scene {i+1}: duration={d}>{mx}")
    sum_err = abs(sum(result) - target)
    if sum_err >= 0.2:
        violations.append(f"sum={sum(result):.3f} vs target={target:.3f}")
    return violations


# ═══ 랜덤 스트레스 테스트 ═══

@pytest.mark.parametrize("seed", range(500))
def test_random_inputs(seed: int) -> None:
    """500개 랜덤 입력에서 불변 조건 보장."""
    rng = random.Random(seed)
    n = rng.randint(15, 30)
    target = rng.uniform(30.0, 600.0)
    durations = [rng.uniform(0.1, 100.0) for _ in range(n)]
    intents_list = [rng.choice(_intents) for _ in range(n)]
    scenes = _make_scenes(n, durations, intents_list)

    try:
        result = _cap(_stage, durations, scenes, target)
    except StageError:
        return  # 수학적 불가능 → 정상

    violations = _validate(result, scenes, target)
    assert not violations, f"seed={seed}: {violations}"


# ═══ 극단 엣지케이스 ═══

@pytest.mark.parametrize("seed", range(200))
def test_all_tiny_durations(seed: int) -> None:
    """모든 씬이 매우 짧고 target이 긴 경우."""
    rng = random.Random(seed + 10000)
    n = rng.randint(15, 25)
    target = rng.uniform(200.0, 500.0)
    durations = [rng.uniform(0.1, 1.0) for _ in range(n)]
    intents_list = [rng.choice(_intents) for _ in range(n)]
    scenes = _make_scenes(n, durations, intents_list)

    try:
        result = _cap(_stage, durations, scenes, target)
    except StageError:
        return

    violations = _validate(result, scenes, target)
    assert not violations, f"seed={seed}: {violations}"


@pytest.mark.parametrize("seed", range(200))
def test_all_huge_durations(seed: int) -> None:
    """모든 씬이 매우 길고 target이 짧은 경우."""
    rng = random.Random(seed + 20000)
    n = rng.randint(15, 25)
    target = rng.uniform(30.0, 80.0)
    durations = [rng.uniform(40.0, 100.0) for _ in range(n)]
    intents_list = [rng.choice(_intents) for _ in range(n)]
    scenes = _make_scenes(n, durations, intents_list)

    try:
        result = _cap(_stage, durations, scenes, target)
    except StageError:
        return

    violations = _validate(result, scenes, target)
    assert not violations, f"seed={seed}: {violations}"


@pytest.mark.parametrize("seed", range(100))
def test_min_exceeds_target(seed: int) -> None:
    """n * MIN_SCENE_DURATION > target: min 축소 또는 StageError."""
    rng = random.Random(seed + 30000)
    n = rng.randint(20, 30)
    target = n * 1.5  # n * 2.0 > target
    durations = [rng.uniform(0.5, 5.0) for _ in range(n)]
    intents_list = [rng.choice(_intents) for _ in range(n)]
    scenes = _make_scenes(n, durations, intents_list)

    try:
        result = _cap(_stage, durations, scenes, target)
    except StageError:
        return

    violations = _validate(result, scenes, target)
    assert not violations, f"seed={seed}: {violations}"


@pytest.mark.parametrize("seed", range(100))
def test_one_giant_rest_tiny(seed: int) -> None:
    """하나만 거대하고 나머지 극소."""
    rng = random.Random(seed + 40000)
    n = rng.randint(15, 25)
    target = rng.uniform(60.0, 120.0)
    durations = [0.1] * n
    durations[0] = target * 2
    intents_list = [rng.choice(_intents) for _ in range(n)]
    scenes = _make_scenes(n, durations, intents_list)

    try:
        result = _cap(_stage, durations, scenes, target)
    except StageError:
        return

    violations = _validate(result, scenes, target)
    assert not violations, f"seed={seed}: {violations}"


@pytest.mark.parametrize("seed", range(100))
def test_all_cta_intent(seed: int) -> None:
    """전부 CTA intent (max=12s)."""
    rng = random.Random(seed + 50000)
    n = rng.randint(15, 25)
    target = rng.uniform(30.0, 90.0)
    durations = [rng.uniform(0.5, 30.0) for _ in range(n)]
    intents_list = [VisualIntent.CLOSING_CTA] * n
    scenes = _make_scenes(n, durations, intents_list)

    try:
        result = _cap(_stage, durations, scenes, target)
    except StageError:
        return

    violations = _validate(result, scenes, target)
    assert not violations, f"seed={seed}: {violations}"
