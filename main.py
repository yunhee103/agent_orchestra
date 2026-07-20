"""CLI 진입점.

실행 예:
    export ANTHROPIC_API_KEY=...
    python main.py "FastAPI로 할일 관리 REST API 만들어줘" ./output_project
"""

import asyncio
import sys
import uuid
from pathlib import Path

from langgraph.types import Command

from config import default_models
from graph import build_graph
from state import initial_state


def _prompt_decisions(payload: dict) -> dict:
    """결정 요청 payload를 출력하고 사용자 선택을 받는다."""
    print(f"\n{payload['message']}\n")
    choices = {}
    for d in payload["decisions"]:
        print(f"[{d['decision_id']}] {d['question']}")
        print(f"  중요한 이유: {d['why_important']}")
        for i, opt in enumerate(d["options"], start=1):
            print(f"  {i}. {opt['name']}")
            print(f"     장점: {opt['pros']}")
            print(f"     단점: {opt['cons']}")
            print(f"     적합: {opt['fit']}")
        print(f"  추천: {d['recommended']} — {d['reason']}")
        selected = input("  선택 (후보 이름 입력, 엔터 시 추천안): ").strip()
        choices[d["decision_id"]] = selected or d["recommended"]
        print()
    return choices


def _prompt_escalation(payload: dict) -> str:
    """에스컬레이션 payload를 출력하고 retry/stop을 받는다."""
    print(f"\n{payload['message']}\n")
    for item in payload["failing_tasks"]:
        print(f"--- {item['task_id']} ---")
        print(item["error"])
    answer = input("\n재시도하려면 retry, 중단하려면 엔터: ").strip()
    return answer or "stop"


async def run(user_request: str, workdir: str) -> None:
    """그래프를 실행하고 interrupt를 콘솔 입력으로 처리한다."""
    Path(workdir).mkdir(parents=True, exist_ok=True)
    graph = build_graph()
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    initial = initial_state(
        user_request, str(Path(workdir).resolve()), default_models()
    )

    result = await graph.ainvoke(initial, config)

    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        if payload.get("type") == "decision_required":
            resume_value = _prompt_decisions(payload)
        elif payload.get("type") == "escalation":
            resume_value = _prompt_escalation(payload)
        else:
            raise RuntimeError(f"알 수 없는 interrupt 유형: {payload}")
        result = await graph.ainvoke(Command(resume=resume_value), config)

    print(result.get("final_summary", "요약 없음"))


def main() -> None:
    if len(sys.argv) < 3:
        print("사용법: python main.py \"<요청>\" <출력 디렉토리>")
        sys.exit(1)
    asyncio.run(run(sys.argv[1], sys.argv[2]))


if __name__ == "__main__":
    main()
