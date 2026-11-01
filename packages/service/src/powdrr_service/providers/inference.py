"""OpenAI-compatible model inference routed through service-held credentials."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from powdrr_contracts.models import ChatCompletionRequest, ChatCompletionResponse

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


class DeepInfraInferenceClient:
    """Forward chat completions to DeepInfra without exposing its key to agents."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        timeout: float = 300.0,
    ) -> None:
        self._api_key = (
            api_key
            or os.environ.get("DEEPINFRA_API_TOKEN")
            or os.environ.get("DEEPINFRA_API_KEY")
        )
        configured_endpoint = endpoint or os.environ.get(
            "DEEPINFRA_CHAT_COMPLETIONS_URL"
        )
        if configured_endpoint:
            self._endpoint = configured_endpoint
        else:
            base_url = (
                os.environ.get("DEEPINFRA_BASE_URL") or DEFAULT_DEEPINFRA_BASE_URL
            )
            self._endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self._timeout = timeout

    def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        if not self._api_key:
            raise ProviderClientError("DeepInfra inference requires an API key.")
        overrides = _RESERVED_OPTIONS.intersection(request.options)
        if overrides:
            names = ", ".join(sorted(overrides))
            raise ProviderResponseError(
                f"chat completion options cannot override reserved fields: {names}",
                error_code="invalid_inference_request",
            )
        payload = dict(request.options)
        payload["model"] = request.model
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
            raise ProviderHTTPError("DeepInfra", error.code, detail) from error
        except (URLError, TimeoutError, ConnectionError) as error:
            raise ProviderClientError(
                f"DeepInfra inference request failed: {error}"
            ) from error

        try:
            data = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProviderResponseError(
                "DeepInfra returned invalid JSON.",
                error_code="invalid_inference_response",
            ) from error
        return _response(data)


def _response(data: object) -> ChatCompletionResponse:
    if not isinstance(data, dict):
        raise ProviderResponseError(
            "DeepInfra response must be a JSON object.",
            error_code="invalid_inference_response",
        )
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProviderResponseError(
            "DeepInfra response did not include a chat completion choice.",
            error_code="invalid_inference_response",
        )
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ProviderResponseError(
            "DeepInfra response choice did not include an assistant message.",
            error_code="invalid_inference_response",
        )
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise ProviderResponseError(
            "DeepInfra assistant content must be a string or null.",
            error_code="invalid_inference_response",
        )
    tool_calls = message.get("tool_calls") or []
    if not isinstance(tool_calls, list) or not all(
        isinstance(call, dict) for call in tool_calls
    ):
        raise ProviderResponseError(
            "DeepInfra tool_calls must be an array of objects.",
            error_code="invalid_inference_response",
        )
    finish_reason = choice.get("finish_reason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise ProviderResponseError(
            "DeepInfra finish_reason must be a string or null.",
            error_code="invalid_inference_response",
        )
    usage = data.get("usage") or {}
    if not isinstance(usage, Mapping):
        raise ProviderResponseError(
            "DeepInfra usage must be a JSON object.",
            error_code="invalid_inference_response",
        )
    return ChatCompletionResponse(
        content=content,
        tool_calls=tuple(tool_calls),
        finish_reason=finish_reason,
        usage=dict(usage),
        provider_data=data,
    )
