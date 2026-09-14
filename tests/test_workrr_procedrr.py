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


class RecordingRepairingClient(RepairingClient):
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        super().__init__(responses)
        self.messages: list[list[dict[str, str]]] = []

    def complete_json(
        self, messages: list[dict[str, str]], **kwargs: Any
    ) -> dict[str, Any]:
        self.messages.append(messages)
        return super().complete_json(messages, **kwargs)


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


def test_workrr_repairs_generated_fragment_using_json_pointer(tmp_path: Path) -> None:
    provider = RecordingRepairingClient(
        [
            {
                "name": "generated",
                "steps": [{"operation": {"tool": "imaginary", "bind": "result"}}],
            },
            {
                "edits": [
                    {
                        "op": "replace",
                        "path": "/steps/0/operation/tool",
                        "value": "list_files",
                    }
                ]
            },
        ]
    )
    client = WorkrrProcedrrClient(provider, skills_dir=tmp_path)

    result = client.complete_fragment(
        [{"role": "user", "content": "Generate the inspection fragment."}],
        allowed_tools={"list_files"},
    )

    assert result["steps"][0]["operation"]["tool"] == "list_files"
    assert provider.calls == 2
    assert "/steps/0/operation/tool" in str(provider.messages[1])


def test_workrr_repairs_fragment_that_violates_single_decision_form(
    tmp_path: Path,
) -> None:
    provider = RepairingClient(
        [
            {
                "name": "generated",
                "steps": [
                    {
                        "judge": {
                            "question": "Which actions?",
                            "subject": "request",
                            "prompt_system": "Return JSON.",
                            "instructions": ["Use the request."],
                            "context": ["request"],
                            "output": {
                                "name": "actions",
                                "schema": {
                                    "type": "array",
                                    "items": {"type": "object"},
                                },
                            },
                            "validation": {"kind": "json_schema"},
                        }
                    }
                ],
                "inputs": [{"name": "request"}],
            },
            {"name": "generated", "steps": [{"terminal": "succeeded"}]},
        ]
    )

    result = WorkrrProcedrrClient(provider, skills_dir=tmp_path).complete_fragment(
        [{"role": "user", "content": "Generate a fragment."}]
    )

    assert result["steps"] == [{"terminal": "succeeded"}]
    assert provider.calls == 2
