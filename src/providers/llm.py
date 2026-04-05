"""LLM 프로바이더 - Claude Opus 4.6 + GPT-5.4."""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

from ..core.config import load_env, load_providers
from ..core.exceptions import (
    ContentFilterError,
    ProviderError,
    ProviderTimeoutError,
    QuotaExhaustedError,
    RateLimitError,
)
from ..core.retry import retry
from .base import LLMProvider

logger = logging.getLogger(__name__)


class ClaudeLLM(LLMProvider):
    """Claude Opus 4.6 LLM 프로바이더."""

    def __init__(self) -> None:
        env = load_env()
        providers = load_providers()
        claude_cfg = providers.get("llm", {}).get("claude", {})

        self.api_key = env.anthropic_api_key
        self.model = claude_cfg.get("model", "claude-opus-4-6")
        self.default_max_tokens = claude_cfg.get("max_tokens", 8192)
        self.default_temperature = claude_cfg.get("temperature", 0.7)
        self.pricing = claude_cfg.get("pricing", {})

        self.client = anthropic.AsyncAnthropic(api_key=self.api_key)

    @retry(max_attempts=3, base_delay=2.0)
    async def generate(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 0,
        temperature: float = 0.0,
        response_format: str | None = None,
    ) -> str:
        """Claude API로 텍스트 생성."""
        max_tokens = max_tokens or self.default_max_tokens
        temperature = temperature or self.default_temperature

        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system

        if not self.api_key:
            raise ProviderError("claude", "Authentication failed: no API key configured", retryable=False)

        try:
            response = await self.client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            raise RateLimitError("claude", retry_after_seconds=60) from e
        except anthropic.AuthenticationError as e:
            raise ProviderError("claude", f"Authentication failed: {e}", retryable=False) from e
        except anthropic.BadRequestError as e:
            error_msg = str(e)
            if "content_filter" in error_msg.lower() or "safety" in error_msg.lower():
                raise ContentFilterError("claude", error_msg) from e
            raise ProviderError("claude", error_msg, retryable=False) from e
        except anthropic.InternalServerError as e:
            raise ProviderError("claude", f"Server error: {e}", retryable=True) from e
        except anthropic.APITimeoutError as e:
            raise ProviderTimeoutError("claude", timeout_seconds=60) from e
        except anthropic.APIError as e:
            raise ProviderError("claude", str(e), retryable=True) from e

        text = response.content[0].text

        # stop_reason이 content_filter인 경우
        if response.stop_reason == "content_filter":
            raise ContentFilterError("claude", "Response filtered by safety system")

        logger.info(
            f"Claude generate: in={response.usage.input_tokens}, "
            f"out={response.usage.output_tokens}"
        )
        return text

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """비용 추정."""
        input_cost = (input_tokens / 1000) * self.pricing.get("input_per_1k_tokens", 0.015)
        output_cost = (output_tokens / 1000) * self.pricing.get("output_per_1k_tokens", 0.075)
        return input_cost + output_cost

    def get_usage_from_response(self, response: Any) -> dict[str, int]:
        """응답에서 토큰 사용량 추출 (cost_tracker 연동용)."""
        return {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }


class GPTLLM(LLMProvider):
    """GPT-5.4 LLM 프로바이더 - 구조 설계/마케팅 최적화 용도.

    역할 분담:
        GPT = 구조 설계 (Hook→Retention→Loop, 마케팅)
        Claude = 대본 작성 (글쓰기)
    """

    def __init__(self) -> None:
        env = load_env()
        providers = load_providers()
        gpt_cfg = providers.get("llm", {}).get("gpt", {})

        self.api_key = env.openai_api_key
        self.model = gpt_cfg.get("model", "gpt-5.4")
        self.default_max_tokens = gpt_cfg.get("max_tokens", 8192)
        self.default_temperature = gpt_cfg.get("temperature", 0.7)
        self.pricing = gpt_cfg.get("pricing", {})

    @retry(max_attempts=3, base_delay=2.0)
    async def generate(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 0,
        temperature: float = 0.0,
        response_format: str | None = None,
    ) -> str:
        """OpenAI API로 텍스트 생성."""
        import openai

        max_tokens = max_tokens or self.default_max_tokens
        temperature = temperature or self.default_temperature

        if not self.api_key:
            raise ProviderError("gpt", "Authentication failed: no API key configured", retryable=False)

        client = openai.AsyncOpenAI(api_key=self.api_key)

        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
        }

        # JSON 모드 지원
        if response_format == "json":
            kwargs["response_format"] = {"type": "json_object"}

        try:
            response = await client.chat.completions.create(**kwargs)
        except openai.RateLimitError as e:
            raise RateLimitError("gpt", retry_after_seconds=60) from e
        except openai.AuthenticationError as e:
            raise ProviderError("gpt", f"Authentication failed: {e}", retryable=False) from e
        except openai.BadRequestError as e:
            error_msg = str(e)
            if "content_filter" in error_msg.lower() or "safety" in error_msg.lower():
                raise ContentFilterError("gpt", error_msg) from e
            raise ProviderError("gpt", error_msg, retryable=False) from e
        except openai.InternalServerError as e:
            raise ProviderError("gpt", f"Server error: {e}", retryable=True) from e
        except openai.APITimeoutError as e:
            raise ProviderTimeoutError("gpt", timeout_seconds=60) from e
        except openai.APIError as e:
            raise ProviderError("gpt", str(e), retryable=True) from e

        text = response.choices[0].message.content or ""

        # content_filter finish_reason 처리
        if response.choices[0].finish_reason == "content_filter":
            raise ContentFilterError("gpt", "Response filtered by safety system")

        usage = response.usage
        if usage:
            logger.info(
                f"GPT generate: in={usage.prompt_tokens}, "
                f"out={usage.completion_tokens}"
            )
        return text

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """비용 추정."""
        input_cost = (input_tokens / 1000) * self.pricing.get("input_per_1k_tokens", 0.005)
        output_cost = (output_tokens / 1000) * self.pricing.get("output_per_1k_tokens", 0.015)
        return input_cost + output_cost

    def get_usage_from_response(self, response: Any) -> dict[str, int]:
        """응답에서 토큰 사용량 추출."""
        usage = response.usage
        return {
            "input_tokens": usage.prompt_tokens if usage else 0,
            "output_tokens": usage.completion_tokens if usage else 0,
        }
