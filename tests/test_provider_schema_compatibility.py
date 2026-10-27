from __future__ import annotations

import json
from typing import Any, cast
from urllib.request import Request

import pytest

from powdrr_lift.workrr.protocol import SchemaAwareWorkflowLLMClient
from powdrr_lift.workrr.providers import (
    OpenAIChatClient,
    _remove_schema_keywords,
    build_provider_client,
)


def test_deepinfra_client_declares_unsupported_schema_keywords() -> None:
    client = cast(
        OpenAIChatClient,
        build_provider_client(
            provider="deepinfra-cheap",
            model="deepseek-ai/DeepSeek-V4-Flash-0731",
            api_key="test-token",
            base_url="https://api.deepinfra.com/v1/openai",
        ),
    )

    assert client._unsupported_schema_keywords == frozenset({"uniqueItems"})


def test_remove_schema_keywords_recursively_copies_schema() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "refs": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "string"},
            }
        },
        "uniqueItems": True,
    }

    result = _remove_schema_keywords(schema, frozenset({"uniqueItems"}))

    assert result == {
        "type": "object",
        "properties": {"refs": {"type": "array", "items": {"type": "string"}}},
    }
    assert schema["uniqueItems"] is True
    assert schema["properties"]["refs"]["uniqueItems"] is True


def test_deepinfra_request_omits_unique_items_keyword(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = cast(
        SchemaAwareWorkflowLLMClient,
        build_provider_client(
            provider="deepinfra-cheap",
            model="deepseek-ai/DeepSeek-V4-Flash-0731",
            api_key="test-token",
            base_url="https://api.deepinfra.com/v1/openai",
        ),
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "evidence_ids": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "string"},
            }
        },
        "required": ["evidence_ids"],
        "additionalProperties": False,
    }
    captured: dict[str, Any] = {}

    def capture_request(request: Request, *, timeout: float) -> object:
        del timeout
        assert isinstance(request.data, bytes)
        captured.update(json.loads(request.data.decode("utf-8")))
        raise RuntimeError("stop after capturing request")

    monkeypatch.setattr("powdrr_lift.workrr.providers.urlopen", capture_request)
    with pytest.raises(RuntimeError, match="stop after capturing request"):
        client.complete_json(
            [{"role": "user", "content": "return evidence ids"}],
            response_schema=schema,
        )

    response_format = captured["response_format"]
    assert isinstance(response_format, dict)
    json_schema = response_format["json_schema"]
    assert isinstance(json_schema, dict)
    transmitted_schema = json_schema["schema"]
    assert isinstance(transmitted_schema, dict)
    assert "uniqueItems" not in transmitted_schema["properties"]["evidence_ids"]
    assert schema["properties"]["evidence_ids"]["uniqueItems"] is True
