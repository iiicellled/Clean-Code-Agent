from __future__ import annotations

import json
import logging

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from .config import PROJECT_DIR, settings
from .database import get_db, init_db
from .model_service import RemoteModelError, chat_model, coder_chat_model, primary_chat_model
from .schemas import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    CodeRunRequest,
    CodeRunResponse,
    ConversationChatRequest,
    ConversationChatResponse,
    ConversationCreate,
    ConversationDetail,
    ConversationSummary,
    ModelStatus,
)
from .services import code_runner_service, conversation_service, model_router_service


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


app = FastAPI(title="Coder Agent", version="0.4.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

WEB_DIR = PROJECT_DIR / "web"
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
@app.exception_handler(Exception)
def unhandled_exception_handler(request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled request error path=%s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal server error: {exc}"},
    )


@app.on_event("startup")
def startup() -> None:
    logger.info("Starting Coder Agent: database_configured=%s routing_enabled=%s coder_configured=%s primary_configured=%s", settings.database_url is not None, settings.model_routing_enabled, coder_chat_model.configured, primary_chat_model.configured)
    init_db()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, str | bool]:
    return {"status": "ok", "database_configured": settings.database_url is not None}


@app.get("/api/model/status", response_model=ModelStatus)
def model_status() -> ModelStatus:
    return ModelStatus(
        provider="remote-openai-compatible",
        coder_model_url=settings.coder_model_url,
        coder_debug_url=settings.coder_debug_url,
        coder_ping_url=settings.coder_ping_url,
        configured=chat_model.configured,
        model_routing_enabled=settings.model_routing_enabled,
        coder_model_name=coder_chat_model.model_name,
        coder_configured=coder_chat_model.configured,
        primary_model_name=primary_chat_model.model_name or None,
        primary_configured=primary_chat_model.configured,
    )


@app.get("/api/debug/remote-status")
def remote_debug_status() -> dict:
    logger.info("Remote debug status requested; remote ping/status is disabled")
    return {"ok": False, "disabled": True, "message": "Remote debug ping/status is disabled."}



def sse_json(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
@app.get("/api/conversations", response_model=list[ConversationSummary])
def list_conversations(db: Session = Depends(get_db)) -> list[ConversationSummary]:
    return conversation_service.list_conversations(db)


@app.post("/api/conversations", response_model=ConversationDetail)
def create_conversation(
    request: ConversationCreate | None = None,
    db: Session = Depends(get_db),
) -> ConversationDetail:
    return conversation_service.create_conversation(db, request)


@app.get("/api/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(conversation_id: int, db: Session = Depends(get_db)) -> ConversationDetail:
    return conversation_service.get_conversation(db, conversation_id)


@app.delete("/api/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: int, db: Session = Depends(get_db)) -> Response:
    conversation_service.delete_conversation(db, conversation_id)
    return Response(status_code=204)



@app.post("/api/conversations/{conversation_id}/chat/stream")
def chat_in_conversation_stream(
    conversation_id: int,
    request: ConversationChatRequest,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    def event_stream():
        try:
            for event in conversation_service.stream_chat_in_conversation(db, conversation_id, request):
                yield sse_json(event)
        except HTTPException as exc:
            yield sse_json({"type": "error", "detail": exc.detail})
        except Exception as exc:
            logger.exception("Unhandled streaming chat error conversation_id=%s", conversation_id)
            yield sse_json({"type": "error", "detail": f"Streaming chat failed: {exc}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.post("/api/conversations/{conversation_id}/chat", response_model=ConversationChatResponse)
def chat_in_conversation(
    conversation_id: int,
    request: ConversationChatRequest,
    db: Session = Depends(get_db),
) -> ConversationChatResponse:
    return conversation_service.chat_in_conversation(db, conversation_id, request)



@app.post("/api/code/run", response_model=CodeRunResponse)
def run_code(request: CodeRunRequest) -> CodeRunResponse:
    if request.language != "python":
        raise HTTPException(status_code=400, detail="Only Python code execution is supported")
    return code_runner_service.run_python_code(request)


@app.post("/api/chat/stream")
def chat_stream(request: ChatRequest) -> StreamingResponse:
    def event_stream():
        try:
            for chunk in model_router_service.stream_chat(request.messages):
                yield sse_json({"type": "delta", "content": chunk})
            yield sse_json({"type": "done"})
        except RemoteModelError as exc:
            yield sse_json({"type": "error", "detail": str(exc)})
        except Exception as exc:
            logger.exception("Unhandled streaming chat error")
            yield sse_json({"type": "error", "detail": f"Streaming chat failed: {exc}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    try:
        answer = model_router_service.chat(
            request.messages,
        )
    except RemoteModelError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return ChatResponse(message=ChatMessage(role="assistant", content=answer))
