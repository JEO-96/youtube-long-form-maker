# YouTube Long Form Maker - Codex 가이드

## 프로젝트 개요

"영상 만들어줘" 한마디로 Codex가 여러 AI를 오케스트레이션하여 유튜브 롱폼 영상을 자동 생성하는 시스템.

## 기술 스택

- **언어**: Python 3.12
- **핵심 패키지**: pydantic, click, rich, jinja2, httpx, moviepy, Pillow, pydub, anthropic, openai
- **외부 도구**: FFmpeg 7.1
- **DB**: SQLite (WAL 모드)
- **AI API**: Codex Opus 4.6, GPT-5.4, ElevenLabs, TypeCast, FLUX.2, Grok Imagine, Kling 3.0

## 프로젝트 구조

```
src/
├── core/           # 공통 인프라 (config, models, state, exceptions, retry, cost_tracker)
├── providers/      # AI 서비스 어댑터 (llm, tts, stt, image_gen, video_gen, stock_media, factory 등)
├── pipeline/       # 8단계 파이프라인 (S1~S8 + orchestrator)
├── retention/      # 조회수 최적화 (hook, pattern_interrupt, audio_mixing, loop, visual_keywords)
├── growth/         # 성장 엔진 (analytics, feedback_loop, shorts, ab_testing, tts_cache)
├── Codex/    # Codex 자연어 명령 통합 (skill, handlers)
├── cli/            # Click CLI (main, produce)
└── templates/      # Jinja2 프롬프트 템플릿
config/
├── settings.yaml           # 글로벌 설정
├── providers.yaml          # AI 프로바이더 설정 + 가격
└── channels/               # 채널별 설정 (finance, health, ai, business, realestate)
```

## 코딩 컨벤션

- **타입 힌트**: Python 3.12+ 스타일 (`list[str]`, `dict[str, Any]`, `X | None`)
- **async/await**: 모든 프로바이더 + 파이프라인 스테이지는 async
- **Pydantic**: 도메인 모델은 `pydantic.BaseModel` 사용
- **에러 처리**: 커스텀 예외 계층 (`src/core/exceptions.py`) - ProviderError, RateLimitError, ContentFilterError 등
- **재시도**: `@retry` 데코레이터 (지수 백오프, `src/core/retry.py`)
- **로깅**: `logging` 모듈, 모듈별 `logger = logging.getLogger(__name__)`
- **docstring**: 한국어 (프로젝트 전체 한국어 주석)

## 핵심 패턴

### Provider Factory (`src/providers/factory.py`)
```python
from src.providers.factory import create_llm, create_tts, create_video_gen

llm = create_llm("Codex", fallback="gpt")     # 초기화 실패 시 fallback
tts = create_tts("elevenlabs", fallback=None)
```

### Pipeline Stage (Template Method)
```python
class S1Benchmark(BaseStage):
    stage = Stage.BENCHMARK
    async def run(self, topic: str = "", **kwargs) -> BenchmarkResult:
        ...
```
모든 스테이지는 `BaseStage.execute()` → `run()` 템플릿 메서드 패턴.

### Graceful Fallback (선택적 기능)
```python
# Topaz/CapCut 등 선택적 기능은 *_safe() 래퍼 사용
upscaled = await topaz.upscale_safe(video_path)  # 실패 시 원본 반환
```

### Jinja2 프롬프트 (`src/templates/`)
```python
from src.templates import render_prompt
prompt = render_prompt("script_body.j2", channel_name="...", topic="...")
```

## 채널 추가 방법

`config/channels/_template.yaml`을 복사하여 `channel_{id}.yaml`로 저장.
코드 변경 없이 YAML만 추가하면 새 채널 운영 가능.

## 파이프라인 흐름

```
S1 벤치마킹 → S2 대본 → S3 음성+자막 → S4 스토리보드 → S5 미디어 생성 → S6 편집 → S7 썸네일 → S8 내보내기
```

각 스테이지 결과는 SQLite에 저장되며, 실패 시 해당 스테이지부터 재개(resume) 가능.

## 테스트 실행

```bash
# dry_run 모드 (API 호출 없이 전체 파이프라인 테스트)
python -m src.cli.main produce --channel finance --topic "통장관리" --dry-run

# Python에서 직접 실행
import asyncio
from src.pipeline.orchestrator import Orchestrator
from src.core.state import StateManager
from src.core.cost_tracker import CostTracker

async def test():
    sm = StateManager()
    ct = CostTracker(state_manager=sm)
    orch = Orchestrator(state_manager=sm, cost_tracker=ct, dry_run=True)
    pid = await orch.produce(channel_id="finance", topic="사회초년생 통장관리")
    print(pid)

asyncio.run(test())
```

## 환경변수 (.env)

`.env.example` 참조. 최소 필수 키:
- `ANTHROPIC_API_KEY` (Codex)
- `ELEVENLABS_API_KEY` + `ELEVENLABS_VOICE_ID` (TTS)
- `FLUX_API_KEY` (이미지)
- `XAI_API_KEY` (Grok 영상)
- `YOUTUBE_API_KEY` (업로드)
- `PEXELS_API_KEY` (스톡)

## 주의사항

- `data/productions/`는 gitignore됨 (대용량 미디어 파일)
- `.env`는 절대 커밋 금지
- S6 편집 원칙: "MoviePy가 기준 렌더, CapCut/Topaz는 선택적 후처리"
- 건강 채널은 `safety_policy` 필수 (의료 면책 조항)
