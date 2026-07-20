"""그래프 전역 상태 스키마.

모든 워커 결과에는 task_id가 박혀 있어야 병렬 fan-out 시 추적이 가능하다.
"""

import operator
from typing import Annotated, Optional, TypedDict


class DecisionOption(TypedDict):
    """사용자에게 제시할 결정 후보 하나."""

    name: str
    pros: str
    cons: str
    fit: str


class Decision(TypedDict):
    """중대 결정 항목. 오케스트레이터가 분해 단계에서 수집한다."""

    decision_id: str
    question: str
    why_important: str
    options: list[DecisionOption]
    recommended: str
    reason: str
    user_choice: Optional[str]


class Specialist(TypedDict):
    """총괄이 초청을 요청한 전담 전문가."""

    role: str        # 예: "금융 도메인 전문가", "보안 전문가"
    reason: str      # 왜 필요한지
    notes: str       # 자문 결과 (consult 노드가 채움)


class SubTask(TypedDict):
    """워커에게 전달되는 완결된 작업 단위."""

    task_id: str
    role: str        # backend | frontend | design | test | docs | devops
    description: str
    interface_spec: str
    target_file: str
    context: str


class TaskResult(TypedDict):
    """워커 산출물. task_id로 어느 태스크의 결과인지 추적한다."""

    task_id: str
    file_path: str
    code: str
    retry_count: int
    verified: bool
    last_error: Optional[str]


def merge_results(
    existing: list[TaskResult], new: list[TaskResult]
) -> list[TaskResult]:
    """task_id 기준 병합 리듀서. 같은 task_id는 새 값으로 교체, 없으면 추가.

    Send 병렬 fan-out과 검증/재작업 노드의 부분 갱신을 모두 지원한다.
    operator.add를 쓰면 갱신이 교체가 아닌 중복 append가 되므로 쓰지 않는다.
    """
    merged = {r["task_id"]: r for r in existing}
    for r in new:
        merged[r["task_id"]] = r
    return list(merged.values())


class OrchestraState(TypedDict):
    """그래프 전체 상태."""

    user_request: str
    models: dict                     # 역할 -> 모델 ID (실행 시작 시 확정)
    ponytail_level: str              # 단순화 렌즈 강도: lite | full | ultra | off
    trend_report: str                # 트렌드 봇의 실시간 조사 요약
    decisions: list[Decision]
    specialists: list[Specialist]    # 초청된 전문가와 자문 결과
    # 체계 산출물: 분해 시 함께 확정된다
    prd: str                         # 제품 요구사항 문서 (PRD.md로 저장됨)
    architecture: str                # 설계 체계 (레이어, 모듈 경계, 데이터 흐름)
    conventions: str                 # 개발 체계 (컨벤션, 에러 처리, 로깅 규칙)
    verification_plan: str           # 검증 체계 (무엇을 어떻게 테스트할지)
    interface_spec: str
    design_review_rounds: int        # 설계 반려 횟수
    design_feedback: str             # 아키텍트의 반려 사유 (재설계 입력)
    code_review_report: str          # 검증 통과 후 과잉 설계 리뷰 결과
    code_review_findings: list       # 리뷰 발견 [{file, issue, suggestion}] — 리팩터 입력
    tasks: list[SubTask]
    results: Annotated[list[TaskResult], merge_results]
    # 병렬 브랜치가 동시에 갱신하므로 누적 리듀서 필수. 노드는 총합이 아닌 증분을 반환할 것.
    llm_call_count: Annotated[int, operator.add]
    base_dir: str                    # 프로젝트 폴더들이 생성되는 상위 디렉토리
    project_name: str                # 총괄이 설계 시 지은 이름 (폴더명)
    workdir: str                     # 실제 생성 위치 = base_dir/project_name
    final_summary: Optional[str]


def initial_state(user_request: str, workdir: str, models: dict,
                  ponytail_level: str = "full") -> OrchestraState:
    """초기 상태를 만든다. CLI와 서버가 공유."""
    return {
        "user_request": user_request,
        "models": models,
        "ponytail_level": ponytail_level,
        "trend_report": "",
        "prd": "",
        "decisions": [],
        "specialists": [],
        "architecture": "",
        "conventions": "",
        "verification_plan": "",
        "interface_spec": "",
        "design_review_rounds": 0,
        "design_feedback": "",
        "code_review_report": "",
        "code_review_findings": [],
        "tasks": [],
        "results": [],
        "llm_call_count": 0,
        "base_dir": workdir,
        "project_name": "",
        # 총괄이 프로젝트 이름을 짓기 전까지는 base에 직접 쓰지 않도록
        # decompose에서 base_dir/<이름> 으로 갱신된다.
        "workdir": workdir,
        "final_summary": None,
    }
