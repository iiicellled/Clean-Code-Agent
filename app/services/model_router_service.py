from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import logging

from ..config import settings
from ..model_service import coder_chat_model, primary_chat_model
from ..schemas import ChatMessage
from . import chatbot_service, code_review_service, coder_service, intent_service
from .intent_service import ActiveTaskState, IntentDecision
from .service_configs import CODER_CONFIG


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntentChatResult:
    content: str
    decision: IntentDecision | None
    executed: bool = False


@dataclass(frozen=True)
class IntentStreamEvent:
    content: str = ""
    result: IntentChatResult | None = None


def chat(
    messages: list[ChatMessage],
    active_task: ActiveTaskState | None = None,
) -> str:
    return handle_chat(messages, active_task=active_task).content


def handle_chat(
    messages: list[ChatMessage],
    active_task: ActiveTaskState | None = None,
) -> IntentChatResult:
    if not _intent_routing_available():
        logger.info("Intent routing unavailable or disabled; using coder model directly")
        content = coder_chat_model.chat(messages, cfg=CODER_CONFIG)
        return IntentChatResult(content=content, decision=None, executed=True)

    decision = _decide_intent(messages, active_task)

    if decision.intent == "write_code":
        if decision.missing_slots:
            content = decision.follow_up_question or intent_service.default_follow_up(
                decision.intent,
                decision.missing_slots,
            )
            return IntentChatResult(content=content, decision=decision, executed=False)

        raw_code = coder_service.generate_code(decision, messages)
        content = code_review_service.review_and_present_code(
            decision=decision,
            messages=messages,
            raw_code=raw_code,
        )
        return IntentChatResult(content=content, decision=decision, executed=True)

    content = chatbot_service.chat(messages)
    return IntentChatResult(content=content, decision=decision, executed=False)


def stream_handle_chat(
    messages: list[ChatMessage],
    active_task: ActiveTaskState | None = None,
) -> Iterator[IntentStreamEvent]:
    if not _intent_routing_available():
        logger.info("Intent routing unavailable or disabled; streaming coder model directly")
        chunks: list[str] = []
        for chunk in coder_chat_model.stream_chat(messages, cfg=CODER_CONFIG):
            chunks.append(chunk)
            yield IntentStreamEvent(content=chunk)
        yield IntentStreamEvent(result=IntentChatResult(content="".join(chunks), decision=None, executed=True))
        return

    decision = _decide_intent(messages, active_task)

    if decision.intent == "write_code":
        if decision.missing_slots:
            content = decision.follow_up_question or intent_service.default_follow_up(
                decision.intent,
                decision.missing_slots,
            )
            yield IntentStreamEvent(content=content)
            yield IntentStreamEvent(result=IntentChatResult(content=content, decision=decision, executed=False))
            return

        raw_code = coder_service.generate_code(decision, messages)
        chunks: list[str] = []
        for chunk in code_review_service.stream_review_and_present_code(
            decision=decision,
            messages=messages,
            raw_code=raw_code,
        ):
            chunks.append(chunk)
            yield IntentStreamEvent(content=chunk)
        yield IntentStreamEvent(result=IntentChatResult(content="".join(chunks), decision=decision, executed=True))
        return

    chunks = []
    for chunk in chatbot_service.stream_chat(messages):
        chunks.append(chunk)
        yield IntentStreamEvent(content=chunk)
    yield IntentStreamEvent(result=IntentChatResult(content="".join(chunks), decision=decision, executed=False))


def stream_chat(
    messages: list[ChatMessage],
    active_task: ActiveTaskState | None = None,
) -> Iterator[str]:
    for event in stream_handle_chat(messages, active_task=active_task):
        if event.content:
            yield event.content


def _decide_intent(messages: list[ChatMessage], active_task: ActiveTaskState | None) -> IntentDecision:
    decision = intent_service.analyze_intent(messages, active_task=active_task)
    logger.info(
        "Intent decision intent=%s confidence=%.2f missing_slots=%s slots=%s active_task=%s",
        decision.intent,
        decision.confidence,
        decision.missing_slots,
        intent_service.safe_slots_for_log(decision.slots),
        bool(active_task),
    )
    return decision


def _intent_routing_available() -> bool:
    return settings.model_routing_enabled and primary_chat_model.configured and coder_chat_model.configured