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
├── core/           # 공통 인프라
│   ├── config.py           # Pydantic 기반 설정 로더
│   ├── models.py           # 도메인 모델 + Enum (Stage, VisualIntent, SceneFailureRecord 등)
│   ├── state.py            # SQLite 상태 관리자
│   ├── exceptions.py       # 커스텀 예외 계층
│   ├── retry.py            # 지수 백오프 재시도 데코레이터
│   ├── cost_tracker.py     # API 비용 추적
│   ├── fonts.py            # 폰트 관리
│   ├── korean_number.py    # 한국어 숫자+조수사 발음 교정
│   ├── visual_templates.py # Pillow 기반 씬 이미지 카드 렌더링 (chart, checklist, comparison 등)
│   └── text_render.py      # Pillow 텍스트 렌더링 유틸 (줄바꿈 + 자막 안전영역)
├── providers/      # AI 서비스 어댑터
│   ├── base.py             # 추상 프로바이더 인터페이스
│   ├── llm.py              # Claude/GPT LLM 오케스트레이션
│   ├── tts.py              # ElevenLabs TTS + 한국어 전처리
│   ├── stt.py              # faster-whisper/openai-whisper STT
│   ├── image_gen.py        # Flux 이미지 생성 (현재 비활성)
│   ├── image_gen_openai.py # OpenAI GPT Image (gpt-image-1) — 1순위
│   ├── video_gen.py        # Kling 3.0, Grok Imagine 비디오 생성
│   ├── stock_media.py      # Pexels 스톡 미디어
│   ├── upscaler.py         # Topaz 업스케일링 (선택적)
│   ├── capcut.py           # CapCut 프로젝트 통합 (선택적)
│   ├── youtube.py          # YouTube 업로드/API
│   ├── trends.py           # Google Trends
│   └── factory.py          # Provider Factory 패턴
├── pipeline/       # 8단계 파이프라인
│   ├── base_stage.py       # BaseStage 템플릿 메서드
│   ├── s1_benchmark.py     # 벤치마킹 (경쟁 분석)
│   ├── s2_script.py        # 대본 생성
│   ├── s3_voice.py         # 음성 합성 + 자막
│   ├── s4_storyboard.py    # 스토리보드 (의미 단위 씬 분해 + VisualIntent)
│   ├── s5_media.py         # 미디어 생성 (GPT Image + Pillow fallback)
│   ├── s6_editing.py       # FFmpeg 편집 + 씬-나레이션 싱크 정렬 + blackdetect
│   ├── s7_thumbnail.py     # 썸네일 생성
│   ├── s8_export.py        # 최종 내보내기
│   └── orchestrator.py     # 파이프라인 오케스트레이터
├── retention/      # 조회수 최적화
│   ├── hook_system.py      # 오프닝 훅 스타일 시스템
│   ├── loop_structure.py   # 비디오 루프 패턴
│   ├── pattern_interrupt.py # 패턴 인터럽트 (참여 유도)
│   ├── audio_mixing.py     # 오디오 레벨 믹싱/더킹
│   └── visual_keywords.py  # 온스크린 키워드 팝업
├── growth/         # 성장 엔진
│   ├── analytics.py        # 비디오 성과 분석
│   ├── feedback_loop.py    # 자동 피드백 루프
│   ├── shorts_generator.py # YouTube Shorts 생성
│   ├── ab_testing.py       # A/B 테스팅 프레임워크
│   └── tts_cache.py        # TTS 캐싱 레이어
├── claude_code/    # Claude Code 자연어 명령 통합 (skill, handlers)
├── cli/            # Click CLI (main, produce)
└── templates/      # Jinja2 프롬프트 템플릿
    ├── prompts/            # LLM 프롬프트 템플릿
    └── visual/             # 비주얼 템플릿
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
- `SceneFailureRecord`로 각 씬의 실패 원인을 구조화하여 기록 (provider, http_status, fallback 여부 등)

### S6 편집 — ffmpeg 네이티브 + 씬-나레이션 싱크 정렬 + 품질 게이트

- ffmpeg concat demuxer + subtitles 필터로 합성 (MoviePy 제거)
- **씬-나레이션 싱크 정렬**: 각 씬의 이미지 표시 시간을 나레이션 타이밍에 정렬
  - `alignment_max_drift`: 최대 드리프트 (초) 추적
  - `alignment_warnings`: drift > 3s인 씬 목록 기록
  - hard gate: 드리프트 기준 초과 시 경고/실패 처리
- **blackdetect 품질 게이트**: ffmpeg `blackdetect` 필터로 1초 이상 검은 프레임 탐지
- `scene_relevance_report.json` 생성: 각 씬의 narration-visual 매핑을 검수용으로 기록
- 품질 게이트: 최종 영상 길이 >= 오디오의 80%

## 비주얼 템플릿 시스템 (`src/core/visual_templates.py`)

Pillow 기반 고품질 씬 이미지 카드 렌더링. GPT Image 실패 시 fallback으로 사용.

- **표준 캔버스**: 1920x1080, 하단 180px 자막 안전영역 (`SUBTITLE_SAFE_MARGIN`)
- **텍스트 렌더링**: `src/core/text_render.py`의 `draw_text_box()` — 픽셀 기반 줄바꿈 + 박스 클리핑
- **intent별 전용 렌더 함수**:
  - `draw_chart_kpi_bar()` — KPI 카드 + 막대 차트
  - `draw_chart_kpi_only()` — KPI 카드 단독
  - `draw_chart_line()` — 라인 차트
  - `draw_chart_gauge()` — 게이지 차트
  - `draw_comparison_card()` — A vs B 비교 카드
  - `draw_checklist_card()` — 체크리스트 카드
  - `draw_emphasis_card()` — 핵심 문장 풀스크린
  - `draw_infographic_card()` — 인포그래픽
  - `draw_cta_card()` — 엔딩 CTA 카드
- `derive_scene_title()`: 씬 나레이션에서 타이틀 + 카테고리 칩 자동 추출

## 이미지 생성 프로바이더

- **1순위**: OpenAI GPT Image (`gpt-image-1`) — `src/providers/image_gen_openai.py`
- **2순위**: 간소화 프롬프트로 재시도
- **최종 폴백**: Pillow 기반 카드 이미지 (`src/core/visual_templates.py`, visual_intent별 디자인)
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

## 핵심 도메인 모델 (`src/core/models.py`)

- **Stage**: BENCHMARK → SCRIPT → VOICE → STORYBOARD → MEDIA → EDITING → THUMBNAIL → EXPORT
- **ProductionStatus**: PENDING, RUNNING, PAUSED, COMPLETED, FAILED
- **MediaType**: AI_IMAGE, AI_VIDEO, STOCK_VIDEO, STOCK_IMAGE
- **TransitionType**: CUT, FADE, DISSOLVE, SLIDE, ZOOM
- **VisualIntent**: 9종 — 씬의 시각 표현 방식 강제 지정
- **SectionTiming**: 스크립트 섹션별 실제 오디오 타이밍 (STT 기반)
- **SceneFailureRecord**: 씬 미디어 생성 실패 구조화 기록
  - `scene_number`, `provider`, `exception_type`, `http_status`
  - `error_message`, `fallback_used`, `fallback_path`, `human_summary`
- **EditingResult**: S6 결과 — `alignment_max_drift`, `alignment_warnings`, `quality_gate_passed`

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
- 비주얼 템플릿 수정 시 `SUBTITLE_SAFE_MARGIN`(180px) 하단 안전영역 준수 필수
- `visual_templates.py`에서 narration 텍스트는 반드시 `draw_text_box()` 사용 (substring 금지)
- S6의 alignment drift 경고가 3초 초과 시 씬 타이밍 조정 필요
