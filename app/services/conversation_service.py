from __future__ import annotations

from datetime import UTC, datetime
import logging
import re
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..model_service import RemoteModelError
from ..models import Conversation, ConversationTask, Message
from ..schemas import (
    ChatMessage,
    CodeFile,
    ConversationChatRequest,
    ConversationChatResponse,
    ConversationCreate,
    ConversationDetail,
    ConversationSummary,
    StoredMessage,
    WorkspaceState,
)
from . import intent_service, model_router_service, patch_service, planner_service
from .service_configs import WORKSPACE_CONTEXT_PROMPT
from ..context import current_file_search


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CONTEXT_MESSAGE_LIMIT = 20
TASK_STATUS_PENDING = "pending_slots"
TASK_STATUS_READY = "ready"
TASK_STATUS_COMPLETED = "completed"
CODE_BLOCK_RE = re.compile(r"```([^`\n]*)\n([\s\S]*?)```", re.MULTILINE)
LANG_EXTENSIONS = {
    "python": "py",
    "py": "py",
    "javascript": "js",
    "js": "js",
    "typescript": "ts",
    "ts": "ts",
    "json": "json",
    "html": "html",
    "css": "css",
    "bash": "sh",
    "shell": "sh",
    "sql": "sql",
    "text": "txt",
}


def _get_pending_task(db: Session, conversation_id: int, active_file: str | None = None) -> ConversationTask | None:
    query = select(ConversationTask).where(
        ConversationTask.conversation_id == conversation_id,
        ConversationTask.status == TASK_STATUS_PENDING,
    )
    if active_file:
        query = query.where(ConversationTask.active_file == active_file)
    return db.scalar(query.order_by(ConversationTask.id.desc()).limit(1))


def _task_state(task: ConversationTask | None) -> intent_service.ActiveTaskState | None:
    if task is None:
        return None
    return intent_service.ActiveTaskState(
        intent=task.intent,  # type: ignore[arg-type]
        slots=task.slots_json or {},
        missing_slots=task.missing_slots_json or [],
    )


def _sync_task_state(
    db: Session,
    conversation_id: int,
    active_file: str | None,
    active_task: ConversationTask | None,
    result: model_router_service.IntentChatResult,
) -> None:
    decision = result.decision
    if decision is None or not intent_service.is_code_intent(decision.intent):
        return

    if decision.missing_slots:
        status = TASK_STATUS_PENDING
    elif result.executed:
        status = TASK_STATUS_COMPLETED
    else:
        status = TASK_STATUS_READY

    task = active_task or ConversationTask(conversation_id=conversation_id, intent=decision.intent)
    task.intent = decision.intent
    task.active_file = active_file
    task.status = status
    task.slots_json = decision.slots
    task.missing_slots_json = decision.missing_slots
    touch(task)
    db.add(task)
    logger.info(
        "Synced conversation task conversation_id=%s task_id=%s active_file=%s intent=%s status=%s missing_slots=%s",
        conversation_id,
        task.id,
        task.active_file,
        task.intent,
        task.status,
        task.missing_slots_json,
    )


def conversation_summary(conversation: Conversation) -> ConversationSummary:
    return ConversationSummary(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


def stored_message(message: Message) -> StoredMessage:
    return StoredMessage(
        id=message.id,
        role=message.role,  # type: ignore[arg-type]
        content=message.content,
        created_at=message.created_at,
        active_file=message.active_file,
    )


def _normalise_file(raw: CodeFile | dict, fallback_path: str | None = None) -> dict:
    if isinstance(raw, CodeFile):
        item = raw.model_dump()
    else:
        item = dict(raw)
    path = str(item.get("path") or item.get("name") or fallback_path or "generated.txt").strip()
    language = str(item.get("language") or _language_from_path(path) or "text").strip().lower()
    content = str(item.get("content") or "")
    return {"path": path, "language": language, "content": content}


def _normalise_files(files: list[CodeFile] | list[dict] | None) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    for index, file in enumerate(files or []):
        item = _normalise_file(file, fallback_path=f"generated_{index + 1}.txt")
        if not item["path"] or item["path"] in seen:
            continue
        seen.add(item["path"])
        result.append(item)
    return result


def _language_from_path(path: str) -> str:
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    for language, ext in LANG_EXTENSIONS.items():
        if suffix == ext:
            return language
    return "text"


def _request_workspace(request: ConversationChatRequest) -> WorkspaceState | None:
    files = _normalise_files(request.current_files)
    if not files:
        return None
    active_file = request.active_file or files[0]["path"]
    return WorkspaceState(files=[CodeFile(**file) for file in files], active_file=active_file)



def _active_file_for_request(request: ConversationChatRequest, workspace: WorkspaceState | None = None) -> str | None:
    if request.active_file:
        return request.active_file
    if workspace is not None and workspace.active_file:
        return workspace.active_file
    files = _normalise_files(request.current_files)
    return files[0]["path"] if files else None

def _workspace_context_message(
    workspace: WorkspaceState | None,
    latest_user: str = "",
    preferred_symbols: list[str] | None = None,
) -> ChatMessage | None:
    if workspace is None or not workspace.files:
        return None
    search_result = current_file_search.search_current_file(
        workspace,
        latest_user,
        preferred_symbols=preferred_symbols,
    )
    tool_text = current_file_search.format_tool_result(search_result)
    content = WORKSPACE_CONTEXT_PROMPT + "\n\n" + tool_text
    return ChatMessage(role="system", content=content)


def conversation_detail(conversation: Conversation, db: Session | None = None) -> ConversationDetail:
    return ConversationDetail(
        **conversation_summary(conversation).model_dump(),
        messages=[stored_message(message) for message in conversation.messages],
        workspace=None,
    )


def title_from_content(content: str) -> str:
    title = " ".join(content.strip().split())[:60]
    return title or "New conversation"


def touch(conversation: Conversation | ConversationTask) -> None:
    conversation.updated_at = datetime.now(UTC).astimezone(ZoneInfo("Asia/Shanghai"))


def list_conversations(db: Session) -> list[ConversationSummary]:
    logger.info("Listing conversations")
    conversations = db.scalars(select(Conversation).order_by(Conversation.updated_at.desc())).all()
    logger.info("Listed conversations count=%d", len(conversations))
    return [conversation_summary(conversation) for conversation in conversations]


def create_conversation(
    db: Session,
    request: ConversationCreate | None = None,
) -> ConversationDetail:
    title = request.title.strip() if request and request.title else "New conversation"
    logger.info("Creating conversation title=%s", title)
    conversation = Conversation(title=title)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    logger.info("Created conversation id=%s", conversation.id)
    return conversation_detail(conversation, db)


def get_conversation(db: Session, conversation_id: int) -> ConversationDetail:
    logger.info("Loading conversation id=%s", conversation_id)
    conversation = db.scalar(
        select(Conversation)
        .where(Conversation.id == conversation_id)
        .options(selectinload(Conversation.messages))
    )
    if conversation is None:
        logger.warning("Conversation not found id=%s", conversation_id)
        raise HTTPException(status_code=404, detail="Conversation not found")
    logger.info("Loaded conversation id=%s messages=%d", conversation.id, len(conversation.messages))
    return conversation_detail(conversation, db)


def delete_conversation(db: Session, conversation_id: int) -> None:
    logger.info("Deleting conversation id=%s", conversation_id)
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        logger.warning("Conversation not found for delete id=%s", conversation_id)
        raise HTTPException(status_code=404, detail="Conversation not found")
    db.delete(conversation)
    db.commit()
    logger.info("Deleted conversation id=%s", conversation_id)


def _latest_user_intent_messages(content: str) -> list[ChatMessage]:
    return [ChatMessage(role="user", content=content)]

def _load_prompt_messages(
    db: Session,
    conversation: Conversation,
    workspace: WorkspaceState | None,
    latest_user: str = "",
    preferred_symbols: list[str] | None = None,
    active_file: str | None = None,
) -> list[ChatMessage]:
    query = select(Message).where(Message.conversation_id == conversation.id)
    if active_file:
        query = query.where(Message.active_file == active_file)
    recent_messages = db.scalars(
        query.order_by(Message.id.desc()).limit(CONTEXT_MESSAGE_LIMIT + 1)
    ).all()
    history_messages = list(reversed(recent_messages))
    prompt_messages = [
        ChatMessage(role=message.role, content=message.content)  # type: ignore[arg-type]
        for message in history_messages
        if message.role in {"user", "assistant", "system"}
    ]
    while len(prompt_messages) > CONTEXT_MESSAGE_LIMIT:
        prompt_messages.pop(0)
    while prompt_messages and prompt_messages[0].role == "assistant":
        prompt_messages.pop(0)
    workspace_message = _workspace_context_message(workspace, latest_user, preferred_symbols)
    if workspace_message is not None:
        prompt_messages.insert(0, workspace_message)
    logger.info("Prepared model context conversation_id=%s context_messages=%d", conversation.id, len(prompt_messages))
    return prompt_messages


def _append_planner_message(
    prompt_messages: list[ChatMessage],
    decision: intent_service.IntentDecision,
    workspace: WorkspaceState | None = None,
) -> list[ChatMessage]:
    if not intent_service.is_code_intent(decision.intent) or decision.missing_slots:
        return prompt_messages
    try:
        planner_message = planner_service.build_planner_message(decision, prompt_messages, workspace=workspace)
    except RemoteModelError:
        logger.warning("Planner model failed; continuing without implementation plan")
        return prompt_messages
    return [*prompt_messages, planner_message]

def _slot_symbols(decision: intent_service.IntentDecision | None) -> list[str]:
    if decision is None:
        return []
    raw = decision.slots.get("search_symbols") or ""
    symbols = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", raw)
    function_name = decision.slots.get("function_name") or ""
    if function_name:
        symbols.extend(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", function_name))
    result: list[str] = []
    for symbol in symbols:
        if symbol not in result:
            result.append(symbol)
    return result

def chat_in_conversation(
    db: Session,
    conversation_id: int,
    request: ConversationChatRequest,
) -> ConversationChatResponse:
    logger.info(
        "Chat request received conversation_id=%s content_chars=%d current_files=%d",
        conversation_id,
        len(request.content),
        len(request.current_files),
    )
    conversation = db.scalar(
        select(Conversation)
        .where(Conversation.id == conversation_id)
        .options(selectinload(Conversation.messages))
    )
    if conversation is None:
        logger.warning("Conversation not found for chat id=%s", conversation_id)
        raise HTTPException(status_code=404, detail="Conversation not found")

    had_messages = bool(conversation.messages)
    active_file = _active_file_for_request(request)
    user_message = Message(conversation_id=conversation.id, role="user", content=request.content, active_file=active_file)
    conversation.messages.append(user_message)
    if not had_messages and conversation.title == "New conversation":
        conversation.title = title_from_content(request.content)
    touch(conversation)
    db.commit()
    db.refresh(user_message)
    logger.info("Saved user message conversation_id=%s message_id=%s", conversation.id, user_message.id)

    request_workspace = _request_workspace(request)
    workspace = request_workspace
    active_file = _active_file_for_request(request, workspace)

    active_task = _get_pending_task(db, conversation.id, active_file)
    active_task_state = _task_state(active_task)
    intent_prompt_messages = _latest_user_intent_messages(request.content)
    decision = intent_service.analyze_intent(intent_prompt_messages, active_task=active_task_state)
    logger.info(
        "Precomputed intent decision intent=%s confidence=%.2f missing_slots=%s slots=%s active_task=%s",
        decision.intent,
        decision.confidence,
        decision.missing_slots,
        intent_service.safe_slots_for_log(decision.slots),
        bool(active_task),
    )
    prompt_messages = _load_prompt_messages(
        db,
        conversation,
        workspace,
        request.content,
        preferred_symbols=_slot_symbols(decision),
        active_file=active_file,
    )
    prompt_messages = _append_planner_message(prompt_messages, decision, workspace)
    try:
        result = model_router_service.handle_chat(
            prompt_messages,
            active_task=active_task_state,
            decision=decision,
            workspace=workspace,
        )
        answer = result.content
    except RemoteModelError as exc:
        logger.exception("Model call failed conversation_id=%s", conversation.id)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unexpected chat failure conversation_id=%s", conversation.id)
        raise HTTPException(status_code=500, detail=f"Conversation chat failed: {exc}") from exc

    display_answer = answer
    patch = patch_service.propose_patch(request.content, workspace, display_answer, result.decision) if result.executed else None
    assistant_message = Message(conversation_id=conversation.id, role="assistant", content=display_answer, active_file=active_file)
    conversation.messages.append(assistant_message)
    _sync_task_state(db, conversation.id, active_file, active_task, result)
    touch(conversation)
    db.commit()
    db.refresh(assistant_message)
    db.refresh(conversation)
    logger.info(
        "Saved assistant message conversation_id=%s message_id=%s answer_chars=%d",
        conversation.id,
        assistant_message.id,
        len(display_answer),
    )

    return ConversationChatResponse(
        conversation=conversation_summary(conversation),
        message=stored_message(assistant_message),
        workspace=workspace,
        patch=patch,
    )


def stream_chat_in_conversation(
    db: Session,
    conversation_id: int,
    request: ConversationChatRequest,
):
    logger.info(
        "Streaming chat request received conversation_id=%s content_chars=%d current_files=%d",
        conversation_id,
        len(request.content),
        len(request.current_files),
    )
    conversation = db.scalar(
        select(Conversation)
        .where(Conversation.id == conversation_id)
        .options(selectinload(Conversation.messages))
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    had_messages = bool(conversation.messages)
    active_file = _active_file_for_request(request)
    user_message = Message(conversation_id=conversation.id, role="user", content=request.content, active_file=active_file)
    conversation.messages.append(user_message)
    if not had_messages and conversation.title == "New conversation":
        conversation.title = title_from_content(request.content)
    touch(conversation)
    db.commit()
    db.refresh(user_message)

    request_workspace = _request_workspace(request)
    workspace = request_workspace
    active_file = _active_file_for_request(request, workspace)

    yield {"type": "conversation", "conversation": conversation_summary(conversation).model_dump(mode="json")}

    chunks: list[str] = []
    result: model_router_service.IntentChatResult | None = None
    active_task = _get_pending_task(db, conversation.id, active_file)
    active_task_state = _task_state(active_task)
    intent_prompt_messages = _latest_user_intent_messages(request.content)
    decision = intent_service.analyze_intent(intent_prompt_messages, active_task=active_task_state)
    logger.info(
        "Precomputed intent decision intent=%s confidence=%.2f missing_slots=%s slots=%s active_task=%s",
        decision.intent,
        decision.confidence,
        decision.missing_slots,
        intent_service.safe_slots_for_log(decision.slots),
        bool(active_task),
    )
    prompt_messages = _load_prompt_messages(
        db,
        conversation,
        workspace,
        request.content,
        preferred_symbols=_slot_symbols(decision),
        active_file=active_file,
    )
    prompt_messages = _append_planner_message(prompt_messages, decision, workspace)
    try:
        for event in model_router_service.stream_handle_chat(
            prompt_messages,
            active_task=active_task_state,
            decision=decision,
            workspace=workspace,
        ):
            if event.content:
                chunks.append(event.content)
                yield {"type": "delta", "content": event.content}
            if event.result is not None:
                result = event.result
    except RemoteModelError as exc:
        logger.exception("Streaming model call failed conversation_id=%s", conversation.id)
        yield {"type": "error", "detail": str(exc)}
        return
    except Exception as exc:
        logger.exception("Unexpected streaming chat failure conversation_id=%s", conversation.id)
        yield {"type": "error", "detail": f"Conversation stream failed: {exc}"}
        return

    answer = "".join(chunks).strip()
    display_answer = answer
    if result is None:
        result = model_router_service.IntentChatResult(content=answer, decision=None, executed=False)
    patch = patch_service.propose_patch(request.content, workspace, display_answer, result.decision) if result.executed else None
    assistant_message = Message(conversation_id=conversation.id, role="assistant", content=display_answer, active_file=active_file)
    conversation.messages.append(assistant_message)
    _sync_task_state(db, conversation.id, active_file, active_task, result)
    touch(conversation)
    db.commit()
    db.refresh(assistant_message)
    db.refresh(conversation)
    yield {
        "type": "done",
        "conversation": conversation_summary(conversation).model_dump(mode="json"),
        "message": stored_message(assistant_message).model_dump(mode="json"),
        "workspace": workspace.model_dump(mode="json") if workspace is not None else None,
        "patch": patch.model_dump(mode="json") if patch is not None else None,
    }
