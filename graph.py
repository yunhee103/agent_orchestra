"""그래프 조립.

흐름: 트렌드 조사 -> 결정 수집 -> 결정 게이트(interrupt)
      -> 전문가 자문(초청 시) -> 설계(체계 확정·분해) -> 설계 리뷰(반려 시 재설계)
      -> 워커 병렬 fan-out(Send) -> 검증 -> (실패 시) 재작업 루프 (한도 내)
      -> (한도 초과) 인간 에스컬레이션 -> 종료
"""

import asyncio
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Send, interrupt

from config import BUDGET
from nodes.decision_gate import decision_gate
from nodes.orchestrator import (code_review, collect_decisions,
                                consult_specialists, decompose, design_review)
from nodes.trend import trend_research
from nodes.verifier import verify_node
from nodes.worker import implement_node, implement_task
from state import OrchestraState


def route_after_gate(state: OrchestraState) -> str:
    """전문가 초청이 요청됐으면 자문 노드를 거친다."""
    return "consult" if state["specialists"] else "decompose"


def route_after_review(state: OrchestraState) -> str:
    """설계 리뷰 결과 분기. 반려 한도 초과 시 그대로 진행(무한 재설계 방지)."""
    if (state["design_feedback"]
            and state["design_review_rounds"] <= BUDGET.max_design_review_rounds):
        return "decompose"
    return "fan_out"


def fan_out_tasks(state: OrchestraState) -> list[Send]:
    """분해된 태스크를 워커에 병렬로 흩뿌린다. 각 Send가 브랜치 하나."""
    return [
        Send(
            "implement",
            {
                "task": task,
                "interface_spec": state["interface_spec"],
                "conventions": state["conventions"],
                "workdir": state["workdir"],
                "model": state["models"]["worker"],
            },
        )
        for task in state["tasks"]
    ]



def _worker_usage(task_map: dict, results: list) -> dict:
    """재작성 결과들의 토큰 사용량을 워커 role별로 합산한다."""
    usage: dict = {}
    for r in results:
        role = task_map.get(r["task_id"], {}).get("role", "worker")
        cur = usage.setdefault(f"워커:{role}", {"input": 0, "output": 0})
        u = r.get("usage") or {}
        cur["input"] += u.get("input", 0)
        cur["output"] += u.get("output", 0)
    return usage

async def rework_node(state: OrchestraState) -> dict:
    """검증 실패가 특정된 태스크만 실패 로그와 함께 병렬 재구현한다."""
    task_map = {t["task_id"]: t for t in state["tasks"]}
    to_fix = [
        r for r in state["results"] if not r["verified"] and r["last_error"]
    ]
    fixed = await asyncio.gather(
        *(
            implement_task(
                task_map[r["task_id"]],
                state["interface_spec"],
                state["conventions"],
                state["workdir"],
                state["models"]["worker"],
                previous=r,
            )
            for r in to_fix
        )
    )
    return {"results": list(fixed), "llm_call_count": len(fixed),
            "token_usage": _worker_usage(task_map, fixed)}


def escalate_node(state: OrchestraState) -> dict:
    """재시도 한도 초과 시 사람에게 넘긴다. 자동 루프의 탈출구."""
    failing = [r for r in state["results"] if not r["verified"]]
    decision = interrupt(
        {
            "type": "escalation",
            "message": "자동 재작업 한도를 초과했습니다. 실패 로그를 확인하세요.",
            "failing_tasks": [
                {"task_id": r["task_id"], "error": r["last_error"]} for r in failing
            ],
        }
    )
    # 사람이 "retry"를 입력하면 카운터를 리셋하고 한 사이클 더 허용한다.
    if decision == "retry":
        results = [{**r, "retry_count": 0} for r in state["results"]]
        return {"results": results}
    return {"final_summary": "사용자 중단: 검증 실패 상태로 종료."}


def finalize_node(state: OrchestraState) -> dict:
    """성공 종료 요약. 체계 산출물 요약도 함께."""
    files = [r["file_path"] for r in state["results"]]
    review = state.get("code_review_report")
    return {"final_summary": (
        "검증 통과. 생성 파일:\n" + "\n".join(files)
        + (f"\n\n[포니테일 코드 리뷰]\n{review}" if review else "")
        + f"\n\nLLM 호출 {state['llm_call_count']}회 사용."
    )}


def route_after_verify(state: OrchestraState) -> str:
    """검증 결과에 따라 분기한다. 전부 통과면 포니테일 코드 리뷰를 거친다.

    리뷰->리팩터 후 재검증이 통과하면(리포트 이미 존재) 바로 종료 — 리뷰 1회 원칙.
    """
    if all(r["verified"] for r in state["results"]):
        if state.get("ponytail_level") == "off" or state.get("code_review_report"):
            return "finalize"
        return "code_review"
    over_retry = any(
        r["retry_count"] >= BUDGET.max_retries_per_task
        for r in state["results"]
        if not r["verified"]
    )
    over_budget = state["llm_call_count"] >= BUDGET.max_total_llm_calls
    if over_retry or over_budget:
        return "escalate"
    return "rework"


def route_after_code_review(state: OrchestraState) -> str:
    """리뷰 발견이 있으면 반영(리팩터)한다. lite는 권고만 남기고 종료."""
    if (state.get("code_review_findings")
            and state.get("ponytail_level") in ("full", "ultra")):
        return "refactor"
    return "finalize"


async def refactor_node(state: OrchestraState) -> dict:
    """포니테일 리뷰 지시를 담당 워커가 반영해 재작성한다. 이후 재검증."""
    task_map = {t["task_id"]: t for t in state["tasks"]}
    by_path = {r["file_path"]: r for r in state["results"]}
    by_name = {Path(r["file_path"]).name: r for r in state["results"]}

    notes: dict[str, list[str]] = {}   # task_id -> 지시 목록 (파일당 병합)
    targets: dict[str, dict] = {}
    for f in state["code_review_findings"]:
        r = by_path.get(f["file"]) or by_name.get(Path(f["file"]).name)
        if r is None:
            continue
        notes.setdefault(r["task_id"], []).append(
            f"{f.get('issue', '')}\n지시: {f['suggestion']}")
        targets[r["task_id"]] = r

    jobs = [
        implement_task(
            task_map[tid], state["interface_spec"], state["conventions"],
            state["workdir"], state["models"]["worker"],
            previous=targets[tid], revision_note="\n\n".join(note_list),
        )
        for tid, note_list in notes.items() if tid in task_map
    ]
    if not jobs:
        return {"llm_call_count": 0}
    refactored = await asyncio.gather(*jobs)
    return {"results": list(refactored), "llm_call_count": len(refactored),
            "token_usage": _worker_usage(task_map, refactored)}


def route_after_escalate(state: OrchestraState) -> str:
    """에스컬레이션 후 사용자 결정에 따라 분기한다."""
    if state.get("final_summary"):
        return END
    return "rework"


def build_graph(checkpointer=None):
    """그래프를 컴파일해 반환한다. interrupt 사용을 위해 checkpointer 필수.

    서버는 AsyncSqliteSaver를 넘겨 재시작에도 상태를 보존하고,
    CLI는 기본 MemorySaver로 동작한다.
    """
    builder = StateGraph(OrchestraState)

    builder.add_node("trend_research", trend_research)
    builder.add_node("collect_decisions", collect_decisions)
    builder.add_node("decision_gate", decision_gate)
    builder.add_node("consult", consult_specialists)
    builder.add_node("decompose", decompose)
    builder.add_node("design_review", design_review)
    builder.add_node("implement", implement_node)
    builder.add_node("verify", verify_node)
    builder.add_node("code_review", code_review)
    builder.add_node("refactor", refactor_node)
    builder.add_node("rework", rework_node)
    builder.add_node("escalate", escalate_node)
    builder.add_node("finalize", finalize_node)

    builder.set_entry_point("trend_research")
    builder.add_edge("trend_research", "collect_decisions")
    builder.add_edge("collect_decisions", "decision_gate")
    builder.add_conditional_edges(
        "decision_gate", route_after_gate,
        {"consult": "consult", "decompose": "decompose"},
    )
    builder.add_edge("consult", "decompose")
    builder.add_edge("decompose", "design_review")
    # 설계 리뷰: 반려 시 피드백과 함께 재설계, 승인 시 병렬 구현으로 fan-out.
    # fan_out_tasks가 Send 목록을 반환하므로 라우터를 겸한다.
    builder.add_conditional_edges(
        "design_review",
        lambda s: route_after_review(s),
        {"decompose": "decompose", "fan_out": "fan_out_router"},
    )
    builder.add_node("fan_out_router", lambda s: {})
    builder.add_conditional_edges("fan_out_router", fan_out_tasks, ["implement"])
    builder.add_edge("implement", "verify")
    builder.add_conditional_edges(
        "verify",
        route_after_verify,
        {"finalize": "finalize", "code_review": "code_review",
         "rework": "rework", "escalate": "escalate"},
    )
    builder.add_conditional_edges(
        "code_review", route_after_code_review,
        {"refactor": "refactor", "finalize": "finalize"},
    )
    builder.add_edge("refactor", "verify")
    builder.add_edge("rework", "verify")
    builder.add_conditional_edges(
        "escalate",
        route_after_escalate,
        {"rework": "rework", END: END},
    )
    builder.add_edge("finalize", END)

    return builder.compile(checkpointer=checkpointer or MemorySaver())
