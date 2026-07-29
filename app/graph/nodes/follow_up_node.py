from __future__ import annotations

from ...services import intent_service
from ..state import CoderAgentState


def follow_up_node(state: CoderAgentState) -> CoderAgentState:
    decision = state.get("decision")
    if decision is None:
        return {"content": "", "executed": False}
    content = decision.follow_up_question or intent_service.default_follow_up(
        decision.intent,
        decision.missing_slots,
    )
    return {"content": content, "executed": False}
