"""워커 노드.

완결된 태스크 명세를 받아 코드를 생성한다. 태스크의 role에 따라
시니어 직군 페르소나가 배정되고, 총괄이 확정한 개발 체계(컨벤션)를 따른다.
Send API 병렬 fan-out으로 태스크당 워커 하나가 동시 실행된다.
"""

from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from nodes.util import make_llm, strip_code_fence
from state import SubTask, TaskResult

# role -> 시니어 페르소나. 모두 "경력 많고 능력 있는 시니어" 전제.
ROLE_PERSONAS = {
    "backend": "너는 15년차 시니어 백엔드 엔지니어다. 대규모 트래픽 서비스의 "
               "API·데이터 계층을 설계·운영해왔고, 견고한 에러 처리와 명확한 "
               "경계 설계가 몸에 배어 있다.",
    "frontend": "너는 12년차 시니어 프론트엔드 엔지니어다. 접근성과 성능을 "
                "타협하지 않으면서 유지보수하기 쉬운 UI 코드를 짜는 것으로 "
                "정평이 나 있다.",
    "design": "너는 10년차 시니어 UI/UX 디자이너 겸 퍼블리셔다. HTML/CSS/템플릿을 "
              "직접 다루며, 일관된 디자인 시스템과 사용성 좋은 화면을 만든다.",
    "test": "너는 12년차 시니어 QA 자동화 엔지니어다. 경계 케이스를 집요하게 "
            "찾아내는 pytest 테스트를 짜고, 통과가 아닌 검증을 목표로 한다.",
    "docs": "너는 10년차 시니어 테크니컬 라이터다. 정확하고 군더더기 없는 "
            "문서를 쓴다.",
    "devops": "너는 12년차 시니어 DevOps 엔지니어다. 의존성과 실행 환경을 "
              "최소·명확하게 유지한다.",
}
DEFAULT_PERSONA = ROLE_PERSONAS["backend"]

WORKER_PROMPT = """{persona}

주어진 태스크 명세대로 파일 하나를 구현한다.

규칙:
- 인터페이스 명세의 시그니처와 타입을 정확히 따를 것. 임의로 바꾸지 말 것.
- 개발 체계(컨벤션)를 반드시 준수할 것.
- 확정된 결정사항의 취지에 맞게 구현할 것.
- 응답은 해당 파일의 완성 내용만. 설명, 마크다운 코드펜스 없이 원문만 출력.
"""

RETRY_SUFFIX = """
이전 시도가 검증에 실패했다. 아래 실패 로그를 분석하고 원인을 고쳐 전체 파일을 다시 작성하라.

[이전 코드]
{previous_code}

[검증 실패 로그 원문]
{error_log}
"""

REVISION_SUFFIX = """
검증은 통과했지만 수석 아키텍트 코드 리뷰에서 아래 지시가 나왔다.
동작(외부 인터페이스)은 바꾸지 말고, 지시를 반영해 전체 파일을 다시 작성하라.

[이전 코드]
{previous_code}

[코드 리뷰 지시]
{note}
"""


def _build_task_message(task: SubTask, interface_spec: str, conventions: str) -> str:
    """워커에게 전달할 태스크 메시지를 조립한다. 컨텍스트는 요약 없이 전부 포함."""
    return (
        f"[대상 파일] {task['target_file']}\n\n"
        f"[태스크 설명]\n{task['description']}\n\n"
        f"[개발 체계 (컨벤션)]\n{conventions}\n\n"
        f"[전체 인터페이스 명세]\n{interface_spec}\n\n"
        f"[이 태스크의 인터페이스]\n{task['interface_spec']}\n\n"
        f"[컨텍스트]\n{task['context']}"
    )


async def implement_task(
    task: SubTask,
    interface_spec: str,
    conventions: str,
    workdir: str,
    model: str,
    previous: TaskResult | None = None,
    revision_note: str | None = None,
) -> TaskResult:
    """태스크 하나를 구현하고 파일로 쓴다.

    Args:
        task: 구현할 태스크 명세.
        interface_spec: 프로젝트 전체 인터페이스.
        conventions: 총괄이 확정한 개발 체계.
        workdir: 코드를 쓸 작업 디렉토리.
        model: 이 워커가 쓸 모델 ID.
        previous: 재시도인 경우 직전 실패 결과.

    Returns:
        검증 전 상태의 TaskResult.
    """
    llm = make_llm(model, max_tokens=8192)
    persona = ROLE_PERSONAS.get(task.get("role", ""), DEFAULT_PERSONA)
    message = _build_task_message(task, interface_spec, conventions)
    if previous is not None and revision_note:
        message += REVISION_SUFFIX.format(
            previous_code=previous["code"], note=revision_note)
    elif previous is not None and previous["last_error"]:
        message += RETRY_SUFFIX.format(
            previous_code=previous["code"],
            error_log=previous["last_error"],
        )

    response = await llm.ainvoke([
        SystemMessage(content=WORKER_PROMPT.format(persona=persona)),
        HumanMessage(content=message),
    ])
    code = strip_code_fence(response.content)

    file_path = Path(workdir) / task["target_file"]
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(code, encoding="utf-8")

    return {
        "task_id": task["task_id"],
        "file_path": str(file_path),
        "code": code,
        "retry_count": (previous["retry_count"] + 1) if previous else 0,
        "verified": False,
        "last_error": None,
    }


async def implement_node(payload: dict) -> dict:
    """Send fan-out 대상 노드. 태스크 하나를 받아 병렬로 구현한다.

    payload는 그래프 상태가 아니라 Send(...)로 전달된
    {task, interface_spec, conventions, workdir, model} dict다.
    """
    result = await implement_task(
        payload["task"], payload["interface_spec"], payload["conventions"],
        payload["workdir"], payload["model"],
    )
    # llm_call_count는 operator.add 리듀서이므로 증분만 반환한다.
    return {"results": [result], "llm_call_count": 1}
