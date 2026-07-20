from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_DIR / ".env")


def _bool_from_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _base_coder_url(chat_url: str) -> str:
    if chat_url.endswith("/v1/chat/completions"):
        return chat_url[: -len("/v1/chat/completions")]
    return chat_url.rstrip("/")


@dataclass(frozen=True)
class Settings:
    database_url: str | None = os.getenv("DATABASE_URL") or None
    model_routing_enabled: bool = _bool_from_env("MODEL_ROUTING_ENABLED", False)

    coder_model_url: str = os.getenv("CODER_MODEL_URL", "http://127.0.0.1:9000/v1/chat/completions")
    coder_model_name: str = os.getenv("CODER_MODEL_NAME", "qwen-coder-simplifier-dpo-lora")
    coder_api_key: str | None = os.getenv("CODER_API_KEY") or None
    coder_timeout_seconds: float = float(os.getenv("CODER_TIMEOUT_SECONDS", "300"))
    coder_debug_url: str = os.getenv("CODER_DEBUG_URL") or _base_coder_url(
        os.getenv("CODER_MODEL_URL", "http://127.0.0.1:9000/v1/chat/completions")
    ) + "/debug/status"
    coder_ping_url: str = os.getenv("CODER_PING_URL") or _base_coder_url(
        os.getenv("CODER_MODEL_URL", "http://127.0.0.1:9000/v1/chat/completions")
    ) + "/debug/ping"
    coder_debug_timeout_seconds: float = float(os.getenv("CODER_DEBUG_TIMEOUT_SECONDS", "30"))

    primary_model_url: str = os.getenv("PRIMARY_MODEL_URL", "")
    primary_model_name: str = os.getenv("PRIMARY_MODEL_NAME", "")
    primary_api_key: str | None = os.getenv("PRIMARY_API_KEY") or None
    primary_timeout_seconds: float = float(os.getenv("PRIMARY_TIMEOUT_SECONDS", os.getenv("CODER_TIMEOUT_SECONDS", "300")))

    verify_coder_tls: bool = _bool_from_env("VERIFY_CODER_TLS", True)


settings = Settings()