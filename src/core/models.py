"""도메인 모델 - 파이프라인 스테이지 간 데이터 계약 (Source of Truth)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


# ═══ Enums ═══

class Stage(str, Enum):
    BENCHMARK = "benchmark"
    SCRIPT = "script"
    VOICE = "voice"
    STORYBOARD = "storyboard"
    MEDIA = "media"
    EDITING = "editing"
    THUMBNAIL = "thumbnail"
    EXPORT = "export"


class ProductionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class MediaType(str, Enum):
    AI_IMAGE = "ai_image"
    AI_VIDEO = "ai_video"
    STOCK_VIDEO = "stock_video"
    STOCK_IMAGE = "stock_image"


class TransitionType(str, Enum):
    CUT = "cut"
    FADE = "fade"
    DISSOLVE = "dissolve"
    SLIDE = "slide"
    ZOOM = "zoom"


# ═══ S1: Benchmark Result ═══

class CompetitorVideo(BaseModel):
    """경쟁 영상 메타데이터."""
    video_id: str
    title: str
    view_count: int = 0
    like_count: int = 0
    comment_count: int = 0
    published_at: str = ""
    tags: list[str] = []


class BenchmarkResult(BaseModel):
    """S1 벤치마킹 결과."""
    topic: str
    keywords: list[str] = []
    trend_velocity: float = 0.0  # Google Trends 속도
    competitor_videos: list[CompetitorVideo] = []
    content_gaps: list[str] = []  # 경쟁자가 다루지 않은 주제
    suggested_angle: str = ""
    analysis_summary: str = ""


# ═══ S2: Script Result ═══

class ScriptSection(BaseModel):
    """대본 섹션."""
    header: str
    body: str
    visual_prompt: str = ""  # 이 섹션에 맞는 영상 프롬프트 (JSON Mode 출력)
    tts_tags: dict[str, Any] = {}  # TTS 감정/속도 태그
    estimated_duration_seconds: float = 0.0


class ScriptResult(BaseModel):
    """S2 대본 결과."""
    title: str
    hook: str  # 첫 5초 Hook 텍스트
    intro: str
    sections: list[ScriptSection] = []
    cta: str  # Call to Action
    outro: str = ""
    full_text: str = ""
    word_count: int = 0
    estimated_duration_seconds: float = 0.0


# ═══ S3: Voice Result ═══

class TimedSegment(BaseModel):
    """단어/문장별 타임스탬프."""
    text: str
    start: float  # 초
    end: float  # 초
    confidence: float = 1.0


class VoiceResult(BaseModel):
    """S3 음성+자막 결과."""
    audio_path: str = ""
    srt_path: str = ""
    total_duration_seconds: float = 0.0
    segments: list[TimedSegment] = []
    tts_provider: str = ""
    voice_id: str = ""


# ═══ S4: Storyboard Result ═══

class Scene(BaseModel):
    """개별 씬."""
    scene_number: int
    start_time: float  # 초
    end_time: float  # 초
    duration: float = 0.0
    narration_text: str = ""
    visual_description: str = ""
    media_type: MediaType = MediaType.AI_IMAGE
    image_prompt: str = ""
    video_prompt: str = ""
    stock_search_query: str = ""  # 스톡 영상 검색 키워드
    transition: TransitionType = TransitionType.CUT
    is_hook: bool = False  # 첫 5초 Hook 씬
    visual_keywords: list[str] = []  # 화면에 팝업할 키워드


class StoryboardResult(BaseModel):
    """S4 스토리보드 결과."""
    scenes: list[Scene] = []
    total_scenes: int = 0
    ai_video_count: int = 0
    stock_video_count: int = 0
    ai_image_count: int = 0


# ═══ S5: Media Result ═══

class MediaAsset(BaseModel):
    """생성된 미디어 자산."""
    scene_number: int
    media_type: MediaType
    file_path: str = ""
    original_resolution: list[int] = []
    upscaled: bool = False
    generation_cost: float = 0.0
    provider: str = ""


class MediaResult(BaseModel):
    """S5 미디어 생성 결과."""
    assets: list[MediaAsset] = []
    total_cost: float = 0.0
    failed_scenes: list[int] = []  # 실패한 씬 번호 (재시도용)


# ═══ S6: Editing Result ═══

class EditingResult(BaseModel):
    """S6 편집 결과."""
    output_path: str = ""  # 최종 MP4
    capcut_project_path: str | None = None  # 선택적 CapCut 프로젝트
    duration_seconds: float = 0.0
    resolution: list[int] = [1920, 1080]
    file_size_mb: float = 0.0
    applied_effects: list[str] = []
    pattern_interrupts_count: int = 0


# ═══ S7: Thumbnail Result ═══

class ThumbnailResult(BaseModel):
    """S7 썸네일+메타데이터 결과."""
    thumbnail_path: str = ""
    thumbnail_candidates: list[str] = []  # 3-5개 후보 (A/B 테스트용)
    youtube_title: str = ""
    youtube_description: str = ""
    youtube_tags: list[str] = []
    chapter_timestamps: list[dict[str, Any]] = []  # {"time": "0:00", "title": "..."}


# ═══ S8: Export Result ═══

class ExportResult(BaseModel):
    """S8 내보내기+업로드 결과."""
    final_video_path: str = ""
    youtube_video_id: str = ""
    youtube_url: str = ""
    upload_status: str = ""  # "uploaded", "failed", "dry_run"
    final_file_size_mb: float = 0.0


# ═══ 비용 추적 ═══

class CostEntry(BaseModel):
    """API 호출 비용 기록."""
    stage: Stage
    provider: str
    operation: str
    units: float = 0.0
    unit_cost: float = 0.0
    total_cost: float = 0.0
    timestamp: datetime = Field(default_factory=datetime.now)


# ═══ 프로덕션 (Root Entity) ═══

class VideoProduction(BaseModel):
    """영상 프로덕션 - 하나의 영상 전체 생명주기."""
    production_id: str  # {channel}_{date}_{seq}
    channel_id: str
    topic: str = ""
    current_stage: Stage = Stage.BENCHMARK
    status: ProductionStatus = ProductionStatus.PENDING
    error_message: str = ""
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    # 스테이지 결과 (각 스테이지 완료 시 채워짐)
    benchmark: BenchmarkResult | None = None
    script: ScriptResult | None = None
    voice: VoiceResult | None = None
    storyboard: StoryboardResult | None = None
    media: MediaResult | None = None
    editing: EditingResult | None = None
    thumbnail: ThumbnailResult | None = None
    export: ExportResult | None = None

    # 비용 누적
    costs: list[CostEntry] = []
    total_cost: float = 0.0

    @property
    def production_dir(self) -> Path:
        """프로덕션 작업 디렉토리."""
        from .config import DATA_DIR
        return DATA_DIR / "productions" / self.production_id

    def add_cost(self, entry: CostEntry) -> None:
        """비용 기록 추가."""
        self.costs.append(entry)
        self.total_cost = sum(c.total_cost for c in self.costs)

    def advance_to(self, stage: Stage) -> None:
        """다음 스테이지로 진행."""
        self.current_stage = stage
        self.status = ProductionStatus.RUNNING
        self.updated_at = datetime.now()

    def mark_completed(self) -> None:
        """프로덕션 완료."""
        self.status = ProductionStatus.COMPLETED
        self.updated_at = datetime.now()

    def mark_failed(self, error: str) -> None:
        """프로덕션 실패."""
        self.status = ProductionStatus.FAILED
        self.error_message = error
        self.updated_at = datetime.now()
