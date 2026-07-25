from __future__ import annotations

import logging

from ..model_service import coder_chat_model
from ..schemas import ChatMessage
from .intent_service import IntentDecision
from .service_configs import (
    CODER_CONFIG,
    CODER_CREATE_FUNCTION_RULE,
    CODER_MODIFY_FUNCTION_RULE,
    CODER_PATCH_OUTPUT_RULE,
    CODER_PLANNER_CONTEXT_PROMPT,
    CODER_PLANNER_FOLLOW_RULE,
    CODER_USER_PROMPT_TEMPLATE,
    CODER_WORKSPACE_CONTEXT_PROMPT,
    PLANNER_CONTEXT_PREFIX,
    ServiceModelConfig,
    WORKSPACE_CONTEXT_PREFIX,
)

logger = logging.getLogger(__name__)


def generate_code(
    decision: IntentDecision,
    messages: list[ChatMessage],
    config: ServiceModelConfig = CODER_CONFIG,
) -> str:
    coder_messages = _build_coder_messages(decision, messages, config)
    logger.info(
        "Coder prompt intent=%s slots=%s system_prompt=%r user_prompt=%r",
        decision.intent,
        decision.slots,
        coder_messages[0].content,
        coder_messages[1].content,
    )
    return coder_chat_model.chat(coder_messages, cfg=config)


def _build_coder_messages(
    decision: IntentDecision,
    messages: list[ChatMessage],
    config: ServiceModelConfig,
) -> list[ChatMessage]:
    slots = decision.slots
    latest_user = next((message.content for message in reversed(messages) if message.role == "user"), "")
    workspace_context = next((
        message.content for message in messages
        if message.role == "system" and message.content.startswith(WORKSPACE_CONTEXT_PREFIX)
    ), "")
    planner_context = next((
        message.content for message in messages
        if message.role == "system" and message.content.startswith(PLANNER_CONTEXT_PREFIX)
    ), "")
    prompt = CODER_USER_PROMPT_TEMPLATE.format(
        language=slots.get("language"),
        task=slots.get("task"),
        constraints=slots.get("constraints") or "no extra constraints",
        latest_user=latest_user,
    )
    function_name = slots.get("function_name") or ""
    parameters = slots.get("parameters") or ""
    if function_name:
        prompt += f"\n\n目标函数：{function_name}"
    if parameters:
        prompt += f"\n函数参数：{parameters}"
    if decision.intent == "create_function":
        prompt += "\n\n" + CODER_CREATE_FUNCTION_RULE
    elif decision.intent == "modify_function":
        prompt += "\n\n" + CODER_MODIFY_FUNCTION_RULE
    if planner_context:
        prompt += "\n\n" + CODER_PLANNER_CONTEXT_PROMPT + "\n" + planner_context[:3000]
        prompt += "\n\n" + CODER_PLANNER_FOLLOW_RULE
    if workspace_context and not planner_context:
        prompt += "\n\n" + CODER_WORKSPACE_CONTEXT_PROMPT + "\n" + workspace_context[:5000]
        prompt += "\n\n" + CODER_PATCH_OUTPUT_RULE
    return [
        ChatMessage(role="system", content=config.system_prompt),
        ChatMessage(role="user", content=prompt),
    ]
