from __future__ import annotations

from ...services import patch_service
from ..state import CoderAgentState


def patch_node(state: CoderAgentState) -> CoderAgentState:
    if not state.get("executed"):
        return {"patch": None}
    patch = patch_service.propose_patch(
        state.get("user_request", ""),
        state.get("workspace"),
        state.get("content", ""),
        state.get("decision"),
    )
    return {"patch": patch}
