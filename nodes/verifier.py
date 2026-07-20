"""검증 노드.

LLM이 아닌 결정적 검증. 2단계로 나눈다:
  1단계: requirements.txt가 있으면 네트워크 허용 컨테이너에서 .deps에 의존성 설치.
         pip 캐시 볼륨으로 반복 실행 시 재다운로드를 피한다.
  2단계: --network none 컨테이너에서 컴파일 체크 + pytest. 생성 코드는 오프라인 격리.

실패 시 로그에 등장하는 파일 경로로 원인 태스크를 특정해 해당 태스크에만
last_error를 붙인다. 특정 불가하면 전체에 붙인다(기존 동작으로 폴백).
"""

import hashlib
import subprocess
from pathlib import Path

from config import SANDBOX
from state import OrchestraState, TaskResult

_DOCKERFILE_DIR = Path(__file__).resolve().parent.parent / "sandbox"


class SandboxError(Exception):
    """샌드박스 실행 자체가 실패한 경우 (Docker 미설치 등)."""


def ensure_image() -> None:
    """샌드박스 이미지가 없으면 sandbox/Dockerfile로 빌드한다."""
    try:
        inspect = subprocess.run(
            ["docker", "image", "inspect", SANDBOX.image], capture_output=True
        )
    except FileNotFoundError as exc:
        raise SandboxError("docker 실행 파일을 찾을 수 없다.") from exc
    if inspect.returncode == 0:
        return
    build = subprocess.run(
        ["docker", "build", "-t", SANDBOX.image, str(_DOCKERFILE_DIR)],
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        raise SandboxError(
            f"샌드박스 이미지 빌드 실패:\n{build.stdout}{build.stderr}"
        )


def run_in_sandbox(
    workdir: str,
    command: list[str],
    *,
    network: bool = False,
    pip_cache: bool = False,
    timeout: int | None = None,
) -> tuple[int, str]:
    """작업 디렉토리를 마운트한 일회성 컨테이너에서 명령을 실행한다.

    Args:
        workdir: 호스트의 프로젝트 디렉토리 절대경로.
        command: 컨테이너 안에서 실행할 명령.
        network: True면 네트워크 허용 (의존성 설치 단계 전용).
        pip_cache: True면 pip 캐시 볼륨을 마운트.
        timeout: 초 단위 제한. None이면 SANDBOX.timeout_seconds.

    Returns:
        (returncode, stdout+stderr 결합 로그).

    Raises:
        SandboxError: docker 실행 자체가 불가능한 경우.
    """
    docker_cmd = ["docker", "run", "--rm"]
    if not network:
        docker_cmd += ["--network", "none"]
    if pip_cache:
        docker_cmd += ["-v", f"{SANDBOX.pip_cache_volume}:/root/.cache/pip"]
    docker_cmd += [
        "-v", f"{workdir}:{SANDBOX.workdir_mount}",
        "-w", SANDBOX.workdir_mount,
        SANDBOX.image,
        *command,
    ]
    limit = timeout or SANDBOX.timeout_seconds
    try:
        proc = subprocess.run(
            docker_cmd, capture_output=True, text=True, timeout=limit
        )
    except FileNotFoundError as exc:
        raise SandboxError("docker 실행 파일을 찾을 수 없다.") from exc
    except subprocess.TimeoutExpired:
        return 124, f"실행 시간 초과 ({limit}초)"
    return proc.returncode, proc.stdout + proc.stderr


def _requirements_hash(req_file: Path) -> str:
    return hashlib.sha256(req_file.read_bytes()).hexdigest()


def _deps_current(workdir: Path, req_file: Path) -> bool:
    """requirements.txt가 마지막 설치 이후 안 바뀌었으면 True."""
    marker = workdir / SANDBOX.deps_dir / ".installed"
    return marker.exists() and marker.read_text() == _requirements_hash(req_file)


def _install_deps(workdir: Path, req_file: Path) -> tuple[int, str]:
    """1단계: 네트워크 허용 컨테이너에서 .deps에 의존성을 설치한다."""
    code, log = run_in_sandbox(
        str(workdir),
        ["pip", "install", "-q", "-r", "requirements.txt", "-t", SANDBOX.deps_dir],
        network=True,
        pip_cache=True,
        timeout=SANDBOX.install_timeout_seconds,
    )
    if code == 0:
        (workdir / SANDBOX.deps_dir / ".installed").write_text(
            _requirements_hash(req_file)
        )
    return code, log


def implicated_task_ids(
    results: list[TaskResult], log: str, workdir: str
) -> set[str]:
    """실패 로그에 파일 경로가 등장하는 태스크를 특정한다.

    컨테이너 로그의 경로는 posix 상대경로(예: app/main.py)로 나타나므로
    각 결과 파일의 workdir 기준 상대경로로 매칭한다. 아무것도 못 찾으면
    전체를 반환한다(폴백: 전부 재작업).
    """
    hits = set()
    base = Path(workdir).resolve()
    for r in results:
        rel = Path(r["file_path"]).resolve().relative_to(base).as_posix()
        if rel in log:
            hits.add(r["task_id"])
    return hits or {r["task_id"] for r in results}


def _mark_failed(
    results: list[TaskResult], log: str, workdir: str
) -> list[TaskResult]:
    """실패 원인 태스크에만 last_error를 붙인다. 나머지는 미검증 상태 유지."""
    ids = implicated_task_ids(results, log, workdir)
    return [
        {**r, "verified": False, "last_error": log if r["task_id"] in ids else None}
        for r in results
    ]


def verify_node(state: OrchestraState) -> dict:
    """의존성 설치(필요 시) 후 오프라인 컨테이너에서 컴파일 체크 + pytest."""
    ensure_image()
    workdir = Path(state["workdir"])
    req_file = workdir / "requirements.txt"

    if req_file.exists() and not _deps_current(workdir, req_file):
        code, log = _install_deps(workdir, req_file)
        if code != 0:
            return {"results": _mark_failed(state["results"], log, state["workdir"])}

    test_cmd = (
        f"export PYTHONPATH={SANDBOX.workdir_mount}/{SANDBOX.deps_dir} && "
        f"python -m compileall -q . -x '{SANDBOX.deps_dir}' && "
        f"python -m pytest -x -q --ignore={SANDBOX.deps_dir} 2>&1"
    )
    code, log = run_in_sandbox(state["workdir"], ["sh", "-c", test_cmd])
    if code == 0:
        updated = [{**r, "verified": True, "last_error": None} for r in state["results"]]
    else:
        updated = _mark_failed(state["results"], log, state["workdir"])
    return {"results": updated}


if __name__ == "__main__":
    # 실패 특정 로직 자가 점검
    _results = [
        {"task_id": "t1", "file_path": r"C:\out\app\main.py", "code": "",
         "retry_count": 0, "verified": False, "last_error": None},
        {"task_id": "t2", "file_path": r"C:\out\test_main.py", "code": "",
         "retry_count": 0, "verified": False, "last_error": None},
    ]
    assert implicated_task_ids(_results, 'File "app/main.py", line 3', r"C:\out") == {"t1"}
    assert implicated_task_ids(
        _results, "test_main.py::test_x FAILED\napp/main.py:3 error", r"C:\out"
    ) == {"t1", "t2"}
    # 경로 매칭 실패 시 전체 폴백
    assert implicated_task_ids(_results, "collection error", r"C:\out") == {"t1", "t2"}
    print("ok")
