from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


Role = Literal["system", "user", "assistant"]


class CodeFile(BaseModel):
    path: str = Field(min_length=1, max_length=260)
    language: str = Field(default="text", max_length=40)
    content: str = Field(default="", max_length=200000)


class WorkspaceState(BaseModel):
    files: list[CodeFile] = Field(default_factory=list)
    active_file: str | None = Field(default=None, max_length=260)
    snapshot_id: int | None = None


class ChatMessage(BaseModel):
    role: Role
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1)
    current_files: list[CodeFile] = Field(default_factory=list)
    active_file: str | None = Field(default=None, max_length=260)


class ChatResponse(BaseModel):
    message: ChatMessage
    workspace: WorkspaceState | None = None


class ModelStatus(BaseModel):
    provider: str
    coder_model_url: str
    coder_debug_url: str
    coder_ping_url: str
    coder_model_name: str
    configured: bool
    model_routing_enabled: bool = False
    coder_configured: bool | None = None
    primary_model_name: str | None = None
    primary_configured: bool | None = None


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=160)


class StoredMessage(ChatMessage):
    id: int
    created_at: datetime
    code_snapshot_id: int | None = None


class ConversationSummary(BaseModel):
    id: int
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationSummary):
    messages: list[StoredMessage]
    workspace: WorkspaceState | None = None


class ConversationChatRequest(BaseModel):
    content: str = Field(min_length=1)
    current_files: list[CodeFile] = Field(default_factory=list)
    active_file: str | None = Field(default=None, max_length=260)


class ConversationChatResponse(BaseModel):
    conversation: ConversationSummary
    message: StoredMessage
    workspace: WorkspaceState | None = None


class CodeRunRequest(BaseModel):
    language: Literal["python"] = "python"
    code: str = Field(min_length=1, max_length=20000)
    stdin: str = Field(default="", max_length=10000)
    call_code: str = Field(default="", max_length=5000)
    timeout_seconds: float = Field(default=5.0, ge=0.5, le=10.0)


class CodeRunResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int | None
    timeout: bool
    duration_ms: int
