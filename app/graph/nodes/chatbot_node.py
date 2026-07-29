from __future__ import annotations

from ...services import chatbot_service
from ..state import CoderAgentState


def chatbot_node(state: CoderAgentState) -> CoderAgentState:
    content = chatbot_service.chat(
        state.get("messages", []),
        workspace=state.get("workspace"),
    )
    return {"content": content, "executed": False}
