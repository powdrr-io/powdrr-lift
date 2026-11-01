from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from powdrr_service.providers.clients import _parse_json_object
from powdrr_service.providers.errors import ProviderResponseError

from powdrr_lift.errors import PowdrrExecutionError, ProviderExecutionError
from powdrr_lift.workrr.procedrr import (
    ProcedrrResponseError,
    StructuredToolExecutor,
    WorkrrProcedrrClient,
    _is_retryable_provider_failure,
)


class RepairingClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls = 0

    def complete_json(self, messages: list[dict[str, str]], **_: Any) -> dict[str, Any]:
        del messages
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


SCHEMA = {
    "type": "object",
    "required": ["complete"],
    "properties": {"complete": {"type": "boolean"}},
}


@pytest.mark.parametrize(
    "message",
    [
        "Temporary failure in name resolution",
        "OpenAI streaming response content was empty.",
        "OpenAI response message content was empty.",
    ],
)
def test_procedrr_recognizes_transient_provider_errors(message: str) -> None:
    assert _is_retryable_provider_failure(RuntimeError(message))


def test_procedrr_retries_empty_provider_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class EmptyContentThenSuccess:
        calls = 0

        def complete_json(
            self, _messages: list[dict[str, str]], **_: Any
        ) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                raise PowdrrExecutionError("OpenAI response message content was empty.")
            return {"complete": True}

    provider = EmptyContentThenSuccess()
    client = WorkrrProcedrrClient(
        cast(Any, provider),
        process_directory=tmp_path,
        provider_retry_delay_seconds=2.0,
    )
    delays: list[float] = []
    monkeypatch.setattr("powdrr_lift.workrr.procedrr.time.sleep", delays.append)

    assert client.complete_json(
        [{"role": "user", "content": "judge this"}],
        response_schema=SCHEMA,
    ) == {"complete": True}
    assert provider.calls == 2
    assert delays == [2.0]


def test_procedrr_retries_http_429(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RateLimitedThenSuccess:
        calls = 0

        def complete_json(
            self, _messages: list[dict[str, str]], **_: Any
        ) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                error = RuntimeError("provider is busy")
                error.status_code = 429  # type: ignore[attr-defined]
                raise error
            return {"complete": True}

    provider = RateLimitedThenSuccess()
    client = WorkrrProcedrrClient(
        cast(Any, provider),
        process_directory=tmp_path,
        provider_retry_delay_seconds=2.0,
    )
    delays: list[float] = []
    monkeypatch.setattr("powdrr_lift.workrr.procedrr.time.sleep", delays.append)

    assert client.complete_json(
        [{"role": "user", "content": "judge this"}],
        response_schema=SCHEMA,
    ) == {"complete": True}
    assert provider.calls == 2
    assert delays == [2.0]


def test_procedrr_records_provider_retry_attempts_without_prompt_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RetryOnce:
        calls = 0

        def complete_json(
            self, _messages: list[dict[str, str]], **_: Any
        ) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                error = RuntimeError("upstream unavailable")
                error.status_code = 503  # type: ignore[attr-defined]
                raise error
            return {"complete": True}

    class FallbackWrapper:
        def __init__(self) -> None:
            self._fallback = RetryOnce()

        def complete_json(
            self, messages: list[dict[str, str]], **kwargs: Any
        ) -> dict[str, Any]:
            return self._fallback.complete_json(messages, **kwargs)

    events: list[dict[str, Any]] = []
    monkeypatch.setattr("powdrr_lift.workrr.procedrr.time.sleep", lambda _: None)
    client = WorkrrProcedrrClient(
        cast(Any, FallbackWrapper()),
        process_directory=tmp_path,
        telemetry_sink=lambda record: events.append(dict(record)),
    )

    client.complete_json(
        [{"role": "user", "content": "secret prompt text"}],
        response_schema=SCHEMA,
    )

    assert [event["outcome"] for event in events] == ["retrying", "succeeded"]
    assert [event["attempt"] for event in events] == [1, 2]
    assert events[0]["retryable"] is True
    assert events[0]["trace_id"] == events[1]["trace_id"]
    assert events[0]["provider_delegate"] == "RetryOnce"
    assert "secret prompt text" not in str(events)


def test_procedrr_uses_workrr_schema_repair_transport(tmp_path: Path) -> None:
    provider = RepairingClient([{}, {"complete": True}])
    client = WorkrrProcedrrClient(provider, process_directory=tmp_path)

    result = client.complete_json(
        [{"role": "user", "content": "judge this"}],
        response_schema=SCHEMA,
    )

    assert result == {"complete": True}
    assert provider.calls == 2


def test_procedrr_batch_preserves_order_and_replay_responses(tmp_path: Path) -> None:
    class BatchClient:
        def __init__(self) -> None:
            self.batch_sizes: list[int] = []

        def complete_json(self, *_: Any, **__: Any) -> dict[str, Any]:
            raise AssertionError("individual completion should not be called")

        def complete_json_batch(
            self,
            requests: list[tuple[list[dict[str, str]], Mapping[str, Any]]],
        ) -> list[dict[str, Any]]:
            self.batch_sizes.append(len(requests))
            return [{"complete": True} for _ in requests]

    first = [{"role": "user", "content": "first"}]
    second = [{"role": "user", "content": "second"}]
    provider = BatchClient()
    client = WorkrrProcedrrClient(
        cast(Any, provider),
        process_directory=tmp_path,
        replay_responses={WorkrrProcedrrClient.replay_key(first): {"complete": False}},
    )

    result = client.complete_json_batch([(first, SCHEMA), (second, SCHEMA)])

    assert result == [{"complete": False}, {"complete": True}]
    assert provider.batch_sizes == [1]


def test_procedrr_repairs_non_object_json_provider_response(tmp_path: Path) -> None:
    class NonObjectThenSuccess:
        calls = 0
        messages: list[list[dict[str, str]]] = []

        def complete_json(
            self, messages: list[dict[str, str]], **_: Any
        ) -> dict[str, Any]:
            self.messages.append(messages)
            self.calls += 1
            if self.calls == 1:
                raise PowdrrExecutionError(
                    "OpenAI response content must be a JSON object.",
                    error_code="invalid_json_response",
                )
            return {"complete": True}

    provider = NonObjectThenSuccess()
    client = WorkrrProcedrrClient(
        cast(Any, provider), process_directory=tmp_path, max_retries=1
    )

    result = client.complete_json(
        [{"role": "user", "content": "judge this"}],
        response_schema=SCHEMA,
    )

    assert result == {"complete": True}
    assert provider.calls == 2
    assert "previous response was invalid" in provider.messages[1][-1]["content"]


def test_procedrr_falls_back_for_unsupported_schema_grammar_and_validates_locally(
    tmp_path: Path,
) -> None:
    class LimitedGrammarProvider:
        def __init__(self) -> None:
            self.calls: list[tuple[bool, list[dict[str, str]]]] = []
            self.unconstrained_outputs = iter(
                [{"labels": ["python"]}, {"labels": ["pr-prep"]}]
            )

        def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            response_schema: Mapping[str, Any] | None = None,
        ) -> dict[str, Any]:
            self.calls.append((response_schema is not None, messages))
            if response_schema is not None:
                raise ProviderExecutionError(
                    "OpenAI-compatible provider returned an error event: "
                    '{"type":"invalid_request_error","code":400,'
                    '"message":"Grammar error: Unimplemented keys: [\\"contains\\"]"}'
                )
            return next(self.unconstrained_outputs)

    schema = {
        "type": "object",
        "required": ["labels"],
        "properties": {
            "labels": {
                "type": "array",
                "minItems": 1,
                "contains": {"const": "pr-prep"},
                "items": {"type": "string"},
            }
        },
    }
    provider = LimitedGrammarProvider()
    client = WorkrrProcedrrClient(
        cast(Any, provider),
        process_directory=tmp_path,
        max_retries=1,
        provider_retry_attempts=0,
    )

    result = client.complete_json(
        [{"role": "user", "content": "Choose validation labels."}],
        response_schema=schema,
    )

    assert result == {"labels": ["pr-prep"]}
    assert [has_schema for has_schema, _messages in provider.calls] == [
        True,
        False,
        True,
        False,
    ]
    assert "previous response was invalid" in provider.calls[2][1][-1]["content"]


def test_provider_marks_non_object_json_as_repairable_output() -> None:
    with pytest.raises(ProviderResponseError) as error:
        _parse_json_object('"not an object"', "OpenAI response content")

    assert error.value.error_code == "invalid_json_response"


def test_procedrr_retries_transient_provider_failures_with_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingClient:
        calls = 0

        def complete_json(
            self, _messages: list[dict[str, str]], **_: Any
        ) -> dict[str, Any]:
            self.calls += 1
            if self.calls <= 5:
                error = RuntimeError("upstream failure")
                error.status_code = 503  # type: ignore[attr-defined]
                raise error
            return {"complete": True}

    provider = FailingClient()
    client = WorkrrProcedrrClient(
        cast(Any, provider),
        process_directory=tmp_path,
    )
    delays: list[float] = []
    monkeypatch.setattr("powdrr_lift.workrr.procedrr.time.sleep", delays.append)

    result = client.complete_json(
        [{"role": "user", "content": "judge this"}],
        response_schema=SCHEMA,
    )

    assert result == {"complete": True}
    assert provider.calls == 6
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_procedrr_retries_empty_stream_provider_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class EmptyStreamThenSuccess:
        calls = 0

        def complete_json(
            self, _messages: list[dict[str, str]], **_: Any
        ) -> dict[str, Any]:
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError(
                    "OpenAI streaming response did not include any events."
                )
            return {"complete": True}

    provider = EmptyStreamThenSuccess()
    client = WorkrrProcedrrClient(
        cast(Any, provider),
        process_directory=tmp_path,
        provider_retry_delay_seconds=2.0,
    )
    delays: list[float] = []
    monkeypatch.setattr("powdrr_lift.workrr.procedrr.time.sleep", delays.append)

    assert client.complete_json(
        [{"role": "user", "content": "judge this"}],
        response_schema=SCHEMA,
    ) == {"complete": True}
    assert provider.calls == 3
    assert delays == [2.0, 4.0]


def test_procedrr_retries_truncated_stream_provider_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class TruncatedStreamThenSuccess:
        calls = 0

        def complete_json(
            self, _messages: list[dict[str, str]], **_: Any
        ) -> dict[str, Any]:
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError(
                    "OpenAI streaming response ended before a completion marker; "
                    "received 3 content chunks"
                )
            return {"complete": True}

    provider = TruncatedStreamThenSuccess()
    client = WorkrrProcedrrClient(
        cast(Any, provider),
        process_directory=tmp_path,
        provider_retry_delay_seconds=2.0,
    )
    delays: list[float] = []
    monkeypatch.setattr("powdrr_lift.workrr.procedrr.time.sleep", delays.append)

    assert client.complete_json(
        [{"role": "user", "content": "judge this"}],
        response_schema=SCHEMA,
    ) == {"complete": True}
    assert provider.calls == 3
    assert delays == [2.0, 4.0]


def test_procedrr_does_not_retry_explicit_provider_request_errors(
    tmp_path: Path,
) -> None:
    class UnsupportedSchemaClient:
        calls = 0

        def complete_json(
            self, _messages: list[dict[str, str]], **_: Any
        ) -> dict[str, Any]:
            self.calls += 1
            raise ProviderExecutionError(
                "OpenAI-compatible provider returned an error event: "
                '{"type":"invalid_request_error","code":400,'
                '"message":"Unimplemented keys: [uniqueItems]"}'
            )

    provider = UnsupportedSchemaClient()
    client = WorkrrProcedrrClient(
        cast(Any, provider),
        process_directory=tmp_path,
        provider_retry_attempts=3,
    )

    with pytest.raises(ProviderExecutionError, match="uniqueItems"):
        client.complete_json(
            [{"role": "user", "content": "judge this"}],
            response_schema=SCHEMA,
        )

    assert provider.calls == 1


def test_procedrr_replays_completed_judge_without_provider_call(
    tmp_path: Path,
) -> None:
    provider = RepairingClient([{"complete": False}])
    messages = [{"role": "user", "content": "judge this"}]
    client = WorkrrProcedrrClient(
        provider,
        process_directory=tmp_path,
        replay_responses={
            WorkrrProcedrrClient.replay_key(messages): {"complete": True}
        },
    )

    assert client.complete_json(messages, response_schema=SCHEMA) == {"complete": True}
    assert provider.calls == 0


def test_procedrr_does_not_retry_non_transient_provider_failures(
    tmp_path: Path,
) -> None:
    class FailingClient:
        calls = 0

        def complete_json(
            self, _messages: list[dict[str, str]], **_: Any
        ) -> dict[str, Any]:
            self.calls += 1
            raise RuntimeError("invalid credentials")

    provider = FailingClient()
    client = WorkrrProcedrrClient(cast(Any, provider), process_directory=tmp_path)

    with pytest.raises(RuntimeError, match="invalid credentials"):
        client.complete_json(
            [{"role": "user", "content": "judge this"}],
            response_schema=SCHEMA,
        )

    assert provider.calls == 1


def test_procedrr_requires_a_schema() -> None:
    provider = RepairingClient([{"complete": True}])
    client = WorkrrProcedrrClient(provider, process_directory=Path("."))

    with pytest.raises(ProcedrrResponseError, match="output schema"):
        client.complete_json([{"role": "user", "content": "judge this"}])


def test_evaluator_can_be_constructed_with_workrr_transport(tmp_path: Path) -> None:
    from procedrr_evaluator import Evaluator

    evaluator = Evaluator.with_workrr(
        RepairingClient([{"complete": True}]),
        lambda _tool, _parameters: None,
        process_directory=tmp_path,
    )

    assert evaluator.llm.__class__.__name__ == "WorkrrProcedrrClient"


def test_tool_failures_are_structured_for_workrr(tmp_path: Path) -> None:
    executor = StructuredToolExecutor(
        lambda _tool, _parameters: (_ for _ in ()).throw(
            FileNotFoundError("missing source")
        ),
        available_paths=lambda: ["hello.py"],
    )

    result = executor("read_document", {"file_path": "src/hello_world.py"})

    assert result == {
        "ok": False,
        "error": {
            "code": "file_not_found",
            "message": "missing source",
            "path": "src/hello_world.py",
            "retryable": True,
            "available_paths": ["hello.py"],
        },
    }


def test_no_op_edits_are_structured_retryable_errors() -> None:
    executor = StructuredToolExecutor(lambda _tool, _parameters: {"changed": True})
    result = executor(
        "edit",
        {"file_path": "hello.py", "edits": [{"old_text": "x", "new_text": "x"}]},
    )
    assert result["error"]["code"] == "no_op_edit"
    assert result["error"]["retryable"] is True


def test_line_edits_are_materialized_for_underlying_executor() -> None:
    calls: list[tuple[str, Any]] = []

    def execute(tool: str, parameters: Any) -> Any:
        calls.append((tool, parameters))
        return "one\ntwo\n" if tool == "read_document" else {"changed": True}

    result = StructuredToolExecutor(execute)(
        "edit",
        {
            "file_path": "hello.py",
            "edits": [{"start": 1, "end": 1, "new_text": "ONE\n"}],
        },
    )
    assert result == {"changed": True}
    assert calls[-1] == (
        "edit",
        {
            "file_path": "hello.py",
            "edits": [{"old_text": "one\n", "new_text": "ONE\n"}],
        },
    )


def test_line_edit_rejects_replacement_identical_to_source() -> None:
    executor = StructuredToolExecutor(
        lambda tool, _parameters: (
            "one\n" if tool == "read_document" else {"changed": True}
        )
    )
    result = executor(
        "edit",
        {
            "file_path": "hello.py",
            "edits": [{"start": 1, "end": 1, "new_text": "one\n"}],
        },
    )
    assert result["error"]["code"] == "no_op_edit"


def test_replace_lines_shape_is_materialized() -> None:
    calls: list[tuple[str, Any]] = []

    def execute(tool: str, parameters: Any) -> Any:
        calls.append((tool, parameters))
        return "one\ntwo\n" if tool == "read_document" else {"changed": True}

    result = StructuredToolExecutor(execute)(
        "edit",
        {
            "operation": "replace_lines",
            "file_path": "hello.py",
            "start_line": 1,
            "end_line": 1,
            "replacement": "ONE\n",
        },
    )
    assert result == {"changed": True}
    assert calls[-1][1]["edits"] == [{"old_text": "one\n", "new_text": "ONE\n"}]


def test_structured_executor_exposes_fragment_construction_primitives() -> None:
    executor = StructuredToolExecutor(lambda _tool, _parameters: None)
    state = executor(
        "procedrr_fragment_start",
        {
            "name": "generated",
            "available_bindings": ["request"],
            "allowed_tools": ["read_document"],
            "max_steps": 2,
        },
    )

    result = executor(
        "procedrr_fragment_apply_edit",
        {
            "state": state,
            "step_json": '{"terminal":"succeeded"}',
            "evidence": {},
        },
    )

    assert result["done"] is True
    assert result["fragment"]["steps"] == [{"terminal": "succeeded"}]
