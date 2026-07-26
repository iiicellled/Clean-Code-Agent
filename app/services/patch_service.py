from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..schemas import CodePatchProposal, WorkspaceState
from ..context.current_file_search import (
    compact_workspace_context,
    definition_names,
    first_definition_name,
    is_python_language,
    python_block_by_name,
    requested_definition_names,
)

if TYPE_CHECKING:
    from .intent_service import IntentDecision


CODE_BLOCK_RE = re.compile(r"```([^`\n]*)\n([\s\S]*?)```", re.MULTILINE)


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


def _propose_modify_function(
    file_path: str,
    source: str,
    language: str,
    new_code: str,
    target_name: str,
    user_request: str,
) -> CodePatchProposal | None:
    if not target_name or not is_python_language(language):
        return None
    old_code = python_block_by_name(source, target_name)
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
    if not target_name or not is_python_language(language):
        return None
    if python_block_by_name(source, target_name):
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
    return text if definition_names(text) else ""


def _candidate_new_regions(source: str, new_code: str, user_request: str, language: str, target_name: str = "") -> list[str]:
    if not is_python_language(language):
        return [new_code]
    requested_names = [target_name] if target_name else requested_definition_names(user_request, source)
    new_names = definition_names(new_code)
    candidates: list[str] = []
    ordered_names = [name for name in requested_names if name in new_names]
    ordered_names.extend(name for name in new_names if name not in ordered_names)
    for name in ordered_names:
        block = python_block_by_name(new_code, name)
        if block:
            candidates.append(block.strip("\n"))
    if not candidates and len(new_names) == 1:
        block = python_block_by_name(new_code, new_names[0])
        if block:
            candidates.append(block.strip("\n"))
    if not candidates and len(new_code) < max(400, int(len(source) * 0.65)):
        candidates.append(new_code)
    return candidates


def _select_old_region(source: str, new_code: str, user_request: str, language: str, target_name: str = "") -> str:
    name = target_name or first_definition_name(new_code)
    if name and is_python_language(language):
        return python_block_by_name(source, name)
    return ""


def _target_name(user_request: str, source: str, decision: "IntentDecision | None") -> str:
    if decision:
        value = decision.slots.get("function_name")
        if value:
            return _clean_identifier(value)
    names = requested_definition_names(user_request, source)
    return names[0] if names else ""


def _clean_identifier(value: str) -> str:
    match = re.search(r"[A-Za-z_]\w*", value or "")
    return match.group(0) if match else ""


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


def _summary_for_patch(user_request: str, old_code: str, new_code: str) -> str:
    old_lines = len(old_code.splitlines())
    new_lines = len(new_code.splitlines())
    request = " ".join((user_request or "").split())[:160]
    return f"Proposed replacement based on: {request}. Lines: {old_lines} -> {new_lines}."
