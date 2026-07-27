from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, field_validator

from ..schemas import WorkspaceState
from ..context.current_file_search import format_workspace_tool_result, search_workspace


SEARCH_TOOL_NAME = "search_workspace"
MAX_TOOL_RESULT_CHARS = 9000

SEARCH_TOOL_INSTRUCTIONS = """
Available tool: search_workspace.
Use it only when the provided context is not enough to complete your current role confidently.
After a tool result is provided, continue with your final response for your current role unless one more search is necessary.
""".strip()


class SearchWorkspaceArgs(BaseModel):
    query: str = Field(default="", description="Natural language or identifier query for relevant code snippets.")
    file_path: str | None = Field(default=None, description="Optional exact workspace file path to search within.")
    symbol: str | None = Field(default=None, description="Optional Python function or class name to prioritize.")
    symbols: list[str] | None = Field(default=None, description="Optional Python function or class names to prioritize.")
    max_chars: int = Field(default=MAX_TOOL_RESULT_CHARS, ge=1000, le=MAX_TOOL_RESULT_CHARS)
    max_files: int = Field(default=8, ge=1, le=20)

    @field_validator("symbols", mode="before")
    @classmethod
    def parse_symbols(cls, value: Any) -> list[str] | None:
        if value is None or isinstance(value, list):
            return value
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
            return re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text)
        return [str(value)]


@dataclass(frozen=True)
class AgentToolCall:
    tool: str
    arguments: dict[str, Any]


TOOL_CALL_BLOCK_RE = re.compile(r"```(?:tool_call|json)\s*\n([\s\S]*?)```", re.IGNORECASE)


def build_search_workspace_tool(workspace: WorkspaceState | None) -> StructuredTool:
    def _search_workspace(
        query: str = "",
        file_path: str | None = None,
        symbol: str | None = None,
        symbols: list[str] | None = None,
        max_chars: int = MAX_TOOL_RESULT_CHARS,
        max_files: int = 8,
    ) -> str:
        return execute_search_workspace(
            workspace,
            query=query,
            file_path=file_path,
            symbol=symbol,
            symbols=symbols,
            max_chars=max_chars,
            max_files=max_files,
        )

    return StructuredTool.from_function(
        name=SEARCH_TOOL_NAME,
        description=(
            "Search the current workspace files for relevant code snippets. "
            "Use this to inspect functions, classes, usages, or keyword matches before planning code changes."
        ),
        func=_search_workspace,
        args_schema=SearchWorkspaceArgs,
    )


def execute_search_workspace(
    workspace: WorkspaceState | None,
    query: str = "",
    file_path: str | None = None,
    symbol: str | None = None,
    symbols: list[str] | str | None = None,
    max_chars: int = MAX_TOOL_RESULT_CHARS,
    max_files: int = 8,
) -> str:
    if workspace is None or not workspace.files:
        return "Tool: search_workspace\nStatus: no workspace files available."

    preferred_symbols = _normalise_symbols(symbol or "", symbols)
    query = (query or "").strip()
    file_path = (file_path or "").strip() or None
    max_chars = _bounded_int(max_chars, default=MAX_TOOL_RESULT_CHARS, low=1000, high=MAX_TOOL_RESULT_CHARS)
    max_files = _bounded_int(max_files, default=8, low=1, high=20)

    if not query and preferred_symbols:
        query = " ".join(preferred_symbols)
    if not query:
        query = "relevant code"

    results = search_workspace(
        workspace,
        query,
        preferred_symbols=preferred_symbols,
        file_path=file_path,
        max_chars=max_chars,
        max_files=max_files,
        include_fallback=bool(file_path),
    )
    return format_workspace_tool_result(results)[:MAX_TOOL_RESULT_CHARS]


def parse_tool_call(content: str) -> AgentToolCall | None:
    text = (content or "").strip()
    if not text:
        return None

    candidates = [match.group(1).strip() for match in TOOL_CALL_BLOCK_RE.finditer(text)]
    if text.startswith("{") and text.endswith("}"):
        candidates.append(text)

    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        tool = str(payload.get("tool") or payload.get("name") or "")
        if tool != SEARCH_TOOL_NAME:
            continue
        arguments = payload.get("arguments") or payload.get("args") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        return AgentToolCall(tool=tool, arguments=arguments)
    return None


def execute_tool_call(call: AgentToolCall, workspace: WorkspaceState | None) -> str:
    if call.tool != SEARCH_TOOL_NAME:
        return f"Tool error: unknown tool {call.tool!r}."
    args = call.arguments
    return execute_search_workspace(
        workspace,
        query=str(args.get("query") or args.get("pattern") or ""),
        file_path=str(args.get("file_path") or args.get("path") or "") or None,
        symbol=str(args.get("symbol") or "") or None,
        symbols=args.get("symbols"),
        max_chars=args.get("max_chars", MAX_TOOL_RESULT_CHARS),
        max_files=args.get("max_files", 8),
    )


def _normalise_symbols(symbol: str, symbols: Any) -> list[str]:
    result: list[str] = []
    raw_values: list[Any] = []
    if symbol:
        raw_values.append(symbol)
    if isinstance(symbols, list):
        raw_values.extend(symbols)
    elif isinstance(symbols, str):
        text = symbols.strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            raw_values.extend(parsed)
        else:
            raw_values.append(symbols)

    for raw in raw_values:
        for name in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(raw or "")):
            if name not in result:
                result.append(name)
    return result


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))
