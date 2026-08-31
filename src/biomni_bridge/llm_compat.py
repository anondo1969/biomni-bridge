from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_OBSERVATION_ONLY_RE = re.compile(r"^\s*<observation>.*?</observation>\s*$", re.IGNORECASE | re.DOTALL)
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "openai_api_key",
    "password",
    "secret",
    "access_token",
}
_LOG = logging.getLogger(__name__)


def _is_observation_only(content: Any) -> bool:
    """Return True for Biomni's synthetic observation message."""
    return isinstance(content, str) and bool(_OBSERVATION_ONLY_RE.fullmatch(content))


def normalize_biomni_messages(messages: Any) -> Any:
    """Adapt Biomni 0.0.8 messages for strict OpenAI-compatible gateways.

    Biomni 0.0.8 appends tool execution results as ``AIMessage`` objects even
    though they are observations produced by the execution environment. This
    creates ``user -> assistant -> assistant`` histories after every tool call.
    Some OpenAI-compatible gateways require strict user/assistant alternation.

    Only the specific Biomni observation case is changed: when an observation
    immediately follows an AI message, a new ``HumanMessage`` with the same
    content is sent to the provider. The original LangGraph state is untouched.
    """
    if not isinstance(messages, list | tuple):
        return messages

    try:
        from langchain_core.messages import AIMessage, HumanMessage
    except ImportError:
        return messages

    normalized: list[Any] = []
    previous_was_ai = False
    for message in messages:
        if isinstance(message, AIMessage) and previous_was_ai and _is_observation_only(message.content):
            normalized.append(HumanMessage(content=message.content))
            previous_was_ai = False
            continue

        normalized.append(message)
        previous_was_ai = isinstance(message, AIMessage)

    return normalized


def _role_for_message(message: Any) -> str:
    message_type = str(getattr(message, "type", "") or "").lower()
    class_name = message.__class__.__name__.lower()
    if message_type in {"human", "user"} or "human" in class_name:
        return "user"
    if message_type in {"ai", "assistant"} or class_name.startswith("ai"):
        return "assistant"
    if message_type == "system" or "system" in class_name:
        return "system"
    if message_type == "tool" or "tool" in class_name:
        return "tool"
    return message_type or class_name or "unknown"


def _fallback_message_payload(message: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "role": _role_for_message(message),
        "content": getattr(message, "content", ""),
    }
    name = getattr(message, "name", None)
    if name:
        payload["name"] = name
    tool_call_id = getattr(message, "tool_call_id", None)
    if tool_call_id:
        payload["tool_call_id"] = tool_call_id
    return payload


def _is_qwen3_model(llm: Any) -> bool:
    model = str(getattr(llm, "model_name", None) or getattr(llm, "model", None) or "").lower()
    return "qwen3" in model


def _provider_kwargs(
    llm: Any,
    invoke_kwargs: Mapping[str, Any],
    *,
    disable_qwen_thinking: bool,
) -> dict[str, Any]:
    """Merge provider-specific request options without mutating the LLM.

    ``ChatOpenAI.extra_body`` is the supported LangChain seam for vLLM-only
    request fields.  Merge model-level and call-level values so an existing
    provider option is never accidentally discarded.
    """
    result = dict(invoke_kwargs)
    if not (disable_qwen_thinking and _is_qwen3_model(llm)):
        return result

    extra_body: dict[str, Any] = {}
    chat_kwargs: dict[str, Any] = {}

    configured = getattr(llm, "extra_body", None)
    if isinstance(configured, Mapping):
        extra_body.update(configured)
        configured_chat = configured.get("chat_template_kwargs")
        if isinstance(configured_chat, Mapping):
            chat_kwargs.update(configured_chat)

    supplied = result.get("extra_body")
    if isinstance(supplied, Mapping):
        extra_body.update(supplied)
        supplied_chat = supplied.get("chat_template_kwargs")
        if isinstance(supplied_chat, Mapping):
            chat_kwargs.update(supplied_chat)

    chat_kwargs["enable_thinking"] = False
    extra_body["chat_template_kwargs"] = chat_kwargs
    result["extra_body"] = extra_body
    return result


def _flatten_extra_body_for_replay(payload: dict[str, Any]) -> dict[str, Any]:
    """Turn OpenAI-SDK ``extra_body`` into the JSON actually sent on the wire."""
    flattened = dict(payload)
    extra_body = flattened.pop("extra_body", None)
    if isinstance(extra_body, Mapping):
        for key, value in extra_body.items():
            flattened[str(key)] = value
    return flattened


def _fallback_request_payload(llm: Any, messages: Any) -> dict[str, Any]:
    if isinstance(messages, list | tuple):
        serialized_messages = [_fallback_message_payload(message) for message in messages]
    else:
        serialized_messages = [{"role": "user", "content": str(messages)}]

    payload: dict[str, Any] = {"messages": serialized_messages}
    model = getattr(llm, "model_name", None) or getattr(llm, "model", None)
    if model:
        payload["model"] = model

    attribute_map = {
        "temperature": "temperature",
        "max_tokens": "max_tokens",
        "streaming": "stream",
        "top_p": "top_p",
        "frequency_penalty": "frequency_penalty",
        "presence_penalty": "presence_penalty",
        "n": "n",
        "seed": "seed",
        "logprobs": "logprobs",
        "top_logprobs": "top_logprobs",
    }
    for attribute, request_key in attribute_map.items():
        value = getattr(llm, attribute, None)
        if value is not None:
            payload[request_key] = value

    model_kwargs = getattr(llm, "model_kwargs", None)
    if isinstance(model_kwargs, Mapping):
        payload.update(model_kwargs)
    return payload


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return normalized in _SENSITIVE_KEYS or normalized.endswith("_api_key")


def _sanitize_json(value: Any, redactions: tuple[str, ...]) -> Any:
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                continue
            sanitized[key_text] = _sanitize_json(item, redactions)
        return sanitized
    if isinstance(value, list | tuple):
        return [_sanitize_json(item, redactions) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        sanitized = value
        for secret in redactions:
            if secret:
                sanitized = sanitized.replace(secret, "[REDACTED]")
        return sanitized
    if value is None or isinstance(value, bool | int | float):
        return value
    if hasattr(value, "model_dump"):
        try:
            return _sanitize_json(value.model_dump(), redactions)
        except Exception:  # pragma: no cover - defensive for arbitrary provider objects
            pass
    return str(value)


def _content_char_count(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    try:
        return len(json.dumps(content, ensure_ascii=False, default=str))
    except TypeError:  # pragma: no cover - json default already handles ordinary objects
        return len(str(content))


def _request_shape(payload: Mapping[str, Any]) -> tuple[list[str], list[int], int]:
    roles: list[str] = []
    message_chars: list[int] = []
    messages = payload.get("messages", [])
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            roles.append(str(message.get("role", "unknown")))
            message_chars.append(_content_char_count(message.get("content", "")))
    return roles, message_chars, sum(message_chars)


def _http_status_from_exception(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _secure_write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - permissions vary by platform/filesystem
        pass


class LLMRequestDiagnostics:
    """Capture replayable, sanitized provider requests for opt-in debugging.

    The capture deliberately contains prompt text, because reproducing a
    provider-specific timeout requires the real request body. Authentication
    headers and API-key fields are never written, and the configured API key is
    also redacted if it somehow appears inside a string value.
    """

    def __init__(self, directory: Path, *, redactions: tuple[str, ...] = ()):
        self.directory = directory
        self.redactions = tuple(value for value in redactions if value)

    def _payload(self, llm: Any, messages: Any, invoke_kwargs: dict[str, Any]) -> tuple[dict[str, Any], str]:
        builder = getattr(llm, "_get_request_payload", None)
        if callable(builder):
            # ChatOpenAI's request-payload helper is used only for this optional
            # diagnostic, never for normal execution. It lets the saved JSON
            # match the SDK request body much more closely than a hand-built
            # serializer. Fall back safely if a future LangChain version moves it.
            payload_kwargs = {key: value for key, value in invoke_kwargs.items() if key != "config"}
            try:
                payload = builder(messages, **payload_kwargs)
                sanitized = _sanitize_json(payload, self.redactions)
                if isinstance(sanitized, dict):
                    return _flatten_extra_body_for_replay(sanitized), "langchain_request_payload"
            except Exception as exc:  # pragma: no cover - depends on provider implementation
                _LOG.debug("Could not use LangChain request-payload helper: %s", type(exc).__name__)

        sanitized = _sanitize_json(_fallback_request_payload(llm, messages), self.redactions)
        assert isinstance(sanitized, dict)
        return _flatten_extra_body_for_replay(sanitized), "wrapper_fallback"

    def start(self, llm: Any, messages: Any, invoke_kwargs: dict[str, Any]) -> dict[str, Any]:
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            self.directory.chmod(0o700)
        except OSError:  # pragma: no cover - permissions vary by platform/filesystem
            pass

        payload, capture_method = self._payload(llm, messages, invoke_kwargs)
        request_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        roles, message_chars, prompt_chars = _request_shape(payload)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        request_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
        request_path = self.directory / f"llm-request-{request_id}.json"
        meta_path = self.directory / f"llm-request-{request_id}.meta.json"

        _secure_write_json(request_path, payload)
        _secure_write_json(self.directory / "latest-request.json", payload)

        metadata: dict[str, Any] = {
            "request_id": request_id,
            "captured_at_utc": datetime.now(UTC).isoformat(),
            "capture_method": capture_method,
            "model": payload.get("model"),
            "message_count": len(roles),
            "roles": roles,
            "message_content_chars": message_chars,
            "prompt_content_chars": prompt_chars,
            "serialized_payload_bytes": len(request_json.encode("utf-8")),
            "request_file": str(request_path),
            "result": "pending",
        }
        _secure_write_json(meta_path, metadata)
        _secure_write_json(self.directory / "latest-request.meta.json", metadata)

        _LOG.info(
            "LLM diagnostic request: model=%s messages=%d roles=%s prompt_chars=%d payload_bytes=%d file=%s",
            metadata["model"],
            metadata["message_count"],
            ",".join(roles),
            prompt_chars,
            metadata["serialized_payload_bytes"],
            request_path,
        )
        return {
            "started": time.perf_counter(),
            "metadata": metadata,
            "meta_path": meta_path,
        }

    def finish(self, state: dict[str, Any], *, error: BaseException | None = None) -> None:
        metadata = dict(state["metadata"])
        metadata["elapsed_seconds"] = round(time.perf_counter() - state["started"], 3)
        if error is None:
            metadata["result"] = "success"
        else:
            metadata["result"] = "error"
            metadata["error_type"] = type(error).__name__
            http_status = _http_status_from_exception(error)
            if http_status is not None:
                metadata["http_status"] = http_status

        _secure_write_json(state["meta_path"], metadata)
        _secure_write_json(self.directory / "latest-request.meta.json", metadata)
        _LOG.info(
            "LLM diagnostic result: model=%s result=%s elapsed=%.3fs http_status=%s",
            metadata.get("model"),
            metadata["result"],
            metadata["elapsed_seconds"],
            metadata.get("http_status", "-"),
        )


class StrictRoleCompatLLM:
    """Provider-boundary compatibility wrapper for Biomni 0.0.8.

    It performs three narrow adaptations without changing Biomni's LangGraph
    state: strict role normalization, optional Qwen3 non-thinking mode, and a
    streaming HTTP transport that is aggregated back into a normal message for
    Biomni.  The latter keeps reverse proxies active during long generations.
    """

    __slots__ = ("_diagnostics", "_disable_qwen_thinking", "_llm", "_stream_transport")

    def __init__(
        self,
        llm: Any,
        diagnostics: LLMRequestDiagnostics | None = None,
        *,
        stream_transport: bool = True,
        disable_qwen_thinking: bool = True,
    ):
        self._llm = llm
        self._diagnostics = diagnostics
        self._stream_transport = stream_transport
        self._disable_qwen_thinking = disable_qwen_thinking

    @property
    def underlying(self) -> Any:
        return self._llm

    def __getattr__(self, name: str) -> Any:
        return getattr(self._llm, name)

    def _stream_to_message(self, input_: Any, *args: Any, **kwargs: Any) -> Any:
        from langchain_core.messages import BaseMessageChunk, message_chunk_to_message

        aggregate: Any = None
        for chunk in self._llm.stream(input_, *args, **kwargs):
            if aggregate is None:
                aggregate = chunk
            else:
                aggregate = aggregate + chunk

        if aggregate is None:
            raise RuntimeError("The model stream completed without returning any message chunks")
        if isinstance(aggregate, BaseMessageChunk):
            return message_chunk_to_message(aggregate)
        return aggregate

    def invoke(self, input_: Any, *args: Any, **kwargs: Any) -> Any:
        normalized = normalize_biomni_messages(input_)
        provider_kwargs = _provider_kwargs(
            self._llm,
            kwargs,
            disable_qwen_thinking=self._disable_qwen_thinking,
        )

        use_stream_transport = self._stream_transport and callable(getattr(self._llm, "stream", None))

        diagnostic_state = None
        if self._diagnostics is not None:
            diagnostic_kwargs = dict(provider_kwargs)
            if use_stream_transport:
                diagnostic_kwargs["stream"] = True
            try:
                diagnostic_state = self._diagnostics.start(self._llm, normalized, diagnostic_kwargs)
            except Exception as exc:  # pragma: no cover - filesystem/provider specific
                _LOG.warning("LLM diagnostics capture could not start: %s", type(exc).__name__)

        try:
            if use_stream_transport:
                result = self._stream_to_message(normalized, *args, **provider_kwargs)
            else:
                result = self._llm.invoke(normalized, *args, **provider_kwargs)
        except BaseException as exc:
            if diagnostic_state is not None:
                try:
                    self._diagnostics.finish(diagnostic_state, error=exc)
                except Exception as diagnostic_exc:  # pragma: no cover - filesystem specific
                    _LOG.warning("LLM diagnostics capture could not finish: %s", type(diagnostic_exc).__name__)
            raise

        if diagnostic_state is not None:
            try:
                self._diagnostics.finish(diagnostic_state)
            except Exception as exc:  # pragma: no cover - filesystem specific
                _LOG.warning("LLM diagnostics capture could not finish: %s", type(exc).__name__)
        return result
