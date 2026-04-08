# YouTube Long Form Maker - Claude Code 가이드

## 프로젝트 개요

"영상 만들어줘" 한마디로 Claude Code가 여러 AI를 오케스트레이션하여 유튜브 롱폼 영상을 자동 생성하는 시스템.

## 기술 스택

- **언어**: Python 3.12
- **핵심 패키지**: pydantic, click, rich, jinja2, httpx, Pillow, pydub, anthropic, openai, faster-whisper
- **영상 합성**: FFmpeg 7.1 네이티브 (concat demuxer + subtitles 필터)
- **DB**: SQLite (WAL 모드)
- **AI API**: Claude Opus 4.6, GPT-5.4, ElevenLabs, TypeCast, OpenAI GPT Image (gpt-image-1), Grok Imagine, Kling 3.0
- **STT**: faster-whisper (CTranslate2 백엔드, CUDA float16)

## 프로젝트 구조

```
src/
├── core/           # 공통 인프라 (config, models, state, exceptions, retry, cost_tracker, fonts, korean_number)
├── providers/      # AI 서비스 어댑터 (llm, tts, stt, image_gen, image_gen_openai, video_gen, stock_media, factory)
├── pipeline/       # 8단계 파이프라인 (S1~S8 + orchestrator)
├── retention/      # 조회수 최적화 (hook, pattern_interrupt, audio_mixing, loop, visual_keywords)
├── growth/         # 성장 엔진 (analytics, feedback_loop, shorts, ab_testing, tts_cache)
├── claude_code/    # Claude Code 자연어 명령 통합 (skill, handlers)
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
from src.providers.factory import create_llm, create_tts, create_image_gen

llm = create_llm("claude", fallback="gpt")
tts = create_tts("elevenlabs", fallback=None)
img = create_image_gen("openai", fallback="flux")  # GPT Image 우선
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

### S4 스토리보드 — 의미 단위 씬 분해 + VisualIntent

S4는 대본을 섹션 단위가 아닌 **의미 단위(문장/주장)** 로 씬을 분해한다.
각 씬에 `VisualIntent`를 강제 지정하여 내레이션-화면 관련도를 극대화.

```python
class VisualIntent(str, Enum):
    REAL_BROLL = "real_broll"         # 실사 B-roll
    MAP = "map"                       # 지도/항공샷
    CHART = "chart"                   # 차트/그래프/숫자 카드
    INFOGRAPHIC = "infographic"       # 인포그래픽
    CHECKLIST = "checklist"           # 체크리스트 카드
    COMPARISON_CARD = "comparison_card"  # A vs B 비교
    EMPHASIS_CAPTION = "emphasis_caption" # 핵심 문장 풀스크린
    TALKING_HEAD_STYLE = "talking_head_style" # 토킹헤드
    CLOSING_CTA = "closing_cta"       # 엔딩 CTA
```

- **LLM 기반 분해** (실제 실행): Claude가 스크립트를 분석하여 씬 분해 + visual_intent + 영문 stock_search_query 생성
- **규칙 기반 폴백** (dry_run/LLM 실패): 문장 단위 분할 + 키워드→intent 매핑
- **채널 니치별 규칙**: 부동산(금리→chart, 지역→map, 전세vs매매→comparison_card), 금융, 건강
- **재미 요소 강제**: 같은 visual_intent 3연속 방지, 5-10초마다 시각 변화

### S5 미디어 — visual_intent 기반 이미지 생성

- `_build_gpt_image_prompt()`: intent별 구도/레이아웃 지시 (chart=차트, checklist=체크박스 등)
- Failover: OpenAI GPT Image → 간소화 프롬프트 → Pillow fallback
- Pillow fallback도 intent별 전용 디자인 (차트/체크리스트/비교카드/강조캡션 등)

### S6 편집 — ffmpeg 네이티브 + 장면 관련도 품질 게이트

- ffmpeg concat demuxer + subtitles 필터로 합성 (MoviePy 제거)
- `scene_relevance_report.json` 생성: 각 씬의 narration-visual 매핑을 검수용으로 기록
- 품질 게이트: 최종 영상 길이 >= 오디오의 80%

## 이미지 생성 프로바이더

- **1순위**: OpenAI GPT Image (`gpt-image-1`) — `src/providers/image_gen_openai.py`
- **2순위**: 간소화 프롬프트로 재시도
- **최종 폴백**: Pillow 기반 카드 이미지 (visual_intent별 디자인)
- Flux는 코드에 존재하지만 현재 실행 경로에서 제외 (API 접속 불가)

## STT (음성→텍스트)

- **1순위**: faster-whisper (CTranslate2, CUDA float16) — 3~5배 빠름
- **2순위**: openai-whisper (폴백)
- 인스턴스 캐시로 중복 전사 방지
- S3에서 segments를 직접 전달하여 1-pass 전사

## TTS 전처리

- `src/core/korean_number.py`: 한국어 숫자+조수사 발음 교정
  - "3가지" → "세 가지", "5개" → "다섯 개", "12시" → "열두 시"
  - 한자어 조수사(원, 월, 년)는 변환하지 않음
- `src/providers/tts.py`에서 ElevenLabs API 호출 전 자동 적용

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
- `ANTHROPIC_API_KEY` (Claude)
- `OPENAI_API_KEY` (GPT Image + GPT LLM)
- `ELEVENLABS_API_KEY` + `ELEVENLABS_VOICE_ID` (TTS)
- `XAI_API_KEY` (Grok 영상)
- `YOUTUBE_API_KEY` (업로드)
- `PEXELS_API_KEY` (스톡)

## 주의사항

- `data/productions/`는 gitignore됨 (대용량 미디어 파일)
- `.env`는 절대 커밋 금지
- S6 편집 원칙: "ffmpeg 네이티브가 기준 렌더, CapCut/Topaz는 선택적 후처리"
- 건강 채널은 `safety_policy` 필수 (의료 면책 조항)
- S4 스토리보드 수정 시 `VisualIntent` enum과 `NICHE_VISUAL_RULES` 동기화 필수
- `scene_relevance_report.json`으로 영상 제작 후 장면 관련도 검수 가능
