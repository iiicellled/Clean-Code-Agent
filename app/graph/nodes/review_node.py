from __future__ import annotations

import logging

from ...model_service import RemoteModelError
from ...services import code_review_service
from ..state import CoderAgentState


logger = logging.getLogger(__name__)


def review_node(state: CoderAgentState) -> CoderAgentState:
    decision = state.get("decision")
    raw_code = (state.get("raw_code") or "").strip()
    if decision is None:
        return {"content": raw_code, "executed": True}
    try:
        content = code_review_service.review_and_present_code(
            decision=decision,
            messages=state.get("planner_messages") or state.get("messages", []),
            raw_code=raw_code,
        )
    except RemoteModelError:
        logger.exception("LangGraph code review model failed; returning raw coder output")
        content = raw_code
    if not content:
        logger.warning("LangGraph code review model returned an empty answer; returning raw coder output")
        content = raw_code
    return {"content": content, "executed": True}
