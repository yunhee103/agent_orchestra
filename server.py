"""웹 UI 서버.

graph.astream()의 노드 단위 이벤트를 SSE로 브라우저에 밀어주고,
interrupt(결정/에스컬레이션)는 POST /runs/{id}/resume 으로 재개한다.
GET /models 로 역할별 모델 선택지를 내려주고, 실행 시작 시 선택을 반영한다.

실행:
    uvicorn server:app --port 8000
    브라우저에서 http://localhost:8000
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command
from pydantic import BaseModel

import re

import os

from config import (MODEL_CATALOG, PROVIDER_KEY_ENV,
                    SELECTABLE_MODELS, resolve_models)
from graph import build_graph
from nodes.orchestrator import PONYTAIL_LEVELS, load_skills, skills_block
from nodes.util import make_llm, strip_code_fence
from state import initial_state

ENV_FILE = Path(__file__).parent / ".env"


def _load_env_file() -> None:
    """UI에서 저장한 API 키(.env)를 서버 시작 시 환경에 올린다.

    이미 셸에서 설정된 환경변수가 우선한다(setdefault).
    """
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


_load_env_file()

CHECKPOINT_DB = Path(__file__).parent / "orchestra.db"
_SAVER = None   # AsyncSqliteSaver — 서버 기동 시 생성


@asynccontextmanager
async def _lifespan(app):
    """체크포인터를 서버 수명과 함께 연다. 재시작해도 그래프 상태가 남는다."""
    global _SAVER
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    conn = await aiosqlite.connect(str(CHECKPOINT_DB))
    _SAVER = AsyncSqliteSaver(conn)
    await _SAVER.setup()
    yield
    await conn.close()


app = FastAPI(title="agent-orchestra", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"),
          name="static")


@app.middleware("http")
async def no_cache_static(request, call_next):
    """개발 편의: static을 항상 재검증하게 해 수정 즉시 반영."""
    response = await call_next(request)
    if request.url.path.startswith("/static") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache"
    return response


# 노드 -> (역할, 하는 일, 모델 역할 키). 모델 ID는 실행별 models에서 채운다.
NODE_INFO = {
    "trend_research": ("트렌드봇", "실시간 웹 트렌드 조사", "utility"),
    "collect_decisions": ("총괄", "기획 체계 — 중대 결정 수집", "orchestrator"),
    "decision_gate": ("결정 게이트", "사용자 선택 반영", None),
    "consult": ("초청 전문가", "도메인 자문", "orchestrator"),
    "decompose": ("총괄", "설계 체계 — 아키텍처·컨벤션·검증 계획 확정", "orchestrator"),
    "design_review": ("수석 아키텍트", "구현 전 설계 리뷰 (QA 렌즈)", "reviewer"),
    "fan_out_router": (None, None, None),   # 내부 라우터, UI 표시 안 함
    "implement": ("개발팀", "구축 체계 — 태스크 구현", "worker"),
    "verify": ("QA", "검증 체계 — Docker 샌드박스 (LLM 아님)", None),
    "code_review": ("수석 아키텍트", "포니테일 코드 리뷰 — 과잉 설계 감지", "reviewer"),
    "refactor": ("개발팀", "리뷰 반영 — 단순화 재작성", "worker"),
    "rework": ("개발팀", "보강 체계 — 실패 태스크 재작업", "worker"),
    "escalate": ("에스컬레이션", "사람 판단 대기", None),
    "finalize": ("완료", "최종 요약", None),
}


class Run:
    """실행 하나. 이벤트 히스토리 + 실시간 구독자 + interrupt 재개 채널."""

    def __init__(self, run_id: str, user_request: str, workdir: str,
                 models: dict, ponytail_level: str = "full"):
        self.id = run_id
        self.user_request = user_request
        self.workdir = workdir
        self.models = models
        self.ponytail_level = ponytail_level
        self.events: list[dict] = []
        self.subscribers: list[asyncio.Queue] = []
        self.resume_future: asyncio.Future | None = None
        self.finished = False

    def emit(self, event: dict) -> None:
        event["seq"] = len(self.events)
        self.events.append(event)
        for q in self.subscribers:
            q.put_nowait(event)

    async def wait_resume(self):
        self.resume_future = asyncio.get_event_loop().create_future()
        value = await self.resume_future
        self.resume_future = None
        return value


RUNS: dict[str, Run] = {}


def _node_event(node: str, update: dict, models: dict) -> dict | None:
    """노드 완료 update를 UI용 이벤트로 변환한다."""
    role, action, model_key = NODE_INFO.get(node, (node, "", None))
    if role is None:
        return None
    event = {"type": "node", "node": node, "role": role, "action": action,
             "model": models.get(model_key) if model_key else None}
    if node == "trend_research":
        event["trend_report"] = update.get("trend_report", "")
    if node == "collect_decisions":
        event["decision_count"] = len(update.get("decisions", []))
        event["specialists"] = update.get("specialists", [])
    if node == "consult":
        event["specialists"] = update.get("specialists", [])
    if node == "decompose":
        event["project_name"] = update.get("project_name", "")
        event["workdir"] = update.get("workdir", "")
        event["prd"] = update.get("prd", "")
        event["tasks"] = [
            {"task_id": t["task_id"], "role": t.get("role", "backend"),
             "target_file": t["target_file"], "description": t["description"]}
            for t in update.get("tasks", [])
        ]
        event["architecture"] = update.get("architecture", "")
        event["conventions"] = update.get("conventions", "")
        event["verification_plan"] = update.get("verification_plan", "")
    if node == "design_review":
        event["approved"] = not update.get("design_feedback")
        event["feedback"] = update.get("design_feedback", "")
    if node == "code_review":
        event["report"] = update.get("code_review_report", "")
    if "results" in update:
        event["results"] = [
            {"task_id": r["task_id"], "file_path": r["file_path"],
             "retry_count": r["retry_count"], "verified": r["verified"],
             "last_error": r["last_error"], "code": r["code"]}
            for r in update["results"]
        ]
    if "llm_call_count" in update:
        event["llm_calls_delta"] = update["llm_call_count"]
    if update.get("final_summary"):
        event["final_summary"] = update["final_summary"]
    return event


async def _execute(run: Run) -> None:
    """그래프를 스트리밍 실행하며 이벤트를 emit하고, interrupt 시 재개를 기다린다."""
    import time
    started_at = time.monotonic()
    wait_total = 0.0   # 회의(사용자 결정) 대기 시간 — 제작 시간에서 제외
    graph = build_graph(_SAVER)
    config = {"configurable": {"thread_id": run.id}}
    graph_input = initial_state(run.user_request, run.workdir, run.models,
                                run.ponytail_level)
    try:
        while True:
            interrupt_payload = None
            async for chunk in graph.astream(graph_input, config, stream_mode="updates"):
                for node, update in chunk.items():
                    if node == "__interrupt__":
                        interrupt_payload = update[0].value
                        run.emit({"type": "interrupt", "payload": interrupt_payload})
                    else:
                        event = _node_event(node, update or {}, run.models)
                        if event:
                            run.emit(event)
            if interrupt_payload is None:
                break
            wait_start = time.monotonic()
            value = await run.wait_resume()
            wait_total += time.monotonic() - wait_start
            run.emit({"type": "resumed", "value": value})
            graph_input = Command(resume=value)

        state = await graph.aget_state(config)
        elapsed = time.monotonic() - started_at
        run.emit({"type": "done",
                  "final_summary": state.values.get("final_summary", "요약 없음"),
                  "llm_calls_total": state.values.get("llm_call_count", 0),
                  "elapsed_seconds": round(elapsed),
                  "work_seconds": round(elapsed - wait_total)})
    except Exception as exc:  # 실행 오류도 UI에 보여야 한다
        run.emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        run.finished = True


class StartRequest(BaseModel):
    user_request: str
    workdir: str = "./output_project"
    models: dict | None = None      # 역할 -> 모델 (선택 가능한 역할만 반영됨)
    ponytail_level: str = "full"    # lite | full | ultra | off


class ResumeRequest(BaseModel):
    value: dict | str


@app.get("/models")
async def get_models():
    """역할별 모델 선택지 + 프로바이더/키 보유 여부 + 포니테일 강도.

    available=False(해당 프로바이더 API 키 없음)인 모델은 UI에서 비활성화된다.
    """
    subscription = os.environ.get("CLAUDE_AUTH_MODE") == "subscription"

    def enrich(model_id: str) -> dict:
        provider = MODEL_CATALOG.get(model_id, "anthropic")
        available = bool(os.environ.get(PROVIDER_KEY_ENV[provider]))
        if provider == "anthropic" and subscription:
            available = True   # 구독 인증은 API 키 불필요
        return {"id": model_id, "provider": provider, "available": available}

    selectable = {
        role: {"default": cfg["default"],
               "options": [enrich(m) for m in cfg["options"]]}
        for role, cfg in SELECTABLE_MODELS.items()
    }
    return {"selectable": selectable,
            "claude_auth": "subscription" if subscription else "api",
            "ponytail_levels": list(PONYTAIL_LEVELS)}


@app.get("/runs")
async def list_runs():
    """실행 목록 (최신순). UI가 새로고침 후에도 진행 중 실행에 다시 붙는 용도."""
    return [{"run_id": r.id, "request": r.user_request, "finished": r.finished}
            for r in reversed(RUNS.values())]


@app.post("/runs")
async def start_run(body: StartRequest):
    workdir = Path(body.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    models = resolve_models(body.models)
    level = body.ponytail_level if body.ponytail_level in PONYTAIL_LEVELS else "full"
    run = Run(str(uuid.uuid4()), body.user_request, str(workdir), models, level)
    RUNS[run.id] = run
    run.emit({"type": "started", "request": body.user_request,
              "workdir": str(workdir), "models": models,
              "ponytail_level": level,
              "skills": [name for name, _ in load_skills(level)]})
    asyncio.get_event_loop().create_task(_execute(run))
    return {"run_id": run.id}


# ---------- API 키 설정 ----------

class KeysRequest(BaseModel):
    anthropic: str | None = None
    openai: str | None = None
    google: str | None = None


def _key_status() -> dict:
    """프로바이더별 키 보유 여부 + 마스킹 힌트. 전체 키는 절대 반환하지 않는다."""
    status = {}
    for provider, env_name in PROVIDER_KEY_ENV.items():
        value = os.environ.get(env_name, "")
        status[provider] = {"set": bool(value),
                            "hint": ("****" + value[-4:]) if value else ""}
    return status


@app.get("/settings/keys")
async def get_keys():
    return _key_status()


def _write_env() -> None:
    """API 키 + 인증 모드를 .env에 저장한다."""
    names = list(PROVIDER_KEY_ENV.values()) + ["CLAUDE_AUTH_MODE"]
    lines = [f"{n}={os.environ[n]}" for n in names if os.environ.get(n)]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


@app.post("/settings/keys")
async def save_keys(body: KeysRequest):
    """비어있지 않은 키만 갱신한다. 프로세스 환경 즉시 반영 + .env에 저장."""
    for provider, value in body.model_dump().items():
        if value and value.strip():
            os.environ[PROVIDER_KEY_ENV[provider]] = value.strip()
    _write_env()
    return _key_status()


class AuthModeRequest(BaseModel):
    mode: str   # "api" | "subscription"


@app.get("/settings/claude-auth")
async def get_claude_auth():
    return {"mode": os.environ.get("CLAUDE_AUTH_MODE", "api") or "api"}


@app.get("/settings/claude-auth/status")
async def claude_auth_status():
    """구독 인증의 실체 확인: Claude Code CLI의 로그인 상태를 조회한다.

    authMethod가 'claude.ai'면 OAuth 구독 세션(키 아님)으로 인증 중이라는 뜻.
    비번은 이 앱을 거치지 않는다 — 로그인은 Claude Code가 브라우저 OAuth로 처리.
    """
    import json as _json
    import subprocess
    try:
        proc = await asyncio.to_thread(
            subprocess.run, "claude auth status",
            shell=True, capture_output=True, text=True, timeout=20)
        info = _json.loads(proc.stdout)
        return {"logged_in": bool(info.get("loggedIn")),
                "auth_method": info.get("authMethod", ""),
                "email": info.get("email", ""),
                "org": info.get("orgName", ""),
                "subscription": info.get("subscriptionType", "")}
    except Exception:
        return {"logged_in": False, "auth_method": "",
                "email": "", "org": "", "subscription": "",
                "error": "Claude Code CLI를 찾을 수 없거나 로그인되지 않음"}


@app.post("/settings/claude-auth")
async def set_claude_auth(body: AuthModeRequest):
    """Claude 호출 경로 전환: API 키 결제 vs Pro/Max 구독(Claude Code)."""
    if body.mode not in ("api", "subscription"):
        raise HTTPException(400, "mode는 api 또는 subscription")
    os.environ["CLAUDE_AUTH_MODE"] = body.mode
    _write_env()
    return {"mode": body.mode}


class RenameRequest(BaseModel):
    workdir: str    # 현재 프로젝트 폴더 절대경로
    new_name: str


@app.post("/projects/rename")
async def rename_project(body: RenameRequest):
    """생성된 프로젝트 폴더 이름을 바꾼다 (총괄이 지은 이름이 마음에 안 들 때)."""
    old = Path(body.workdir)
    if not old.is_dir():
        raise HTTPException(404, "프로젝트 폴더가 없음")
    safe = re.sub(r'[<>:"/\\|?*\s]+', "-", body.new_name.strip()).strip("-")[:50]
    if not safe:
        raise HTTPException(400, "폴더명이 비었음")
    if safe == old.name:   # 같은 이름이면 에러 대신 그대로 통과
        return {"workdir": str(old), "project_name": safe}
    new = old.parent / safe
    if new.exists():
        raise HTTPException(409, "같은 이름의 폴더가 이미 있음")
    try:
        old.rename(new)
    except OSError as exc:
        raise HTTPException(409, f"이름 변경 실패 (사용 중일 수 있음): {exc}")
    return {"workdir": str(new), "project_name": safe}


# ---------- 포니테일 도구 (온디맨드) ----------

_SOURCE_EXTS = {".py", ".html", ".css", ".js", ".txt", ".md", ".toml", ".cfg", ".ini"}
_DEBT_RE = re.compile(r"#\s*(TODO|FIXME|HACK|XXX|ponytail:)(.*)|<!--\s*(TODO|FIXME)(.*?)-->",
                      re.IGNORECASE)


def _source_files(workdir: str) -> list[Path]:
    """출력 프로젝트의 소스 파일 목록. .deps와 캐시는 제외."""
    root = Path(workdir)
    if not root.exists():
        return []
    return [p for p in root.rglob("*")
            if p.is_file() and p.suffix in _SOURCE_EXTS
            and ".deps" not in p.parts and "__pycache__" not in p.parts]


@app.get("/ponytail/audit")
async def ponytail_audit(workdir: str, level: str = "full"):
    """/ponytail-audit — 출력 프로젝트 전체를 과잉 설계 렌즈로 스캔 (Fable 5)."""
    files = _source_files(workdir)
    if not files:
        raise HTTPException(404, "스캔할 파일이 없음")
    corpus = "\n\n".join(
        f"=== {p.relative_to(workdir)} ===\n{p.read_text(encoding='utf-8', errors='replace')[:4000]}"
        for p in files[:30]
    )
    skills = skills_block(level if level in PONYTAIL_LEVELS else "full")
    llm = make_llm(resolve_models(None)["reviewer"], max_tokens=4096)
    response = await llm.ainvoke([
        ("system", "너는 수석 아키텍트다. 레포 전체를 스캔해 불필요한 복잡도, "
                   "데드코드, 삭제/병합 후보를 찾아라(/ponytail-audit). "
                   "파일별로 구체적으로, 없으면 없다고. 한국어 보고서로.\n\n"
                   f"[장착된 리뷰 스킬]\n{skills}"),
        ("human", corpus),
    ])
    return {"files_scanned": len(files), "report": strip_code_fence(response.content)}


@app.get("/ponytail/debt")
async def ponytail_debt(workdir: str):
    """/ponytail-debt — TODO/FIXME/ponytail 주석 추적. LLM 없이 무료."""
    items = []
    for p in _source_files(workdir):
        for i, line in enumerate(
                p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if _DEBT_RE.search(line):
                items.append({"file": str(p.relative_to(workdir)),
                              "line": i, "text": line.strip()[:200]})
    return {"count": len(items), "items": items}


@app.get("/ponytail/gain")
async def ponytail_gain(workdir: str):
    """/ponytail-gain — 코드 규모 지표. LLM 없이 무료."""
    files = _source_files(workdir)
    stats = []
    total = 0
    for p in files:
        n = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
        total += n
        stats.append({"file": str(p.relative_to(workdir)), "lines": n})
    stats.sort(key=lambda s: -s["lines"])
    deps = 0
    req = Path(workdir) / "requirements.txt"
    if req.exists():
        deps = len([l for l in req.read_text(encoding="utf-8").splitlines()
                    if l.strip() and not l.startswith("#")])
    return {"files": len(files), "total_lines": total,
            "dependencies": deps, "largest": stats[:5]}


@app.post("/runs/{run_id}/resume")
async def resume_run(run_id: str, body: ResumeRequest):
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "run 없음")
    if run.resume_future is None or run.resume_future.done():
        raise HTTPException(409, "대기 중인 interrupt가 없음")
    run.resume_future.set_result(body.value)
    return {"ok": True}


@app.get("/runs/{run_id}/events")
async def stream_events(run_id: str):
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "run 없음")

    async def gen():
        import json
        queue: asyncio.Queue = asyncio.Queue()
        run.subscribers.append(queue)
        try:
            # 히스토리 재생 후 실시간 전환. 재접속해도 상태 복원 가능.
            for event in list(run.events):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            while not (run.finished and queue.empty()):
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            run.subscribers.remove(queue)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
