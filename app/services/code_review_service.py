from __future__ import annotations

from collections.abc import Iterator
import logging

from ..model_service import primary_chat_model
from ..schemas import ChatMessage
from .intent_service import IntentDecision
from .service_configs import ServiceModelConfig, CODE_REVIEW_CONFIG, CODE_REVIEW_USER_PROMPT_TEMPLATE


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def review_and_present_code(
    decision: IntentDecision,
    messages: list[ChatMessage],
    raw_code: str,
    config: ServiceModelConfig = CODE_REVIEW_CONFIG,
) -> str:
    logger.info("Reviewing coder output with primary model raw_code_chars=%d", len(raw_code))
    return primary_chat_model.chat(
        _build_review_messages(decision, messages, raw_code, config),
        cfg=config,
    )


def stream_review_and_present_code(
    decision: IntentDecision,
    messages: list[ChatMessage],
    raw_code: str,
    config: ServiceModelConfig = CODE_REVIEW_CONFIG,
) -> Iterator[str]:
    logger.info("Streaming reviewed coder output with primary model raw_code_chars=%d", len(raw_code))
    yield from primary_chat_model.stream_chat(
        _build_review_messages(decision, messages, raw_code, config),
        cfg=config,
    )


def _build_review_messages(
    decision: IntentDecision,
    messages: list[ChatMessage],
    raw_code: str,
    config: ServiceModelConfig,
) -> list[ChatMessage]:
    slots = decision.slots
    latest_user = next((message.content for message in reversed(messages) if message.role == "user"), "")
    user_prompt = CODE_REVIEW_USER_PROMPT_TEMPLATE.format(
        language=slots.get("language"),
        task=slots.get("task"),
        constraints=slots.get("constraints") or "无额外约束",
        latest_user=latest_user,
        raw_code=raw_code.strip(),
    )
    return [
        ChatMessage(role="system", content=config.system_prompt),
        ChatMessage(role="user", content=user_prompt),
    ]