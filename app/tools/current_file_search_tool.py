from __future__ import annotations

import ast
from dataclasses import dataclass
import re

from ..schemas import CodeFile, WorkspaceState


IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DEF_OR_CLASS_RE = re.compile(r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)\b", re.MULTILINE)


@dataclass(frozen=True)
class CurrentFileSearchHit:
    kind: str
    name: str
    start_line: int
    end_line: int
    code: str


@dataclass(frozen=True)
class CurrentFileSearchResult:
    file_path: str
    language: str
    query: str
    target_name: str
    defined_names: list[str]
    hits: list[CurrentFileSearchHit]
    truncated: bool = False


def search_current_file(
    workspace: WorkspaceState | None,
    user_request: str,
    preferred_symbols: list[str] | None = None,
    max_chars: int = 4500,
) -> CurrentFileSearchResult | None:
    active_file = _active_file(workspace)
    if active_file is None:
        return None

    source = active_file.content or ""
    defined_names = _definition_names(source)
    target_names = _merge_target_names(preferred_symbols, _guess_target_names(user_request, defined_names), defined_names)
    target_name = target_names[0] if target_names else ""
    hits: list[CurrentFileSearchHit] = []

    if active_file.language.lower() in {"python", "py"}:
        for name in target_names:
            definition = _python_block_by_name(source, name)
            if definition is not None:
                hits.append(definition)
        for name in target_names:
            hits.extend(_python_usage_hits(source, name))
        if not hits:
            hits.extend(_keyword_hits(source, user_request))
        if not hits:
            hits.extend(_python_import_and_head_hits(source))
    else:
        hits.extend(_keyword_hits(source, user_request))
        if not hits:
            hits.extend(_head_hits(source))

    kept: list[CurrentFileSearchHit] = []
    used_chars = 0
    truncated = False
    for hit in hits:
        cost = len(hit.code) + 120
        if kept and used_chars + cost > max_chars:
            truncated = True
            break
        kept.append(hit)
        used_chars += cost
    return CurrentFileSearchResult(
        file_path=active_file.path,
        language=active_file.language,
        query=user_request,
        target_name=target_name,
        defined_names=defined_names,
        hits=kept,
        truncated=truncated,
    )


def format_tool_result(result: CurrentFileSearchResult | None) -> str:
    if result is None:
        return "Tool: search_current_file\nStatus: no active file."
    lines = [
        "Tool: search_current_file",
        "Scope: current active file only",
        f"Active file: {result.file_path}",
        f"Language: {result.language}",
        f"User request: {result.query}",
        f"Target guess: {result.target_name or 'none'}",
        f"Defined names: {', '.join(result.defined_names[:30]) or 'none'}",
        "Results:",
    ]
    if not result.hits:
        lines.append("No relevant snippets found in the active file.")
    for hit in result.hits:
        title = f"[{hit.kind}: {hit.name or 'snippet'} lines {hit.start_line}-{hit.end_line}]"
        lines.append(title)
        lines.append(f"```{result.language}")
        lines.append(hit.code.rstrip())
        lines.append("```")
    if result.truncated:
        lines.append("# ... search results truncated ...")
    return "\n".join(lines)


def _active_file(workspace: WorkspaceState | None) -> CodeFile | None:
    if workspace is None or not workspace.files:
        return None
    return next((file for file in workspace.files if file.path == workspace.active_file), workspace.files[0])


def _merge_target_names(
    preferred_symbols: list[str] | None,
    guessed_names: list[str],
    defined_names: list[str],
) -> list[str]:
    defined_set = set(defined_names)
    result: list[str] = []
    for raw_name in preferred_symbols or []:
        for name in _split_symbol_names(raw_name):
            if name in defined_set and name not in result:
                result.append(name)
    for name in guessed_names:
        if name in defined_set and name not in result:
            result.append(name)
    return result


def _split_symbol_names(value: str) -> list[str]:
    return IDENTIFIER_RE.findall(value or "")

def _guess_target_names(user_request: str, defined_names: list[str]) -> list[str]:
    request = user_request or ""
    defined_set = set(defined_names)
    guessed: list[str] = []
    name = r"([A-Za-z_][A-Za-z0-9_]*)"
    explicit_patterns = [
        rf"[`'\"]{name}[`'\"]",
        rf"(?:类|class|函数|方法|function|func|def)\s*{name}",
        rf"{name}\s*(?:类|函数|方法)",
        rf"(?:实现|新增|创建|补全|修改|修复|重构|定义)\s*{name}",
    ]
    for pattern in explicit_patterns:
        for match in re.finditer(pattern, request, re.IGNORECASE):
            found = match.group(1)
            if found in defined_set and found not in guessed:
                guessed.append(found)

    for token in IDENTIFIER_RE.findall(request):
        if token in defined_set and token not in guessed:
            guessed.append(token)
    return guessed


def _definition_names(source: str) -> list[str]:
    return list(dict.fromkeys(DEF_OR_CLASS_RE.findall(source or "")))


def _python_block_by_name(source: str, name: str) -> CurrentFileSearchHit | None:
    if not name:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _indent_block_by_name(source, name)
    lines = source.splitlines(keepends=True)
    matches: list[CurrentFileSearchHit] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            start = getattr(node, "lineno", 0)
            end = getattr(node, "end_lineno", 0)
            if start and end:
                matches.append(CurrentFileSearchHit("definition", name, start, end, "".join(lines[start - 1:end])))
    return matches[0] if len(matches) == 1 else None


def _indent_block_by_name(source: str, name: str) -> CurrentFileSearchHit | None:
    lines = source.splitlines(keepends=True)
    start_index = -1
    start_indent = 0
    pattern = re.compile(rf"^(\s*)(?:async\s+def|def|class)\s+{re.escape(name)}\b")
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if match:
            start_index = index
            start_indent = len(match.group(1))
            break
    if start_index < 0:
        return None
    end_index = len(lines)
    for index in range(start_index + 1, len(lines)):
        line = lines[index]
        if line.strip() and len(line) - len(line.lstrip()) <= start_indent:
            end_index = index
            break
    return CurrentFileSearchHit(
        "definition",
        name,
        start_index + 1,
        end_index,
        "".join(lines[start_index:end_index]),
    )


def _python_usage_hits(source: str, name: str) -> list[CurrentFileSearchHit]:
    lines = source.splitlines()
    hits = []
    pattern = re.compile(rf"\b{re.escape(name)}\s*\(")
    def_pattern = re.compile(rf"^\s*(?:async\s+def|def)\s+{re.escape(name)}\s*\(")
    for index, line in enumerate(lines):
        if def_pattern.search(line):
            continue
        if pattern.search(line) or f".{name}(" in line:
            hits.append(index)
    return _window_hits(lines, hits[:8], "usage", name, radius=8)


def _keyword_hits(source: str, user_request: str) -> list[CurrentFileSearchHit]:
    terms = [term.lower() for term in IDENTIFIER_RE.findall(user_request or "") if len(term) >= 3]
    if not terms:
        return []
    lines = source.splitlines()
    hits = []
    for index, line in enumerate(lines):
        lowered = line.lower()
        if any(term in lowered for term in terms):
            hits.append(index)
    return _window_hits(lines, hits[:8], "keyword", ",".join(terms[:3]), radius=6)


def _python_import_and_head_hits(source: str) -> list[CurrentFileSearchHit]:
    lines = source.splitlines()
    end = 0
    for index, line in enumerate(lines[:80]):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("import ") or stripped.startswith("from "):
            end = index + 1
            continue
        break
    if end > 0:
        return [CurrentFileSearchHit("imports", "top", 1, end, "\n".join(lines[:end]))]
    return _head_hits(source)


def _head_hits(source: str) -> list[CurrentFileSearchHit]:
    lines = source.splitlines()
    if not lines:
        return []
    end = min(len(lines), 80)
    return [CurrentFileSearchHit("head", "top", 1, end, "\n".join(lines[:end]))]


def _window_hits(lines: list[str], hit_indexes: list[int], kind: str, name: str, radius: int) -> list[CurrentFileSearchHit]:
    ranges: list[tuple[int, int]] = []
    for hit in hit_indexes:
        ranges.append((max(0, hit - radius), min(len(lines), hit + radius + 1)))
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return [
        CurrentFileSearchHit(kind, name, start + 1, end, "\n".join(lines[start:end]))
        for start, end in merged
    ]