from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..model_service import RemoteModelError, primary_chat_model
from ..schemas import ChatMessage, WorkspaceState
from ..tools import agent_search_tool
from .intent_service import IntentDecision
from .service_configs import (
    PLANNER_COMPACT_SYSTEM_PROMPT,
    PLANNER_COMPACT_USER_PROMPT_TEMPLATE,
    PLANNER_CONFIG,
    PLANNER_CONTEXT_PREFIX,
    PLANNER_USER_PROMPT_TEMPLATE,
    ServiceModelConfig,
    WORKSPACE_CONTEXT_PREFIX,
)


logger = logging.getLogger(__name__)
MAX_PLANNER_TOOL_CALLS = 6
MAX_CODE_FACT_CHARS = 3200
REQUIRED_PLAN_KEYS = {
    "target",
    "current_code_facts",
    "required_changes",
    "constraints",
    "uncertainties",
}


def build_planner_message(
    decision: IntentDecision,
    messages: list[ChatMessage],
    workspace: WorkspaceState | None = None,
    config: ServiceModelConfig = PLANNER_CONFIG,
) -> ChatMessage:
    plan = plan_code_change(decision, messages, workspace=workspace, config=config)
    return ChatMessage(role="system", content=f"{PLANNER_CONTEXT_PREFIX}\n{plan}")


def plan_code_change(
    decision: IntentDecision,
    messages: list[ChatMessage],
    workspace: WorkspaceState | None = None,
    config: ServiceModelConfig = PLANNER_CONFIG,
) -> str:
    latest_user = next((message.content for message in reversed(messages) if message.role == "user"), "")
    workspace_context = next((
        message.content for message in messages
        if message.role == "system" and message.content.startswith(WORKSPACE_CONTEXT_PREFIX)
    ), "")
    user_prompt = PLANNER_USER_PROMPT_TEMPLATE.format(
        intent=decision.intent,
        slots=decision.slots,
        latest_user=latest_user,
        workspace_context=workspace_context[:7000],
    )
    if workspace is not None:
        user_prompt += "\n\n" + agent_search_tool.SEARCH_TOOL_INSTRUCTIONS
    planner_messages = [
        ChatMessage(role="system", content=config.system_prompt),
        ChatMessage(role="user", content=user_prompt),
    ]
    logger.info(
        "Planner prompt intent=%s slots=%s system_prompt=%r user_prompt=%r",
        decision.intent,
        decision.slots,
        planner_messages[0].content,
        planner_messages[1].content,
    )
    try:
        raw_plan = _call_planner_with_tools(planner_messages, workspace, config).strip()
    except RemoteModelError as exc:
        logger.warning("Planner model returned no usable plan; retrying with compact planner prompt. error=%s", exc)
        raw_plan = _retry_compact_plan(decision, latest_user, workspace_context, config)

    plan = _normalise_plan_json(raw_plan, decision, latest_user, workspace_context)
    if not plan.strip():
        logger.warning("Planner model returned blank plan text; using fallback JSON plan")
        plan = _fallback_plan(decision, latest_user, workspace_context)
    logger.info("Planner output intent=%s plan=%r", decision.intent, plan)
    return plan


def _call_planner_with_tools(
    planner_messages: list[ChatMessage],
    workspace: WorkspaceState | None,
    config: ServiceModelConfig,
) -> str:
    if workspace is None:
        return primary_chat_model.chat(planner_messages, cfg=config)
    tool = agent_search_tool.build_search_workspace_tool(workspace)
    return primary_chat_model.chat_with_tools(
        planner_messages,
        cfg=config,
        tools=[tool],
        max_tool_calls=MAX_PLANNER_TOOL_CALLS,
    )


def _retry_compact_plan(
    decision: IntentDecision,
    latest_user: str,
    workspace_context: str,
    config: ServiceModelConfig,
) -> str:
    compact_prompt = PLANNER_COMPACT_USER_PROMPT_TEMPLATE.format(
        intent=decision.intent,
        slots=decision.slots,
        latest_user=latest_user,
        workspace_context=workspace_context[:2500],
    )
    retry_messages = [
        ChatMessage(role="system", content=PLANNER_COMPACT_SYSTEM_PROMPT),
        ChatMessage(role="user", content=compact_prompt),
    ]
    logger.info(
        "Planner compact retry prompt intent=%s system_prompt=%r user_prompt=%r",
        decision.intent,
        retry_messages[0].content,
        retry_messages[1].content,
    )
    try:
        return primary_chat_model.chat(retry_messages, cfg=config).strip()
    except RemoteModelError as retry_exc:
        logger.warning("Planner compact retry failed; using fallback plan. error=%s", retry_exc)
        return _fallback_plan(decision, latest_user, workspace_context)


def _normalise_plan_json(
    raw_plan: str,
    decision: IntentDecision,
    latest_user: str,
    workspace_context: str,
) -> str:
    if not raw_plan.strip():
        return ""
    data = _parse_json_object(raw_plan)
    if data is None:
        logger.warning("Planner did not return JSON; wrapping output in fallback JSON plan")
        return _fallback_plan(decision, latest_user, workspace_context, planner_notes=raw_plan)
    if not isinstance(data, dict):
        return _fallback_plan(decision, latest_user, workspace_context, planner_notes=str(data))

    fallback = json.loads(_fallback_plan(decision, latest_user, workspace_context))
    normalised = {**fallback, **data}
    normalised["target"] = _normalise_target(normalised.get("target"), fallback["target"], decision)
    normalised["current_code_facts"] = _normalise_string_list(
        normalised.get("current_code_facts"), fallback["current_code_facts"]
    )
    normalised["required_changes"] = _normalise_string_list(
        normalised.get("required_changes"), fallback["required_changes"]
    )
    normalised["constraints"] = _without_line_number_instructions(
        _normalise_string_list(normalised.get("constraints"), fallback["constraints"])
    )
    normalised["uncertainties"] = _normalise_string_list(
        normalised.get("uncertainties"), fallback["uncertainties"]
    )
    if "implementation_notes" in normalised:
        normalised["implementation_notes"] = _normalise_string_list(normalised.get("implementation_notes"), [])
    normalised["insufficient_context"] = bool(normalised.get("insufficient_context", False))
    return json.dumps(normalised, ensure_ascii=False, indent=2)


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    if text.startswith("```json"):
        text = text[7:].strip()
    elif text.startswith("```"):
        text = text[3:].strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _fallback_plan(
    decision: IntentDecision,
    latest_user: str,
    workspace_context: str,
    planner_notes: str = "",
) -> str:
    slots = decision.slots
    function_name = slots.get("function_name") or "target_symbol"
    parameters = slots.get("parameters") or "除非用户明确指定，否则保持现有参数"
    task = slots.get("task") or latest_user or "实现用户请求的代码修改。"
    symbols = slots.get("search_symbols") or function_name
    current_code_facts = _extract_relevant_code_facts(workspace_context)
    has_workspace_context = bool(current_code_facts)
    constraints = [
        "不要使用行号作为修改指令。",
        "使用符号名、函数/类签名和可见代码片段定位修改目标。",
        "不要重写无关函数、类、import 或文件。",
        "除非用户明确要求，否则保持现有公开函数签名。",
    ]
    plan: dict[str, Any] = {
        "target": {
            "action": decision.intent,
            "symbol": function_name,
            "parameters": parameters,
            "search_symbols": symbols,
        },
        "current_code_facts": current_code_facts,
        "required_changes": [task],
        "constraints": constraints,
        "uncertainties": [],
        "insufficient_context": not has_workspace_context,
    }
    if planner_notes.strip():
        plan["implementation_notes"] = [planner_notes.strip()[:1200]]
    if not has_workspace_context:
        plan["uncertainties"].append(
            "没有找到可靠的工作区代码片段；请根据用户需求和目标符号保守实现。"
        )
    return json.dumps(plan, ensure_ascii=False, indent=2)


def _normalise_target(value: Any, fallback: dict[str, Any], decision: IntentDecision) -> dict[str, Any]:
    if isinstance(value, dict):
        target = {**fallback, **value}
    else:
        target = dict(fallback)
    target["action"] = str(target.get("action") or decision.intent)
    target["symbol"] = str(target.get("symbol") or target.get("function_name") or fallback.get("symbol") or "target_symbol")
    return target


def _normalise_string_list(value: Any, fallback: list[str]) -> list[str]:
    if isinstance(value, list):
        result = [str(item).strip() for item in value if str(item).strip()]
    elif isinstance(value, str) and value.strip():
        result = [value.strip()]
    else:
        result = []
    return result or list(fallback)


def _without_line_number_instructions(items: list[str]) -> list[str]:
    result = [
        item
        for item in items
        if not re.search(r"\bline\s+\d+\b|第\s*\d+\s*行", item, flags=re.IGNORECASE)
    ]
    guardrail = "不要使用行号作为修改指令；请改用准确的可见代码片段。"
    if guardrail not in result:
        result.insert(0, guardrail)
    return result


def _extract_relevant_code_facts(workspace_context: str) -> list[str]:
    if not workspace_context.strip():
        return []
    code_blocks = re.findall(r"```(?:\w+)?\s*\n([\s\S]*?)```", workspace_context)
    facts: list[str] = []
    for block in code_blocks:
        cleaned = block.strip()
        if cleaned and cleaned not in facts:
            facts.append(cleaned[:MAX_CODE_FACT_CHARS])
        if len(facts) >= 3:
            return facts

    lines = []
    for line in workspace_context.splitlines():
        stripped = line.rstrip()
        if stripped and not stripped.startswith(WORKSPACE_CONTEXT_PREFIX):
            lines.append(stripped)
        if sum(len(item) for item in lines) >= MAX_CODE_FACT_CHARS:
            break
    fallback = "\n".join(lines).strip()
    return [fallback[:MAX_CODE_FACT_CHARS]] if fallback else []