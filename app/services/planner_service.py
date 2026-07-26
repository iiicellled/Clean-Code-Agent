from __future__ import annotations

import logging

from ..model_service import RemoteModelError, primary_chat_model
from ..schemas import ChatMessage, WorkspaceState
from ..tools import agent_search_tool
from .intent_service import IntentDecision
from .service_configs import (
    PLANNER_COMPACT_SYSTEM_PROMPT,
    PLANNER_COMPACT_USER_PROMPT_TEMPLATE,
    PLANNER_CONFIG,
    PLANNER_CONTEXT_PREFIX,
    PLANNER_FALLBACK_INSUFFICIENT_CONTEXT_LINE,
    PLANNER_FALLBACK_PLAN_LINES,
    PLANNER_USER_PROMPT_TEMPLATE,
    ServiceModelConfig,
    WORKSPACE_CONTEXT_PREFIX,
)


logger = logging.getLogger(__name__)
MAX_PLANNER_TOOL_CALLS = 2


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
        plan = _call_planner_with_tools(planner_messages, workspace, config).strip()
    except RemoteModelError as exc:
        logger.warning("Planner model returned no usable plan; retrying with compact planner prompt. error=%s", exc)
        plan = _retry_compact_plan(decision, latest_user, workspace_context, config)
    if not plan.strip():
        logger.warning("Planner model returned blank plan text; using fallback plan")
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


def _fallback_plan(decision: IntentDecision, latest_user: str, workspace_context: str) -> str:
    slots = decision.slots
    function_name = slots.get("function_name") or "逶ｮ譬・・謨ｰ"
    parameters = slots.get("parameters") or "謖臥畑謌ｷ隕∵ｱら｡ｮ螳壼盾謨ｰ"
    task = slots.get("task") or latest_user or "螳梧・逕ｨ謌ｷ隸ｷ豎・"
    symbols = slots.get("search_symbols") or "蠖灘燕譁・ｻｶ譽邏｢扈捺棡荳ｭ逧・嶌蜈ｳ螳壻ｹ・"
    has_workspace_context = "Results:" in workspace_context and "No relevant snippets" not in workspace_context
    lines = [
        line.format(
            function_name=function_name,
            parameters=parameters,
            task=task,
            symbols=symbols,
        )
        for line in PLANNER_FALLBACK_PLAN_LINES
    ]
    if not has_workspace_context:
        lines.append(PLANNER_FALLBACK_INSUFFICIENT_CONTEXT_LINE)
    return "\n".join(lines)