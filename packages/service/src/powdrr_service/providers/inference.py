"""OpenAI-compatible model inference routed through service-held credentials."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from powdrr_contracts.models import ChatCompletionRequest, ChatCompletionResponse

from powdrr_service.providers.config import ISOQUANT_MODEL, provider_definition
from powdrr_service.providers.errors import (
    ProviderClientError,
    ProviderHTTPError,
    ProviderResponseError,
)

DEFAULT_DEEPINFRA_CHAT_COMPLETIONS_URL = (
    "https://api.deepinfra.com/v1/openai/chat/completions"
)
DEFAULT_DEEPINFRA_BASE_URL = "https://api.deepinfra.com/v1/openai"
_RESERVED_OPTIONS = frozenset({"model", "messages", "tools"})


class InferenceClient(Protocol):
    def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse: ...


class OpenAIInferenceClient:
    """Forward compatible chat completions using service-held credentials."""

    provider = "deepinfra"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        timeout: float = 300.0,
    ) -> None:
        definition = provider_definition(self.provider)
        self._display_name = definition.display_name
        self._api_key = api_key or next(
            (
                os.environ[name]
                for name in definition.api_key_env_names
                if os.environ.get(name)
            ),
            None,
        )
        configured_endpoint = endpoint or (
            os.environ.get("DEEPINFRA_CHAT_COMPLETIONS_URL")
            if self.provider == "deepinfra"
            else None
        )
        if configured_endpoint:
            self._endpoint = configured_endpoint
        else:
            base_url = next(
                (
                    os.environ[name]
                    for name in definition.base_url_env_names
                    if os.environ.get(name)
                ),
                definition.default_base_url,
            )
            self._endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self._timeout = timeout

    def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        if not self._api_key:
            raise ProviderClientError(
                f"{self._display_name} inference requires an API key."
            )
        overrides = _RESERVED_OPTIONS.intersection(request.options)
        if overrides:
            names = ", ".join(sorted(overrides))
            raise ProviderResponseError(
                f"chat completion options cannot override reserved fields: {names}",
                error_code="invalid_inference_request",
            )
        payload = dict(request.options)
        # IPC keeps the routing prefix out of the upstream API's model ID.
        payload["model"] = request.model.removeprefix(f"{self.provider}/")
        if self.provider == "isoquant":
            payload["model"] = ISOQUANT_MODEL
            payload.setdefault("reasoning_effort", "low")
            if not any(
                key in payload
                for key in ("max_tokens", "max_completion_tokens", "max_output_tokens")
            ):
                payload["max_tokens"] = 4096
        payload["messages"] = list(request.messages)
        if request.tools:
            payload["tools"] = list(request.tools)
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        http_request = Request(
            self._endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(http_request, timeout=self._timeout) as response:
                raw = response.read()
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise ProviderHTTPError(self._display_name, error.code, detail) from error
        except (URLError, TimeoutError, ConnectionError) as error:
            raise ProviderClientError(
                f"{self._display_name} inference request failed: {error}"
            ) from error

        try:
            data = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProviderResponseError(
                f"{self._display_name} returned invalid JSON.",
                error_code="invalid_inference_response",
            ) from error
        return _response(data, self._display_name)


class DeepInfraInferenceClient(OpenAIInferenceClient):
    """Forward chat completions to DeepInfra with service-held credentials."""


class IsoquantInferenceClient(OpenAIInferenceClient):
    """Use Isoquant's pinned Flash model for coding inference."""

    provider = "isoquant"


class RoutedInferenceClient:
    """Route coding models without sharing provider credentials with workers."""

    def __init__(
        self, *, timeout: float = 300.0, default_client: InferenceClient | None = None
    ) -> None:
        self._deepinfra = default_client or DeepInfraInferenceClient(timeout=timeout)
        self._isoquant = IsoquantInferenceClient(timeout=timeout)

    def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        if request.model.startswith("isoquant/") or request.model == ISOQUANT_MODEL:
            return self._isoquant.complete(request)
        return self._deepinfra.complete(request)


def _response(data: object, provider: str = "DeepInfra") -> ChatCompletionResponse:
    if not isinstance(data, dict):
        raise ProviderResponseError(
            f"{provider} response must be a JSON object.",
            error_code="invalid_inference_response",
        )
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProviderResponseError(
            f"{provider} response did not include a chat completion choice.",
            error_code="invalid_inference_response",
        )
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ProviderResponseError(
            f"{provider} response choice did not include an assistant message.",
            error_code="invalid_inference_response",
        )
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise ProviderResponseError(
            f"{provider} assistant content must be a string or null.",
            error_code="invalid_inference_response",
        )
    tool_calls = message.get("tool_calls") or []
    if not isinstance(tool_calls, list) or not all(
        isinstance(call, dict) for call in tool_calls
    ):
        raise ProviderResponseError(
            f"{provider} tool_calls must be an array of objects.",
            error_code="invalid_inference_response",
        )
    finish_reason = choice.get("finish_reason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise ProviderResponseError(
            f"{provider} finish_reason must be a string or null.",
            error_code="invalid_inference_response",
        )
    usage = data.get("usage") or {}
    if not isinstance(usage, Mapping):
        raise ProviderResponseError(
            f"{provider} usage must be a JSON object.",
            error_code="invalid_inference_response",
        )
    return ChatCompletionResponse(
        content=content,
        tool_calls=tuple(tool_calls),
        finish_reason=finish_reason,
        usage=dict(usage),
        provider_data=data,
    )
