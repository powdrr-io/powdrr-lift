from __future__ import annotations

from collections.abc import Callable

import pytest
from powdrr_contracts.models import ChatCompletionRequest, ChatCompletionResponse
from powdrr_local.miniswe_inference import create_miniswe_inference_handler


class FakeInferenceClient:
    def __init__(self, response: ChatCompletionResponse) -> None:
        self.response = response
        self.requests: list[ChatCompletionRequest] = []

    def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        self.requests.append(request)
        return self.response


def test_local_composition_routes_worker_request_through_json_service_bridge() -> None:
    response = ChatCompletionResponse(
        content=None,
        tool_calls=(
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "bash", "arguments": '{"command":"pwd"}'},
            },
        ),
        finish_reason="tool_calls",
        usage={"prompt_tokens": 9, "completion_tokens": 3},
    )
    client = FakeInferenceClient(response)
    handler = create_miniswe_inference_handler(300.0, inference_client=client)
    request = ChatCompletionRequest(
        model="deepinfra/test-model",
        messages=({"role": "user", "content": "run pwd"},),
        request_id="ipc-request-1",
    )

    actual = handler(request)

    assert actual == response
    assert client.requests == [request]


def test_local_composition_bounds_provider_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, float] = {}
    response = ChatCompletionResponse(content="ok")

    class CapturingInferenceClient(FakeInferenceClient):
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout
            super().__init__(response)

    monkeypatch.setattr(
        "powdrr_local.miniswe_inference.DeepInfraInferenceClient",
        CapturingInferenceClient,
    )
    handler: Callable[[ChatCompletionRequest], ChatCompletionResponse] = (
        create_miniswe_inference_handler(1000.0)
    )

    actual = handler(
        ChatCompletionRequest(
            model="deepinfra/test-model",
            messages=({"role": "user", "content": "respond"},),
        )
    )

    assert captured["timeout"] == 120.0
    assert actual == response
