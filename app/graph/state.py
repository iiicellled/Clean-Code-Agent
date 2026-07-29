from __future__ import annotations

from typing import TypedDict

from ..schemas import ChatMessage, CodePatchProposal, WorkspaceState
from ..services.intent_service import ActiveTaskState, IntentDecision


class CoderAgentState(TypedDict, total=False):
    messages: list[ChatMessage]
    workspace: WorkspaceState | None
    active_task: ActiveTaskState | None
    decision: IntentDecision | None
    user_request: str
    planner_messages: list[ChatMessage]
    raw_code: str
    content: str
    executed: bool
    patch: CodePatchProposal | None
