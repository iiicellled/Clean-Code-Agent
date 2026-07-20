from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ..schemas import CodeRunRequest, CodeRunResponse


OUTPUT_LIMIT = 12000


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