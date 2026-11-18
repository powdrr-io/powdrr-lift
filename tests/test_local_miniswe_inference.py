from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from powdrr_contracts.models import ChatCompletionRequest, ChatCompletionResponse
from powdrr_local.miniswe_inference import (
    create_miniswe_inference_handler,
    trace_miniswe_inference_handler,
)


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


def test_miniswe_trace_records_redacted_success_metadata(tmp_path: Path) -> None:
    response = ChatCompletionResponse(
        content="private response text",
        usage={"prompt_tokens": 11, "completion_tokens": 5, "cost": 0.02},
    )
    trace_path = tmp_path / "trace.jsonl"
    request = ChatCompletionRequest(
        model="deepinfra/test-model",
        messages=({"role": "user", "content": "private prompt text"},),
        request_id="request-123",
    )
    traced = trace_miniswe_inference_handler(
        lambda _: response,
        trace_path=trace_path,
        task_id="task-456",
        attempt_id="attempt-2",
        caller="story_test_author",
        gate_consumer="story_test_baseline_gate",
        evidence_fingerprint="evidence-hash",
        evidence_ids=("story:checkout", "case:retry"),
        check_id="tests/test_resume.py::test_retry",
        candidate_id="base-commit-sha",
        request_response_artifact_ref="attempt-2.traj.json",
    )

    assert traced(request) == response
    record = json.loads(trace_path.read_text(encoding="utf-8"))
    assert record["record_type"] == "powdrr_service.miniswe.provider_attempt"
    assert record["interaction_id"] == "request-123"
    assert record["task_id"] == "task-456"
    assert record["attempt_id"] == "attempt-2"
    assert record["caller"] == "story_test_author"
    assert record["gate_consumer"] == "story_test_baseline_gate"
    assert record["evidence_fingerprint"] == "evidence-hash"
    assert record["evidence_ids"] == ["story:checkout", "case:retry"]
    assert record["check_id"] == "tests/test_resume.py::test_retry"
    assert record["candidate_id"] == "base-commit-sha"
    assert record["request_response_artifact_ref"] == "attempt-2.traj.json"
    assert record["provider"] == "deepinfra"
    assert record["model"] == "deepinfra/test-model"
    assert record["prompt_fingerprint"]
    assert record["usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 5,
        "cost": 0.02,
    }
    assert "private prompt text" not in trace_path.read_text(encoding="utf-8")
    assert "private response text" not in trace_path.read_text(encoding="utf-8")


def test_miniswe_trace_records_error_then_reraises(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    request = ChatCompletionRequest(
        model="isoquant/test-model",
        messages=({"role": "user", "content": "private prompt text"},),
        request_id="request-error",
    )

    def fail(_: ChatCompletionRequest) -> ChatCompletionResponse:
        raise RuntimeError("provider failure containing private prompt text")

    traced = trace_miniswe_inference_handler(
        fail,
        trace_path=trace_path,
        task_id="task-789",
        attempt_id="attempt-1",
    )

    with pytest.raises(RuntimeError, match="provider failure"):
        traced(request)
    record = json.loads(trace_path.read_text(encoding="utf-8"))
    assert record["outcome"] == "error"
    assert record["error_type"] == "RuntimeError"
    assert record["provider"] == "isoquant"
    assert "error message" not in record
    assert "private prompt text" not in trace_path.read_text(encoding="utf-8")
