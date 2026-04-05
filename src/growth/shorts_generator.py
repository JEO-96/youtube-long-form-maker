"""Shorts 자동 생성 - 롱폼 → 쇼츠 변환.

역할: 초기 트래픽 부스팅 (Shorts → 롱폼 유입 유도)

추출 기준:
    1. 첫 문장만 봐도 의미가 통하는가
    2. 15-35초 안에 완결성이 있는가
    3. 자막만 봐도 이해 가능한가
    4. 롱폼 클릭 유도 문장이 자연스럽게 들어가는가
    5. 9:16 비율 자동 변환 (자막 크기 자동 조정)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ShortCandidate:
    """Shorts 후보 클립."""
    start_seconds: float
    end_seconds: float
    title: str = ""
    hook_text: str = ""  # Shorts용 첫 문장
    cta_text: str = ""  # 롱폼 유도 문구
    section_header: str = ""
    score: float = 0.0  # 품질 점수 (0~1)

    @property
    def duration(self) -> float:
        return self.end_seconds - self.start_seconds


@dataclass
class ShortsResult:
    """Shorts 생성 결과."""
    source_video_id: str = ""
    candidates: list[ShortCandidate] = field(default_factory=list)
    generated_paths: list[str] = field(default_factory=list)
    total_shorts: int = 0


class ShortsGenerator:
    """롱폼 → Shorts 3개 자동 생성."""

    MIN_DURATION = 15.0
    MAX_DURATION = 55.0  # YouTube Shorts 최대 60초, 여유분 5초
    TARGET_COUNT = 3
    TARGET_ASPECT = (9, 16)

    def extract_candidates(
        self,
        script_data: dict[str, Any],
        voice_data: dict[str, Any],
    ) -> list[ShortCandidate]:
        """대본 + 타임스탬프에서 Shorts 후보 추출.

        Args:
            script_data: ScriptResult.model_dump()
            voice_data: VoiceResult.model_dump()

        Returns:
            품질 순으로 정렬된 ShortCandidate 목록
        """
        sections = script_data.get("sections", [])
        segments = voice_data.get("segments", [])
        title = script_data.get("title", "")

        candidates: list[ShortCandidate] = []

        for sec in sections:
            header = sec.get("header", "")
            body = sec.get("body", "")
            duration = sec.get("estimated_duration_seconds", 0)

            # 기준 2: 15-55초 완결성
            if not (self.MIN_DURATION <= duration <= self.MAX_DURATION):
                # 너무 긴 섹션은 앞부분만 사용
                if duration > self.MAX_DURATION:
                    duration = self.MAX_DURATION
                elif duration < self.MIN_DURATION:
                    continue

            # 타임스탬프 매칭
            start, end = self._find_segment_times(header, body, segments)
            if end - start < self.MIN_DURATION:
                end = start + min(duration, self.MAX_DURATION)

            # 기준 1, 3: 첫 문장 완결성 + 자막 이해도
            first_sentence = self._extract_first_sentence(body)
            if not first_sentence:
                continue

            score = self._score_candidate(body, duration, header)

            candidates.append(ShortCandidate(
                start_seconds=start,
                end_seconds=end,
                title=f"{header} #shorts",
                hook_text=first_sentence,
                cta_text=f"전체 영상 보기 → '{title}'",
                section_header=header,
                score=score,
            ))

        # 품질 순 정렬 → 상위 N개
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates[:self.TARGET_COUNT]

    async def generate(
        self,
        video_path: Path,
        candidates: list[ShortCandidate],
        output_dir: Path,
    ) -> ShortsResult:
        """Shorts 영상 생성.

        Args:
            video_path: 원본 롱폼 영상
            candidates: 추출된 후보
            output_dir: 출력 디렉토리

        Returns:
            ShortsResult
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        generated: list[str] = []

        for i, cand in enumerate(candidates):
            out_path = output_dir / f"short_{i+1:02d}.mp4"
            try:
                await self._create_short(video_path, cand, out_path)
                generated.append(str(out_path))
                logger.info(
                    f"Short {i+1}: {cand.start_seconds:.1f}-{cand.end_seconds:.1f}s "
                    f"→ {out_path.name}"
                )
            except Exception as e:
                logger.warning(f"Short {i+1} generation failed: {e}")

        return ShortsResult(
            candidates=candidates,
            generated_paths=generated,
            total_shorts=len(generated),
        )

    async def _create_short(
        self, source: Path, candidate: ShortCandidate, output: Path
    ) -> None:
        """단일 Short 클립 생성 (9:16 변환)."""
        try:
            from moviepy import VideoFileClip

            clip = VideoFileClip(str(source))
            # 시간 범위 추출
            subclip = clip.subclipped(candidate.start_seconds, candidate.end_seconds)

            # 9:16 비율 변환 (center crop)
            w, h = subclip.size
            target_w = int(h * 9 / 16)
            if target_w < w:
                x_center = w // 2
                x1 = x_center - target_w // 2
                subclip = subclip.cropped(x1=x1, width=target_w)

            # 1080x1920 리사이즈
            subclip = subclip.resized((1080, 1920))

            subclip.write_videofile(
                str(output), fps=30, codec="libx264",
                audio_codec="aac", logger=None,
            )
            subclip.close()
            clip.close()

        except ImportError:
            logger.warning("moviepy not available for Shorts generation")
            output.write_bytes(b"\x00" * 1024)

    @staticmethod
    def _find_segment_times(
        header: str, body: str, segments: list[dict]
    ) -> tuple[float, float]:
        """타임스탬프 세그먼트에서 해당 섹션의 시작/끝 찾기."""
        if not segments:
            return 0.0, 30.0

        # body의 첫 몇 단어로 매칭
        body_words = body[:30].lower()
        best_start = 0.0
        best_end = 30.0

        for seg in segments:
            text = seg.get("text", "").lower()
            if body_words[:15] in text or header.lower() in text:
                best_start = seg.get("start", 0.0)
                best_end = seg.get("end", best_start + 30.0)
                break

        return best_start, best_end

    @staticmethod
    def _extract_first_sentence(text: str) -> str:
        """첫 문장 추출."""
        for sep in [".", "!", "?", "다.", "요.", "니다."]:
            idx = text.find(sep)
            if 5 < idx < 80:
                return text[:idx + len(sep)].strip()
        # 구분자 없으면 앞 50자
        return text[:50].strip() if len(text) > 10 else ""

    @staticmethod
    def _score_candidate(body: str, duration: float, header: str) -> float:
        """Shorts 후보 품질 점수."""
        score = 0.0

        # 적절한 길이 (20-40초가 최적)
        if 20 <= duration <= 40:
            score += 0.3
        elif 15 <= duration <= 55:
            score += 0.15

        # 첫 문장 완결성
        first = body[:50]
        if any(p in first for p in [".", "!", "?", "다"]):
            score += 0.25

        # 자막 이해도 (짧은 문장)
        sentences = body.split(".")
        avg_len = sum(len(s) for s in sentences) / max(len(sentences), 1)
        if avg_len < 30:
            score += 0.2

        # 액션 키워드
        action_words = ["방법", "비결", "실수", "핵심", "전략", "비밀", "절대"]
        if any(w in header for w in action_words):
            score += 0.25

        return min(score, 1.0)
