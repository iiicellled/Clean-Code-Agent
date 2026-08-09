from __future__ import annotations

from typing import TypedDict

from ..schemas import ChatMessage, CodePatchProposal, WorkspaceState
from ..services.intent_service import ActiveTaskState, IntentDecision


class CoderAgentState(TypedDict, total=False):
    messages: list[ChatMessage]             # 历史对话记录(如果打开文件, 只算此文件的对话)
    workspace: WorkspaceState | None        # 工作区有哪些文件、当前文件
    active_task: ActiveTaskState | None     # 当前active的task
    decision: IntentDecision | None         # 意图, 包括意图、可信度(没用)、槽位(包括搜寻目标)、缺槽、跟进提问
    user_request: str                       # 用户输入
    planner_messages: list[ChatMessage]     # messages.extend(plan)
    raw_code: str                           # Coder原版代码
    content: str                            # Reviewer生成的最终聊天输出
    executed: bool                          # 
    patch: CodePatchProposal | None         # Patch
