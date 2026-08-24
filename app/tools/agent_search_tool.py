from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
from typing import Any, Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, field_validator

from ..schemas import WorkspaceState
from ..context.file_search import format_workspace_tool_result, infer_search_root_path, search_workspace


SEARCH_TOOL_NAME = "search_workspace"
MAX_TOOL_RESULT_CHARS = 9000
SEARCH_LOG_COLOR = "\033[1;97;45m"
SEARCH_LOG_RESET = "\033[0m"

logger = logging.getLogger(__name__)

SEARCH_TOOL_INSTRUCTIONS = """
Available tool: search_workspace.
Use it only when the provided context is not enough to complete your current role confidently.
For questions about how a function, method, or class is defined or implemented, first call search_workspace with mode="survey" unless you already know the exact file.
After survey mode returns likely targets and suggested next searches, call search_workspace once more with mode="inspect" for the most relevant file and symbol.
After an inspect result is provided, continue with your final response for your current role unless one more search is necessary.
""".strip()


class SearchWorkspaceArgs(BaseModel):
    mode: Literal["auto", "survey", "inspect"] = Field(
        default="auto",
        description="survey returns a compact workspace map; inspect returns exact snippets or full definitions.",
    )
    query: str = Field(default="", description="Natural language or identifier query for relevant code snippets.")
    file_path: str | None = Field(default=None, description="Optional exact workspace file path to search within.")
    symbol: str | None = Field(default=None, description="Optional primary Python function, method, or class name to prioritize.")
    symbols: list[str] | None = Field(default=None, description="Optional Python function or class names to prioritize.")
    qualified_symbol: str | None = Field(default=None, description="Optional qualified target such as Customer.check_id.")
    owner: str | None = Field(default=None, description="Optional owner class/module for a member symbol, such as Customer.")
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
        mode: str = "auto",
        query: str = "",
        file_path: str | None = None,
        symbol: str | None = None,
        symbols: list[str] | None = None,
        qualified_symbol: str | None = None,
        owner: str | None = None,
        max_chars: int = MAX_TOOL_RESULT_CHARS,
        max_files: int = 8,
    ) -> str:
        return execute_search_workspace(
            workspace,
            mode=mode,
            query=query,
            file_path=file_path,
            symbol=symbol,
            symbols=symbols,
            qualified_symbol=qualified_symbol,
            owner=owner,
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
    mode: str = "auto",
    query: str = "",
    file_path: str | None = None,
    symbol: str | None = None,
    symbols: list[str] | str | None = None,
    qualified_symbol: str | None = None,
    owner: str | None = None,
    max_chars: int = MAX_TOOL_RESULT_CHARS,
    max_files: int = 8,
) -> str:
    mode = _normalise_mode(mode)
    qualified_symbol = (qualified_symbol or "").strip() or None
    owner = (owner or "").strip() or None
    inferred_owner, inferred_symbol = _split_qualified_symbol(qualified_symbol or query)
    owner = owner or inferred_owner
    symbol = symbol or inferred_symbol
    preferred_symbols = _normalise_symbols(symbol or "", symbols)
    raw_query = (query or qualified_symbol or "").strip()
    query = _normalise_query_for_search(raw_query, owner=owner, symbol=symbol)
    file_path = (file_path or "").strip() or None
    max_chars = _bounded_int(max_chars, default=MAX_TOOL_RESULT_CHARS, low=1000, high=MAX_TOOL_RESULT_CHARS)
    max_files = _bounded_int(max_files, default=8, low=1, high=20)

    if not query and preferred_symbols:
        query = " ".join(preferred_symbols)
    if not query:
        query = "relevant code"

    search_root = infer_search_root_path(workspace, file_path)
    results = search_workspace(
        workspace,
        query,
        preferred_symbols=preferred_symbols,
        file_path=file_path,
        max_chars=max_chars,
        max_files=max_files,
        include_fallback=bool(file_path),
        mode=mode,
        qualified_symbol=qualified_symbol,
        owner=owner,
    )
    result_root = getattr(results, "root_path", "")
    if not result_root and isinstance(results, list) and results and results[0].root_path:
        result_root = results[0].root_path
    search_root = result_root or search_root
    final_result = format_workspace_tool_result(results, search_root=search_root)[:MAX_TOOL_RESULT_CHARS]
    _log_search_result(
        final_result,
        query=query,
        file_path=file_path,
        preferred_symbols=preferred_symbols,
        max_chars=max_chars,
        max_files=max_files,
        search_root=search_root,
        mode=mode,
        qualified_symbol=qualified_symbol,
        owner=owner,
    )
    return final_result


def _log_search_result(
    result: str,
    query: str,
    file_path: str | None,
    preferred_symbols: list[str],
    max_chars: int,
    max_files: int,
    search_root: str,
    mode: str,
    qualified_symbol: str | None,
    owner: str | None,
) -> None:
    header = (
        " SEARCH_WORKSPACE RESULT "
        f"mode={mode!r} root={search_root!r} query={query!r} file_path={file_path!r} "
        f"qualified={qualified_symbol!r} owner={owner!r} "
        f"symbols={preferred_symbols!r} max_chars={max_chars} max_files={max_files} "
    )
    separator = "=" * max(80, len(header))
    logger.info(
        "\n%s%s\n%s\n%s\n%s%s",
        SEARCH_LOG_COLOR,
        separator,
        header,
        separator,
        result,
        SEARCH_LOG_RESET,
    )


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
        mode=str(args.get("mode") or "auto"),
        query=str(args.get("query") or args.get("pattern") or ""),
        file_path=str(args.get("file_path") or args.get("path") or "") or None,
        symbol=str(args.get("symbol") or "") or None,
        symbols=args.get("symbols"),
        qualified_symbol=str(args.get("qualified_symbol") or args.get("qualified") or "") or None,
        owner=str(args.get("owner") or "") or None,
        max_chars=args.get("max_chars", MAX_TOOL_RESULT_CHARS),
        max_files=args.get("max_files", 8),
    )



def _normalise_mode(value: str) -> str:
    mode = (value or "auto").strip().lower()
    return mode if mode in {"auto", "survey", "inspect"} else "auto"


def _split_qualified_symbol(value: str) -> tuple[str | None, str | None]:
    match = re.search(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b", value or "")
    if not match:
        return None, None
    return match.group(1), match.group(2)


def _normalise_query_for_search(query: str, owner: str | None, symbol: str | None) -> str:
    if owner and symbol and re.search(rf"\b{re.escape(owner)}\.{re.escape(symbol)}\b", query or ""):
        return " ".join([symbol, owner])
    return query

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
