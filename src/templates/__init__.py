"""Jinja2 프롬프트 템플릿 로더."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "prompts"


def render_prompt(template_name: str, **kwargs: Any) -> str:
    """Jinja2 템플릿 렌더링.

    Args:
        template_name: 템플릿 파일명 (예: "benchmark_analysis.j2")
        **kwargs: 템플릿 변수

    Returns:
        렌더링된 프롬프트 문자열
    """
    try:
        from jinja2 import Environment, FileSystemLoader

        env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        template = env.get_template(template_name)
        return template.render(**kwargs)
    except ImportError:
        logger.warning("jinja2 not installed, using fallback prompt")
        return _fallback_render(template_name, **kwargs)
    except Exception as e:
        logger.warning(f"Template render failed: {e}, using fallback")
        return _fallback_render(template_name, **kwargs)


def _fallback_render(template_name: str, **kwargs: Any) -> str:
    """Jinja2 없을 때 기본 문자열 조합."""
    path = TEMPLATES_DIR / template_name
    if path.exists():
        text = path.read_text(encoding="utf-8")
        # 간단한 {{ var }} 치환
        for key, value in kwargs.items():
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value)
            text = text.replace("{{ " + key + " }}", str(value))
        return text
    return f"Template not found: {template_name}"
