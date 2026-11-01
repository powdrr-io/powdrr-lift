"""Concrete model provider clients and response handling."""

from __future__ import annotations

import importlib
import itertools
import json
import math
import os
import re
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, TextIO, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import laga

from powdrr_service.providers.config import (
    DEFAULT_MODEL_LIMITS,
    MAX_COMPLETION_TOKENS,
    LLMModelLimits,
    provider_definition,
)
from powdrr_service.providers.errors import (
    ProviderClientError,
    ProviderHTTPError,
    ProviderResponseError,
)
from powdrr_service.providers.selection import provider_model_limits

_DEFAULT_LOCAL_MODEL_CONTEXT = 24576
_TOKEN_ESTIMATE_CHARS_PER_TOKEN = 3
_CONTEXT_SAFETY_MARGIN_TOKENS = 1024
_MAX_STREAM_CHUNKS = 16384
_MAX_STREAM_CONTENT_CHARS = 524288
_MAX_STRUCTURED_COMPLETION_TOKENS = 2048
_MAX_STRUCTURED_STREAM_CONTENT_CHARS = 65536
_STREAM_EXCERPT_CHARS = 512
_STREAM_PROGRESS_INTERVAL_SECONDS = 10.0
_STREAM_CAPTURE_COUNTER = itertools.count(1)


class ProviderClient(Protocol):
    """Structural interface implemented by concrete provider clients."""

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class OpenAIChatClient:
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        timeout: float = 120.0,
        limits: LLMModelLimits | None = None,
        structured_output_token_limit: int | None = None,
        progress_stream: TextIO | None = None,
        reasoning_effort: str | None = None,
        unsupported_schema_keywords: frozenset[str] = frozenset(),
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._limits = limits or DEFAULT_MODEL_LIMITS
        if (
            structured_output_token_limit is not None
            and structured_output_token_limit < 1
        ):
            raise ValueError("structured_output_token_limit must be positive")
        self._structured_output_token_limit = (
            structured_output_token_limit or _MAX_STRUCTURED_COMPLETION_TOKENS
        )
        self._progress_stream = progress_stream
        self._reasoning_effort = reasoning_effort
        self._unsupported_schema_keywords = unsupported_schema_keywords
        self._usage_local = threading.local()
        self.last_usage: dict[str, Any] = {}
        self.last_serialized_messages: str | None = None

    @property
    def last_usage(self) -> dict[str, Any]:
        """Usage for the current calling thread, safe for concurrent requests."""
        value = getattr(self._usage_local, "last_usage", None)
        return value if isinstance(value, dict) else {}

    @last_usage.setter
    def last_usage(self, value: dict[str, Any]) -> None:
        self._usage_local.last_usage = value

    def set_structured_output_token_limit(self, token_limit: int) -> None:
        """Set a per-client structured-output budget for staged tasks."""
        if token_limit < 1:
            raise ValueError("structured output token limit must be positive")
        self._structured_output_token_limit = token_limit

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        serialized_messages = _serialize_messages(messages)
        self.last_serialized_messages = serialized_messages
        max_tokens, estimated_input_tokens = _request_token_budget(
            messages,
            self._limits,
            serialized_messages=serialized_messages,
        )
        if response_schema is not None:
            max_tokens = min(max_tokens, self._structured_output_token_limit)
        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": (
                {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "workflow_action",
                        "strict": True,
                        "schema": _remove_schema_keywords(
                            response_schema, self._unsupported_schema_keywords
                        ),
                    },
                }
                if response_schema is not None
                else {"type": "json_object"}
            ),
            "stream": True,
        }
        if self._reasoning_effort is not None:
            payload["reasoning_effort"] = self._reasoning_effort
        request = Request(
            f"{self._base_url}/chat/completions",
            data=_serialize_openai_payload(
                payload,
                serialized_messages=serialized_messages,
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        request_started = time.monotonic()
        try:
            with urlopen(request, timeout=self._timeout) as response:
                raw_response = _read_openai_response(
                    response,
                    progress_stream=self._progress_stream,
                    max_content_chars=(
                        _MAX_STRUCTURED_STREAM_CONTENT_CHARS
                        if response_schema is not None
                        else _MAX_STREAM_CONTENT_CHARS
                    ),
                )
        except HTTPError as exc:
            raise ProviderHTTPError(
                "OpenAI",
                exc.code,
                exc.read().decode("utf-8", errors="replace"),
            ) from exc
        except URLError as exc:
            raise ProviderClientError(f"OpenAI request failed: {exc.reason}") from exc
        except ConnectionError as exc:
            raise ProviderClientError(
                f"OpenAI request connection dropped: {exc}"
            ) from exc
        except TimeoutError as exc:
            raise ProviderClientError(
                _provider_timeout_message(
                    provider="OpenAI-compatible",
                    model=self._model,
                    endpoint=request.full_url,
                    timeout=self._timeout,
                    elapsed=time.monotonic() - request_started,
                    message=str(exc),
                    message_count=len(messages),
                    max_tokens=max_tokens,
                    estimated_input_tokens=estimated_input_tokens,
                )
            ) from exc

        loaded_response = _parse_json_object(
            raw_response,
            "OpenAI response",
        )
        _raise_openai_provider_error(loaded_response)
        usage = loaded_response.get("usage")
        self.last_usage = dict(usage) if isinstance(usage, dict) else {}
        choices = loaded_response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderResponseError("OpenAI response did not include any choices.")
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise ProviderResponseError("OpenAI response choice was not an object.")
        message = first_choice.get("message")
        if not isinstance(message, dict):
            raise ProviderResponseError(
                "OpenAI response choice message was not an object."
            )
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            # Some OpenAI-compatible providers put a refusal or reasoning-only
            # response in a separate field. Preserve that metadata so Workrr's
            # structured recovery can distinguish an unsupported response mode
            # from a genuinely empty generation.
            metadata = {
                key: value
                for key, value in message.items()
                if key != "content" and value not in (None, "", [], {})
            }
            detail = (
                f"; message fields={json.dumps(metadata, ensure_ascii=False)[:1000]}"
                if metadata
                else ""
            )
            finish_reason = first_choice.get("finish_reason")
            if finish_reason not in (None, ""):
                detail += f"; finish_reason={finish_reason!r}"
            raise ProviderResponseError(
                f"OpenAI-compatible provider response message content was empty"
                f"{detail}."
            )

        return _parse_json_object(content, "OpenAI response content")


def _read_openai_response(
    response: Any,
    *,
    progress_stream: TextIO | None,
    max_content_chars: int = _MAX_STREAM_CONTENT_CHARS,
) -> str:
    """Read a streamed OpenAI response and return its normal response shape.

    Providers sometimes ignore ``stream=true`` (and test doubles commonly do
    too), so a regular JSON response is still accepted. ``readline`` is used
    for SSE responses so each completed event is consumed as soon as it is
    available; the socket timeout therefore applies to inactivity between
    events rather than waiting for the entire generation to finish.
    """
    content_type = ""
    headers = getattr(response, "headers", None)
    if headers is not None:
        content_type = str(headers.get("Content-Type", "")).casefold()
    if "text/event-stream" not in content_type or not hasattr(response, "readline"):
        return response.read().decode("utf-8")

    content_parts: list[str] = []
    capture_path = os.environ.get("POWDRR_STREAM_CAPTURE_PATH")
    capture_id = next(_STREAM_CAPTURE_COUNTER)
    if capture_path:
        with Path(capture_path).open("a", encoding="utf-8") as capture:
            capture.write(f"\n---STREAM-BEGIN id={capture_id}---\n")
            capture.flush()
    response_metadata: dict[str, Any] | None = None
    finish_reason: Any = None
    event_data: list[str] = []
    chunk_count = 0
    stream_complete = False
    last_progress_report = float("-inf")
    while True:
        line = response.readline()
        if not line:
            break
        decoded_line = line.decode("utf-8", errors="replace").rstrip("\r\n")
        if decoded_line:
            if decoded_line.startswith("data:"):
                event_data.append(decoded_line[5:].lstrip())
            continue
        if not event_data:
            continue
        event_payload = "\n".join(event_data)
        event_data.clear()
        if event_payload == "[DONE]":
            stream_complete = True
            break
        try:
            event = json.loads(event_payload)
        except json.JSONDecodeError as exc:
            raise ProviderResponseError(
                f"OpenAI streaming response contained invalid JSON: {exc.msg}"
            ) from exc
        if not isinstance(event, dict):
            continue
        _raise_openai_provider_error(event)
        if response_metadata is None:
            response_metadata = event
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            continue
        if first_choice.get("finish_reason") is not None:
            stream_complete = True
            finish_reason = first_choice["finish_reason"]
        delta = first_choice.get("delta")
        if not isinstance(delta, dict):
            continue
        content = delta.get("content")
        if isinstance(content, str):
            content_parts.append(content)
            if capture_path:
                with Path(capture_path).open("a", encoding="utf-8") as capture:
                    capture.write(content)
                    capture.flush()
            chunk_count += 1
            content_length = sum(len(part) for part in content_parts)
            if chunk_count > _MAX_STREAM_CHUNKS or content_length > max_content_chars:
                excerpt = _stream_excerpt(content_parts)
                raise _ModelUnavailableError(
                    "OpenAI streaming response exceeded the bounded output limit "
                    f"({chunk_count} chunks, {content_length} characters; "
                    f"limit={max_content_chars} characters); "
                    f"partial content prefix: {excerpt!r}"
                )
            now = time.monotonic()
            if (
                progress_stream is not None
                and now - last_progress_report >= _STREAM_PROGRESS_INTERVAL_SECONDS
            ):
                print(
                    f"received streamed LLM data ({chunk_count} chunks)...",
                    file=progress_stream,
                    flush=True,
                )
                last_progress_report = now

    if response_metadata is None:
        raise ProviderClientError(
            "OpenAI streaming response did not include any events."
        )
    if not content_parts:
        metadata = response_metadata or {}
        choices = metadata.get("choices")
        detail = ""
        if choices:
            detail = (
                "; first event="
                + json.dumps({"choices": choices}, ensure_ascii=False)[:1000]
            )
        raise ProviderResponseError(
            f"OpenAI streaming response content was empty{detail}."
        )
    if not stream_complete:
        raise _ModelUnavailableError(
            "OpenAI streaming response ended before a completion marker; "
            f"received {chunk_count} content chunks; "
            f"partial content: {_stream_excerpt(content_parts)!r}"
        )
    response_metadata["choices"] = [
        {
            "message": {"content": "".join(content_parts)},
            "finish_reason": finish_reason,
        }
    ]
    if capture_path:
        with Path(capture_path).open("a", encoding="utf-8") as capture:
            capture.write(f"\n---STREAM-END id={capture_id}---\n")
            capture.flush()
    return json.dumps(response_metadata)


def _raise_openai_provider_error(response: Mapping[str, Any]) -> None:
    """Surface OpenAI-compatible API errors returned inside HTTP 200 bodies."""
    error = response.get("error")
    if error is None and response.get("type") == "error":
        error = response
    if error is None:
        return

    if isinstance(error, Mapping):
        details = {
            key: error[key]
            for key in ("type", "code", "param", "message")
            if error.get(key) not in (None, "")
        }
    else:
        details = {"message": str(error)}
    rendered = json.dumps(details, ensure_ascii=False, default=str)
    if len(rendered) > 1500:
        rendered = rendered[:1497] + "..."
    raise ProviderClientError(
        f"OpenAI-compatible provider returned an error event: {rendered}"
    )


def _stream_excerpt(content_parts: Sequence[str]) -> str:
    """Return bounded beginning/end content for diagnosing truncated streams."""
    content = "".join(content_parts)
    if len(content) <= _STREAM_EXCERPT_CHARS * 2:
        return content
    return (
        content[:_STREAM_EXCERPT_CHARS]
        + "...<truncated>..."
        + content[-_STREAM_EXCERPT_CHARS:]
    )


class LocalLlamaChatClient:
    def __init__(
        self,
        *,
        model_path: Path,
        n_ctx: int = _DEFAULT_LOCAL_MODEL_CONTEXT,
    ) -> None:
        try:
            llama_module = importlib.import_module("llama_cpp")
            Llama = llama_module.Llama
            llama_supports_gpu_offload = llama_module.llama_supports_gpu_offload
        except ImportError as exc:
            raise ProviderResponseError(
                "Local provider requires llama-cpp-python. Install the local "
                "extra (with Metal support on macOS): "
                "CMAKE_ARGS='-DGGML_METAL=on' uv sync --extra local."
            ) from exc
        if not model_path.is_file():
            raise ProviderResponseError(
                f"Local GGUF model file does not exist: {model_path}"
            )
        if "q5_k_m" not in model_path.name.casefold():
            raise ProviderResponseError(
                "Local Qwen model must be the Q5_K_M GGUF variant; expected a "
                "model filename containing 'q5_k_m'."
            )
        if not llama_supports_gpu_offload():
            raise ProviderResponseError(
                "Local model execution requires GPU offload support, but the "
                "installed llama-cpp-python build cannot use a GPU. Reinstall "
                "the local extra with Metal or CUDA support."
            )
        try:
            self._llama: Any = Llama(
                model_path=str(model_path),
                n_ctx=n_ctx,
                n_gpu_layers=-1,
                verbose=False,
            )
        except Exception as exc:
            raise LocalModelRuntimeError(
                "Local Qwen GPU model failed to initialize. The model was "
                "required to offload all layers to the GPU; no CPU fallback "
                f"is allowed. Model={model_path}, context={n_ctx}. "
                f"Underlying error: {exc}"
            ) from exc

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self._llama.create_chat_completion(
                messages=messages,
                temperature=0,
                max_tokens=MAX_COMPLETION_TOKENS,
                response_format=(
                    {
                        "type": "json_object",
                        "schema": dict(response_schema),
                    }
                    if response_schema is not None
                    else {"type": "json_object"}
                ),
            )
        except Exception as exc:
            raise LocalModelRuntimeError(
                "Local Qwen GPU inference failed. The workflow cannot continue "
                "with a CPU fallback. Check Metal/CUDA availability, GPU memory, "
                f"and POWDRR_LOCAL_MODEL_CONTEXT. Underlying error: {exc}"
            ) from exc
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderResponseError(
                "Local LLM response did not include any choices."
            )
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise ProviderResponseError("Local LLM response choice was not an object.")
        message = first_choice.get("message")
        if not isinstance(message, dict):
            raise ProviderResponseError("Local LLM response message was not an object.")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ProviderResponseError("Local LLM response content was empty.")
        return _parse_json_object(content, "Local LLM response content")


class AnthropicChatClient:
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        timeout: float = 120.0,
        api_version: str = "2023-06-01",
        limits: LLMModelLimits | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._api_version = api_version
        self._limits = limits or DEFAULT_MODEL_LIMITS
        self.last_serialized_messages: str | None = None

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _ = response_schema
        serialized_messages = _serialize_messages(messages)
        self.last_serialized_messages = serialized_messages
        system_prompt, conversation_messages = _split_system_message(messages)
        conversation_messages = [
            _anthropic_message(message) for message in conversation_messages
        ]
        serialized_conversation_messages = _serialize_messages(conversation_messages)
        max_tokens, estimated_input_tokens = _request_token_budget(
            messages,
            self._limits,
            serialized_messages=serialized_messages,
        )
        request = Request(
            f"{self._base_url}/v1/messages",
            data=_serialize_anthropic_payload(
                model=self._model,
                max_tokens=max_tokens,
                serialized_messages=serialized_conversation_messages,
                system_prompt=system_prompt,
                response_schema=response_schema,
            ).encode("utf-8"),
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": self._api_version,
                "content-type": "application/json",
            },
            method="POST",
        )
        request_started = time.monotonic()
        try:
            with urlopen(request, timeout=self._timeout) as response:
                raw_response = response.read().decode("utf-8")
        except HTTPError as exc:
            raise ProviderHTTPError(
                "Anthropic",
                exc.code,
                exc.read().decode("utf-8", errors="replace"),
            ) from exc
        except URLError as exc:
            raise ProviderResponseError(
                f"Anthropic request failed: {exc.reason}"
            ) from exc
        except ConnectionError as exc:
            raise ProviderResponseError(
                f"Anthropic request connection dropped: {exc}"
            ) from exc
        except TimeoutError as exc:
            raise ProviderResponseError(
                _provider_timeout_message(
                    provider="Anthropic",
                    model=self._model,
                    endpoint=request.full_url,
                    timeout=self._timeout,
                    elapsed=time.monotonic() - request_started,
                    message=str(exc),
                    message_count=len(conversation_messages),
                    max_tokens=max_tokens,
                    estimated_input_tokens=estimated_input_tokens,
                )
            ) from exc

        loaded_response = _parse_json_object(
            raw_response,
            "Anthropic response",
        )
        content = loaded_response.get("content")
        if not isinstance(content, list) or not content:
            raise ProviderResponseError(
                "Anthropic response did not include any content."
            )

        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if (
                block.get("type") == "tool_use"
                and block.get("name") == "workflow_action"
                and isinstance(block.get("input"), dict)
            ):
                return cast(dict[str, Any], block["input"])
            if block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                text_parts.append(text)

        response_text = "".join(text_parts).strip()
        if not response_text:
            raise ProviderResponseError("Anthropic response content was empty.")

        return _parse_json_object(response_text, "Anthropic response content")


def _provider_timeout_message(
    *,
    provider: str,
    model: str,
    endpoint: str,
    timeout: float,
    elapsed: float,
    message: str,
    message_count: int,
    max_tokens: int,
    estimated_input_tokens: int,
) -> str:
    return (
        f"{provider} request timed out for model {model!r}: {message}. "
        f"Elapsed {elapsed:.1f}s of configured {timeout:g}s timeout; "
        f"endpoint={endpoint!r}, messages={message_count}, "
        f"estimated_input_tokens={estimated_input_tokens}, "
        f"max_tokens={max_tokens}."
    )


def _serialize_messages(messages: Sequence[Mapping[str, str]]) -> str:
    return json.dumps(messages, ensure_ascii=False, separators=(",", ":"))


def _remove_schema_keywords(
    schema: Mapping[str, Any], unsupported: frozenset[str]
) -> dict[str, Any]:
    """Copy a JSON schema while removing provider-unsupported keywords."""

    def convert(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                key: convert(child)
                for key, child in value.items()
                if key not in unsupported
            }
        if isinstance(value, list):
            return [convert(child) for child in value]
        return value

    return convert(schema)


def _serialize_openai_payload(
    payload: Mapping[str, Any],
    *,
    serialized_messages: str,
) -> str:
    return (
        "{"
        + '"model":'
        + json.dumps(payload["model"], ensure_ascii=False)
        + ',"messages":'
        + serialized_messages
        + ',"temperature":'
        + json.dumps(payload["temperature"])
        + ',"max_tokens":'
        + json.dumps(payload["max_tokens"])
        + ',"response_format":'
        + json.dumps(payload["response_format"], separators=(",", ":"))
        + ',"stream":'
        + json.dumps(payload["stream"])
        + "}"
    )


def _serialize_anthropic_payload(
    *,
    model: str,
    max_tokens: int,
    serialized_messages: str,
    system_prompt: str | None,
    response_schema: Mapping[str, Any] | None = None,
) -> str:
    serialized = (
        "{"
        + '"model":'
        + json.dumps(model, ensure_ascii=False)
        + ',"max_tokens":'
        + json.dumps(max_tokens)
        + ',"messages":'
        + serialized_messages
    )
    if system_prompt is not None:
        serialized += ',"system":' + json.dumps(system_prompt, ensure_ascii=False)
    if response_schema is not None:
        serialized += (
            ',"tools":['
            '{"name":"workflow_action","description":"Return the next workflow '
            'action.","input_schema":'
            + json.dumps(response_schema, ensure_ascii=False, separators=(",", ":"))
            + '}],"tool_choice":{"type":"tool","name":"workflow_action"}'
        )
    return serialized + "}"


def _request_token_budget(
    messages: list[dict[str, str]],
    limits: LLMModelLimits,
    *,
    serialized_messages: str | None = None,
) -> tuple[int, int]:
    estimated_input_tokens = _estimate_message_tokens(
        messages,
        serialized_messages=serialized_messages,
    )
    available_output_tokens = (
        limits.context_window - estimated_input_tokens - _CONTEXT_SAFETY_MARGIN_TOKENS
    )
    if available_output_tokens < 1:
        raise ProviderResponseError(
            "Model context window is exhausted: "
            f"estimated input is {estimated_input_tokens} tokens, "
            f"context window is {limits.context_window} tokens."
        )
    return (
        min(MAX_COMPLETION_TOKENS, limits.max_output_tokens, available_output_tokens),
        estimated_input_tokens,
    )


def _estimate_message_tokens(
    messages: list[dict[str, str]],
    *,
    serialized_messages: str | None = None,
) -> int:
    serialized = serialized_messages or _serialize_messages(messages)
    return max(
        1,
        math.ceil(len(serialized) / _TOKEN_ESTIMATE_CHARS_PER_TOKEN),
    )


class _ModelUnavailableError(ProviderClientError):
    pass


class _SemanticRepairExhaustedError(_ModelUnavailableError):
    """Raised when a model repeats an invalid structured response."""


class _EmptyProviderResponseError(ProviderClientError):
    def __init__(
        self,
        message: str,
        *,
        messages: Sequence[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.messages = messages


class LocalModelRuntimeError(ProviderClientError):
    """Raised when the required local GPU model cannot run."""


def build_provider_client(
    *,
    provider: str,
    model: str,
    api_key: str,
    base_url: str,
    local_model_path: Path | None = None,
    local_context: int = _DEFAULT_LOCAL_MODEL_CONTEXT,
    progress_stream: TextIO | None = None,
    timeout: float = 120.0,
) -> ProviderClient:
    definition = provider_definition(provider)
    if definition.client_kind == "local":
        if local_model_path is None:
            raise ProviderResponseError("A local model path is required.")
        return LocalLlamaChatClient(
            model_path=local_model_path,
            n_ctx=local_context,
        )
    limits = provider_model_limits(
        provider,
        model,
        local_context=local_context,
    )
    if definition.client_kind == "anthropic":
        return AnthropicChatClient(
            model=model,
            api_key=api_key,
            base_url=base_url,
            limits=limits,
            timeout=timeout,
        )
    return OpenAIChatClient(
        model=model,
        api_key=api_key,
        base_url=base_url,
        limits=limits,
        progress_stream=progress_stream,
        reasoning_effort="none" if provider.startswith("deepinfra") else None,
        unsupported_schema_keywords=(
            frozenset({"uniqueItems"})
            if provider.startswith("deepinfra")
            else frozenset()
        ),
        timeout=timeout,
    )


def _parse_json_object(content: str, context: str) -> dict[str, Any]:
    normalized_content = content.strip()
    try:
        parsed_content = json.loads(normalized_content)
    except json.JSONDecodeError as exc:
        try:
            parsed_content = laga.repair(normalized_content)
        except laga.LagaError:
            parsed_content = _extract_embedded_json_object(normalized_content)
            if parsed_content is None:
                raise ProviderResponseError(
                    f"{context} was not valid JSON: {exc.msg} at line "
                    f"{exc.lineno}, column {exc.colno}.\nResponse content:\n{content}",
                    error_code="invalid_json_response",
                ) from exc
    if not isinstance(parsed_content, dict):
        raise ProviderResponseError(
            f"{context} must be a JSON object.",
            error_code="invalid_json_response",
        )
    return cast("dict[str, Any]", parsed_content)


def _extract_embedded_json_object(content: str) -> dict[str, Any] | None:
    """Accept JSON objects surrounded by common LLM presentation noise."""
    fenced_blocks = re.findall(
        r"```(?:json|JSON)?\s*\n?(.*?)```",
        content,
        flags=re.DOTALL,
    )
    candidates = [*fenced_blocks, content]
    decoder = json.JSONDecoder()
    for candidate in candidates:
        for index, character in enumerate(candidate):
            if character != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(candidate, index)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return cast("dict[str, Any]", parsed)
    return None


def _split_system_message(
    messages: list[dict[str, str]],
) -> tuple[str | None, list[dict[str, str]]]:
    if not messages:
        return None, []
    first_message = messages[0]
    if first_message.get("role") != "system":
        return None, list(messages)
    system_content = first_message.get("content")
    if not isinstance(system_content, str):
        return None, list(messages)
    return system_content, list(messages[1:])


def _anthropic_message(message: dict[str, str]) -> dict[str, Any]:
    role = message.get("role")
    content = message.get("content")
    if role not in {"user", "assistant"}:
        raise ProviderResponseError(
            "Anthropic messages must use user or assistant roles after splitting "
            "the system prompt."
        )
    if not isinstance(content, str):
        raise ProviderResponseError("Anthropic message content must be a string.")
    return {
        "role": role,
        "content": [{"type": "text", "text": content}],
    }
