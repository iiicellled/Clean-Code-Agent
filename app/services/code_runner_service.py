from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from ..config import settings
from ..schemas import CodeRunRequest, CodeRunResponse


OUTPUT_LIMIT = 12000
CONTAINER_WORKDIR = "/workspace"
CONTAINER_SCRIPT_PATH = f"{CONTAINER_WORKDIR}/main.py"


def run_python_code(request: CodeRunRequest) -> CodeRunResponse:
    code = request.code.strip()
    call_code = request.call_code.strip()
    script_body = code
    if call_code:
        script_body = f"{code}\n\n# User-provided run snippet.\n{_to_repl_style_snippet(call_code)}\n"

    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="coder-agent-run-") as tmpdir:
        script_path = Path(tmpdir) / "main.py"
        script_path.write_text(script_body, encoding="utf-8")

        if settings.code_runner_backend == "local":
            return _run_local_python(request, script_path, tmpdir, started)
        if settings.code_runner_backend != "docker":
            duration_ms = int((time.perf_counter() - started) * 1000)
            return CodeRunResponse(
                stdout="",
                stderr=f"Unsupported code runner backend: {settings.code_runner_backend}",
                exit_code=None,
                timeout=False,
                duration_ms=duration_ms,
            )
        return _run_docker_python(request, tmpdir, started)


def _run_docker_python(request: CodeRunRequest, tmpdir: str, started: float) -> CodeRunResponse:
    docker_executable = shutil.which("docker")
    if docker_executable is None:
        duration_ms = int((time.perf_counter() - started) * 1000)
        return CodeRunResponse(
            stdout="",
            stderr="Docker executable was not found. Install Docker or set CODE_RUNNER_BACKEND=local.",
            exit_code=None,
            timeout=False,
            duration_ms=duration_ms,
        )

    container_name = f"coder-agent-run-{uuid.uuid4().hex[:12]}"
    command = [
        docker_executable,
        "run",
        "--rm",
        "--name",
        container_name,
        "--network",
        settings.code_runner_docker_network,
        "--memory",
        settings.code_runner_docker_memory,
        "--cpus",
        settings.code_runner_docker_cpus,
        "--pids-limit",
        str(settings.code_runner_docker_pids_limit),
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=16m",
        "-e",
        "PYTHONIOENCODING=utf-8",
        "-e",
        "PYTHONUTF8=1",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-v",
        f"{Path(tmpdir).resolve()}:{CONTAINER_WORKDIR}:ro",
        "-w",
        CONTAINER_WORKDIR,
        settings.code_runner_docker_image,
        "python",
        "-I",
        "-X",
        "utf8",
        CONTAINER_SCRIPT_PATH,
    ]

    try:
        result = subprocess.run(
            command,
            input=request.stdin,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=request.timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        _force_remove_container(docker_executable, container_name)
        duration_ms = int((time.perf_counter() - started) * 1000)
        return CodeRunResponse(
            stdout=_clip_output(_decode_timeout_output(exc.stdout)),
            stderr=_clip_output(_decode_timeout_output(exc.stderr) or "Execution timed out."),
            exit_code=None,
            timeout=True,
            duration_ms=duration_ms,
        )
    except OSError as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        return CodeRunResponse(
            stdout="",
            stderr=f"Failed to start Docker code runner: {exc}",
            exit_code=None,
            timeout=False,
            duration_ms=duration_ms,
        )

    duration_ms = int((time.perf_counter() - started) * 1000)
    return CodeRunResponse(
        stdout=_clip_output(result.stdout),
        stderr=_clip_output(result.stderr),
        exit_code=result.returncode,
        timeout=False,
        duration_ms=duration_ms,
    )


def _run_local_python(request: CodeRunRequest, script_path: Path, tmpdir: str, started: float) -> CodeRunResponse:
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-X", "utf8", str(script_path)],
            input=request.stdin,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=request.timeout_seconds,
            cwd=tmpdir,
            env=_safe_env(),
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        return CodeRunResponse(
            stdout=_clip_output(_decode_timeout_output(exc.stdout)),
            stderr=_clip_output(_decode_timeout_output(exc.stderr) or "Execution timed out."),
            exit_code=None,
            timeout=True,
            duration_ms=duration_ms,
        )

    duration_ms = int((time.perf_counter() - started) * 1000)
    return CodeRunResponse(
        stdout=_clip_output(result.stdout),
        stderr=_clip_output(result.stderr),
        exit_code=result.returncode,
        timeout=False,
        duration_ms=duration_ms,
    )


def _force_remove_container(docker_executable: str, container_name: str) -> None:
    try:
        subprocess.run(
            [docker_executable, "rm", "-f", container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except OSError:
        pass
    except subprocess.TimeoutExpired:
        pass


def _to_repl_style_snippet(snippet: str) -> str:
    """Print the value of a trailing expression, similar to Python's REPL."""
    try:
        module = ast.parse(snippet)
    except SyntaxError:
        return snippet

    if not module.body or not isinstance(module.body[-1], ast.Expr):
        return snippet

    expression = module.body[-1].value
    module.body[-1] = ast.Expr(
        value=ast.Call(
            func=ast.Name(id="print", ctx=ast.Load()),
            args=[
                ast.Call(
                    func=ast.Name(id="repr", ctx=ast.Load()),
                    args=[expression],
                    keywords=[],
                )
            ],
            keywords=[],
        )
    )
    ast.fix_missing_locations(module)
    return ast.unparse(module)


def _safe_env() -> dict[str, str]:
    allowed_keys = ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP")
    env = {key: value for key, value in os.environ.items() if key in allowed_keys}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _decode_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _clip_output(value: str | None) -> str:
    if value is None:
        return ""
    if len(value) <= OUTPUT_LIMIT:
        return value
    omitted = len(value) - OUTPUT_LIMIT
    return value[:OUTPUT_LIMIT] + f"\n... output truncated, omitted {omitted} characters ...\n"
