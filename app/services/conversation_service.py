from __future__ import annotations

from datetime import UTC, datetime
import logging
import re
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..model_service import RemoteModelError
from ..models import CodeSnapshot, Conversation, ConversationTask, Message
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
from . import intent_service, model_router_service


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


def _get_pending_task(db: Session, conversation_id: int) -> ConversationTask | None:
    return db.scalar(
        select(ConversationTask)
        .where(
            ConversationTask.conversation_id == conversation_id,
            ConversationTask.status == TASK_STATUS_PENDING,
        )
        .order_by(ConversationTask.id.desc())
        .limit(1)
    )


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
    active_task: ConversationTask | None,
    result: model_router_service.IntentChatResult,
) -> None:
    decision = result.decision
    if decision is None or decision.intent != "write_code":
        return

    if decision.missing_slots:
        status = TASK_STATUS_PENDING
    elif result.executed:
        status = TASK_STATUS_COMPLETED
    else:
        status = TASK_STATUS_READY

    task = active_task or ConversationTask(conversation_id=conversation_id, intent=decision.intent)
    task.intent = decision.intent
    task.status = status
    task.slots_json = decision.slots
    task.missing_slots_json = decision.missing_slots
    touch(task)
    db.add(task)
    logger.info(
        "Synced conversation task conversation_id=%s task_id=%s intent=%s status=%s missing_slots=%s",
        conversation_id,
        task.id,
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


def _message_snapshot_id(db: Session, message_id: int) -> int | None:
    snapshot = db.scalar(
        select(CodeSnapshot)
        .where(CodeSnapshot.message_id == message_id)
        .order_by(CodeSnapshot.id.desc())
        .limit(1)
    )
    return snapshot.id if snapshot else None


def stored_message(message: Message, db: Session | None = None) -> StoredMessage:
    return StoredMessage(
        id=message.id,
        role=message.role,  # type: ignore[arg-type]
        content=message.content,
        created_at=message.created_at,
        code_snapshot_id=_message_snapshot_id(db, message.id) if db is not None else None,
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


def _path_for_code_block(info: str, index: int) -> tuple[str, str]:
    tokens = [token for token in info.strip().split() if token]
    language = (tokens[0].lower() if tokens else "text").strip("{}[]()") or "text"
    path = next((token for token in tokens[1:] if "." in token or "/" in token or "\\" in token), "")
    if not path:
        extension = LANG_EXTENSIONS.get(language, "txt")
        path = f"generated_{index}.{extension}"
    return path, language


def extract_code_files(content: str) -> list[dict]:
    files: list[dict] = []
    for index, match in enumerate(CODE_BLOCK_RE.finditer(content), start=1):
        info = match.group(1) or ""
        code = match.group(2).strip("\n")
        if not code.strip():
            continue
        path, language = _path_for_code_block(info, index)
        files.append(_normalise_file({"path": path, "language": language, "content": code}))
    return files


def strip_code_blocks(content: str) -> str:
    cleaned = CODE_BLOCK_RE.sub("", content)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _latest_snapshot(db: Session, conversation_id: int) -> CodeSnapshot | None:
    return db.scalar(
        select(CodeSnapshot)
        .where(CodeSnapshot.conversation_id == conversation_id)
        .order_by(CodeSnapshot.id.desc())
        .limit(1)
    )


def _workspace_from_snapshot(snapshot: CodeSnapshot | None) -> WorkspaceState | None:
    if snapshot is None:
        return None
    return WorkspaceState(
        files=[CodeFile(**file) for file in _normalise_files(snapshot.files_json or [])],
        active_file=snapshot.active_file,
        snapshot_id=snapshot.id,
    )


def _request_workspace(request: ConversationChatRequest) -> WorkspaceState | None:
    files = _normalise_files(request.current_files)
    if not files:
        return None
    active_file = request.active_file or files[0]["path"]
    return WorkspaceState(files=[CodeFile(**file) for file in files], active_file=active_file)


def _save_snapshot(
    db: Session,
    conversation_id: int,
    files: list[CodeFile] | list[dict],
    active_file: str | None,
    created_by: str,
    message_id: int | None = None,
) -> CodeSnapshot | None:
    normalised = _normalise_files(files)
    if not normalised:
        return None
    snapshot = CodeSnapshot(
        conversation_id=conversation_id,
        message_id=message_id,
        files_json=normalised,
        active_file=active_file or normalised[0]["path"],
        created_by=created_by,
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def _workspace_context_message(workspace: WorkspaceState | None) -> ChatMessage | None:
    if workspace is None or not workspace.files:
        return None
    file_chunks = []
    for file in workspace.files:
        file_chunks.append(
            f"File: {file.path}\nLanguage: {file.language}\n```{file.language}\n{file.content}\n```"
        )
    active = workspace.active_file or workspace.files[0].path
    content = (
        "Current editable code workspace. Treat this as the source of truth for any code changes. "
        f"Active file: {active}\n\n" + "\n\n".join(file_chunks)
    )
    return ChatMessage(role="system", content=content)


def conversation_detail(conversation: Conversation, db: Session | None = None) -> ConversationDetail:
    return ConversationDetail(
        **conversation_summary(conversation).model_dump(),
        messages=[stored_message(message, db) for message in conversation.messages],
        workspace=_workspace_from_snapshot(_latest_snapshot(db, conversation.id)) if db is not None else None,
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


def _load_prompt_messages(
    db: Session,
    conversation: Conversation,
    workspace: WorkspaceState | None,
) -> list[ChatMessage]:
    recent_messages = db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.id.desc())
        .limit(CONTEXT_MESSAGE_LIMIT + 1)
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
    workspace_message = _workspace_context_message(workspace)
    if workspace_message is not None:
        prompt_messages.insert(0, workspace_message)
    logger.info("Prepared model context conversation_id=%s context_messages=%d", conversation.id, len(prompt_messages))
    return prompt_messages


def _finalise_answer(answer: str) -> tuple[str, list[dict]]:
    files = extract_code_files(answer)
    if not files:
        return answer, []
    cleaned = strip_code_blocks(answer)
    if cleaned:
        cleaned = f"{cleaned}\n\n代码已放到右侧代码区。"
    else:
        cleaned = "已生成代码，如右侧代码区所示。"
    return cleaned, files


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
    user_message = Message(conversation_id=conversation.id, role="user", content=request.content)
    conversation.messages.append(user_message)
    if not had_messages and conversation.title == "New conversation":
        conversation.title = title_from_content(request.content)
    touch(conversation)
    db.commit()
    db.refresh(user_message)
    logger.info("Saved user message conversation_id=%s message_id=%s", conversation.id, user_message.id)

    request_workspace = _request_workspace(request)
    if request_workspace is not None:
        _save_snapshot(db, conversation.id, request_workspace.files, request_workspace.active_file, "user", user_message.id)
        db.commit()
    workspace = request_workspace or _workspace_from_snapshot(_latest_snapshot(db, conversation.id))
    prompt_messages = _load_prompt_messages(db, conversation, workspace)

    active_task = _get_pending_task(db, conversation.id)
    active_task_state = _task_state(active_task)
    try:
        result = model_router_service.handle_chat(
            prompt_messages,
            active_task=active_task_state,
        )
        answer = result.content
    except RemoteModelError as exc:
        logger.exception("Model call failed conversation_id=%s", conversation.id)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unexpected chat failure conversation_id=%s", conversation.id)
        raise HTTPException(status_code=500, detail=f"Conversation chat failed: {exc}") from exc

    display_answer, files = _finalise_answer(answer)
    assistant_message = Message(conversation_id=conversation.id, role="assistant", content=display_answer)
    conversation.messages.append(assistant_message)
    _sync_task_state(db, conversation.id, active_task, result)
    touch(conversation)
    db.commit()
    db.refresh(assistant_message)
    snapshot = None
    if files:
        snapshot = _save_snapshot(db, conversation.id, files, request.active_file, "assistant", assistant_message.id)
        db.commit()
    db.refresh(conversation)
    logger.info(
        "Saved assistant message conversation_id=%s message_id=%s answer_chars=%d files=%d",
        conversation.id,
        assistant_message.id,
        len(display_answer),
        len(files),
    )

    return ConversationChatResponse(
        conversation=conversation_summary(conversation),
        message=stored_message(assistant_message, db),
        workspace=_workspace_from_snapshot(snapshot) or _workspace_from_snapshot(_latest_snapshot(db, conversation.id)),
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
    user_message = Message(conversation_id=conversation.id, role="user", content=request.content)
    conversation.messages.append(user_message)
    if not had_messages and conversation.title == "New conversation":
        conversation.title = title_from_content(request.content)
    touch(conversation)
    db.commit()
    db.refresh(user_message)

    request_workspace = _request_workspace(request)
    if request_workspace is not None:
        _save_snapshot(db, conversation.id, request_workspace.files, request_workspace.active_file, "user", user_message.id)
        db.commit()
    workspace = request_workspace or _workspace_from_snapshot(_latest_snapshot(db, conversation.id))
    prompt_messages = _load_prompt_messages(db, conversation, workspace)

    yield {"type": "conversation", "conversation": conversation_summary(conversation).model_dump(mode="json")}

    chunks: list[str] = []
    result: model_router_service.IntentChatResult | None = None
    active_task = _get_pending_task(db, conversation.id)
    active_task_state = _task_state(active_task)
    try:
        for event in model_router_service.stream_handle_chat(
            prompt_messages,
            active_task=active_task_state,
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
    display_answer, files = _finalise_answer(answer)
    if result is None:
        result = model_router_service.IntentChatResult(content=answer, decision=None, executed=False)
    assistant_message = Message(conversation_id=conversation.id, role="assistant", content=display_answer)
    conversation.messages.append(assistant_message)
    _sync_task_state(db, conversation.id, active_task, result)
    touch(conversation)
    db.commit()
    db.refresh(assistant_message)
    snapshot = None
    if files:
        snapshot = _save_snapshot(db, conversation.id, files, request.active_file, "assistant", assistant_message.id)
        db.commit()
    db.refresh(conversation)
    workspace = _workspace_from_snapshot(snapshot) or _workspace_from_snapshot(_latest_snapshot(db, conversation.id))
    yield {
        "type": "done",
        "conversation": conversation_summary(conversation).model_dump(mode="json"),
        "message": stored_message(assistant_message, db).model_dump(mode="json"),
        "workspace": workspace.model_dump(mode="json") if workspace is not None else None,
    }
