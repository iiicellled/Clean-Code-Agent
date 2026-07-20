from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceModelConfig:
    temperature: float | None = None
    top_p: float | None = None
    max_new_tokens: int | None = None
    min_new_tokens: int | None = None
    system_prompt: str = ""


INTENT_CONFIG = ServiceModelConfig(
    temperature=0.0,
    top_p=1.0,
    max_new_tokens=1600,
    system_prompt=(
        "你是一个意图识别与槽位抽取模块，只能返回严格 JSON。\n"
        "请根据最近对话判断用户最新输入的意图。如果用户正在补充一个未完成任务的缺失槽位，请合并已有槽位和新输入。\n"
        "如果必填槽位缺失或过于模糊，必须把它放入 missing_slots，并给出 follow_up_question。\n"
        "槽位值请使用简短字符串；不确定的槽位值必须为 null。不要使用 Markdown。"
    ),
)

CHATBOT_CONFIG = ServiceModelConfig(
    temperature=0.7,
    top_p=0.9,
    max_new_tokens=2048,
    system_prompt=(
        "你是一个乐于助人的代码编程专家。请语气自然、条理清晰地回答用户的问题。"
    ),
)

CODER_CONFIG = ServiceModelConfig(
    temperature=0.2,
    top_p=0.9,
    max_new_tokens=2048,
    system_prompt=(
        "你是一个专注的代码生成模型。请只返回简洁、可读、可运行的代码。"
        "不要使用 Markdown 代码围栏。除非注释是代码理解所必需的，否则不要添加解释。"
    ),
)

CODE_REVIEW_CONFIG = ServiceModelConfig(
    temperature=0.1,
    top_p=1.0,
    max_new_tokens=4096,
    system_prompt=(
        "你是一个严谨的 Python 代码审阅与整理助手。"
        "请根据用户需求检查候选代码，并直接修正明显错误、遗漏的边界条件、薄弱的异常处理和不清晰的命名。"
        "最终回答要像正常助手回复一样自然，不要提到内部模型、路由、审阅流程或候选代码来源。"
    ),
)


CODER_USER_PROMPT_TEMPLATE = (
    "请根据下面的结构化需求生成代码。\n"
    "编程语言：{language}\n"
    "任务：{task}\n"
    "约束：{constraints}\n"
    "用户最新输入：{latest_user}\n\n"
    "请只返回简洁、可读、可运行的代码。不要使用 Markdown 代码围栏。"
    "除非注释是代码理解所必需的，否则不要添加解释。"
)

CODE_REVIEW_USER_PROMPT_TEMPLATE = (
    "请根据用户需求审阅并整理下面的候选代码。\n\n"
    "编程语言：{language}\n"
    "任务：{task}\n"
    "约束：{constraints}\n"
    "用户最新输入：{latest_user}\n\n"
    "候选代码：\n"
    "```python\n"
    "{raw_code}\n"
    "```\n\n"
    "请完成：\n"
    "1. 检查代码是否满足用户需求；如果有明显错误或边界条件缺失，请直接修正。\n"
    "2. 尽量保持原始核心思路，避免做不必要的大幅重写。\n"
    "3. 最终代码必须放在 Markdown 的 ```python 代码围栏中。\n"
    "4. 在代码中添加必要、清楚的中文注释，但不要每一行都写注释。\n"
    "5. 代码后用简短中文说明核心逻辑和关键修正。\n"
    "6. 不要输出审阅清单，不要提到候选代码或内部模型。\n"
)
