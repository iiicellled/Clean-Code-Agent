from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import re
from typing import Any, Literal

from ..model_service import RemoteModelError, primary_chat_model
from ..schemas import ChatMessage
from .service_configs import INTENT_CONFIG


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

IntentName = Literal["general_chat", "write_code", "unknown"]
INTENT_CONTEXT_LIMIT = 12
INTENT_PARSE_RETRIES = 2
FOLLOW_UP_LANGUAGE = "你想用什么编程语言？"
FOLLOW_UP_TASK = "具体要实现什么算法、函数或功能？"
FOLLOW_UP_DEFAULT = "请补充一下具体需求。"


@dataclass(frozen=True)
class SlotSchema:
    name: str
    description: str
    required: bool = False
    follow_up_question: str | None = None
    invalid_values: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class IntentSchema:
    name: IntentName
    description: str
    slots: tuple[SlotSchema, ...] = ()

    @property
    def required_slot_names(self) -> list[str]:
        return [slot.name for slot in self.slots if slot.required]

    @property
    def slot_names(self) -> list[str]:
        return [slot.name for slot in self.slots]


@dataclass(frozen=True)
class IntentDecision:
    intent: IntentName
    confidence: float
    slots: dict[str, str | None]
    missing_slots: list[str]
    follow_up_question: str | None

    @property
    def ready_to_execute(self) -> bool:
        return self.intent == "write_code" and not self.missing_slots


@dataclass(frozen=True)
class ActiveTaskState:
    intent: IntentName
    slots: dict[str, str | None]
    missing_slots: list[str]


INTENT_SCHEMAS: dict[str, IntentSchema] = {
    "general_chat": IntentSchema(
        name="general_chat",
        description="普通聊天、问答、概念解释、项目讨论，或任何不需要生成代码的请求。",
    ),
    "write_code": IntentSchema(
        name="write_code",
        description="用户想要生成代码、实现算法、编写函数、脚本或补全程序。",
        slots=(
            SlotSchema(
                name="language",
                description="编程语言，例如 Python、JavaScript、Java 或 C++。",
                required=True,
                follow_up_question=FOLLOW_UP_LANGUAGE,
            ),
            SlotSchema(
                name="task",
                description="需要实现的具体算法、函数、脚本或功能；必须具体到足以生成代码。",
                required=True,
                follow_up_question=FOLLOW_UP_TASK,
                invalid_values={
                    "code",
                    "program",
                    "function",
                    "script",
                    "algorithm",
                    "代码",
                    "程序",
                    "函数",
                    "脚本",
                    "算法",
                    "一个算法",
                    "算法代码",
                },
            ),
            SlotSchema(
                name="constraints",
                description="可选约束，例如注释要求、复杂度、输入输出格式或禁用的库。",
                required=False,
            ),
        ),
    ),
    "unknown": IntentSchema(
        name="unknown",
        description="无法判断用户意图。",
    ),
}

EMPTY_SLOT_VALUES = {"null", "none", "unknown", "未指定", "不知道", "不确定", "无"}


def build_intent_system_prompt() -> str:
    schema_lines: list[str] = []
    for intent in INTENT_SCHEMAS.values():
        schema_lines.append(f"- {intent.name}: {intent.description}")
        if intent.slots:
            schema_lines.append("  槽位：")
            for slot in intent.slots:
                requirement = "必填" if slot.required else "可选"
                invalid = f" 无效值={sorted(slot.invalid_values)}" if slot.invalid_values else ""
                schema_lines.append(f"    - {slot.name} ({requirement}): {slot.description}{invalid}")

    output_slots = {slot_name: None for slot_name in _all_slot_names()}
    return (
        INTENT_CONFIG.system_prompt
        + "\n意图和槽位结构：\n"
        + "\n".join(schema_lines)
        + "\n\n必须返回以下 JSON 结构：\n"
        + json.dumps(
            {
                "intent": "general_chat | write_code | unknown",
                "confidence": 0.0,
                "slots": output_slots,
                "missing_slots": [],
                "follow_up_question": None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def analyze_intent(
    messages: list[ChatMessage],
    active_task: ActiveTaskState | None = None,
) -> IntentDecision:
    system_prompt = build_intent_system_prompt()
    user_prompt = _build_intent_user_prompt(messages, active_task=active_task)
    last_error: Exception | None = None
    raw = ""

    for attempt in range(1, INTENT_PARSE_RETRIES + 1):
        prompt = user_prompt
        if attempt > 1:
            prompt = (
                f"上一次 JSON 响应无法解析。错误：{last_error}\n"
                "请返回一个完整、合法、可被 json.loads 解析的 JSON 对象。不要使用 Markdown，不要解释。\n\n"
                f"{user_prompt}"
            )

        raw = primary_chat_model.chat(
            [
                ChatMessage(role="system", content=system_prompt),
                ChatMessage(role="user", content=prompt),
            ],
            cfg=INTENT_CONFIG,
        )
        try:
            data = _parse_json_object(raw)
            return _normalize_decision(data, active_task=active_task)
        except RemoteModelError as exc:
            last_error = exc
            logger.warning(
                "Intent parse attempt failed attempt=%d/%d raw_chars=%d error=%s raw_prefix=%s",
                attempt,
                INTENT_PARSE_RETRIES,
                len(raw),
                exc,
                raw[:300],
            )

    logger.error("Intent parsing failed after retries; falling back to general_chat. raw=%s", raw[:800])
    return fallback_general_chat_decision()


def fallback_general_chat_decision() -> IntentDecision:
    return IntentDecision(
        intent="general_chat",
        confidence=0.0,
        slots={slot_name: None for slot_name in _all_slot_names()},
        missing_slots=[],
        follow_up_question=None,
    )


def default_follow_up(intent: IntentName, missing_slots: list[str]) -> str:
    schema = INTENT_SCHEMAS.get(intent)
    if not schema:
        return FOLLOW_UP_DEFAULT
    questions = [
        slot.follow_up_question
        for slot in schema.slots
        if slot.name in missing_slots and slot.follow_up_question
    ]
    return " ".join(questions) or FOLLOW_UP_DEFAULT


def safe_slots_for_log(slots: dict[str, str | None]) -> dict[str, str | None]:
    return {key: (value[:80] if value else value) for key, value in slots.items()}


def _build_intent_user_prompt(messages: list[ChatMessage], active_task: ActiveTaskState | None = None) -> str:
    recent = messages[-INTENT_CONTEXT_LIMIT:]
    transcript = "\n".join(f"{message.role}: {message.content}" for message in recent)
    task_hint = ""
    if active_task:
        task_hint = (
            "\n\n当前存在未完成任务。若用户最新输入可以补全或细化该任务，请优先视为槽位补充。"
            "请合并已有槽位和新输入。\n"
            + json.dumps(
                {
                    "intent": active_task.intent,
                    "slots": active_task.slots,
                    "missing_slots": active_task.missing_slots,
                },
                ensure_ascii=False,
            )
        )
    return f"最近对话：\n{transcript}{task_hint}\n\n请识别用户最新意图并抽取槽位。只返回 JSON。"


def _parse_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```json"):
        text = text[7:].strip()
    if text.startswith("```"):
        text = text[3:].strip()
    if text.endswith("```"):
        text = text[:-3].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            logger.warning("Intent model did not return JSON: %s", raw[:500])
            raise RemoteModelError("Intent model did not return valid JSON")
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            logger.warning("Intent JSON parse failed: %s", raw[:500])
            raise RemoteModelError(f"Intent JSON parse failed: {exc}") from exc


def _normalize_decision(data: dict[str, Any], active_task: ActiveTaskState | None = None) -> IntentDecision:
    intent = _normalize_intent(data.get("intent"))
    if active_task and intent == "unknown":
        intent = active_task.intent
    schema = INTENT_SCHEMAS[intent]
    raw_slots = data.get("slots") if isinstance(data.get("slots"), dict) else {}
    slots = _normalize_slots(schema, raw_slots)
    if active_task and active_task.intent == intent:
        slots = _merge_active_slots(schema, active_task.slots, slots)
    missing_slots = _missing_required_slots(schema, slots)

    confidence = _normalize_confidence(data.get("confidence"))
    follow_up_question = _clean_slot(data.get("follow_up_question"))
    if missing_slots:
        follow_up_question = follow_up_question or default_follow_up(intent, missing_slots)

    return IntentDecision(
        intent=intent,
        confidence=confidence,
        slots=slots,
        missing_slots=missing_slots,
        follow_up_question=follow_up_question,
    )


def _merge_active_slots(
    schema: IntentSchema,
    active_slots: dict[str, str | None],
    new_slots: dict[str, str | None],
) -> dict[str, str | None]:
    merged = {slot_name: None for slot_name in _all_slot_names()}
    for slot_schema in schema.slots:
        active_value = _clean_slot(active_slots.get(slot_schema.name))
        new_value = _clean_slot(new_slots.get(slot_schema.name))
        value = new_value or active_value
        if value and value in slot_schema.invalid_values:
            value = None
        merged[slot_schema.name] = value
    return merged


def _normalize_intent(value: Any) -> IntentName:
    intent = str(value).strip() if value is not None else "unknown"
    if intent not in INTENT_SCHEMAS:
        return "unknown"
    return intent  # type: ignore[return-value]


def _normalize_slots(schema: IntentSchema, raw_slots: dict[str, Any]) -> dict[str, str | None]:
    slots = {slot_name: None for slot_name in _all_slot_names()}
    for slot_schema in schema.slots:
        value = _clean_slot(raw_slots.get(slot_schema.name))
        if value and value in slot_schema.invalid_values:
            value = None
        slots[slot_schema.name] = value
    return slots


def _missing_required_slots(schema: IntentSchema, slots: dict[str, str | None]) -> list[str]:
    return [slot_name for slot_name in schema.required_slot_names if not slots.get(slot_name)]


def _normalize_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 0.0
    return max(0.0, min(1.0, confidence))


def _clean_slot(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in EMPTY_SLOT_VALUES:
        return None
    return text


def _all_slot_names() -> list[str]:
    names: list[str] = []
    for schema in INTENT_SCHEMAS.values():
        for slot in schema.slots:
            if slot.name not in names:
                names.append(slot.name)
    return names