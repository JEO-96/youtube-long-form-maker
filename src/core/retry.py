"""지수 백오프 재시도 데코레이터."""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Callable, TypeVar, ParamSpec

from .exceptions import (
    ProviderError,
    RateLimitError,
    QuotaExhaustedError,
    ContentFilterError,
)

logger = logging.getLogger(__name__)

P = ParamSpec("P")
T = TypeVar("T")


def retry(
    max_attempts: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
) -> Callable:
    """비동기 함수용 재시도 데코레이터.

    - RateLimitError: retry_after_seconds 만큼 대기 후 재시도
    - ProviderTimeoutError: 지수 백오프 재시도
    - ContentFilterError: 재시도 안 함 (즉시 raise)
    - QuotaExhaustedError: 재시도 안 함 (즉시 raise)
    """

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            last_error: Exception | None = None

            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except (ContentFilterError, QuotaExhaustedError):
                    raise  # 재시도 불가
                except RateLimitError as e:
                    last_error = e
                    wait = e.retry_after_seconds or base_delay
                    logger.warning(
                        f"Rate limited on attempt {attempt}/{max_attempts}. "
                        f"Waiting {wait:.1f}s..."
                    )
                    await asyncio.sleep(wait)
                except ProviderError as e:
                    last_error = e
                    if not e.retryable:
                        raise
                    delay = min(base_delay * (backoff_factor ** (attempt - 1)), max_delay)
                    logger.warning(
                        f"Retryable error on attempt {attempt}/{max_attempts}: {e}. "
                        f"Waiting {delay:.1f}s..."
                    )
                    await asyncio.sleep(delay)
                except Exception as e:
                    last_error = e
                    delay = min(base_delay * (backoff_factor ** (attempt - 1)), max_delay)
                    logger.warning(
                        f"Unexpected error on attempt {attempt}/{max_attempts}: {e}. "
                        f"Waiting {delay:.1f}s..."
                    )
                    await asyncio.sleep(delay)

            raise last_error  # type: ignore[misc]

        return wrapper  # type: ignore[return-value]

    return decorator
