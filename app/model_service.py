from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Any

import httpx
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from .config import Settings, settings
from .schemas import ChatMessage
from .services.service_configs import ServiceModelConfig


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


class RemoteModelError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelSetting:
    model_url: str
    model_name: str
    api_key: str | None
    timeout_seconds: float
    debug_url: str = ""
    ping_url: str = ""
    debug_timeout_seconds: float = 30.0
    verify_tls: bool = True
    label: str = "coder"


class _ServiceConfigMixin:
    def _effective_max_tokens(self, cfg: ServiceModelConfig) -> int:
        if cfg.max_new_tokens is None:
            if cfg.min_new_tokens is None:
                raise RemoteModelError("ServiceModelConfig must define max_new_tokens or min_new_tokens")
            return cfg.min_new_tokens
        if cfg.min_new_tokens is None:
            return cfg.max_new_tokens
        return max(cfg.max_new_tokens, cfg.min_new_tokens)


class RemoteChatModel(_ServiceConfigMixin):
    def __init__(self, stg: ModelSetting) -> None:
        self.stg = stg

    @property
    def configured(self) -> bool:
        return bool(self.stg.model_url.strip() and self.stg.model_name.strip())

    @property
    def label(self) -> str:
        return self.stg.label

    @property
    def model_url(self) -> str:
        return self.stg.model_url

    @property
    def model_name(self) -> str:
        return self.stg.model_name

    def chat(self, messages: list[ChatMessage], cfg: ServiceModelConfig) -> str:
        if not self.configured:
            raise RemoteModelError(f"{self.stg.label} model URL or name is empty")

        logger.info(
            "Calling %s model via HTTP: model=%s url=%s messages=%d",
            self.stg.label,
            self.stg.model_name,
            self.stg.model_url,
            len(messages),
        )
        payload = {
            "model": self.stg.model_name,
            "messages": [{"role": message.role, "content": message.content} for message in messages],
            "max_tokens": self._effective_max_tokens(cfg),
            "temperature": cfg.temperature,
            "top_p": cfg.top_p,
            "stream": False,
        }

        try:
            with httpx.Client(timeout=self.stg.timeout_seconds, verify=self.stg.verify_tls) as client:
                response = client.post(self.stg.model_url, json=payload, headers=self._headers())
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.exception("%s model HTTP status error", self.stg.label)
            body = exc.response.text[:1000]
            raise RemoteModelError(f"{self.stg.label} model returned HTTP {exc.response.status_code}: {body}") from exc
        except httpx.RemoteProtocolError as exc:
            logger.exception("%s model remote protocol error", self.stg.label)
            raise RemoteModelError(
                f"{self.stg.label} model disconnected before sending a response. "
                f"Last remote diagnostics: {self.remote_diagnostics()}"
            ) from exc
        except httpx.HTTPError as exc:
            logger.exception("%s model HTTP error", self.stg.label)
            raise RemoteModelError(
                f"{self.stg.label} model request failed: {exc}. "
                f"Last remote diagnostics: {self.remote_diagnostics()}"
            ) from exc

        answer = self._extract_answer(response.json())
        logger.info("%s model HTTP call succeeded: answer_chars=%d", self.stg.label, len(answer))
        return answer

    def stream_chat(self, messages: list[ChatMessage], cfg: ServiceModelConfig):
        if not self.configured:
            raise RemoteModelError(f"{self.stg.label} model URL or name is empty")

        payload = {
            "model": self.stg.model_name,
            "messages": [{"role": message.role, "content": message.content} for message in messages],
            "max_tokens": self._effective_max_tokens(cfg),
            "temperature": cfg.temperature,
            "top_p": cfg.top_p,
            "stream": True,
        }

        try:
            with httpx.Client(timeout=None, verify=self.stg.verify_tls, trust_env=False) as client:
                with client.stream("POST", self.stg.model_url, json=payload, headers=self._headers()) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line.removeprefix("data:").strip()
                        if data == "[DONE]":
                            break
                        try:
                            payload = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if "error" in payload:
                            raise RemoteModelError(str(payload["error"]))
                        choices = payload.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        content = delta.get("content")
                        if content:
                            yield content
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:1000]
            raise RemoteModelError(f"{self.stg.label} model returned HTTP {exc.response.status_code}: {body}") from exc
        except httpx.HTTPError as exc:
            raise RemoteModelError(f"{self.stg.label} model stream failed: {exc}") from exc

    def remote_diagnostics(self) -> dict[str, Any]:
        return {
            "ping": self._get_json(self.stg.ping_url, include_auth=False),
            "status": self._get_json(self.stg.debug_url, include_auth=True),
        }

    def debug_status(self) -> dict[str, Any]:
        return self.remote_diagnostics()

    def _get_json(self, url: str, include_auth: bool) -> dict[str, Any]:
        if not url.strip():
            return {"ok": False, "error": "URL is empty"}
        try:
            headers = self._headers() if include_auth else {"Content-Type": "application/json"}
            with httpx.Client(timeout=self.stg.debug_timeout_seconds, verify=self.stg.verify_tls) as client:
                response = client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict):
                    return {"ok": True, **data}
                return {"ok": False, "error": f"Unexpected response: {data}", "url": url}
        except httpx.HTTPError as exc:
            logger.exception("%s model diagnostics failed", self.stg.label)
            return {"ok": False, "error": str(exc), "url": url}

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.stg.api_key:
            headers["Authorization"] = f"Bearer {self.stg.api_key}"
        return headers

    @staticmethod
    def _extract_answer(data: dict[str, Any]) -> str:
        try:
            message = data["choices"][0]["message"]
            content = message.get("content", "")
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            logger.exception("Unexpected remote response format")
            raise RemoteModelError(f"Unexpected remote response format: {data}") from exc

        if isinstance(content, list):
            text_parts = [part.get("text", "") for part in content if isinstance(part, dict)]
            content = "".join(text_parts)
        if not isinstance(content, str):
            raise RemoteModelError(f"Unexpected remote response content: {content}")
        return content.strip()


class LangChainChatModel(_ServiceConfigMixin):
    def __init__(self, cfg: ModelSetting) -> None:
        self.stg = cfg

    @property
    def configured(self) -> bool:
        return bool(self.stg.model_url.strip() and self.stg.model_name.strip() and self.stg.api_key)

    @property
    def label(self) -> str:
        return self.stg.label

    @property
    def model_url(self) -> str:
        return self.stg.model_url

    @property
    def model_name(self) -> str:
        return self.stg.model_name

    def chat(self, messages: list[ChatMessage], cfg: ServiceModelConfig) -> str:
        if not self.configured:
            raise RemoteModelError(f"{self.stg.label} model URL, name, or API key is empty")

        logger.info(
            "Calling %s model via LangChain: model=%s base_url=%s messages=%d",
            self.stg.label,
            self.stg.model_name,
            self.stg.model_url,
            len(messages),
        )
        llm = ChatOpenAI(
            model=self.stg.model_name,
            api_key=self.stg.api_key,
            base_url=self.stg.model_url.rstrip("/"),
            timeout=self.stg.timeout_seconds,
            temperature=cfg.temperature,
            top_p=cfg.top_p,
            max_tokens=self._effective_max_tokens(cfg),
        )

        try:
            response = llm.invoke(self._to_langchain_messages(messages))
        except Exception as exc:
            logger.exception("%s model LangChain request failed", self.stg.label)
            raise RemoteModelError(f"{self.stg.label} model request failed: {exc}") from exc

        content = getattr(response, "content", "")
        if isinstance(content, list):
            text_parts = [part.get("text", "") for part in content if isinstance(part, dict)]
            content = "".join(text_parts)
        if not isinstance(content, str):
            raise RemoteModelError(f"Unexpected {self.stg.label} response content: {content}")
        answer = content.strip()
        logger.info("%s model LangChain call succeeded: answer_chars=%d", self.stg.label, len(answer))
        return answer

    def stream_chat(self, messages: list[ChatMessage], cfg: ServiceModelConfig):
        if not self.configured:
            raise RemoteModelError(f"{self.stg.label} model URL, name, or API key is empty")

        logger.info(
            "Streaming %s model via LangChain: model=%s base_url=%s messages=%d",
            self.stg.label,
            self.stg.model_name,
            self.stg.model_url,
            len(messages),
        )
        llm = ChatOpenAI(
            model=self.stg.model_name,
            api_key=self.stg.api_key,
            base_url=self.stg.model_url.rstrip("/"),
            timeout=self.stg.timeout_seconds,
            temperature=cfg.temperature,
            top_p=cfg.top_p,
            max_tokens=self._effective_max_tokens(cfg),
            streaming=True,
        )

        try:
            for chunk in llm.stream(self._to_langchain_messages(messages)):
                content = getattr(chunk, "content", "")
                if isinstance(content, list):
                    text_parts = [part.get("text", "") for part in content if isinstance(part, dict)]
                    content = "".join(text_parts)
                if content:
                    yield content
        except Exception as exc:
            logger.exception("%s model LangChain stream failed", self.stg.label)
            raise RemoteModelError(f"{self.stg.label} model stream failed: {exc}") from exc

    def remote_diagnostics(self) -> dict[str, Any]:
        return {"ok": self.configured, "provider": "langchain-openai", "model": self.stg.model_name}

    def debug_status(self) -> dict[str, Any]:
        return self.remote_diagnostics()

    @staticmethod
    def _to_langchain_messages(messages: list[ChatMessage]) -> list[SystemMessage | HumanMessage | AIMessage]:
        converted: list[SystemMessage | HumanMessage | AIMessage] = []
        for message in messages:
            if message.role == "system":
                converted.append(SystemMessage(content=message.content))
            elif message.role == "assistant":
                converted.append(AIMessage(content=message.content))
            else:
                converted.append(HumanMessage(content=message.content))
        return converted


def coder_model_config(stg: Settings) -> ModelSetting:
    return ModelSetting(
        label="coder",
        model_url=stg.coder_model_url,
        model_name=stg.coder_model_name,
        api_key=stg.coder_api_key,
        timeout_seconds=stg.coder_timeout_seconds,
        debug_url=stg.coder_debug_url,
        ping_url=stg.coder_ping_url,
        debug_timeout_seconds=stg.coder_debug_timeout_seconds,
        verify_tls=stg.verify_coder_tls,
    )


def primary_model_config(stg: Settings) -> ModelSetting:
    return ModelSetting(
        label="primary",
        model_url=stg.primary_model_url,
        model_name=stg.primary_model_name,
        api_key=stg.primary_api_key,
        timeout_seconds=stg.primary_timeout_seconds,
        verify_tls=stg.verify_coder_tls,
    )


coder_chat_model = RemoteChatModel(coder_model_config(settings))
primary_chat_model = LangChainChatModel(primary_model_config(settings))

# Backward-compatible alias: existing code paths still use the coder model by default.
chat_model = coder_chat_model