from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from procedrr import ProcessCatalog, ProcessCatalogError, ProcessIdentity
from procedrr_evaluator.evaluator import (
    EvaluationCheckpoint,
    EvaluationError,
    EvaluationEvent,
    SuspendEvaluation,
)
from procedrr_evaluator.runtime import (
    ProcedrrExecutionService,
    ProcessExecutionCheckpoint,
    ProcessTerminalStatus,
)


class NoCallLLM:
    def complete_json(self, *_: Any, **__: Any) -> dict[str, Any]:
        raise AssertionError("this process does not make model calls")


class SequenceLLM:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)

    def complete_json(self, *_: Any, **__: Any) -> dict[str, Any]:
        return self.responses.pop(0)


def _write_process(path: Path) -> None:
    path.write_text(
        """\
version: 1
name: smoke-process
inputs:
  - {name: greeting, type: string, required: true}
steps:
  - operation:
      tool: internal
      command: [echo]
      parameters:
        message: {type: reference, value: greeting}
      returns:
        type: object
        required: [message]
        properties: {message: {type: string}}
      bind: result
  - terminal: succeeded
""",
        encoding="utf-8",
    )


def test_catalog_loads_validated_process_and_enforces_declared_inputs(
    tmp_path: Path,
) -> None:
    process_path = tmp_path / "smoke-process.yaml"
    _write_process(process_path)

    catalog = ProcessCatalog.load(tmp_path)

    assert catalog.get("smoke-process").path == process_path
    assert catalog.get("smoke-process").validate_inputs({"greeting": "hello"}) == {
        "greeting": "hello"
    }
    with pytest.raises(ProcessCatalogError, match="missing required inputs"):
        catalog.get("smoke-process").validate_inputs({})
    with pytest.raises(ProcessCatalogError, match="undeclared inputs"):
        catalog.get("smoke-process").validate_inputs(
            {"greeting": "hello", "extra": "value"}
        )
    with pytest.raises(ProcessCatalogError, match="invalid input"):
        catalog.get("smoke-process").validate_inputs({"greeting": 4})


def test_shared_execution_service_binds_events_to_process_revision(
    tmp_path: Path,
) -> None:
    _write_process(tmp_path / "smoke-process.yaml")
    catalog = ProcessCatalog.load(tmp_path)
    events: list[Any] = []

    result = ProcedrrExecutionService(
        catalog,
        NoCallLLM(),
        lambda _tool, parameters: {"message": parameters["message"]},
        event_sink=events.append,
    ).execute("smoke-process", {"greeting": "hello"})

    assert result.bindings["result"] == {"message": "hello"}
    assert result.terminal_status is ProcessTerminalStatus.SUCCEEDED
    assert result.event_cursor == len(events) == 2
    assert all(event.identity == result.identity for event in events)
    assert events[0].to_data()["process_fingerprint"] == result.identity.fingerprint


def test_catalog_rejects_name_that_does_not_match_definition_file(
    tmp_path: Path,
) -> None:
    process_path = tmp_path / "wrong-name.yaml"
    _write_process(process_path)

    with pytest.raises(ProcessCatalogError, match="must match the filename"):
        ProcessCatalog.load(tmp_path)


def test_terminal_node_stops_process_and_reports_failure_status(tmp_path: Path) -> None:
    path = tmp_path / "terminal-process.yaml"
    path.write_text(
        """\
version: 1
name: terminal-process
steps:
  - terminal: failed
  - operation:
      tool: internal
      command: [must-not-run]
""",
        encoding="utf-8",
    )
    calls: list[str] = []
    service = ProcedrrExecutionService(
        ProcessCatalog.load(tmp_path),
        NoCallLLM(),
        lambda tool, _parameters: calls.append(tool),
    )

    result = service.execute("terminal-process")

    assert result.terminal_status is ProcessTerminalStatus.FAILED
    assert [event.kind for event in result.events] == ["terminal"]
    assert calls == []


def test_suspended_process_resumes_without_replaying_completed_operations(
    tmp_path: Path,
) -> None:
    path = tmp_path / "durable-process.yaml"
    path.write_text(
        """\
version: 1
name: durable-process
inputs:
  - {name: request, type: string, required: true}
steps:
  - operation:
      tool: internal
      command: [prepare]
      parameters: {request: "${request}"}
      bind: prepared
  - operation:
      tool: prompt_user
      parameters: {question: "Confirm the prepared result"}
      bind: human_response
  - operation:
      tool: internal
      command: [finish]
      parameters:
        prepared: "${prepared}"
        human_response: "${human_response}"
      bind: outcome
  - terminal: succeeded
""",
        encoding="utf-8",
    )
    catalog = ProcessCatalog.load(tmp_path)
    calls: list[str] = []

    def execute(tool: str, parameters: Any) -> Any:
        command = parameters.get("command", [tool])[0]
        calls.append(command)
        if tool == "prompt_user":
            raise SuspendEvaluation(
                {"pending": True, "question": parameters["question"]},
                "waiting for a human response",
            )
        if command == "prepare":
            return f"prepared:{parameters['request']}"
        return {
            "prepared": parameters["prepared"],
            "human_response": parameters["human_response"],
        }

    service = ProcedrrExecutionService(catalog, NoCallLLM(), execute)
    suspended = service.execute("durable-process", {"request": "change"})

    assert suspended.terminal_status is ProcessTerminalStatus.SUSPENDED
    assert suspended.checkpoint is not None
    assert suspended.checkpoint.evaluator.event_cursor == suspended.event_cursor
    assert calls == ["prepare", "prompt_user"]

    serialized = suspended.checkpoint.to_data()
    checkpoint = ProcessExecutionCheckpoint.from_data(serialized)
    resumed = service.execute(
        "durable-process",
        checkpoint=checkpoint,
        resume_value={"approved": True},
    )

    assert resumed.terminal_status is ProcessTerminalStatus.SUCCEEDED
    assert resumed.checkpoint is None
    assert resumed.bindings["outcome"] == {
        "prepared": "prepared:change",
        "human_response": {"approved": True},
    }
    assert calls == ["prepare", "prompt_user", "finish"]
    assert [event.kind for event in resumed.events].count("resume") == 1


def test_process_checkpoint_rejects_a_changed_process_revision(tmp_path: Path) -> None:
    _write_process(tmp_path / "smoke-process.yaml")
    catalog = ProcessCatalog.load(tmp_path)
    checkpoint = ProcessExecutionCheckpoint(
        ProcessIdentity("smoke-process", "old-fingerprint"),
        EvaluationCheckpoint(
            {"greeting": "hello"},
            (EvaluationEvent("suspend", "steps[0]", {"reason": "wait"}),),
            "steps[0]",
            1,
        ),
    )
    with pytest.raises(EvaluationError, match="identity or revision"):
        ProcedrrExecutionService(catalog, NoCallLLM(), lambda *_: None).execute(
            "smoke-process", checkpoint=checkpoint, resume_value="continue"
        )


def test_migrated_independent_review_runs_as_a_procedrr_process() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    catalog = ProcessCatalog.load(repository_root / "docs/procedrr/processes")
    llm = SequenceLLM(
        [
            {
                "requirements": [
                    {
                        "requirement": "Preserve the failure rule.",
                        "original_evidence": "Do not proceed while it fails.",
                    }
                ]
            },
            {
                "complete": False,
                "findings": [
                    {
                        "judgment": "The rewrite omits the failure rule.",
                        "directive": "Add a fail-closed condition.",
                        "original_requirement": "Do not proceed while it fails.",
                    }
                ],
                "requirements_checked": ["Preserve the failure rule."],
            },
        ]
    )
    result = ProcedrrExecutionService(
        catalog,
        llm,
        lambda *_: pytest.fail("review process must not invoke tools"),
    ).execute(
        "independent-skill-workflow-review",
        {
            "target_goal": "Review one rewritten workflow step.",
            "original_step_text": "Do not proceed while it fails.",
            "proposed_replacement": "Proceed after inspection.",
            "preserved_requirements": ["Fail closed."],
            "inventory_counts": {"steps": 1},
        },
    )

    assert result.terminal_status is ProcessTerminalStatus.SUCCEEDED
    assert result.bindings["independent_review"]["complete"] is False
    assert result.bindings["independent_review"]["findings"][0]["directive"] == (
        "Add a fail-closed condition."
    )
    assert result.llm_activations == 2
