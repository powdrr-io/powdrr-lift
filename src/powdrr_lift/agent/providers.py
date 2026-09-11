"""Concrete provider clients and response handling for agent execution."""

from __future__ import annotations

import importlib
import json
import math
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import laga

from powdrr_lift.agent.provider_config import (
    DEFAULT_MODEL_LIMITS,
    LLM_PROVIDERS,
    MAX_COMPLETION_TOKENS,
    LLMModelLimits,
    provider_definition,
)
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.workflow_llm import (
    ProviderExecutionError,
    WorkflowLLMClient,
    WorkflowLLMHTTPError,
)

_DEFAULT_LOCAL_MODEL_CONTEXT = 24576
LOCAL_MODEL_PATTERN = "qwen2.5-coder-14b-instruct-q5_k_m*.gguf"
_TOKEN_ESTIMATE_CHARS_PER_TOKEN = 3
_CONTEXT_SAFETY_MARGIN_TOKENS = 1024
_MAX_STREAM_CHUNKS = 4096
_MAX_STREAM_CONTENT_CHARS = 131072


@dataclass(frozen=True, slots=True)
class ProviderCredentials:
    provider: str
    api_key: str
    source: str
    base_url: str
    base_url_source: str


def resolve_provider_credentials(
    provider: str,
    api_key_override: str | None = None,
    base_url_override: str | None = None,
) -> ProviderCredentials:
    definition = provider_definition(provider)
    if api_key_override:
        api_key, source = api_key_override, "--api-key"
    elif definition.client_kind == "local":
        api_key, source = "local", "local"
    else:
        api_key = ""
        source = ""
        for env_name in definition.api_key_env_names:
            value = os.environ.get(env_name)
            if value:
                api_key, source = value, env_name
                break
        if not api_key and provider == "openai":
            access_token = _resolve_codex_access_token()
            if access_token is not None:
                api_key, source = access_token, _codex_auth_path_description()
        if not api_key:
            if provider == "openai":
                raise PowdrrExecutionError(
                    "No OpenAI credentials found. Set OPENAI_API_KEY, CODEX_API_KEY, "
                    "or sign in with Codex so ~/.codex/auth.json is available."
                )
            credential_names = " or ".join(definition.api_key_env_names)
            raise PowdrrExecutionError(
                f"No {definition.display_name} credentials found. "
                f"Set {credential_names}, or pass --api-key."
            )

    if base_url_override:
        base_url, base_url_source = base_url_override, "--base-url"
    elif definition.client_kind == "local":
        base_url, base_url_source = "local", "local"
    else:
        base_url, base_url_source = definition.default_base_url, "default"
        for env_name in definition.base_url_env_names:
            value = os.environ.get(env_name)
            if value:
                base_url, base_url_source = value, env_name
                break
    return ProviderCredentials(
        provider=provider,
        api_key=api_key,
        source=source,
        base_url=base_url,
        base_url_source=base_url_source,
    )


def _resolve_codex_access_token() -> str | None:
    auth_path = _resolve_codex_auth_path()
    if not auth_path.exists():
        return None
    try:
        raw_auth = json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(raw_auth, dict):
        return None
    tokens = raw_auth.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return None
    expiry = tokens.get("expiry")
    if isinstance(expiry, str):
        try:
            expiry_dt = datetime.fromisoformat(expiry)
        except ValueError:
            return access_token
        if expiry_dt.tzinfo is None:
            expiry_dt = expiry_dt.replace(tzinfo=UTC)
        if expiry_dt <= datetime.now(UTC):
            return None
    return access_token


def _resolve_codex_auth_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home is not None:
        return Path(codex_home).expanduser() / "auth.json"
    return Path.home() / ".codex" / "auth.json"


def _codex_auth_path_description() -> str:
    return str(_resolve_codex_auth_path())


def provider_has_credentials(provider: str) -> bool:
    definition = provider_definition(provider)
    if provider == "openai" and _resolve_codex_access_token() is not None:
        return True
    return any(os.environ.get(env_name) for env_name in definition.api_key_env_names)


def auto_provider_candidates() -> tuple[str, ...]:
    candidates: list[tuple[int, str]] = []
    for name, definition in LLM_PROVIDERS.items():
        if definition.auto_priority is None or not provider_has_credentials(name):
            continue
        candidates.append((definition.auto_priority, name))
    return tuple(name for _, name in sorted(candidates))


def available_provider_names() -> tuple[str, ...]:
    candidates: list[tuple[int, str]] = []
    for name, definition in LLM_PROVIDERS.items():
        if not definition.api_key_env_names and name != "openai":
            continue
        if not provider_has_credentials(name):
            continue
        priority = definition.auto_priority
        candidates.append((priority if priority is not None else 100, name))
    return tuple(name for _, name in sorted(candidates))


def resolve_local_model_path(model_cache_dir: Path) -> Path:
    cached_model_paths = sorted(model_cache_dir.glob(LOCAL_MODEL_PATTERN))
    if _has_all_local_model_shards(cached_model_paths):
        return cached_model_paths[0]
    raise PowdrrExecutionError(
        "The local Qwen model is not fully cached. Run "
        "`powdrr-lift download-qwen-model` before starting workflow-chat. "
        f"Expected cache={model_cache_dir}."
    )


def _has_all_local_model_shards(model_paths: Sequence[Path]) -> bool:
    if not model_paths:
        return False
    match = re.search(r"-00001-of-(\d+)\.gguf$", model_paths[0].name)
    expected_shards = int(match.group(1)) if match else 1
    return len(model_paths) >= expected_shards


class OpenAIChatClient:
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        timeout: float = 120.0,
        limits: LLMModelLimits | None = None,
        progress_stream: TextIO | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._limits = limits or DEFAULT_MODEL_LIMITS
        self._progress_stream = progress_stream
        self.last_usage: dict[str, Any] = {}
        self.last_serialized_messages: str | None = None

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
                        "schema": dict(response_schema),
                    },
                }
                if response_schema is not None
                else {"type": "json_object"}
            ),
            "stream": True,
        }
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
                )
        except HTTPError as exc:
            raise WorkflowLLMHTTPError(
                "OpenAI",
                exc.code,
                exc.read().decode("utf-8", errors="replace"),
            ) from exc
        except URLError as exc:
            raise ProviderExecutionError(
                f"OpenAI request failed: {exc.reason}"
            ) from exc
        except ConnectionError as exc:
            raise ProviderExecutionError(
                f"OpenAI request connection dropped: {exc}"
            ) from exc
        except TimeoutError as exc:
            raise ProviderExecutionError(
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
        usage = loaded_response.get("usage")
        self.last_usage = dict(usage) if isinstance(usage, dict) else {}
        choices = loaded_response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise PowdrrExecutionError("OpenAI response did not include any choices.")
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise PowdrrExecutionError("OpenAI response choice was not an object.")
        message = first_choice.get("message")
        if not isinstance(message, dict):
            raise PowdrrExecutionError(
                "OpenAI response choice message was not an object."
            )
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise PowdrrExecutionError("OpenAI response message content was empty.")

        return _parse_json_object(content, "OpenAI response content")


def _read_openai_response(
    response: Any,
    *,
    progress_stream: TextIO | None,
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
    response_metadata: dict[str, Any] | None = None
    event_data: list[str] = []
    chunk_count = 0
    stream_complete = False
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
            raise PowdrrExecutionError(
                f"OpenAI streaming response contained invalid JSON: {exc.msg}"
            ) from exc
        if not isinstance(event, dict):
            continue
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
        delta = first_choice.get("delta")
        if not isinstance(delta, dict):
            continue
        content = delta.get("content")
        if isinstance(content, str):
            content_parts.append(content)
            chunk_count += 1
            content_length = sum(len(part) for part in content_parts)
            if (
                chunk_count > _MAX_STREAM_CHUNKS
                or content_length > _MAX_STREAM_CONTENT_CHARS
            ):
                excerpt = "".join(content_parts)[:256]
                raise _ModelUnavailableError(
                    "OpenAI streaming response exceeded the bounded output limit "
                    f"({chunk_count} chunks, {content_length} characters); "
                    f"partial content prefix: {excerpt!r}"
                )
            if progress_stream is not None:
                print(
                    f"received streamed LLM data ({chunk_count} chunks)...",
                    file=progress_stream,
                    flush=True,
                )

    if response_metadata is None:
        raise PowdrrExecutionError(
            "OpenAI streaming response did not include any events."
        )
    if not content_parts:
        raise PowdrrExecutionError("OpenAI streaming response content was empty.")
    if not stream_complete:
        raise _ModelUnavailableError(
            "OpenAI streaming response ended before a completion marker; "
            f"received {chunk_count} content chunks"
        )
    response_metadata["choices"] = [{"message": {"content": "".join(content_parts)}}]
    return json.dumps(response_metadata)


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
            raise PowdrrExecutionError(
                "Local provider requires llama-cpp-python. Install the local "
                "extra (with Metal support on macOS): "
                "CMAKE_ARGS='-DGGML_METAL=on' uv sync --extra local."
            ) from exc
        if not model_path.is_file():
            raise PowdrrExecutionError(
                f"Local GGUF model file does not exist: {model_path}"
            )
        if "q5_k_m" not in model_path.name.casefold():
            raise PowdrrExecutionError(
                "Local Qwen model must be the Q5_K_M GGUF variant; expected a "
                "model filename containing 'q5_k_m'."
            )
        if not llama_supports_gpu_offload():
            raise PowdrrExecutionError(
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
            raise PowdrrExecutionError(
                "Local LLM response did not include any choices."
            )
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise PowdrrExecutionError("Local LLM response choice was not an object.")
        message = first_choice.get("message")
        if not isinstance(message, dict):
            raise PowdrrExecutionError("Local LLM response message was not an object.")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise PowdrrExecutionError("Local LLM response content was empty.")
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
            raise WorkflowLLMHTTPError(
                "Anthropic",
                exc.code,
                exc.read().decode("utf-8", errors="replace"),
            ) from exc
        except URLError as exc:
            raise PowdrrExecutionError(
                f"Anthropic request failed: {exc.reason}"
            ) from exc
        except ConnectionError as exc:
            raise PowdrrExecutionError(
                f"Anthropic request connection dropped: {exc}"
            ) from exc
        except TimeoutError as exc:
            raise PowdrrExecutionError(
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
            raise PowdrrExecutionError(
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
            raise PowdrrExecutionError("Anthropic response content was empty.")

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


def _serialize_prompt_json(value: object) -> str:
    """Serialize structured prompt context without formatting-only whitespace."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


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
        raise PowdrrExecutionError(
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


class _ModelUnavailableError(ProviderExecutionError):
    pass


class _SemanticRepairExhaustedError(_ModelUnavailableError):
    """Raised when a model repeats an invalid structured response."""


class _EmptyProviderResponseError(ProviderExecutionError):
    def __init__(
        self,
        message: str,
        *,
        messages: Sequence[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.messages = messages


class LocalModelRuntimeError(ProviderExecutionError):
    """Raised when the required local GPU model cannot run."""


def provider_model_limits(
    provider: str,
    model: str,
    *,
    local_context: int = _DEFAULT_LOCAL_MODEL_CONTEXT,
) -> LLMModelLimits:
    definition = provider_definition(provider)
    if definition.client_kind == "local":
        return LLMModelLimits(
            context_window=local_context,
            max_output_tokens=MAX_COMPLETION_TOKENS,
        )
    return definition.model_limits.get(model.casefold(), DEFAULT_MODEL_LIMITS)


def build_provider_client(
    *,
    provider: str,
    model: str,
    api_key: str,
    base_url: str,
    local_model_path: Path | None = None,
    local_context: int = _DEFAULT_LOCAL_MODEL_CONTEXT,
    progress_stream: TextIO | None = None,
) -> WorkflowLLMClient:
    definition = provider_definition(provider)
    if definition.client_kind == "local":
        if local_model_path is None:
            raise PowdrrExecutionError("A local model path is required.")
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
        )
    return OpenAIChatClient(
        model=model,
        api_key=api_key,
        base_url=base_url,
        limits=limits,
        progress_stream=progress_stream,
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
                raise PowdrrExecutionError(
                    f"{context} was not valid JSON: {exc.msg} at line "
                    f"{exc.lineno}, column {exc.colno}.\nResponse content:\n{content}"
                ) from exc
    if not isinstance(parsed_content, dict):
        raise PowdrrExecutionError(f"{context} must be a JSON object.")
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
        raise PowdrrExecutionError(
            "Anthropic messages must use user or assistant roles after splitting "
            "the system prompt."
        )
    if not isinstance(content, str):
        raise PowdrrExecutionError("Anthropic message content must be a string.")
    return {
        "role": role,
        "content": [{"type": "text", "text": content}],
    }
