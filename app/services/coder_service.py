from __future__ import annotations

from ..model_service import coder_chat_model
from ..schemas import ChatMessage
from .intent_service import IntentDecision
from .service_configs import ServiceModelConfig, CODER_CONFIG, CODER_USER_PROMPT_TEMPLATE


def generate_code(
    decision: IntentDecision,
    messages: list[ChatMessage],
    config: ServiceModelConfig = CODER_CONFIG,
) -> str:
    return coder_chat_model.chat(_build_coder_messages(decision, messages, config), cfg=config)


def _build_coder_messages(
    decision: IntentDecision,
    messages: list[ChatMessage],
    config: ServiceModelConfig,
) -> list[ChatMessage]:
    slots = decision.slots
    latest_user = next((message.content for message in reversed(messages) if message.role == "user"), "")
    prompt = CODER_USER_PROMPT_TEMPLATE.format(
        language=slots.get("language"),
        task=slots.get("task"),
        constraints=slots.get("constraints") or "no extra constraints",
        latest_user=latest_user,
    )
    return [
        ChatMessage(role="system", content=config.system_prompt),
        ChatMessage(role="user", content=prompt),
    ]
