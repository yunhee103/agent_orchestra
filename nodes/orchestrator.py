"""총괄(오케스트레이터) · 전문가 자문 · 설계 리뷰 노드.

체계 흐름:
  기획 체계   = collect_decisions (트렌드·도메인 관점 포함 결정 수집)
  자문 체계   = consult_specialists (총괄이 요청한 전담 전문가 초청)
  설계 체계   = decompose (아키텍처·개발 컨벤션·검증 계획을 산출물로 확정)
  추론/QA렌즈 = design_review (수석 아키텍트가 구현 전 설계를 비판적으로 검토)

분해 품질이 전체 품질을 결정하므로 총괄과 아키텍트는 Fable 5 고정이다.
"""

import json
import re
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from nodes.util import make_llm, strip_code_fence
from state import OrchestraState

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
PONYTAIL_LEVELS = ("lite", "full", "ultra", "off")


def load_skills(ponytail_level: str = "full") -> list[tuple[str, str]]:
    """skills/*.md 를 (이름, 내용)으로 로드한다. 리뷰 프롬프트에 주입된다.

    ponytail-<level>.md 는 강도 게이트: 선택된 레벨 파일만 로드되고,
    off면 포니테일 계열 전체가 빠진다. 그 외 스킬은 항상 로드된다.
    스킬 추가 = 파일 하나 추가. 코드 수정 불필요.
    """
    if not SKILLS_DIR.exists():
        return []
    skills = []
    for p in sorted(SKILLS_DIR.glob("*.md")):
        if p.stem.startswith("ponytail-"):
            if p.stem != f"ponytail-{ponytail_level}":
                continue
        skills.append((p.stem, p.read_text(encoding="utf-8")))
    return skills


def _safe_folder_name(name: str) -> str:
    """폴더명으로 안전하게 정리한다. 비면 'project'."""
    name = re.sub(r'[<>:"/\\|?*\s]+', "-", name.strip()).strip("-")[:50]
    return name or "project"


def skills_block(ponytail_level: str) -> str:
    """리뷰 프롬프트에 붙일 스킬 텍스트."""
    loaded = load_skills(ponytail_level)
    return "\n\n".join(
        f"=== 스킬: {name} ===\n{body}" for name, body in loaded
    ) or "(장착된 스킬 없음)"

DECISION_COLLECT_PROMPT = """너는 소프트웨어 프로젝트의 총괄 책임자다.
사용자 요청과 최신 트렌드 조사를 읽고 두 가지를 수행하라.

[1] 구현 전에 반드시 사용자가 직접 선택해야 하는 '중대 결정'을 수집하라.
중대 결정의 기준 (하나 이상 충족):
1. 나중에 되돌리는 비용이 큰 것: 프레임워크, DB, 인증 방식, 프로젝트 구조
2. 요구사항이 애매해서 추측하면 틀릴 확률이 높은 것
3. 3. **외부 연동 대상이 특정되지 않은 것 — 
절대 추측 금지.** 인증, 결제, 알림, 클라우드, 스토리지 등 제3자(Third-Party) 외부 시스템과의 모든 접점에서 구체적인 '제품명/벤더명'이 명시되지 않았다면 절대로 임의 선택하지 마라. 
네가 구현하기 쉬운 서비스(공개 API·문서 많은 것)를 기본값처럼 고르는 것이 가장 흔한 실수이고, 완성돼도 사용자가 원한 물건이 아니게 된다. 반드시 결정 항목으로 올리고, 후보에는 폐쇄형/기업용 API의 제약(발급 조건, 승인 절차)과 운영 비용도 장단점에 명시하라.
다음은 절대 묻지 않는다: 변수명, 파일명, 라이브러리 마이너 버전, 코드 스타일.
트렌드 조사 내용이 결정 후보의 장단점 판단에 도움이 되면 반영하라.


[2] 이 프로젝트에 전담 전문가 초청이 필요한지 판단하라.
일반적인 웹/CLI 프로젝트면 필요 없다(빈 배열). 다음처럼 특수 지식이
설계를 좌우할 때만 0~2명 요청하라: 금융/의료/법률 등 규제 도메인,
보안이 핵심인 시스템, 특수 프로토콜/하드웨어 연동.

반드시 아래 JSON 형식으로만 응답하라. 마크다운 코드펜스 없이 순수 JSON만.

{"decisions": [{"decision_id": "d1", "question": "...", "why_important": "...",
"options": [{"name": "...", "pros": "...", "cons": "...", "fit": "..."}],
"recommended": "...", "reason": "...", "user_choice": null}],
"specialists": [{"role": "...", "reason": "...", "notes": ""}]}
"""

CONSULT_PROMPT = """너는 {role}이다. 아래 프로젝트에 대해 설계 전 자문을 제공하라.
초청 사유: {reason}

너의 전문 관점에서만 답하라: 설계가 반드시 지켜야 할 제약, 흔한 실수,
규제/표준 요구사항, 권장 패턴. 일반론은 빼고 이 프로젝트에 구체적으로.
한국어로 10줄 이내."""

DECOMPOSE_PROMPT = """너는 소프트웨어 프로젝트의 총괄 책임자다. 체계 중심으로 설계하라.
사용자 요청, 확정된 결정사항, 트렌드 조사, 전문가 자문을 바탕으로:

[0] 프로젝트 이름 (project_name): 폴더명으로 쓸 짧은 영문 소문자 슬러그
    (예: unit-converter, todo-api). 공백 대신 하이픈.
[1] 설계 체계 (architecture): 레이어 구조, 모듈 경계, 데이터 흐름, 왜 이 구조인지.
[2] 개발 체계 (conventions): 코딩 컨벤션, 에러 처리 규칙, 로깅, 의존성 정책.
    모든 워커가 이 규칙을 따르므로 구체적으로.
[3] 검증 체계 (verification_plan): 무엇을 어떤 테스트로 검증할지, 경계 케이스.
[4] 인터페이스 확정 (interface_spec): 파일 구조, 각 파일의 공개 함수/클래스
    시그니처, 타입. 워커들이 여기에 맞춰 독립 구현한다.
[5] 태스크 분해 (tasks): 파일 단위로 독립 구현 가능하게. 워커끼리 대화할 수
    없으므로 각 태스크에 필요한 컨텍스트(관련 인터페이스, 결정사항, 컨벤션,
    자문 내용)를 요약하지 말고 원문으로 전부 포함하라.
    각 태스크에 role을 지정하라: backend | frontend | design | test | docs | devops
    - 화면/스타일(HTML/CSS/템플릿) 파일은 design 또는 frontend
    - pytest 테스트 파일 태스크를 반드시 포함(role: test)
    - 외부 라이브러리를 쓰면 requirements.txt 태스크 포함(role: devops)

설계 반려 피드백이 있으면 반드시 반영해 다시 설계하라.

반드시 아래 JSON 형식으로만 응답하라. 마크다운 코드펜스 없이 순수 JSON만.

{"project_name": "...", "architecture": "...", "conventions": "...",
"verification_plan": "...", "interface_spec": "...",
"tasks": [{"task_id": "t1", "role": "backend", "description": "...",
"interface_spec": "...", "target_file": "...", "context": "..."}]}
"""

DESIGN_REVIEW_PROMPT = """너는 수석 아키텍트다. 구현이 시작되기 전에 설계를
비판적으로 검토하는 것이 너의 역할이다(QA 렌즈). 승인은 신중하게, 반려는 근거 있게.

다음 렌즈로 검토하라:
1. 정합성: 인터페이스 시그니처끼리 모순이 없는가? 태스크 간 의존이 순환하는가?
2. 완결성: 태스크 컨텍스트만으로 워커가 독립 구현이 가능한가? 빠진 파일은 없는가?
3. 검증 가능성: 검증 계획이 실제 pytest로 실행 가능한가?
4. 결정 준수: 사용자가 내린 결정과 자문 내용이 설계에 반영됐는가?

아래 [장착된 리뷰 스킬]이 있으면 그 지침을 위 렌즈보다 최우선 기준으로 적용하라.

치명적 문제가 없으면 승인하라. 사소한 개선점은 feedback에 적되 approved: true.

반드시 아래 JSON 형식으로만 응답하라. 마크다운 코드펜스 없이 순수 JSON만.

{"approved": true, "issues": ["..."], "feedback": "..."}
"""


async def _parse_json(raw: str, llm) -> dict:
    """모델 응답에서 JSON을 파싱한다. 3단 복구 사다리.

    1. 코드펜스 제거 후 그대로 파싱
    2. 실패 시 가장 바깥 {...} 블록만 추출해 파싱 (앞뒤 잡담 제거)
    3. 그래도 실패하면 같은 모델에게 수리를 1회 요청 (비결정적 이스케이프
       깨짐 대응 — 실전에서 실제로 발생함)
    """
    text = strip_code_fence(raw)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    response = await llm.ainvoke([
        ("system", "아래 텍스트를 의미 변경 없이 유효한 JSON으로 고쳐라. "
                   "마크다운 코드펜스 없이 순수 JSON만 출력하라."),
        ("human", text),
    ])
    return json.loads(strip_code_fence(response.content))


async def collect_decisions(state: OrchestraState) -> dict:
    """중대 결정 수집 + 전담 전문가 필요 여부 판단."""
    llm = make_llm(state["models"]["orchestrator"], max_tokens=4096)
    response = await llm.ainvoke([
        SystemMessage(content=DECISION_COLLECT_PROMPT),
        HumanMessage(content=(
            f"사용자 요청:\n{state['user_request']}\n\n"
            f"최신 트렌드 조사:\n{state['trend_report']}"
        )),
    ])
    parsed = await _parse_json(response.content, llm)
    return {
        "decisions": parsed.get("decisions", []),
        "specialists": parsed.get("specialists", []),
        "llm_call_count": 1,
    }


async def consult_specialists(state: OrchestraState) -> dict:
    """총괄이 초청한 전문가들의 자문을 수집한다. 설계 품질 직결이라 Fable 5."""
    llm = make_llm(state["models"]["orchestrator"], max_tokens=2048)
    updated = []
    calls = 0
    for sp in state["specialists"]:
        response = await llm.ainvoke([
            SystemMessage(content=CONSULT_PROMPT.format(
                role=sp["role"], reason=sp["reason"])),
            HumanMessage(content=state["user_request"]),
        ])
        updated.append({**sp, "notes": response.content})
        calls += 1
    return {"specialists": updated, "llm_call_count": calls}


async def decompose(state: OrchestraState) -> dict:
    """체계(설계·개발·검증)를 확정하고 role별 태스크로 분해한다."""
    decisions_text = "\n".join(
        f"- {d['question']}: 사용자 결정 = {d['user_choice']} (추천 이유: {d['reason']})"
        for d in state["decisions"]
    )
    consult_text = "\n\n".join(
        f"[{sp['role']} 자문]\n{sp['notes']}" for sp in state["specialists"]
    ) or "(자문 없음)"
    feedback = state.get("design_feedback") or "(반려 피드백 없음 — 첫 설계)"

    llm = make_llm(state["models"]["orchestrator"], max_tokens=16384)
    response = await llm.ainvoke([
        SystemMessage(content=DECOMPOSE_PROMPT),
        HumanMessage(content=(
            f"사용자 요청:\n{state['user_request']}\n\n"
            f"확정된 결정사항:\n{decisions_text}\n\n"
            f"최신 트렌드 조사:\n{state['trend_report']}\n\n"
            f"전문가 자문:\n{consult_text}\n\n"
            f"설계 반려 피드백:\n{feedback}"
        )),
    ])
    parsed = await _parse_json(response.content, llm)
    # 프로젝트 폴더 확정: base_dir/<이름>. 이미 있으면 -2, -3... 으로 회피.
    name = _safe_folder_name(parsed.get("project_name", ""))
    base = Path(state["base_dir"])
    workdir = base / name
    suffix = 2
    while workdir.exists() and any(workdir.iterdir()):
        workdir = base / f"{name}-{suffix}"
        suffix += 1
    workdir.mkdir(parents=True, exist_ok=True)
    return {
        "project_name": workdir.name,
        "workdir": str(workdir.resolve()),
        "architecture": parsed.get("architecture", ""),
        "conventions": parsed.get("conventions", ""),
        "verification_plan": parsed.get("verification_plan", ""),
        "interface_spec": parsed["interface_spec"],
        "tasks": parsed["tasks"],
        "llm_call_count": 1,
    }


async def design_review(state: OrchestraState) -> dict:
    """수석 아키텍트의 구현 전 설계 리뷰. 반려 시 피드백과 함께 재설계로."""
    tasks_text = "\n".join(
        f"- [{t['role']}] {t['target_file']}: {t['description']}"
        for t in state["tasks"]
    )
    skills_text = skills_block(state.get("ponytail_level", "full"))
    llm = make_llm(state["models"]["reviewer"], max_tokens=4096)
    response = await llm.ainvoke([
        SystemMessage(content=DESIGN_REVIEW_PROMPT
                      + f"\n\n[장착된 리뷰 스킬]\n{skills_text}"),
        HumanMessage(content=(
            f"사용자 요청:\n{state['user_request']}\n\n"
            f"설계 체계:\n{state['architecture']}\n\n"
            f"개발 체계:\n{state['conventions']}\n\n"
            f"검증 체계:\n{state['verification_plan']}\n\n"
            f"인터페이스:\n{state['interface_spec']}\n\n"
            f"태스크 목록:\n{tasks_text}"
        )),
    ])
    parsed = await _parse_json(response.content, llm)
    approved = bool(parsed.get("approved"))
    feedback = parsed.get("feedback", "")
    issues = parsed.get("issues", [])
    return {
        "design_feedback": "" if approved else
            feedback + ("\n문제점: " + "; ".join(issues) if issues else ""),
        "design_review_rounds": state["design_review_rounds"] + (0 if approved else 1),
        "llm_call_count": 1,
    }


CODE_REVIEW_PROMPT = """너는 수석 아키텍트다. 검증(테스트)을 통과한 최종 코드를
과잉 설계 관점에서 리뷰한다(/ponytail-review). 정확성은 이미 검증됐으므로
오직 단순화·삭제 가능성만 본다. 아래 [장착된 리뷰 스킬]의 강도 기준을 따르라.

이 리뷰는 자문이다 — 코드를 되돌리지 않고 기록으로 남는다.
발견이 없으면 findings를 빈 배열로.

반드시 아래 JSON 형식으로만 응답하라. 마크다운 코드펜스 없이 순수 JSON만.

{"findings": [{"file": "...", "issue": "...", "suggestion": "...",
"deletable_lines": 0}], "summary": "..."}
"""

_MAX_FILE_CHARS = 4000   # ponytail: 파일당 앞부분만 리뷰. 초대형 파일이면 늘릴 것.


async def code_review(state: OrchestraState) -> dict:
    """검증 통과 후 생성 코드 전체를 과잉 설계 렌즈로 리뷰한다. 자문형."""
    corpus = "\n\n".join(
        f"=== {r['file_path']} ===\n{r['code'][:_MAX_FILE_CHARS]}"
        for r in state["results"]
    )
    skills_text = skills_block(state.get("ponytail_level", "full"))
    llm = make_llm(state["models"]["reviewer"], max_tokens=4096)
    response = await llm.ainvoke([
        SystemMessage(content=CODE_REVIEW_PROMPT
                      + f"\n\n[장착된 리뷰 스킬]\n{skills_text}"),
        HumanMessage(content=(
            f"사용자 요청:\n{state['user_request']}\n\n생성된 코드:\n{corpus}"
        )),
    ])
    parsed = await _parse_json(response.content, llm)
    findings = parsed.get("findings", [])
    saved = sum(f.get("deletable_lines", 0) or 0 for f in findings)
    lines = [f"- {f['file']}: {f['issue']} -> {f['suggestion']}" for f in findings]
    report = (parsed.get("summary", "") + "\n" + "\n".join(lines)
              + (f"\n삭제 가능 추정: {saved}줄" if saved else "")).strip()
    return {"code_review_report": report or "발견 없음 — 군더더기 없는 코드.",
            "llm_call_count": 1}
