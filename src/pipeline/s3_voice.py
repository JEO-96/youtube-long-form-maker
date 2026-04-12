"""S3 음성 합성 + 자막 생성."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from pydub import AudioSegment
from pydub.generators import Sine

from ..core.models import Stage, ScriptResult, VoiceResult, TimedSegment, SectionTiming
from .base_stage import BaseStage

logger = logging.getLogger(__name__)


def _resolve_channel_voice_id(raw_voice_id: str) -> str:
    """채널 설정의 보이스 ID를 안전하게 정규화 — 환경변수 치환 포함."""
    import os
    voice_id = raw_voice_id.strip()
    if voice_id.startswith("${") and voice_id.endswith("}"):
        env_key = voice_id[2:-1]
        resolved = os.getenv(env_key, "")
        if resolved:
            return resolved
        return ""
    return voice_id


class S3Voice(BaseStage):
    """S3: TTS 음성 합성 + Whisper 자막."""

    stage = Stage.VOICE

    async def run(self, **kwargs: Any) -> VoiceResult:
        script_data = self.load_previous(Stage.SCRIPT)
        script = ScriptResult(**script_data)

        self.stage_dir.mkdir(parents=True, exist_ok=True)
        audio_path = self.stage_dir / "narration.mp3"
        srt_path = self.stage_dir / "narration.srt"

        if self.dry_run:
            return self._mock_result(script, audio_path, srt_path)

        # 채널 설정 기반 TTS 선택 (기본: elevenlabs)
        from ..providers.factory import create_tts
        tts_provider_name = self.channel.providers.tts
        channel_voice_id = _resolve_channel_voice_id(self.channel.providers.voice_id)
        tts = create_tts(tts_provider_name, fallback="elevenlabs")

        logger.info(
            f"TTS: provider={tts_provider_name}, voice_id={channel_voice_id!r}, "
            f"text_len={len(script.full_text)}"
        )

        await tts.synthesize(
            script.full_text,
            voice_id=channel_voice_id,
            output_path=audio_path,
        )
        char_count = len(script.full_text)
        self.record_cost(tts_provider_name, "synthesize", units=char_count, unit_cost=tts.estimate_cost("x"))

        # Whisper STT → 타임스탬프 (1회 전사 → 세그먼트 + SRT 동시 생성)
        segments: list[TimedSegment] = []
        total_duration = 0.0
        try:
            from ..providers.stt import WhisperSTT
            stt = WhisperSTT()
            # 1회만 전사하고, 그 결과로 SRT도 생성
            raw_segments = stt.transcribe(audio_path)
            stt.transcribe_to_srt(audio_path, srt_path, segments=raw_segments)

            for seg in raw_segments:
                segments.append(TimedSegment(
                    text=seg["text"], start=seg["start"], end=seg["end"],
                    confidence=seg.get("confidence", 0.0),
                ))
            if segments:
                total_duration = segments[-1].end
        except Exception as e:
            logger.warning(f"Whisper STT failed (non-critical): {e}")
            # 음성 파일의 길이로 대체 (mutagen 사용 - ffprobe 불필요)
            from mutagen.mp3 import MP3
            mp3_info = MP3(str(audio_path))
            total_duration = mp3_info.info.length

        # Whisper 실패 시 비례 분배 자막 생성
        if not segments:
            segments = self._generate_proportional_segments(script, total_duration)
            if segments:
                self._generate_proportional_srt(segments, srt_path)
                logger.info(f"Proportional subtitle fallback: {len(segments)} segments over {total_duration:.1f}s")

        self.record_cost("whisper", "transcribe", units=total_duration / 60.0, unit_cost=0.0)

        # ═══ 섹션별 실제 타이밍 계산 (STT 세그먼트 → 스크립트 섹션 매핑) ═══
        section_timings = self._compute_section_timings(script, segments, total_duration)

        return VoiceResult(
            audio_path=str(audio_path),
            srt_path=str(srt_path) if srt_path.exists() else "",
            total_duration_seconds=total_duration,
            segments=segments,
            section_timings=section_timings,
            tts_provider=tts_provider_name,
            voice_id=channel_voice_id or tts.default_voice_id,
        )

    def _compute_section_timings(
        self,
        script: ScriptResult,
        segments: list[TimedSegment],
        total_duration: float,
    ) -> list[SectionTiming]:
        """STT 세그먼트를 스크립트 섹션에 매핑하여 섹션별 실제 타이밍 생성.

        매핑 방식: 스크립트 텍스트와 세그먼트 텍스트를 순차적으로 누적하면서
        각 섹션의 글자수 경계에 도달할 때 세그먼트를 해당 섹션에 할당.
        Whisper 세그먼트가 없으면 비례 분배 폴백.
        """
        # 스크립트 블록 구성
        blocks: list[tuple[str, str]] = []  # (label, text)
        hook_text = " ".join(p for p in [script.hook, script.intro] if p).strip()
        blocks.append(("hook", hook_text or "hook"))
        for i, sec in enumerate(script.sections):
            blocks.append((f"section_{i+1}", sec.body.strip() or sec.header or "section"))
        cta_text = " ".join(p for p in [script.cta, script.outro] if p).strip()
        blocks.append(("cta", cta_text or "cta"))

        n_blocks = len(blocks)

        if not segments or len(segments) < 2:
            # 세그먼트 없으면 비례 분배
            return self._proportional_section_timings(blocks, total_duration)

        # ═══ 세그먼트 → 섹션 매핑 (순차 글자수 누적) ═══
        script_block_chars = [len(b[1]) for b in blocks]
        script_total_chars = sum(script_block_chars)
        if script_total_chars == 0:
            return self._proportional_section_timings(blocks, total_duration)

        # 각 블록 경계의 누적 비율
        block_boundary_ratios: list[float] = []
        cum = 0.0
        for bc in script_block_chars:
            cum += bc / script_total_chars
            block_boundary_ratios.append(cum)

        # 세그먼트 누적 글자수로 블록 할당
        seg_total_chars = sum(len(seg.text) for seg in segments)
        if seg_total_chars == 0:
            return self._proportional_section_timings(blocks, total_duration)

        # 각 블록에 할당된 세그먼트 인덱스 리스트
        block_segment_indices: list[list[int]] = [[] for _ in range(n_blocks)]
        seg_cum_chars = 0
        current_block = 0

        for si, seg in enumerate(segments):
            seg_cum_chars += len(seg.text)
            seg_ratio = seg_cum_chars / seg_total_chars

            # 현재 블록의 경계를 넘었으면 다음 블록으로
            while (current_block < n_blocks - 1
                   and seg_ratio > block_boundary_ratios[current_block] + 0.005):
                current_block += 1

            block_segment_indices[current_block].append(si)

        # ═══ SectionTiming 생성 ═══
        timings: list[SectionTiming] = []
        for bi in range(n_blocks):
            label = blocks[bi][0]
            indices = block_segment_indices[bi]

            if indices:
                first_seg = segments[indices[0]]
                last_seg = segments[indices[-1]]
                start = first_seg.start
                end = last_seg.end
            else:
                # 세그먼트가 할당되지 않은 블록 — 인접 블록에서 추정
                if bi > 0 and timings:
                    start = timings[-1].end
                else:
                    start = 0.0
                end = start + 2.0  # 최소 2초

            # 마지막 블록은 오디오 끝까지
            if bi == n_blocks - 1:
                end = max(end, total_duration)

            dur = round(end - start, 3)
            timings.append(SectionTiming(
                section_index=bi,
                section_label=label,
                start=round(start, 3),
                end=round(end, 3),
                duration=max(dur, 1.0),
                segment_indices=indices,
            ))

        logger.info(
            f"Section timings computed: {len(timings)} sections, "
            + ", ".join(f"{t.section_label}={t.start:.1f}-{t.end:.1f}s" for t in timings)
        )
        return timings

    def _proportional_section_timings(
        self,
        blocks: list[tuple[str, str]],
        total_duration: float,
    ) -> list[SectionTiming]:
        """세그먼트 없을 때 글자수 비례 분배 (최후 폴백)."""
        total_chars = sum(len(b[1]) for b in blocks)
        if total_chars == 0:
            total_chars = len(blocks)

        timings: list[SectionTiming] = []
        cursor = 0.0
        for bi, (label, text) in enumerate(blocks):
            dur = (len(text) / total_chars) * total_duration
            dur = max(dur, 1.5)
            timings.append(SectionTiming(
                section_index=bi,
                section_label=label,
                start=round(cursor, 3),
                end=round(cursor + dur, 3),
                duration=round(dur, 3),
                segment_indices=[],
            ))
            cursor += dur

        # 마지막 블록 보정
        if timings:
            timings[-1].end = round(total_duration, 3)
            timings[-1].duration = round(timings[-1].end - timings[-1].start, 3)

        return timings

    def _generate_proportional_segments(
        self, script: ScriptResult, total_duration: float
    ) -> list[TimedSegment]:
        """대본 텍스트를 글자 수 비례로 타임스탬프 배분.

        한국어 문장/호흡 단위 기준으로 분할.
        """
        if total_duration <= 0:
            return []

        # 텍스트 블록 구성: [hook+intro] + [section bodies] + [cta+outro]
        blocks: list[str] = []
        hook_intro = " ".join(part for part in [script.hook, script.intro] if part).strip()
        if hook_intro:
            blocks.append(hook_intro)
        for sec in script.sections:
            if sec.body.strip():
                blocks.append(sec.body.strip())
        cta_outro = " ".join(part for part in [script.cta, script.outro] if part).strip()
        if cta_outro:
            blocks.append(cta_outro)

        if not blocks:
            return []

        total_chars = sum(len(b) for b in blocks)
        if total_chars == 0:
            return []

        segments: list[TimedSegment] = []
        cursor = 0.0

        for block in blocks:
            block_dur = (len(block) / total_chars) * total_duration

            # 한국어 문장 경계로 분할 (다/요/죠/까/니다/세요 등 어미 + 구두점)
            sub_texts = self._split_korean_sentences(block)

            if not sub_texts:
                sub_texts = [block]

            sub_total_chars = sum(len(s) for s in sub_texts)
            if sub_total_chars == 0:
                continue

            for sub_text in sub_texts:
                sub_dur = (len(sub_text) / sub_total_chars) * block_dur
                seg_start = round(cursor, 3)
                seg_end = round(cursor + sub_dur, 3)
                segments.append(TimedSegment(
                    text=sub_text,
                    start=seg_start,
                    end=seg_end,
                    confidence=0.5,
                ))
                cursor = seg_end

        # 마지막 세그먼트가 total_duration에 정확히 도달하도록 보정
        if segments:
            segments[-1].end = round(total_duration, 3)

        return segments

    @staticmethod
    def _split_korean_sentences(text: str) -> list[str]:
        """한국어 문장/호흡 단위 분할.

        한국어 어미(다, 요, 죠, 까, 니다, 세요 등) + 구두점 기반 분할.
        80자 초과 문장은 쉼표/조사 경계에서 추가 분할.
        """
        # 한국어 문장 종결 패턴: 어미+구두점, 또는 구두점 단독
        sentence_pattern = re.compile(
            r'(?<=[다요죠까니])[.!?]\s*'  # 한국어 어미 + 구두점
            r'|(?<=[.!?])\s+'              # 일반 구두점 후 공백
            r'|(?<=[.!?])\n'               # 구두점 후 줄바꿈
        )

        raw_parts = sentence_pattern.split(text)
        raw_parts = [p.strip() for p in raw_parts if p and p.strip()]

        # 80자 초과 문장 추가 분할
        final: list[str] = []
        for part in raw_parts:
            if len(part) <= 80:
                final.append(part)
            else:
                # 쉼표/중간 호흡 단위로 분할
                breath_parts = re.split(r'[,，]\s*|(?<=고)\s+|(?<=며)\s+|(?<=서)\s+', part)
                merged = ""
                for bp in breath_parts:
                    bp = bp.strip()
                    if not bp:
                        continue
                    candidate = f"{merged} {bp}" if merged else bp
                    if len(candidate) > 60 and merged:
                        final.append(merged)
                        merged = bp
                    else:
                        merged = candidate
                if merged:
                    final.append(merged)

        return final if final else [text]

    def _generate_proportional_srt(
        self, segments: list[TimedSegment], srt_path: Path
    ) -> None:
        """segments를 SRT 파일로 저장."""
        srt_lines: list[str] = []
        for i, seg in enumerate(segments, 1):
            start_ts = self._srt_time(seg.start)
            end_ts = self._srt_time(seg.end)
            srt_lines.extend([str(i), f"{start_ts} --> {end_ts}", seg.text, ""])
        srt_path.write_text("\n".join(srt_lines), encoding="utf-8")

    def _mock_result(
        self, script: ScriptResult, audio_path: Path, srt_path: Path
    ) -> VoiceResult:
        """Mock: 대본 길이에 비례하는 테스트 오디오 + 가상 타임스탬프."""
        # 대본 estimated_duration 기반, 최소 10초 / 최대 60초
        duration_ms = int(script.estimated_duration_seconds * 1000) if script.estimated_duration_seconds > 0 else 10000
        duration_ms = max(10000, min(duration_ms, 60000))

        audio = AudioSegment.silent(duration=500)
        audio += Sine(440).to_audio_segment(duration=duration_ms - 1000).apply_gain(-10)
        audio += AudioSegment.silent(duration=500)
        audio.export(str(audio_path), format="mp3")

        total_dur = duration_ms / 1000.0

        # 가상 세그먼트 — 글자수 비례 배분
        segments = []
        t = 0.5
        usable_dur = total_dur - 1.0  # 앞뒤 0.5초 마진
        char_counts = [max(len(sec.body), 10) for sec in script.sections]
        total_chars = sum(char_counts)
        for i, sec in enumerate(script.sections):
            seg_dur = (char_counts[i] / total_chars) * usable_dur
            seg_dur = max(1.0, seg_dur)
            if t + seg_dur > total_dur - 0.3:
                seg_dur = max(1.0, total_dur - t - 0.3)
            segments.append(TimedSegment(
                text=sec.body[:50], start=round(t, 2), end=round(t + seg_dur, 2),
            ))
            t += seg_dur + 0.3
            if t >= total_dur:
                break

        # SRT 생성
        srt_lines = []
        for i, seg in enumerate(segments, 1):
            start_ts = self._srt_time(seg.start)
            end_ts = self._srt_time(seg.end)
            srt_lines.extend([str(i), f"{start_ts} --> {end_ts}", seg.text, ""])
        srt_path.write_text("\n".join(srt_lines), encoding="utf-8")

        self.record_cost("elevenlabs", "synthesize_mock", units=len(script.full_text), unit_cost=0.0)

        return VoiceResult(
            audio_path=str(audio_path),
            srt_path=str(srt_path),
            total_duration_seconds=total_dur,
            segments=segments,
            tts_provider="mock",
            voice_id="mock_voice",
        )

    @staticmethod
    def _srt_time(seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds % 1) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
