"""역할별 모델 및 실행 한도 설정.

역할 구조:
  orchestrator: 총괄 — 결정 수집, 아키텍처 설계, 태스크 분해. Fable 5 고정.
  reviewer:     수석 아키텍트 — 구현 전 설계 리뷰(추론/QA 렌즈). Fable 5 고정.
  worker:       시니어 개발자들 — 태스크 구현. 선택 가능(기본 Sonnet 5).
  utility:      보조 작업(트렌드 요약 등). 선택 가능(기본 Haiku 4.5).

지휘/설계/리뷰가 Fable 5 고정인 이유: 분해와 설계 품질이 전체 품질을 결정하고,
낮은 모델 워커의 실수는 리뷰·검증 체계가 잡아내는 구조이기 때문.
"""

from dataclasses import dataclass

FABLE = "claude-fable-5"

# 모델 카탈로그: 모델 ID -> 프로바이더. 새 모델 추가는 여기 + SELECTABLE에 한 줄씩.
MODEL_CATALOG = {
    "claude-fable-5": "anthropic",
    "claude-opus-4-8": "anthropic",
    "claude-sonnet-5": "anthropic",
    "claude-sonnet-4-6": "anthropic",
    "claude-haiku-4-5": "anthropic",
    "gpt-5.1": "openai",
    "gpt-5": "openai",
    "gpt-5-mini": "openai",
    "gemini-3-pro-preview": "google",
    "gemini-2.5-pro": "google",
    "gemini-2.5-flash": "google",
}
PROVIDER_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
}

# 사용자가 UI에서 선택 가능한 역할과 후보. 전 역할 선택 가능.
# 총괄/리뷰어 기본값은 Fable 5 — 분해·설계 품질이 전체 품질을 결정하기 때문.
_TOP_TIER = [FABLE, "claude-opus-4-8", "claude-sonnet-5",
             "gpt-5.1", "gemini-3-pro-preview"]
SELECTABLE_MODELS = {
    "orchestrator": {"default": FABLE, "options": _TOP_TIER},
    "reviewer": {"default": FABLE, "options": _TOP_TIER},
    "worker": {
        "default": "claude-sonnet-5",
        "options": ["claude-sonnet-5", "claude-opus-4-8", "claude-sonnet-4-6",
                    "claude-haiku-4-5", "gpt-5.1", "gpt-5",
                    "gemini-3-pro-preview", "gemini-2.5-pro"],
    },
    "utility": {
        # 트렌드 조사 담당 — Gemini 계열은 Google 검색 그라운딩으로 직접 조사
        "default": "claude-haiku-4-5",
        "options": ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-sonnet-5",
                    "gpt-5-mini", "gemini-2.5-flash", "gemini-2.5-pro",
                    "gemini-3-pro-preview"],
    },
}


def default_models() -> dict:
    """실행 한 건에 쓸 역할->모델 매핑 기본값."""
    return {role: cfg["default"] for role, cfg in SELECTABLE_MODELS.items()}


def resolve_models(overrides: dict | None) -> dict:
    """사용자 선택을 반영한다. 허용 목록 밖의 값은 무시."""
    models = default_models()
    for role, model in (overrides or {}).items():
        if role in SELECTABLE_MODELS and model in SELECTABLE_MODELS[role]["options"]:
            models[role] = model
    return models


@dataclass(frozen=True)
class BudgetConfig:
    """요청당 실행 한도. 발산 루프 방지용."""

    max_retries_per_task: int = 3
    max_total_llm_calls: int = 60   # 트렌드/자문/설계리뷰 노드 추가로 40 -> 60
    max_design_review_rounds: int = 2  # 설계 반려 시 재설계 허용 횟수


@dataclass(frozen=True)
class SandboxConfig:
    """Docker 검증 환경 설정."""

    image: str = "agent-orchestra-sandbox:latest"  # sandbox/Dockerfile에서 자동 빌드
    timeout_seconds: int = 120
    install_timeout_seconds: int = 300
    workdir_mount: str = "/app"
    pip_cache_volume: str = "agent-orchestra-pip-cache"
    deps_dir: str = ".deps"  # 프로젝트 의존성이 설치되는 workdir 하위 디렉토리


BUDGET = BudgetConfig()
SANDBOX = SandboxConfig()
