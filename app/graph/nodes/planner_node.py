from __future__ import annotations

import logging

from ...model_service import RemoteModelError
from ...services import planner_service
from ..state import CoderAgentState


logger = logging.getLogger(__name__)


def planner_node(state: CoderAgentState) -> CoderAgentState:
    decision = state.get("decision")
    messages = state.get("messages", [])
    if decision is None:
        return {"planner_messages": messages}
    try:
        planner_message = planner_service.build_planner_message(
            decision,
            messages,
            workspace=state.get("workspace"),
        )
    except RemoteModelError:
        logger.warning("LangGraph planner model failed; continuing without implementation plan")
        return {"planner_messages": messages}
    return {"planner_messages": [*messages, planner_message]}
