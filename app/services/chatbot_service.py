from __future__ import annotations

from collections.abc import Iterator

from ..model_service import primary_chat_model
from ..schemas import ChatMessage, WorkspaceState
from ..tools import agent_search_tool
from .service_configs import ServiceModelConfig, CHATBOT_CONFIG

MAX_CHATBOT_TOOL_CALLS = 2


def _routed_messages(messages: list[ChatMessage], config: ServiceModelConfig) -> list[ChatMessage]:
    if (messages and messages[0].role == "system") or (not config.system_prompt):
        return messages
    return [ChatMessage(role="system", content=config.system_prompt), *messages]


def chat(
    messages: list[ChatMessage],
    config: ServiceModelConfig = CHATBOT_CONFIG,
    workspace: WorkspaceState | None = None,
) -> str:
    routed_messages = _routed_messages(messages, config)
    if workspace is None or not workspace.files:
        return primary_chat_model.chat(routed_messages, cfg=config)
    tool = agent_search_tool.build_search_workspace_tool(workspace)
    return primary_chat_model.chat_with_tools(
        routed_messages,
        cfg=config,
        tools=[tool],
        max_tool_calls=MAX_CHATBOT_TOOL_CALLS,
    )


def stream_chat(
    messages: list[ChatMessage],
    config: ServiceModelConfig = CHATBOT_CONFIG,
    workspace: WorkspaceState | None = None,
) -> Iterator[str]:
    routed_messages = _routed_messages(messages, config)
    if workspace is None or not workspace.files:
        yield from primary_chat_model.stream_chat(routed_messages, cfg=config)
        return
    yield chat(routed_messages, config=config, workspace=workspace)