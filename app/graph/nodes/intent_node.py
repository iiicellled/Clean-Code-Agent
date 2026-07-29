from __future__ import annotations

import logging

from ...services import intent_service
from ..state import CoderAgentState


logger = logging.getLogger(__name__)


def intent_node(state: CoderAgentState) -> CoderAgentState:
    decision = state.get("decision")
    if decision is None:
        decision = intent_service.analyze_intent(
            state.get("messages", []),
            active_task=state.get("active_task"),
        )
    logger.info(
        "LangGraph intent decision intent=%s confidence=%.2f missing_slots=%s slots=%s active_task=%s",
        decision.intent,
        decision.confidence,
        decision.missing_slots,
        intent_service.safe_slots_for_log(decision.slots),
        bool(state.get("active_task")),
    )
    return {"decision": decision}
