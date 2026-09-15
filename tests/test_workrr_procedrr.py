from pathlib import Path
from typing import Any

import pytest

from powdrr_lift.workrr.procedrr import (
    ProcedrrResponseError,
    StructuredToolExecutor,
    WorkrrProcedrrClient,
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


def test_procedrr_uses_workrr_schema_repair_transport(tmp_path: Path) -> None:
    provider = RepairingClient([{}, {"complete": True}])
    client = WorkrrProcedrrClient(provider, skills_dir=tmp_path)

    result = client.complete_json(
        [{"role": "user", "content": "judge this"}],
        response_schema=SCHEMA,
    )

    assert result == {"complete": True}
    assert provider.calls == 2


def test_procedrr_requires_a_schema() -> None:
    provider = RepairingClient([{"complete": True}])
    client = WorkrrProcedrrClient(provider, skills_dir=Path("."))

    with pytest.raises(ProcedrrResponseError, match="output schema"):
        client.complete_json([{"role": "user", "content": "judge this"}])


def test_evaluator_can_be_constructed_with_workrr_transport(tmp_path: Path) -> None:
    from procedrr_evaluator import Evaluator

    evaluator = Evaluator.with_workrr(
        RepairingClient([{"complete": True}]),
        lambda _tool, _parameters: None,
        skills_dir=tmp_path,
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
