"""프로바이더 팩토리 - 채널 설정 기반 프로바이더 인스턴스 생성."""

from __future__ import annotations

import logging
from typing import Any

from .base import LLMProvider, TTSProvider, ImageGenProvider, VideoGenProvider

logger = logging.getLogger(__name__)

# ═══ 프로바이더 레지스트리 ═══

_LLM_REGISTRY: dict[str, type[LLMProvider]] = {}
_TTS_REGISTRY: dict[str, type[TTSProvider]] = {}
_IMAGE_GEN_REGISTRY: dict[str, type[ImageGenProvider]] = {}
_VIDEO_GEN_REGISTRY: dict[str, type[VideoGenProvider]] = {}


def register_llm(name: str, cls: type[LLMProvider]) -> None:
    _LLM_REGISTRY[name] = cls


def register_tts(name: str, cls: type[TTSProvider]) -> None:
    _TTS_REGISTRY[name] = cls


def register_image_gen(name: str, cls: type[ImageGenProvider]) -> None:
    _IMAGE_GEN_REGISTRY[name] = cls


def register_video_gen(name: str, cls: type[VideoGenProvider]) -> None:
    _VIDEO_GEN_REGISTRY[name] = cls


# ═══ Lazy Registration (import 시점 지연) ═══

def _ensure_registered() -> None:
    """필요할 때 한 번만 등록."""
    if _LLM_REGISTRY:
        return

    from .llm import ClaudeLLM, GPTLLM
    from .tts import ElevenLabsTTS, TypeCastTTS
    from .image_gen import FluxImageGen
    from .image_gen_openai import OpenAIImageGen
    from .video_gen import GrokVideoGen, KlingVideoGen

    register_llm("claude", ClaudeLLM)
    register_llm("gpt", GPTLLM)
    register_tts("elevenlabs", ElevenLabsTTS)
    register_tts("typecast", TypeCastTTS)
    register_image_gen("flux", FluxImageGen)
    register_image_gen("openai", OpenAIImageGen)
    register_video_gen("grok", GrokVideoGen)
    register_video_gen("kling", KlingVideoGen)


# ═══ 팩토리 함수 ═══

def create_llm(provider_name: str = "claude", fallback: str | None = None) -> LLMProvider:
    """LLM 프로바이더 생성.

    Args:
        provider_name: 프로바이더 이름 (claude, gpt)
        fallback: 실패 시 대체 프로바이더

    Returns:
        LLMProvider 인스턴스
    """
    _ensure_registered()
    try:
        cls = _LLM_REGISTRY[provider_name]
        return cls()
    except KeyError:
        available = list(_LLM_REGISTRY.keys())
        raise ValueError(f"Unknown LLM provider: '{provider_name}'. Available: {available}")
    except Exception as e:
        if fallback and fallback != provider_name:
            logger.warning(f"LLM '{provider_name}' init failed ({e}), falling back to '{fallback}'")
            return create_llm(fallback)
        raise


def create_tts(provider_name: str = "elevenlabs", fallback: str | None = None) -> TTSProvider:
    """TTS 프로바이더 생성.

    Args:
        provider_name: 프로바이더 이름 (elevenlabs, typecast)
        fallback: 실패 시 대체 프로바이더

    Returns:
        TTSProvider 인스턴스
    """
    _ensure_registered()
    try:
        cls = _TTS_REGISTRY[provider_name]
        return cls()
    except KeyError:
        available = list(_TTS_REGISTRY.keys())
        raise ValueError(f"Unknown TTS provider: '{provider_name}'. Available: {available}")
    except Exception as e:
        if fallback and fallback != provider_name:
            logger.warning(f"TTS '{provider_name}' init failed ({e}), falling back to '{fallback}'")
            return create_tts(fallback)
        raise


def create_image_gen(
    provider_name: str = "flux", fallback: str | None = None
) -> ImageGenProvider:
    """ImageGen 프로바이더 생성.

    Args:
        provider_name: 프로바이더 이름 (flux, openai)
        fallback: 실패 시 대체 프로바이더

    Returns:
        ImageGenProvider 인스턴스
    """
    _ensure_registered()
    try:
        cls = _IMAGE_GEN_REGISTRY[provider_name]
        return cls()
    except KeyError:
        available = list(_IMAGE_GEN_REGISTRY.keys())
        raise ValueError(f"Unknown ImageGen provider: '{provider_name}'. Available: {available}")
    except Exception as e:
        if fallback and fallback != provider_name:
            logger.warning(
                f"ImageGen '{provider_name}' init failed ({e}), falling back to '{fallback}'"
            )
            return create_image_gen(fallback)
        raise


def create_video_gen(
    provider_name: str = "grok", fallback: str | None = None
) -> VideoGenProvider:
    """VideoGen 프로바이더 생성.

    Args:
        provider_name: 프로바이더 이름 (grok, kling)
        fallback: 실패 시 대체 프로바이더

    Returns:
        VideoGenProvider 인스턴스
    """
    _ensure_registered()
    try:
        cls = _VIDEO_GEN_REGISTRY[provider_name]
        return cls()
    except KeyError:
        available = list(_VIDEO_GEN_REGISTRY.keys())
        raise ValueError(f"Unknown VideoGen provider: '{provider_name}'. Available: {available}")
    except Exception as e:
        if fallback and fallback != provider_name:
            logger.warning(
                f"VideoGen '{provider_name}' init failed ({e}), falling back to '{fallback}'"
            )
            return create_video_gen(fallback)
        raise
