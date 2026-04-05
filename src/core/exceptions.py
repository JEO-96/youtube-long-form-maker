"""커스텀 예외 계층."""

from __future__ import annotations


class YTMakerError(Exception):
    """기본 예외."""
    pass


class ConfigError(YTMakerError):
    """설정 오류 (YAML, env 변수 누락)."""
    pass


class ProviderError(YTMakerError):
    """AI 프로바이더 오류."""

    def __init__(self, provider: str, message: str, retryable: bool = False):
        self.provider = provider
        self.retryable = retryable
        super().__init__(f"[{provider}] {message}")


class RateLimitError(ProviderError):
    """429 Rate Limit (재시도 가능)."""

    def __init__(self, provider: str, retry_after_seconds: float = 0):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(provider, f"Rate limited. Retry after {retry_after_seconds}s", retryable=True)


class QuotaExhaustedError(ProviderError):
    """일일 쿼터 소진 (오늘은 재시도 불가)."""

    def __init__(self, provider: str):
        super().__init__(provider, "Daily quota exhausted", retryable=False)


class ContentFilterError(ProviderError):
    """콘텐츠 안전 필터 차단 (재시도 불가)."""

    def __init__(self, provider: str, reason: str = ""):
        super().__init__(provider, f"Content filtered: {reason}", retryable=False)


class ProviderTimeoutError(ProviderError):
    """요청 타임아웃 (재시도 가능)."""

    def __init__(self, provider: str, timeout_seconds: float = 0):
        super().__init__(provider, f"Timeout after {timeout_seconds}s", retryable=True)


class StageError(YTMakerError):
    """파이프라인 스테이지 오류."""

    def __init__(self, stage_name: str, production_id: str, cause: Exception | None = None):
        self.stage_name = stage_name
        self.production_id = production_id
        self.cause = cause
        msg = f"Stage '{stage_name}' failed for production '{production_id}'"
        if cause:
            msg += f": {cause}"
        super().__init__(msg)
