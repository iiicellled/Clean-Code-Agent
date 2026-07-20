from __future__ import annotations

from collections.abc import Iterator

from ..model_service import primary_chat_model
from ..schemas import ChatMessage
from .service_configs import ServiceModelConfig, CHATBOT_CONFIG


def _routed_messages(messages: list[ChatMessage], config: ServiceModelConfig) -> list[ChatMessage]:
    if (messages and messages[0].role == "system") or (not config.system_prompt):
        return messages
    return [ChatMessage(role="system", content=config.system_prompt), *messages]


def chat(
    messages: list[ChatMessage],
    config: ServiceModelConfig = CHATBOT_CONFIG,
) -> str:
    return primary_chat_model.chat(_routed_messages(messages, config), cfg=config)


def stream_chat(
    messages: list[ChatMessage],
    config: ServiceModelConfig = CHATBOT_CONFIG,
) -> Iterator[str]:
    yield from primary_chat_model.stream_chat(_routed_messages(messages, config), cfg=config)