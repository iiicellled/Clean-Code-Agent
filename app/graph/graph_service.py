from __future__ import annotations

from dataclasses import dataclass
import logging

from langgraph.graph import END, StateGraph

from ..config import settings
from ..model_service import coder_chat_model, primary_chat_model
from ..schemas import ChatMessage, CodePatchProposal, WorkspaceState
from ..services.intent_service import ActiveTaskState, IntentDecision, is_code_intent
from ..services.service_configs import CODER_CONFIG
from .nodes.chatbot_node import chatbot_node
from .nodes.coder_node import coder_node
from .nodes.follow_up_node import follow_up_node
from .nodes.intent_node import intent_node
from .nodes.patch_node import patch_node
from .nodes.planner_node import planner_node
from .nodes.review_node import review_node
from .state import CoderAgentState


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GraphChatResult:
    content: str
    decision: IntentDecision | None
    executed: bool = False
    patch: CodePatchProposal | None = None


def handle_chat(
    messages: list[ChatMessage],
    active_task: ActiveTaskState | None = None,
    decision: IntentDecision | None = None,
    workspace: WorkspaceState | None = None,
    user_request: str = "",
) -> GraphChatResult:
    if not _intent_routing_available():
        logger.info("LangGraph routing unavailable or disabled; using coder model directly")
        content = coder_chat_model.chat(messages, cfg=CODER_CONFIG)
        return GraphChatResult(content=content, decision=None, executed=True)

    latest_user = user_request or next((message.content for message in reversed(messages) if message.role == "user"), "")
    final_state = _compiled_graph().invoke(
        {
            "messages": messages,
            "workspace": workspace,
            "active_task": active_task,
            "decision": decision,
            "user_request": latest_user,
            "executed": False,
        }
    )
    return GraphChatResult(
        content=(final_state.get("content") or "").strip(),
        decision=final_state.get("decision"),
        executed=bool(final_state.get("executed")),
        patch=final_state.get("patch"),
    )


def _route_after_intent(state: CoderAgentState) -> str:
    decision = state.get("decision")
    if decision is None:
        return "general_chat"
    if is_code_intent(decision.intent):
        if decision.missing_slots:
            return "follow_up"
        return "code_ready"
    return "general_chat"


def _build_graph():
    graph = StateGraph(CoderAgentState)
    graph.add_node("intent", intent_node)
    graph.add_node("follow_up", follow_up_node)
    graph.add_node("chatbot", chatbot_node)
    graph.add_node("planner", planner_node)
    graph.add_node("coder", coder_node)
    graph.add_node("review", review_node)
    graph.add_node("patch", patch_node)

    graph.set_entry_point("intent")
    graph.add_conditional_edges(
        "intent",
        _route_after_intent,
        {
            "follow_up": "follow_up",
            "general_chat": "chatbot",
            "code_ready": "planner",
        },
    )
    graph.add_edge("planner", "coder")
    graph.add_edge("coder", "review")
    graph.add_edge("review", "patch")
    graph.add_edge("follow_up", END)
    graph.add_edge("chatbot", END)
    graph.add_edge("patch", END)
    return graph.compile()


_GRAPH = None


def _compiled_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = _build_graph()
    return _GRAPH


def _intent_routing_available() -> bool:
    return settings.model_routing_enabled and primary_chat_model.configured and coder_chat_model.configured
