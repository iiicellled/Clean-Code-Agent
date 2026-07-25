from __future__ import annotations

import ast
import re
from typing import TYPE_CHECKING

from ..schemas import CodePatchProposal, WorkspaceState

if TYPE_CHECKING:
    from .intent_service import IntentDecision


CODE_BLOCK_RE = re.compile(r"```([^`\n]*)\n([\s\S]*?)```", re.MULTILINE)
DEF_OR_CLASS_RE = re.compile(r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_]\w*)\b", re.MULTILINE)
PYTHON_DEF_RE = re.compile(r"^\s*(?:async\s+def|def)\s+([A-Za-z_]\w*)\s*\(", re.MULTILINE)


def propose_patch(
    user_request: str,
    workspace: WorkspaceState | None,
    model_answer: str,
    decision: "IntentDecision | None" = None,
) -> CodePatchProposal | None:
    if workspace is None or not workspace.active_file:
        return None
    active_file = next((file for file in workspace.files if file.path == workspace.active_file), None)
    if active_file is None or not active_file.content.strip():
        return None

    new_code = _first_code_block(model_answer) or _plain_code(model_answer)
    if not new_code:
        return None

    intent = decision.intent if decision else ""
    target_name = _target_name(user_request, active_file.content, decision)

    if intent == "create_function":
        return _propose_create_function(active_file.path, active_file.content, active_file.language, new_code, target_name, user_request)

    if intent == "modify_function":
        return _propose_modify_function(active_file.path, active_file.content, active_file.language, new_code, target_name, user_request)

    for candidate in _candidate_new_regions(active_file.content, new_code, user_request, active_file.language, target_name):
        old_code = _select_old_region(active_file.content, candidate, user_request, active_file.language, target_name)
        if not old_code or old_code.strip() == candidate.strip():
            continue
        if active_file.content.count(old_code) != 1:
            continue
        return CodePatchProposal(
            file_path=active_file.path,
            summary=_summary_for_patch(user_request, old_code, candidate),
            old=old_code,
            new=_ensure_trailing_newline(candidate),
        )
    return None


def compact_workspace_context(workspace: WorkspaceState | None, user_request: str = "", max_chars: int = 4500) -> str:
    if workspace is None or not workspace.files:
        return ""
    active_file = next((file for file in workspace.files if file.path == workspace.active_file), workspace.files[0])
    snippet = _relevant_snippet(active_file.content, user_request, active_file.language, max_chars=max_chars)
    return f"Active file: {active_file.path}\nLanguage: {active_file.language}\nRelevant code:\n```{active_file.language}\n{snippet}\n```"


def _propose_modify_function(
    file_path: str,
    source: str,
    language: str,
    new_code: str,
    target_name: str,
    user_request: str,
) -> CodePatchProposal | None:
    if not target_name or language.lower() not in {"python", "py"}:
        return None
    old_code = _python_block_by_name(source, target_name)
    if not old_code or source.count(old_code) != 1:
        return None
    candidate = _normalise_patch_code(new_code)
    if not _contains_python_definition(candidate, target_name):
        return None
    if old_code.strip() == candidate.strip():
        return None
    return CodePatchProposal(
        file_path=file_path,
        summary=_summary_for_patch(user_request, old_code, candidate),
        old=old_code,
        new=_ensure_trailing_newline(candidate),
    )


def _propose_create_function(
    file_path: str,
    source: str,
    language: str,
    new_code: str,
    target_name: str,
    user_request: str,
) -> CodePatchProposal | None:
    if not target_name or language.lower() not in {"python", "py"}:
        return None
    if _python_block_by_name(source, target_name):
        return None
    candidate = _normalise_patch_code(new_code)
    if not _contains_python_definition(candidate, target_name):
        return None
    anchor = _python_insert_anchor(source)
    if not anchor or source.count(anchor) != 1:
        return None
    separator = "\n\n" if anchor.endswith("\n") else "\n\n"
    return CodePatchProposal(
        file_path=file_path,
        summary=f"Insert function {target_name} based on: {' '.join((user_request or '').split())[:160]}.",
        old=anchor,
        new=_ensure_trailing_newline(anchor.rstrip("\n") + separator + candidate),
    )


def _ensure_trailing_newline(code: str) -> str:
    return code if code.endswith("\n") else code + "\n"

def _normalise_patch_code(code: str) -> str:
    return (code or "").strip("\n")


def _contains_python_definition(code: str, name: str) -> bool:
    return bool(name and re.search(rf"^\s*(?:async\s+def|def)\s+{re.escape(name)}\s*\(", code or "", re.MULTILINE))

def _first_code_block(content: str) -> str:
    match = CODE_BLOCK_RE.search(content or "")
    if not match:
        return ""
    return (match.group(2) or "").strip("\n")


def _plain_code(content: str) -> str:
    text = (content or "").strip("\n")
    return text if DEF_OR_CLASS_RE.search(text) else ""


def _candidate_new_regions(source: str, new_code: str, user_request: str, language: str, target_name: str = "") -> list[str]:
    if language.lower() not in {"python", "py"}:
        return [new_code]
    requested_names = [target_name] if target_name else _requested_definition_names(user_request, source)
    new_names = _definition_names(new_code)
    candidates: list[str] = []
    ordered_names = [name for name in requested_names if name in new_names]
    ordered_names.extend(name for name in new_names if name not in ordered_names)
    for name in ordered_names:
        block = _python_block_by_name(new_code, name)
        if block:
            candidates.append(block.strip("\n"))
    if not candidates and len(new_names) == 1:
        block = _python_block_by_name(new_code, new_names[0])
        if block:
            candidates.append(block.strip("\n"))
    if not candidates and len(new_code) < max(400, int(len(source) * 0.65)):
        candidates.append(new_code)
    return candidates


def _select_old_region(source: str, new_code: str, user_request: str, language: str, target_name: str = "") -> str:
    name = target_name or _definition_name(new_code)
    if name and language.lower() in {"python", "py"}:
        return _python_block_by_name(source, name)
    return ""


def _definition_name(code: str) -> str:
    match = DEF_OR_CLASS_RE.search(code or "")
    return match.group(1) if match else ""


def _definition_names(code: str) -> list[str]:
    return list(dict.fromkeys(DEF_OR_CLASS_RE.findall(code or "")))


def _target_name(user_request: str, source: str, decision: "IntentDecision | None") -> str:
    if decision:
        value = decision.slots.get("function_name")
        if value:
            return _clean_identifier(value)
    names = _requested_definition_names(user_request, source)
    return names[0] if names else ""


def _clean_identifier(value: str) -> str:
    match = re.search(r"[A-Za-z_]\w*", value or "")
    return match.group(0) if match else ""


def _requested_definition_names(user_request: str, source: str) -> list[str]:
    source_names = set(_definition_names(source))
    requested = []
    for token in re.findall(r"[A-Za-z_]\w+", user_request or ""):
        if token in source_names and token not in requested:
            requested.append(token)
    return requested


def _python_block_by_name(source: str, name: str) -> str:
    if not name:
        return ""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _indent_block_by_name(source, name)
    lines = source.splitlines(keepends=True)
    candidates = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            start = getattr(node, "lineno", 0)
            end = getattr(node, "end_lineno", 0)
            if start and end:
                candidates.append("".join(lines[start - 1:end]))
    return candidates[0] if len(candidates) == 1 else ""


def _indent_block_by_name(source: str, name: str) -> str:
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
        return ""
    end_index = len(lines)
    for index in range(start_index + 1, len(lines)):
        line = lines[index]
        if line.strip() and len(line) - len(line.lstrip()) <= start_indent:
            end_index = index
            break
    return "".join(lines[start_index:end_index])


def _first_matching_python_function(code: str, target_name: str) -> str:
    for name in _definition_names(code):
        if name == target_name:
            return _indent_block_by_name(code, name)
    return ""


def _python_insert_anchor(source: str) -> str:
    lines = source.splitlines(keepends=True)
    if not lines:
        return ""
    end = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("import ") or stripped.startswith("from "):
            end = index + 1
            continue
        break
    if end > 0:
        return "".join(lines[:end])
    return lines[0]


def _relevant_snippet(source: str, user_request: str, language: str, max_chars: int) -> str:
    if len(source) <= max_chars:
        return source
    terms = [term.lower() for term in re.findall(r"[A-Za-z_]\w+", user_request or "") if len(term) >= 3]
    target_names = _requested_definition_names(user_request, source)
    if language.lower() in {"python", "py"} and target_names:
        name = target_names[0]
        parts = []
        block = _python_block_by_name(source, name)
        if block:
            parts.append(f"# Target definition: {name}\n{block.strip()}")
        usages = _usage_snippets(source, name, max_chars=max(800, max_chars // 2))
        if usages:
            parts.append(f"# Usage snippets for {name}\n{usages}")
        snippet = "\n\n".join(parts)
        if snippet:
            return snippet[:max_chars]
    if language.lower() in {"python", "py"} and terms:
        for term in terms:
            block = _python_block_by_name(source, term)
            if block:
                return block[:max_chars]
    lines = source.splitlines()
    hit = 0
    for index, line in enumerate(lines):
        lowered = line.lower()
        if any(term in lowered for term in terms):
            hit = index
            break
    start = max(0, hit - 60)
    end = min(len(lines), hit + 100)
    snippet = "\n".join(lines[start:end])
    if len(snippet) > max_chars:
        snippet = snippet[:max_chars] + "\n# ... truncated ..."
    return snippet


def _usage_snippets(source: str, name: str, max_chars: int) -> str:
    lines = source.splitlines()
    hits = []
    pattern = re.compile(rf"(?<!def\s)\b{re.escape(name)}\s*\(")
    def_pattern = re.compile(rf"^\s*(?:async\s+def|def)\s+{re.escape(name)}\s*\(")
    for index, line in enumerate(lines):
        if def_pattern.search(line):
            continue
        if pattern.search(line) or f".{name}(" in line:
            hits.append(index)
    ranges = []
    for hit in hits[:8]:
        start = max(0, hit - 8)
        end = min(len(lines), hit + 9)
        ranges.append((start, end))
    merged = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    chunks = []
    for start, end in merged:
        chunks.append(f"# lines {start + 1}-{end}\n" + "\n".join(lines[start:end]))
    text = "\n\n".join(chunks)
    return text[:max_chars]


def _summary_for_patch(user_request: str, old_code: str, new_code: str) -> str:
    old_lines = len(old_code.splitlines())
    new_lines = len(new_code.splitlines())
    request = " ".join((user_request or "").split())[:160]
    return f"Proposed replacement based on: {request}. Lines: {old_lines} -> {new_lines}."