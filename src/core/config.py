"""Pydantic 설정 로더 - YAML 설정 + 환경 변수 통합."""

from __future__ import annotations

import os
from pathlib import Path
from functools import lru_cache
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


# 프로젝트 루트 경로
PROJECT_ROOT = Path(__file__).parent.parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"


def _load_yaml(path: Path) -> dict[str, Any]:
    """YAML 파일을 로드하고 환경 변수를 치환."""
    with open(path, encoding="utf-8") as f:
        content = f.read()
    # ${VAR} 패턴을 환경 변수로 치환
    for key, value in os.environ.items():
        content = content.replace(f"${{{key}}}", value)
    return yaml.safe_load(content)


# ═══ 비디오 설정 ═══

class VideoConfig(BaseModel):
    default_resolution: list[int] = [1920, 1080]
    default_fps: int = 30
    target_duration_minutes: int = 10
    max_file_size_mb: int = 2000

    class EncodingConfig(BaseModel):
        codec: str = "libx264"
        audio_codec: str = "aac"
        preset: str = "medium"
        crf: int = 23

    encoding: EncodingConfig = EncodingConfig()


# ═══ 미디어 생성 설정 ═══

class MediaGenerationConfig(BaseModel):
    concurrency_limit: int = 3
    stock_mix_ratio: float = 0.2


# ═══ Retention 설정 ═══

class PatternInterruptConfig(BaseModel):
    min_interval: float = 4.5
    max_interval: float = 9.0


class TimingOffsetConfig(BaseModel):
    min: float = 0.2
    max: float = 0.5


class AudioDuckingConfig(BaseModel):
    bgm_volume_during_speech: float = 0.15
    fade_duration_ms: int = 500


class RetentionConfig(BaseModel):
    hook_duration_seconds: int = 5
    pattern_interrupt: PatternInterruptConfig = PatternInterruptConfig()
    timing_offset: TimingOffsetConfig = TimingOffsetConfig()
    audio_ducking: AudioDuckingConfig = AudioDuckingConfig()


# ═══ 썸네일 설정 ═══

class SafeZoneConfig(BaseModel):
    bottom_margin: int = 80
    right_margin: int = 40


class TextStyleConfig(BaseModel):
    name: str
    font_size: int
    color: str
    position: str


class ThumbnailConfig(BaseModel):
    width: int = 1280
    height: int = 720
    safe_zone: SafeZoneConfig = SafeZoneConfig()
    text_styles: list[TextStyleConfig] = []


# ═══ YouTube 설정 ═══

class YouTubeConfig(BaseModel):
    daily_quota_limit: int = 10000
    upload_quota_cost: int = 1600
    default_privacy: str = "private"
    default_category_id: str = "22"


# ═══ Rate Limits ═══

class ProviderRateLimit(BaseModel):
    requests_per_minute: int | None = None
    characters_per_month: int | None = None
    requests_per_month: int | None = None


class RateLimitsConfig(BaseModel):
    anthropic: ProviderRateLimit = ProviderRateLimit()
    elevenlabs: ProviderRateLimit = ProviderRateLimit()
    flux: ProviderRateLimit = ProviderRateLimit()
    grok: ProviderRateLimit = ProviderRateLimit()
    pexels: ProviderRateLimit = ProviderRateLimit()


# ═══ 채널 DNA ═══

class ChannelIdentity(BaseModel):
    unique_angle: str = ""
    narrative_style: str = ""
    forbidden_topics: list[str] = []
    recurring_pattern: str = ""


class ChannelContent(BaseModel):
    preferred_topics: list[str] = []
    target_duration_minutes: int = 10
    hook_style: str = "shocking_statistic"
    cta_style: str = "subscribe_and_next"


class ChannelProviders(BaseModel):
    llm: str = "claude"
    tts: str = "elevenlabs"
    voice_id: str = ""
    image_gen: str = "flux"
    video_gen: str = "grok"


class ChannelVisual(BaseModel):
    primary_color: str = "#1B4332"
    secondary_color: str = "#D4A574"
    accent_color: str = "#E63946"
    font_family: str = "NanumGothicBold"
    thumbnail_template: str = "default"
    background_style: str = "gradient_professional"


class ChannelUpload(BaseModel):
    category_id: str = "22"
    default_tags: list[str] = []
    privacy: str = "private"
    playlist_id: str | None = None


class BrandingConfig(BaseModel):
    """채널명 노출 정책."""
    show_in_video: bool = False       # 본문 영상에 채널명 자동 삽입
    show_in_fallback: bool = False    # fallback 비주얼에 채널명 표시
    show_in_thumbnail: bool = True    # 썸네일에 채널명 표시
    show_in_ending: bool = True       # 엔딩 CTA 씬에 채널명 표시
    position: str = "bottom_bar"      # bottom_bar, watermark, none


class ABTestConfig(BaseModel):
    group: str = "default"


class ChannelConfig(BaseModel):
    """채널 설정 - YAML 파일 1개 = 채널 1개."""
    channel: dict[str, str] = {}
    identity: ChannelIdentity = ChannelIdentity()
    content: ChannelContent = ChannelContent()
    providers: ChannelProviders = ChannelProviders()
    visual: ChannelVisual = ChannelVisual()
    upload: ChannelUpload = ChannelUpload()
    branding: BrandingConfig = BrandingConfig()
    ab_test: ABTestConfig = ABTestConfig()

    @property
    def channel_id(self) -> str:
        return self.channel.get("id", "unknown")

    @property
    def channel_name(self) -> str:
        return self.channel.get("name", "Unnamed Channel")

    @property
    def niche(self) -> str:
        return self.channel.get("niche", "general")

    @property
    def language(self) -> str:
        return self.channel.get("language", "ko")

    @property
    def tone(self) -> str:
        return self.channel.get("tone", "")

    @property
    def target_audience(self) -> str:
        return self.channel.get("target_audience", "")


# ═══ 글로벌 설정 ═══

class Settings(BaseModel):
    """글로벌 앱 설정 - settings.yaml에서 로드."""
    video: VideoConfig = VideoConfig()
    media_generation: MediaGenerationConfig = MediaGenerationConfig()
    retention: RetentionConfig = RetentionConfig()
    thumbnail: ThumbnailConfig = ThumbnailConfig()
    youtube: YouTubeConfig = YouTubeConfig()
    rate_limits: RateLimitsConfig = RateLimitsConfig()


# ═══ 환경 변수 ═══

class EnvConfig(BaseSettings):
    """환경 변수에서 API 키 로드."""
    # MVP 1차
    anthropic_api_key: str = ""
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    flux_api_key: str = ""
    xai_api_key: str = ""
    youtube_api_key: str = ""
    pexels_api_key: str = ""
    # Phase 5A 확장
    openai_api_key: str = ""
    typecast_api_key: str = ""
    typecast_voice_id: str = ""
    kling_api_key: str = ""

    model_config = {"env_file": str(PROJECT_ROOT / ".env"), "env_file_encoding": "utf-8", "extra": "ignore"}


# ═══ 로더 함수 ═══

@lru_cache
def load_settings() -> Settings:
    """글로벌 설정 로드 (캐시)."""
    settings_path = CONFIG_DIR / "settings.yaml"
    if settings_path.exists():
        data = _load_yaml(settings_path)
        return Settings(**data)
    return Settings()


@lru_cache
def load_env() -> EnvConfig:
    """환경 변수 로드 (캐시)."""
    return EnvConfig()


def load_channel(channel_id: str) -> ChannelConfig:
    """채널 설정 로드."""
    channel_path = CONFIG_DIR / "channels" / f"channel_{channel_id}.yaml"
    if not channel_path.exists():
        raise FileNotFoundError(f"채널 설정 파일을 찾을 수 없습니다: {channel_path}")
    data = _load_yaml(channel_path)
    return ChannelConfig(**data)


@lru_cache
def load_providers() -> dict[str, Any]:
    """프로바이더 설정 로드 (캐시)."""
    providers_path = CONFIG_DIR / "providers.yaml"
    if providers_path.exists():
        return _load_yaml(providers_path)
    return {}


def list_channels() -> list[str]:
    """사용 가능한 채널 ID 목록."""
    channels_dir = CONFIG_DIR / "channels"
    return [
        p.stem.replace("channel_", "")
        for p in channels_dir.glob("channel_*.yaml")
        if not p.stem.startswith("_")
    ]
