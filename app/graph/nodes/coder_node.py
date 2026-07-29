from __future__ import annotations

from ...services import coder_service
from ..state import CoderAgentState


def coder_node(state: CoderAgentState) -> CoderAgentState:
    decision = state.get("decision")
    if decision is None:
        return {"raw_code": ""}
    raw_code = coder_service.generate_code(
        decision,
        state.get("planner_messages") or state.get("messages", []),
    )
    return {"raw_code": raw_code}
