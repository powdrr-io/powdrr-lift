from __future__ import annotations

import io
import json
from typing import Any
from urllib.request import Request

import pytest
from powdrr_contracts.models import ChatCompletionRequest
from powdrr_local.miniswe_inference import create_miniswe_inference_handler
from powdrr_service.providers.clients import build_provider_client
from powdrr_service.providers.config import ALL_LLM_TYPES, ISOQUANT_MODEL
from powdrr_service.providers.credentials import (
    ProviderCredentialsError,
    auto_provider_candidates,
    resolve_provider_credentials,
)
from powdrr_service.providers.errors import ProviderClientError
from powdrr_service.providers.selection import (
    initial_model_for_provider,
    resolve_llm_mapping,
)

from powdrr_lift.workrr.coding_agent import _remove_provider_credentials


@pytest.mark.parametrize("llm_type", ALL_LLM_TYPES)
def test_isoquant_uses_the_pinned_model_for_every_task(llm_type: str) -> None:
    mapping = resolve_llm_mapping(llm_type, mappings=(), provider="isoquant")
    assert mapping is not None
    assert (mapping.model, mapping.provider) == (ISOQUANT_MODEL, "isoquant")
    assert mapping.backup_model is None
    assert initial_model_for_provider("isoquant", "different-model") == ISOQUANT_MODEL


def test_isoquant_credentials_and_auto_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ISOQUANT_API_KEY", "isoquant-secret")
    monkeypatch.delenv("ISOQUANT_BASE_URL", raising=False)
    credentials = resolve_provider_credentials("isoquant")
    assert credentials.api_key == "isoquant-secret"
    assert credentials.source == "ISOQUANT_API_KEY"
    assert credentials.base_url == "https://api.isoquant.ai/v1"
    assert auto_provider_candidates()[0] == "isoquant"
    monkeypatch.setenv("ISOQUANT_BASE_URL", "https://custom.example/v1")
    assert resolve_provider_credentials("isoquant").base_url == (
        "https://custom.example/v1"
    )


def test_isoquant_reports_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ISOQUANT_API_KEY", raising=False)
    with pytest.raises(ProviderCredentialsError, match="ISOQUANT_API_KEY"):
        resolve_provider_credentials("isoquant")


def test_isoquant_structured_request_uses_expected_wire_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def capture(request: Request, *, timeout: float) -> object:
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        assert isinstance(request.data, bytes)
        captured["payload"] = json.loads(request.data)
        raise RuntimeError("captured request")

    monkeypatch.setattr("powdrr_service.providers.clients.urlopen", capture)
    credentials = resolve_provider_credentials("isoquant", "service-secret")
    client = build_provider_client(
        provider="isoquant",
        model="ignored-model",
        api_key=credentials.api_key,
        base_url=credentials.base_url,
    )
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    with pytest.raises(RuntimeError, match="captured request"):
        client.complete_json(
            [{"role": "user", "content": "return json"}], response_schema=schema
        )
    assert captured["url"] == "https://api.isoquant.ai/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer service-secret"
    payload = captured["payload"]
    assert payload["model"] == ISOQUANT_MODEL
    assert payload["reasoning_effort"] == "low"
    assert payload["max_tokens"] > 0
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["stream"] is True


@pytest.mark.parametrize("model", [ISOQUANT_MODEL, f"isoquant/{ISOQUANT_MODEL}"])
def test_miniswe_isoquant_routing_preserves_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
) -> None:
    monkeypatch.setenv("ISOQUANT_API_KEY", "isoquant-secret")
    monkeypatch.delenv("ISOQUANT_BASE_URL", raising=False)
    captured: dict[str, Any] = {}
    tool_calls = [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "bash", "arguments": '{"command":"pwd"}'},
        }
    ]

    def capture(request: Request, *, timeout: float) -> io.BytesIO:
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        assert isinstance(request.data, bytes)
        captured["payload"] = json.loads(request.data)
        return io.BytesIO(
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {"content": None, "tool_calls": tool_calls},
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 4},
                }
            ).encode()
        )

    monkeypatch.setattr("powdrr_service.providers.inference.urlopen", capture)
    handler = create_miniswe_inference_handler(300.0)
    response = handler(
        ChatCompletionRequest(
            model=model,
            messages=({"role": "user", "content": "run pwd"},),
            tools=({"type": "function", "function": {"name": "bash"}},),
        )
    )
    assert captured["url"] == "https://api.isoquant.ai/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer isoquant-secret"
    assert captured["payload"]["model"] == ISOQUANT_MODEL
    assert captured["payload"]["reasoning_effort"] == "low"
    assert captured["payload"]["max_tokens"] == 4096
    assert captured["payload"]["tools"][0]["function"]["name"] == "bash"
    assert response.tool_calls == tuple(tool_calls)
    assert response.finish_reason == "tool_calls"
    assert "isoquant-secret" not in json.dumps(response.to_dict())


def test_missing_isoquant_key_does_not_fall_back_to_deepinfra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ISOQUANT_API_KEY", raising=False)
    monkeypatch.setenv("DEEPINFRA_API_TOKEN", "deepinfra-secret")
    handler = create_miniswe_inference_handler(300.0)
    with pytest.raises(ProviderClientError, match="Isoquant.*API key"):
        handler(
            ChatCompletionRequest(
                model=f"isoquant/{ISOQUANT_MODEL}",
                messages=({"role": "user", "content": "hello"},),
            )
        )


def test_isoquant_credentials_are_removed_from_worker_environment() -> None:
    environment = {
        "ISOQUANT_API_KEY": "secret",
        "ISOQUANT_BASE_URL": "url",
        "PATH": "/bin",
    }
    _remove_provider_credentials(environment)
    assert environment == {"PATH": "/bin"}
