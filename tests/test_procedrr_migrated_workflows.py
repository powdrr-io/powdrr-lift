from __future__ import annotations

from pathlib import Path
from typing import Any

from procedrr import ProcessCatalog
from procedrr.fragments import apply_fragment_step_json, start_fragment
from procedrr_evaluator.runtime import ProcedrrExecutionService


class SequenceLLM:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)

    def complete_json(self, *_: Any, **__: Any) -> dict[str, Any]:
        return self.responses.pop(0)


def _tool_catalog() -> dict[str, Any]:
    return {
        "matches": [
            {
                "item": {
                    "id": "ruff-check",
                    "validation_action": {
                        "tool": "shell",
                        "parameters": {"command": "ruff check ."},
                    },
                }
            }
        ]
    }


def _scope_and_label_responses() -> list[dict[str, Any]]:
    return [
        {"confirmed": True, "findings": []},
        {"labels": ["pr-prep", "python"]},
    ]


def _handle_ad_hoc_executor(
    *, has_changes: bool
) -> tuple[Any, list[tuple[str, dict[str, Any]]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def execute(tool: str, parameters: Any) -> Any:
        if tool == "procedrr_fragment_start":
            return start_fragment(
                name=str(parameters["name"]),
                available_bindings=parameters["available_bindings"],
                allowed_tools=parameters["allowed_tools"],
                max_steps=int(parameters["max_steps"]),
            )
        if tool == "procedrr_fragment_apply_edit":
            return apply_fragment_step_json(
                parameters["state"], parameters["step_json"]
            )
        if tool == "workflow_chat_snapshot":
            return {"snapshot_id": "snapshot-1"}
        if tool == "workflow_chat_changes":
            return {
                "has_changes": has_changes,
                "paths": ["src/app.py"] if has_changes else [],
                "language_labels": ["python"] if has_changes else [],
            }
        if tool == "gather_context":
            return {"matches": []}
        if tool == "invoke_tool":
            calls.append((str(parameters["tool"]), dict(parameters["parameters"])))
            return {"returncode": 0, "stdout": "", "stderr": ""}
        raise AssertionError(f"unexpected Procedrr operation: {tool}")

    return execute, calls


def test_handle_ad_hoc_skips_pr_prep_when_the_request_changes_no_files() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    catalog = ProcessCatalog.load(repository_root / "docs/procedrr/processes")
    executor, calls = _handle_ad_hoc_executor(has_changes=False)
    llm = SequenceLLM([{"step_json": '{"terminal":"succeeded"}'}])

    result = ProcedrrExecutionService(catalog, llm, executor).execute(
        "handle-ad-hoc",
        {
            "user_request": "Explain the previous result.",
            "workflow_context": {"previous_result": "complete"},
            "language_labels": ["python"],
        },
    )

    assert result.terminal_status.value == "succeeded"
    assert calls == []


def test_handle_ad_hoc_runs_finish_pr_prep_for_changed_files() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    catalog = ProcessCatalog.load(repository_root / "docs/procedrr/processes")
    executor, calls = _handle_ad_hoc_executor(has_changes=True)
    llm = SequenceLLM(
        [
            {"step_json": '{"terminal":"succeeded"}'},
            {"confirmed": True, "findings": []},
            {"labels": ["pr-prep", "python"]},
        ]
    )

    result = ProcedrrExecutionService(catalog, llm, executor).execute(
        "handle-ad-hoc",
        {
            "user_request": "Make the requested small correction.",
            "workflow_context": {},
            "language_labels": ["python"],
        },
    )

    assert result.terminal_status.value == "succeeded"
    assert result.bindings["pr_prep_results"] == []
    assert calls == [
        ("shell", {"command": ["git", "status", "--short"]}),
        ("shell", {"command": ["git", "diff", "--cached", "--stat"]}),
    ]


def test_finish_pr_prep_runs_every_discovered_validation_tool() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    catalog = ProcessCatalog.load(repository_root / "docs/procedrr/processes")
    calls: list[tuple[str, dict[str, Any]]] = []

    def invoke_tool(parameters: Any) -> dict[str, Any]:
        tool = parameters["tool"]
        tool_parameters = dict(parameters["parameters"])
        calls.append((tool, tool_parameters))
        return {"returncode": 0, "stdout": "passed", "stderr": ""}

    executor = {
        "invoke_tool": invoke_tool,
        "gather_context": lambda _parameters: _tool_catalog(),
    }
    result = ProcedrrExecutionService(
        catalog,
        SequenceLLM(_scope_and_label_responses()),
        lambda tool, parameters: executor[tool](parameters),
    ).execute(
        "finish-pr-prep",
        {"files_to_publish": ["src/app.py"], "language_labels": ["python"]},
    )

    assert result.terminal_status.value == "succeeded"
    assert [call for call in calls if call[1].get("command") == "ruff check ."] == [
        ("shell", {"command": "ruff check ."})
    ]
    assert result.bindings["validation_results"][0]["result"]["returncode"] == 0


def test_finish_pr_prep_repairs_then_reruns_all_validations() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    catalog = ProcessCatalog.load(repository_root / "docs/procedrr/processes")
    responses = [
        *_scope_and_label_responses(),
        {
            "repairs": [
                {"file_path": "README.md", "reason": "Fix the reported lint issue."}
            ]
        },
        {"file_path": "README.md", "start": 2, "end": 2, "new_text": "fixed\n"},
    ]
    validation_calls = 0
    restaged_paths: list[str] = []
    edits: list[dict[str, Any]] = []

    def invoke_tool(parameters: Any) -> dict[str, Any]:
        nonlocal validation_calls
        tool = parameters["tool"]
        tool_parameters = dict(parameters["parameters"])
        if tool == "shell" and tool_parameters["command"] == "ruff check .":
            validation_calls += 1
            return {
                "returncode": 1 if validation_calls == 1 else 0,
                "stdout": "",
                "stderr": "README.md:2 lint issue" if validation_calls == 1 else "",
            }
        if tool == "git" and tool_parameters.get("operation") == "add":
            restaged_paths.extend(tool_parameters["paths"])
        return {"returncode": 0, "stdout": "passed", "stderr": ""}

    def edit(parameters: Any) -> dict[str, Any]:
        edits.append(dict(parameters))
        return {"ok": True}

    executor = {
        "invoke_tool": invoke_tool,
        "gather_context": lambda _parameters: _tool_catalog(),
        "read_document": lambda _parameters: "line one\nline two\n",
        "edit": edit,
    }
    result = ProcedrrExecutionService(
        catalog,
        SequenceLLM(responses),
        lambda tool, parameters: executor[tool](parameters),
    ).execute(
        "finish-pr-prep",
        {
            "files_to_publish": ["src/app.py", "README.md"],
            "language_labels": ["python", "markdown"],
        },
    )

    assert result.terminal_status.value == "succeeded"
    assert validation_calls == 2
    assert restaged_paths == ["src/app.py", "README.md"]
    assert edits == [
        {
            "operation": "replace_lines",
            "file_path": "README.md",
            "start_line": 2,
            "end_line": 2,
            "replacement": "fixed\n",
        }
    ]
