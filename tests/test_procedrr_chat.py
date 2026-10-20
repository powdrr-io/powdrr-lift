from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from powdrr_lift.workrr import procedrr_chat
from powdrr_lift.workrr.chat_config import WorkflowChatConfig
from powdrr_lift.workrr.jev_classifier import JevSemanticClassifierClient
from powdrr_lift.workrr.procedrr import WorkrrProcedrrClient
from powdrr_lift.workrr.procedrr_chat import (
    _operation_to_action,
    _select_process,
    run_procedrr_workflow_chat,
)
from procedrr import ProcessCatalog


class _SelectingClient:
    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = dict(response)

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any],
    ) -> dict[str, Any]:
        _ = (messages, response_schema)
        return dict(self.response)


class _SelectingProvider:
    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any],
    ) -> dict[str, Any]:
        _ = (messages, response_schema)
        return {
            "process_name": "echo-process",
            "reason": "The request is handled by the scripted process.",
        }


def test_process_selector_returns_only_a_catalogued_process() -> None:
    process_dir = Path("docs/procedrr/processes")
    catalog = ProcessCatalog.load(process_dir)

    selected = _select_process(
        catalog,
        _SelectingClient(
            {"process_name": "handle-ad-hoc", "reason": "General follow-up."}
        ),  # type: ignore[arg-type]
        "Please explain this change.",
        {},
    )
    unknown = _select_process(
        catalog,
        _SelectingClient({"process_name": "not-a-process", "reason": "Unknown."}),  # type: ignore[arg-type]
        "Please explain this change.",
        {},
    )

    assert selected == "handle-ad-hoc"
    assert unknown is None


def test_all_supported_workflow_names_have_procedrr_processes() -> None:
    catalog = ProcessCatalog.load(Path("docs/procedrr/processes"))
    names = {process.name for process in catalog.list()}
    assert {
        "address-review-comments",
        "adversarial-pr-review",
        "bootstrap-code-structure",
        "create-pull-request",
        "dead-code-review",
        "design-interview",
        "feature-functionality-review",
        "feature-test-coverage-review",
        "finish-pr-prep",
        "fix-ci-failures",
        "fix-merge-conflicts",
        "handle-ad-hoc",
        "independent-skill-workflow-review",
        "review-architecture",
        "review-skill-workflow",
        "review-system",
        "run-tests-and-fix",
        "security-review",
        "spec-v1-design-review",
        "specify-a-feature",
        "specify-architecture",
        "specify-implementation",
        "specify-system",
        "start-implementing-feature",
    } <= names


def test_chat_operation_adapter_converts_line_edits() -> None:
    action = _operation_to_action(
        {
            "kind": "edit",
            "operation": "replace_lines",
            "file_path": "src/example.py",
            "start_line": 4,
            "end_line": 4,
            "replacement": "return 42",
        }
    )

    assert action.kind == "edit"
    assert action.file_path == "src/example.py"
    assert len(action.edits) == 1
    assert action.edits[0].start_line == 4
    assert action.edits[0].text == "return 42"


def test_chat_entrypoint_executes_a_catalogued_procedrr_process(
    tmp_path: Path,
) -> None:
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    repo_root = tmp_path / ".worktrees" / "chat"
    repo_root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
    source_process_dir = Path("docs/procedrr/processes")
    shutil.copy(source_process_dir / "select-process.yaml", process_dir)
    (process_dir / "echo-process.yaml").write_text(
        """\
version: 1
name: echo-process
description: Echo the request for the chat integration test.
inputs:
  - {name: user_request, type: string, required: true}
  - {name: workflow_context, type: object, required: true}
  - {name: language_labels, type: array, required: true}
limits: {llm_activations: 1, tool_calls: 1, context_chars: 2000}
steps:
  - operation:
      tool: respond
      parameters: {text: "Done: ${user_request}"}
      bind: response
  - terminal: succeeded
""",
        encoding="utf-8",
    )
    client = WorkrrProcedrrClient(
        _SelectingProvider(),  # type: ignore[arg-type]
        process_directory=process_dir,
        provider_retry_attempts=0,
    )
    stdout = StringIO()
    stderr = StringIO()

    exit_code = run_procedrr_workflow_chat(
        WorkflowChatConfig(
            processes_dir=process_dir,
            repo_root=repo_root,
            provider="local",
            execution_id="chat-integration",
        ),
        procedrr_client_factory=lambda _role: client,
        input_func=lambda: "Inspect this fixture.",
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0, stderr.getvalue()
    assert "Matched process: echo-process" in stdout.getvalue()
    assert "Done: Inspect this fixture." in stdout.getvalue()


def test_chat_jev_provider_uses_the_decision_classifier_with_planning_fallback(
    tmp_path: Path, monkeypatch: Any
) -> None:
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    repo_root = tmp_path / ".worktrees" / "chat"
    repo_root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
    source_process_dir = Path("docs/procedrr/processes")
    shutil.copy(source_process_dir / "select-process.yaml", process_dir)
    (process_dir / "echo-process.yaml").write_text(
        """\
version: 1
name: echo-process
description: Exercise judge-provider wiring.
inputs:
  - {name: user_request, type: string, required: true}
  - {name: workflow_context, type: object, required: true}
  - {name: language_labels, type: array, required: true}
steps:
  - terminal: succeeded
""",
        encoding="utf-8",
    )
    client = WorkrrProcedrrClient(
        _SelectingProvider(),  # type: ignore[arg-type]
        process_directory=process_dir,
        provider_retry_attempts=0,
    )
    captured: dict[str, Any] = {}

    class CapturingExecutionService:
        def __init__(
            self,
            *args: Any,
            judge_clients: Mapping[str, Any] | None = None,
            **kwargs: Any,
        ):
            _ = (args, kwargs)
            if judge_clients is not None:
                captured.update(judge_clients)

        def execute(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                terminal_status=procedrr_chat.ProcessTerminalStatus.SUCCEEDED,
                bindings={"route": {"process_name": "echo-process"}},
            )

    monkeypatch.setattr(
        procedrr_chat, "ProcedrrExecutionService", CapturingExecutionService
    )
    stderr = StringIO()
    exit_code = run_procedrr_workflow_chat(
        WorkflowChatConfig(
            processes_dir=process_dir,
            repo_root=repo_root,
            provider="local",
            execution_id="jev-provider-wiring",
        ),
        procedrr_client_factory=lambda _role: client,
        input_func=lambda: "Exercise judge wiring.",
        stdout=StringIO(),
        stderr=stderr,
    )

    assert exit_code == 0, stderr.getvalue()
    assert isinstance(captured["jev"]._client, JevSemanticClassifierClient)
