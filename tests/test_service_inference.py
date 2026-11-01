from __future__ import annotations

import io
import json
from email.message import Message
from typing import Any
from urllib.error import HTTPError

import pytest
from powdrr_contracts.models import ChatCompletionRequest
from powdrr_local.bridge import JsonServiceBridge
from powdrr_service.local_service import LocalPowdrrService
from powdrr_service.providers import inference
from powdrr_service.providers.errors import (
    ProviderClientError,
    ProviderHTTPError,
    ProviderResponseError,
)
from powdrr_service.providers.inference import DeepInfraInferenceClient


def _request(**options: Any) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="Qwen/Qwen3-Next-80B-A3B-Instruct",
        messages=({"role": "user", "content": "run the tool"},),
        tools=(
            {
                "type": "function",
                "function": {"name": "bash", "parameters": {"type": "object"}},
            },
        ),
        options=options,
        request_id="request-123",
    )


def test_service_infer_forwards_chat_request_and_preserves_tool_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    response_body = {
        "id": "chatcmpl-1",
        "model": "Qwen/Qwen3-Next-80B-A3B-Instruct",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "bash", "arguments": "{}"},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 23, "completion_tokens": 4},
    }

    def open_request(request: Any, *, timeout: float) -> io.BytesIO:
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return io.BytesIO(json.dumps(response_body).encode())

    monkeypatch.setattr(inference, "urlopen", open_request)
    service = LocalPowdrrService(
        inference_client=DeepInfraInferenceClient(api_key="service-secret")
    )

    response = JsonServiceBridge(service).infer(
        _request(temperature=0.2, max_tokens=2048)
    )

    assert captured["url"] == inference.DEFAULT_DEEPINFRA_CHAT_COMPLETIONS_URL
    assert captured["headers"]["Authorization"] == "Bearer service-secret"
    assert captured["body"] == {
        "model": "Qwen/Qwen3-Next-80B-A3B-Instruct",
        "messages": [{"role": "user", "content": "run the tool"}],
        "tools": [
            {
                "type": "function",
                "function": {"name": "bash", "parameters": {"type": "object"}},
            }
        ],
        "temperature": 0.2,
        "max_tokens": 2048,
    }
    assert response.content is None
    assert response.finish_reason == "tool_calls"
    assert response.tool_calls == (
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "bash", "arguments": "{}"},
        },
    )
    assert response.usage == response_body["usage"]
    assert response.provider_data == response_body
    assert "service-secret" not in json.dumps(response.to_dict())


def test_service_infer_requires_a_service_side_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPINFRA_API_TOKEN", raising=False)
    monkeypatch.delenv("DEEPINFRA_API_KEY", raising=False)

    with pytest.raises(ProviderClientError, match="requires an API key"):
        DeepInfraInferenceClient().complete(_request())


@pytest.mark.parametrize("reserved", ["model", "messages", "tools"])
def test_service_infer_options_cannot_override_request_fields(reserved: str) -> None:
    with pytest.raises(ProviderResponseError, match="reserved fields"):
        DeepInfraInferenceClient(api_key="key").complete(
            _request(**{reserved: "override"})
        )


def test_service_infer_maps_http_errors_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_request(*_args: Any, **_kwargs: Any) -> None:
        raise HTTPError(
            inference.DEFAULT_DEEPINFRA_CHAT_COMPLETIONS_URL,
            429,
            "rate limited",
            Message(),
            io.BytesIO(b"slow down"),
        )

    monkeypatch.setattr(inference, "urlopen", fail_request)

    with pytest.raises(ProviderHTTPError, match="HTTP 429") as raised:
        DeepInfraInferenceClient(api_key="key").complete(_request())
    assert raised.value.status_code == 429


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"choices": []},
        {"choices": [{"message": {"content": 42}}]},
        {"choices": [{"message": {"tool_calls": "bad"}}]},
        {"choices": [{"finish_reason": 42, "message": {}}]},
    ],
)
def test_service_infer_rejects_malformed_provider_responses(payload: object) -> None:
    with pytest.raises(ProviderResponseError, match="DeepInfra"):
        inference._response(payload)
