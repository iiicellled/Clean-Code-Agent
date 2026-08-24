from __future__ import annotations

import ast
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Iterable

from ..config import settings
from ..schemas import WorkspaceState


IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PYTHON_EXTENSIONS = {".py", ".pyw"}
TEXT_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".h", ".hpp",
    ".html", ".java", ".js", ".jsx", ".json", ".md", ".mjs",
    ".py", ".pyw", ".rs", ".sh", ".sql", ".ts", ".tsx", ".txt",
    ".vue", ".yaml", ".yml",
}
EXCLUDED_DIRS = {
    ".git", ".hg", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    ".venv", "__pycache__", "build", "dist", "node_modules", "venv",
}
DEFINITION_RE = re.compile(r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)\b")
ASSIGNMENT_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=")


@dataclass(frozen=True)
class FileSearchHit:
    kind: str
    name: str
    start_line: int
    end_line: int
    code: str
    match_line: int
    match_text: str


@dataclass(frozen=True)
class FileSearchResult:
    file_path: str
    language: str
    query: str
    target_name: str
    hits: list[FileSearchHit]
    truncated: bool = False
    root_path: str = ""


@dataclass(frozen=True)
class SearchSurveyItem:
    kind: str
    file_path: str
    language: str
    line_number: int
    name: str
    match_text: str
    reason: str


@dataclass(frozen=True)
class SearchSurveyResult:
    query: str
    target_name: str
    owner: str
    root_path: str
    items: list[SearchSurveyItem]
    suggestions: list[dict[str, str]]
    truncated: bool = False


@dataclass(frozen=True)
class _LineMatch:
    path: Path
    line_number: int
    text: str
    keyword: str
    kind: str
    name: str


def search_workspace(
    workspace: WorkspaceState | None,
    user_request: str,
    preferred_symbols: list[str] | None = None,
    file_path: str | None = None,
    max_chars: int = 8000,
    max_files: int = 8,
    include_fallback: bool = False,
    mode: str = "auto",
    qualified_symbol: str | None = None,
    owner: str | None = None,
) -> list[FileSearchResult] | SearchSurveyResult:
    root = _infer_search_root(workspace, file_path)
    if root is None:
        return []

    raw_query = (user_request or qualified_symbol or "").strip()
    owner, qualified_target = _qualified_parts(qualified_symbol or raw_query, owner)
    query = _normalise_query_for_search(raw_query, owner=owner, symbol=qualified_target)
    preferred = list(preferred_symbols or [])
    if qualified_target and qualified_target not in preferred:
        preferred.insert(0, qualified_target)
    keywords = _search_keywords(query, preferred)
    target_name = qualified_target or (keywords[0] if keywords else "")
    scoped_path = _resolve_scoped_path(root, file_path)
    if file_path and scoped_path is None:
        return []

    effective_mode = _effective_mode(mode, file_path)
    matches = _grep_matches(root, keywords, scoped_path=scoped_path, max_matches=max_files * 24)
    if include_fallback and not matches and scoped_path is not None:
        matches = _head_match(scoped_path, target_name)
    if effective_mode == "survey":
        return _survey_result(root, query, target_name, owner or "", matches, max_items=max_files * 3)
    if not matches:
        return []

    grouped: dict[Path, list[_LineMatch]] = {}
    for match in matches:
        grouped.setdefault(match.path, []).append(match)

    results: list[FileSearchResult] = []
    used_chars = 0
    truncated = False
    for path, file_matches in grouped.items():
        if len(results) >= max_files:
            truncated = True
            break
        result = _result_for_file(path, root, query, target_name, file_matches)
        cost = sum(len(hit.code) + 140 for hit in result.hits)
        if results and used_chars + cost > max_chars:
            truncated = True
            break
        results.append(result)
        used_chars += cost

    if truncated and results:
        last = results[-1]
        results[-1] = FileSearchResult(
            last.file_path,
            last.language,
            last.query,
            last.target_name,
            last.hits,
            True,
            last.root_path,
        )
    return results


def format_workspace_tool_result(results: list[FileSearchResult] | SearchSurveyResult, search_root: str = "") -> str:
    if isinstance(results, SearchSurveyResult):
        return _format_survey_tool_result(results, search_root=search_root)
    lines = ["Tool: search_workspace", "Scope: full workspace filesystem search"]
    if search_root:
        lines.append(f"Search root: {search_root}")
    lines.append("Results:")
    if not results:
        lines.append("No relevant snippets found in the workspace.")
        return "\n".join(lines)

    for result in results:
        lines.append(f"\nFile: {result.file_path}")
        lines.append(f"Language: {result.language}")
        if result.root_path:
            lines.append(f"Search root: {result.root_path}")
        lines.append(f"Target guess: {result.target_name or 'none'}")
        for hit in result.hits:
            lines.append(
                f"[{hit.kind}: {hit.name or 'snippet'} match line {hit.match_line}, "
                f"returned lines {hit.start_line}-{hit.end_line}]"
            )
            lines.append(f"Match: {hit.match_text.strip()}")
            lines.append(f"```{result.language}")
            lines.append(hit.code.rstrip())
            lines.append("```")
        if result.truncated:
            lines.append("# ... search results truncated ...")
    return "\n".join(lines)


def _format_survey_tool_result(result: SearchSurveyResult, search_root: str = "") -> str:
    lines = ["Tool: search_workspace", "Mode: survey", "Scope: full workspace filesystem search"]
    root = search_root or result.root_path
    if root:
        lines.append(f"Search root: {root}")
    lines.extend([
        f"Query: {result.query}",
        f"Primary target: {result.target_name or 'none'}",
        f"Owner scope: {result.owner or 'none'}",
        "Results:",
    ])
    if not result.items:
        lines.append("No likely targets found in the workspace map.")
    for index, item in enumerate(result.items, start=1):
        lines.append(
            f"{index}. [{item.kind}] {item.file_path}:{item.line_number} "
            f"{item.name or 'snippet'} - {item.reason}"
        )
        lines.append(f"   Match: {item.match_text.strip()}")
    if result.suggestions:
        lines.append("Suggested next searches:")
        for suggestion in result.suggestions[:5]:
            lines.append(
                "- "
                f"mode=inspect file_path={suggestion.get('file_path')!r} "
                f"symbol={suggestion.get('symbol')!r} "
                f"query={suggestion.get('query')!r} "
                f"qualified_symbol={suggestion.get('qualified_symbol')!r}"
            )
    if result.truncated:
        lines.append("# ... survey results truncated ...")
    return "\n".join(lines)




def _normalise_query_for_search(query: str, owner: str | None, symbol: str | None) -> str:
    if owner and symbol and re.search(rf"\b{re.escape(owner)}\.{re.escape(symbol)}\b", query or ""):
        return " ".join([symbol, owner])
    return query

def _effective_mode(mode: str, file_path: str | None) -> str:
    cleaned = (mode or "auto").strip().lower()
    if cleaned in {"survey", "inspect"}:
        return cleaned
    return "inspect" if file_path else "survey"


def _qualified_parts(value: str | None, owner: str | None) -> tuple[str | None, str | None]:
    match = re.search(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b", value or "")
    if match:
        return owner or match.group(1), match.group(2)
    return owner, None


def _survey_result(
    root: Path,
    query: str,
    target_name: str,
    owner: str,
    matches: list[_LineMatch],
    max_items: int,
) -> SearchSurveyResult:
    items: list[SearchSurveyItem] = []
    seen: set[tuple[str, int, str]] = set()
    for match in sorted(matches, key=lambda item: (_survey_priority(item, target_name, owner), str(item.path), item.line_number)):
        key = (str(match.path), match.line_number, match.kind)
        if key in seen:
            continue
        seen.add(key)
        try:
            display_path = str(match.path.resolve().relative_to(root.resolve()))
        except ValueError:
            display_path = str(match.path)
        items.append(
            SearchSurveyItem(
                kind=match.kind,
                file_path=display_path,
                language=_language_for_path(match.path),
                line_number=match.line_number,
                name=match.name,
                match_text=match.text,
                reason=_survey_reason(match, target_name, owner),
            )
        )
        if len(items) >= max_items:
            break
    return SearchSurveyResult(
        query=query,
        target_name=target_name,
        owner=owner,
        root_path=str(root.resolve()),
        items=items,
        suggestions=_survey_suggestions(items, query, target_name, owner),
        truncated=len(matches) > len(items),
    )


def _survey_priority(match: _LineMatch, target_name: str, owner: str) -> tuple[int, int]:
    exact_target = bool(target_name and match.name == target_name)
    owner_match = bool(owner and owner in match.text)
    if match.kind == "definition" and exact_target:
        return (0, 0 if owner_match else 1)
    if match.kind == "definition":
        return (1, 0 if owner_match else 1)
    if match.kind == "call" and exact_target:
        return (2, 0 if owner_match else 1)
    if exact_target:
        return (3, 0 if owner_match else 1)
    if owner_match:
        return (4, 0)
    return (5, 0)


def _survey_reason(match: _LineMatch, target_name: str, owner: str) -> str:
    if match.kind == "definition" and target_name and match.name == target_name:
        return "exact definition candidate"
    if match.kind == "call" and target_name and match.name == target_name:
        return "target call site"
    if owner and owner in match.text:
        return "owner-qualified context"
    return "keyword match"


def _survey_suggestions(
    items: list[SearchSurveyItem],
    query: str,
    target_name: str,
    owner: str,
) -> list[dict[str, str]]:
    suggestions: list[dict[str, str]] = []
    for item in items:
        if item.kind == "definition" and (not target_name or item.name == target_name):
            suggestions.append(_suggestion(item.file_path, target_name or item.name, query, owner))
    if not suggestions:
        for item in items:
            if target_name and item.name == target_name:
                suggestions.append(_suggestion(item.file_path, target_name, query, owner))
                break
    for item in items:
        if item.kind == "call" and target_name and item.name == target_name:
            suggestions.append(_suggestion(item.file_path, target_name, query, owner))
            break
    unique: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for suggestion in suggestions:
        key = (suggestion.get("file_path", ""), suggestion.get("symbol", ""))
        if key not in seen:
            seen.add(key)
            unique.append(suggestion)
    return unique


def _suggestion(file_path: str, symbol: str, query: str, owner: str) -> dict[str, str]:
    qualified = f"{owner}.{symbol}" if owner and symbol else ""
    return {
        "mode": "inspect",
        "file_path": file_path,
        "symbol": symbol,
        "query": query or symbol,
        "qualified_symbol": qualified,
    }



def _infer_search_root(workspace: WorkspaceState | None, file_path: str | None) -> Path | None:
    explicit_root = _explicit_workspace_root(workspace)
    if explicit_root is not None:
        return explicit_root

    candidates: list[Path] = []
    if file_path:
        candidates.append(Path(file_path))
    if workspace is not None:
        if workspace.active_file:
            candidates.append(Path(workspace.active_file))
        candidates.extend(Path(file.path) for file in workspace.files if file.path)

    absolute_paths = [path for path in candidates if path.is_absolute()]
    if absolute_paths:
        existing = [path if path.is_dir() else path.parent for path in absolute_paths if path.exists()]
        if existing:
            return _project_root(existing[0])

    env_root = _existing_directory(settings.workspace_root)
    if env_root is not None:
        subproject_root = _subproject_root_for_relative_file(env_root, file_path, workspace)
        return subproject_root or env_root

    cwd = Path.cwd()
    if (cwd / "app").exists() or (cwd / "pyproject.toml").exists() or (cwd / "README.md").exists():
        return cwd
    coder_agent_root = cwd / "coder_agent"
    if coder_agent_root.exists():
        return coder_agent_root
    return cwd if cwd.exists() else None


def infer_search_root_path(workspace: WorkspaceState | None, file_path: str | None = None) -> str:
    root = _infer_search_root(workspace, file_path)
    return str(root.resolve()) if root is not None else ""


def _subproject_root_for_relative_file(
    env_root: Path,
    file_path: str | None,
    workspace: WorkspaceState | None,
) -> Path | None:
    relative_paths: list[Path] = []
    raw_paths = [file_path]
    if workspace is not None:
        raw_paths.append(workspace.active_file)
        raw_paths.extend(file.path for file in workspace.files if file.path)

    for raw_path in raw_paths:
        if not raw_path:
            continue
        path = Path(raw_path)
        if not path.is_absolute() and path.parts:
            relative_paths.append(path)

    unique_relative_paths = list(dict.fromkeys(relative_paths))
    matches: list[Path] = []
    for child in env_root.iterdir():
        if not child.is_dir() or child.name in EXCLUDED_DIRS:
            continue
        if any((child / relative_path).is_file() for relative_path in unique_relative_paths):
            matches.append(child.resolve())
    return matches[0] if len(matches) == 1 else None


def _explicit_workspace_root(workspace: WorkspaceState | None) -> Path | None:
    raw_root = (getattr(workspace, "workspace_root", None) or "").strip() if workspace is not None else ""
    if not raw_root:
        return None
    root_path = Path(raw_root)
    if root_path.is_absolute():
        return _existing_directory(root_path)

    env_root = _existing_directory(settings.workspace_root)
    if env_root is not None:
        candidate = _existing_directory(env_root / root_path)
        if candidate is None:
            return None
        try:
            candidate.relative_to(env_root)
        except ValueError:
            return None
        return candidate

    return _existing_directory(root_path)


def _existing_directory(path: str | Path) -> Path | None:
    if not path:
        return None
    candidate = Path(path)
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def _project_root(path: Path) -> Path:
    path = path.resolve()
    current = path if path.is_dir() else path.parent
    for parent in [current, *current.parents]:
        if (parent / ".git").exists() or (parent / "pyproject.toml").exists() or (parent / "README.md").exists():
            return parent
    return current


def _resolve_scoped_path(root: Path, file_path: str | None) -> Path | None:
    if not file_path:
        return None
    path = Path(file_path)
    if not path.is_absolute():
        path = root / path
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if not resolved.exists():
        return None
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return None
    return resolved


def _search_keywords(query: str, preferred_symbols: list[str] | None) -> list[str]:
    keywords: list[str] = []
    for value in preferred_symbols or []:
        for token in IDENTIFIER_RE.findall(value or ""):
            if token not in keywords:
                keywords.append(token)
    for token in IDENTIFIER_RE.findall(query or ""):
        if len(token) >= 2 and token not in keywords:
            keywords.append(token)
    return keywords[:12]


def _grep_matches(root: Path, keywords: list[str], scoped_path: Path | None, max_matches: int) -> list[_LineMatch]:
    if not keywords:
        return []
    if shutil.which("rg"):
        return _ripgrep_matches(root, keywords, scoped_path, max_matches)
    return _python_scan_matches(root, keywords, scoped_path, max_matches)


def _ripgrep_matches(root: Path, keywords: list[str], scoped_path: Path | None, max_matches: int) -> list[_LineMatch]:
    pattern = "|".join(re.escape(keyword) for keyword in keywords)
    command = ["rg", "--json", "--line-number", "--column", "--case-sensitive", "--max-count", str(max_matches)]
    for directory in sorted(EXCLUDED_DIRS):
        command.extend(["--glob", f"!**/{directory}/**"])
    command.extend([pattern, str(scoped_path or root)])

    try:
        completed = subprocess.run(
            command,
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return _python_scan_matches(root, keywords, scoped_path, max_matches)

    matches: list[_LineMatch] = []
    for raw_line in completed.stdout.splitlines():
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if payload.get("type") != "match":
            continue
        data = payload.get("data") or {}
        path_text = ((data.get("path") or {}).get("text") or "").strip()
        line_text = (data.get("lines") or {}).get("text") or ""
        line_number = int(data.get("line_number") or 0)
        if not path_text or not line_number:
            continue
        keyword = _matched_keyword(line_text, keywords)
        kind, name = _classify_line(line_text, keyword)
        matches.append(
            _LineMatch(
                path=(root / path_text).resolve() if not Path(path_text).is_absolute() else Path(path_text),
                line_number=line_number,
                text=line_text.rstrip("\r\n"),
                keyword=keyword,
                kind=kind,
                name=name,
            )
        )
        if len(matches) >= max_matches:
            break
    return _dedupe_matches(matches)


def _python_scan_matches(root: Path, keywords: list[str], scoped_path: Path | None, max_matches: int) -> list[_LineMatch]:
    paths = [scoped_path] if scoped_path and scoped_path.is_file() else _iter_search_files(scoped_path or root)
    lowered_keywords = [(keyword, keyword.lower()) for keyword in keywords]
    matches: list[_LineMatch] = []
    for path in paths:
        if path is None or not path.is_file() or _is_excluded(path):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines, start=1):
            keyword = ""
            lowered_line = line.lower()
            for raw_keyword, lowered_keyword in lowered_keywords:
                if lowered_keyword in lowered_line:
                    keyword = raw_keyword
                    break
            if not keyword:
                continue
            kind, name = _classify_line(line, keyword)
            matches.append(_LineMatch(path.resolve(), index, line, keyword, kind, name))
            if len(matches) >= max_matches:
                return _dedupe_matches(matches)
    return _dedupe_matches(matches)


def _iter_search_files(root: Path) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    for path in root.rglob("*"):
        if path.is_file() and not _is_excluded(path) and path.suffix.lower() in TEXT_EXTENSIONS:
            yield path


def _is_excluded(path: Path) -> bool:
    return any(part in EXCLUDED_DIRS for part in path.parts)


def _dedupe_matches(matches: list[_LineMatch]) -> list[_LineMatch]:
    seen: set[tuple[Path, int, str]] = set()
    result: list[_LineMatch] = []
    priority = {"definition": 0, "call": 1, "variable": 2, "keyword": 3}
    for match in sorted(matches, key=lambda item: (priority.get(item.kind, 9), str(item.path), item.line_number)):
        key = (match.path, match.line_number, match.keyword)
        if key in seen:
            continue
        seen.add(key)
        result.append(match)
    return result


def _matched_keyword(line: str, keywords: list[str]) -> str:
    for keyword in keywords:
        if keyword in line:
            return keyword
    lowered = line.lower()
    for keyword in keywords:
        if keyword.lower() in lowered:
            return keyword
    return keywords[0] if keywords else ""


def _classify_line(line: str, keyword: str) -> tuple[str, str]:
    definition = DEFINITION_RE.match(line)
    if definition:
        return "definition", definition.group(1)
    assignment = ASSIGNMENT_RE.match(line)
    if assignment and (not keyword or assignment.group(1) == keyword):
        return "variable", assignment.group(1)
    if keyword and re.search(rf"(?:\b|\.){re.escape(keyword)}\s*\(", line):
        return "call", keyword
    return "keyword", keyword


def _result_for_file(path: Path, root: Path, query: str, target_name: str, matches: list[_LineMatch]) -> FileSearchResult:
    language = _language_for_path(path)
    lines = _read_lines(path)
    hits: list[FileSearchHit] = []
    used_ranges: list[tuple[int, int]] = []

    for match in sorted(matches, key=lambda item: _inspect_priority(item, target_name)):
        hit = _definition_hit(path, lines, match) if match.kind == "definition" else None
        if hit is None:
            hit = _window_hit(lines, match, radius=5)
        if _overlaps(used_ranges, hit.start_line, hit.end_line):
            continue
        used_ranges.append((hit.start_line, hit.end_line))
        hits.append(hit)
        if len(hits) >= 8:
            break

    try:
        display_path = str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        display_path = str(path)
    return FileSearchResult(display_path, language, query, target_name, hits, root_path=str(root.resolve()))


def _inspect_priority(match: _LineMatch, target_name: str) -> tuple[int, int]:
    exact = bool(target_name and match.name == target_name)
    if match.kind == "definition" and exact:
        return (0, match.line_number)
    if match.kind == "call" and exact:
        return (1, match.line_number)
    if exact:
        return (2, match.line_number)
    if match.kind == "definition":
        return (3, match.line_number)
    if match.kind == "call":
        return (4, match.line_number)
    return (5, match.line_number)



def _definition_hit(path: Path, lines: list[str], match: _LineMatch) -> FileSearchHit | None:
    if path.suffix.lower() not in PYTHON_EXTENSIONS:
        return None
    source = "\n".join(lines)
    name = match.name or match.keyword
    if not name:
        return None
    ast_range = _ast_definition_range(source, name, match.line_number)
    if ast_range is None:
        ast_range = _indent_definition_range(lines, name, match.line_number)
    if ast_range is None:
        return None
    start, end = ast_range
    return FileSearchHit("definition", name, start, end, "\n".join(lines[start - 1:end]), match.line_number, match.text)


def _ast_definition_range(source: str, name: str, match_line: int) -> tuple[int, int] | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    candidates: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            start = getattr(node, "lineno", 0)
            end = getattr(node, "end_lineno", 0)
            if start and end:
                candidates.append((start, end))
    for start, end in candidates:
        if start <= match_line <= end:
            return start, end
    return candidates[0] if len(candidates) == 1 else None


def _indent_definition_range(lines: list[str], name: str, match_line: int) -> tuple[int, int] | None:
    pattern = re.compile(rf"^(\s*)(?:async\s+def|def|class)\s+{re.escape(name)}\b")
    start_index = -1
    start_indent = 0
    for index in range(max(0, match_line - 3), min(len(lines), match_line + 2)):
        match = pattern.match(lines[index])
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
    return start_index + 1, end_index


def _window_hit(lines: list[str], match: _LineMatch, radius: int) -> FileSearchHit:
    start = max(1, match.line_number - radius)
    end = min(len(lines), match.line_number + radius)
    return FileSearchHit(
        match.kind,
        match.name or match.keyword,
        start,
        end,
        "\n".join(lines[start - 1:end]),
        match.line_number,
        match.text,
    )


def _head_match(path: Path, target_name: str) -> list[_LineMatch]:
    lines = _read_lines(path)
    if not lines:
        return []
    return [_LineMatch(path.resolve(), 1, lines[0], target_name, "keyword", target_name)]


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _overlaps(ranges: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(start <= kept_end and end >= kept_start for kept_start, kept_end in ranges)


def _language_for_path(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    if suffix in {"py", "pyw"}:
        return "python"
    if suffix in {"js", "jsx", "mjs"}:
        return "javascript"
    if suffix in {"ts", "tsx"}:
        return "typescript"
    return suffix or "text"
