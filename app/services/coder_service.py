from __future__ import annotations

import logging

from ..model_service import coder_chat_model
from ..schemas import ChatMessage
from .intent_service import IntentDecision
from .service_configs import ServiceModelConfig, CODER_CONFIG, CODER_USER_PROMPT_TEMPLATE

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
        if message.role == "system" and message.content.startswith("Current editable code workspace")
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
        prompt += "\n\nFunction output rule: Return only the complete new target function. Include its full signature and body. Do not return imports, tests, examples, explanations, or the whole file."
    elif decision.intent == "modify_function":
        prompt += "\n\nFunction output rule: Return only the complete replacement implementation of the target function. Keep the target function name unchanged and preserve call compatibility unless the user explicitly asks to change the signature. Do not return imports, tests, examples, explanations, or the whole file."
    if workspace_context:
        prompt += "\n\nRelevant current-file context:\n" + workspace_context[:5000]
        prompt += "\n\nPatch output rule: When editing the current file, return only the complete function or class that should replace the old one. Do not return the whole file unless the user explicitly asks for a full-file rewrite."
    return [
        ChatMessage(role="system", content=config.system_prompt),
        ChatMessage(role="user", content=prompt),
    ]
