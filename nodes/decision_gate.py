"""결정 게이트 노드.

오케스트레이터가 수집한 중대 결정을 interrupt()로 사용자에게 제시하고,
선택 결과를 state에 원문 그대로 기록한다. 결정이 없으면 그냥 통과한다.
"""

from langgraph.types import interrupt

from state import OrchestraState


def decision_gate(state: OrchestraState) -> dict:
    """사용자 선택이 필요한 결정이 있으면 실행을 멈추고 입력을 기다린다.

    interrupt()의 payload가 클라이언트로 전달되고,
    재개 시 Command(resume={decision_id: 선택값})으로 받는다.
    """
    pending = [d for d in state["decisions"] if d["user_choice"] is None]
    if not pending:
        return {}

    choices = interrupt(
        {
            "type": "decision_required",
            "message": f"구현 전 확정이 필요한 결정 {len(pending)}건입니다.",
            "decisions": pending,
        }
    )

    updated = []
    for d in state["decisions"]:
        if d["decision_id"] in choices:
            d = {**d, "user_choice": choices[d["decision_id"]]}
        updated.append(d)
    return {"decisions": updated}
