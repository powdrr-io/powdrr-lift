from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping
from contextlib import nullcontext
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from powdrr_service.decision_classifier import JevSemanticClassifierClient

from powdrr_lift.workrr import chat_context, procedrr_chat
from powdrr_lift.workrr.chat_config import WorkflowChatConfig
from powdrr_lift.workrr.chat_events import (
    WorkflowChatEvent,
    WorkflowChatEventKind,
)
from powdrr_lift.workrr.procedrr import WorkrrProcedrrClient
from powdrr_lift.workrr.procedrr_chat import (
    WORKFLOW_CHAT_WAITING_FOR_INPUT,
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


def test_specify_feature_interviews_until_structured_spec_has_no_open_assumptions() -> (
    None
):
    import yaml

    catalog = ProcessCatalog.load(Path("docs/procedrr/processes"))
    specialize = catalog.get("specify-a-feature").document["steps"][0]["specialize"]
    question = specialize["question"]
    workflow = yaml.safe_load(
        question.split("Embedded workflow document:\n---\n", 1)[1]
    )
    interview = next(
        step
        for step in workflow["steps"]
        if step.get("id") == "conduct-spec-design-interview"
    )
    details = interview["details"]
    compiler = next(
        step
        for step in workflow["steps"]
        if step.get("id") == "conduct-design-interview"
    )

    assert interview["actions"] == ["prompt_user"]
    assert "distinct user-visible features" in details
    assert "must be preserved" in details
    assert "legacy_behavior" in details
    assert "related_modules" in details
    assert "multiple modules" in details
    assert "quality_tier" in details
    assert all(
        level in details
        for level in ("prototyping", "internal beta", "production ready")
    )
    assert "which should land first and in what order" in details
    assert "enough detail to continue without assumptions" in details
    assert "assistant-provided" in details
    assert "not confirmed by the user" in details
    assert "later review in the resulting specification" in details
    assert "explicit specification guidance" in compiler["details"]
    assert "Carry the quality expectation and delivery order" in compiler["details"]
    assert "related_modules" in compiler["details"]


def test_feature_planning_retrieves_module_scoped_decisions() -> None:
    import yaml

    catalog = ProcessCatalog.load(Path("docs/procedrr/processes"))
    specialize = catalog.get("start-implementing-feature").document["steps"][0][
        "specialize"
    ]
    workflow = yaml.safe_load(
        specialize["question"].split("Embedded workflow document:\n---\n", 1)[1]
    )
    planning = next(
        step for step in workflow["steps"] if step.get("id") == "plan-proposed-prs"
    )

    assert "gather_context" in planning["actions"]
    assert "filters: {module_id:" in planning["details"]
    assert "excluding decisions linked only to other modules" in planning["details"]

    implementation = catalog.get("specify-implementation").document["steps"][0][
        "specialize"
    ]
    implementation_workflow = yaml.safe_load(
        implementation["question"].split("Embedded workflow document:\n---\n", 1)[1]
    )
    fill = next(
        step
        for step in implementation_workflow["steps"]
        if step.get("id") == "fill-implementation"
    )
    assert "gather_context" in fill["actions"]
    assert "related_modules" in fill["details"]


def test_address_review_comments_flow_uses_structrr_and_thread_aware_gh_tools() -> None:
    process = ProcessCatalog.load(Path("docs/procedrr/processes")).get(
        "address-review-comments"
    )
    specialize = process.document["steps"][0]["specialize"]
    question = " ".join(specialize["question"].split())

    assert "pr_review_threads" in question
    assert "gather_context" in question
    assert "proposed-PR" in question
    assert "pr_review_reply" in question
    assert "pr_review_resolve" in question
    assert (
        "Leave deferred, blocked, or clarification-needed threads unresolved."
        in question
    )
    assert "Do not use the clean-branch PR publisher" in question
    assert "gather_context" in specialize["allowed_tools"]


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
            conversation_id="chat-integration",
            initial_message="Inspect this fixture.",
        ),
        procedrr_client_factory=lambda _role: client,
        input_func=lambda: pytest.fail("explicit initial message must skip input"),
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0, stderr.getvalue()
    assert "Matched process: echo-process" in stdout.getvalue()
    assert "Done: Inspect this fixture." in stdout.getvalue()
    assert "What do you want to do?" not in stdout.getvalue()


def test_chat_suspends_and_resumes_prompt_without_replaying_prior_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
description: Ask a durable question and return the answer.
inputs:
  - {name: user_request, type: string, required: true}
  - {name: workflow_context, type: object, required: true}
  - {name: language_labels, type: array, required: true}
steps:
  - operation:
      tool: respond
      parameters: {text: "Started: ${user_request}"}
      bind: started
  - operation:
      tool: prompt_user
      parameters: {text: "Which region should I use?"}
      bind: reply
  - operation:
      tool: respond
      parameters: {text: "Selected: ${reply.answer}"}
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
    monkeypatch.setattr(
        procedrr_chat, "_persist_workflow_context", lambda *_a, **_k: None
    )
    execution_id = "durable-chat-test"
    initial = WorkflowChatConfig(
        processes_dir=process_dir,
        repo_root=repo_root,
        provider="local",
        execution_id=execution_id,
        conversation_id="chat-one",
        initial_message="Configure the deployment.",
        defer_user_input=True,
    )

    first_stdout = StringIO()
    first_stderr = StringIO()
    first_events: list[WorkflowChatEvent] = []
    first_code = run_procedrr_workflow_chat(
        initial,
        procedrr_client_factory=lambda _role: client,
        input_func=lambda: pytest.fail("all process inputs are prebound"),
        stdout=first_stdout,
        stderr=first_stderr,
        event_callback=first_events.append,
    )

    manifest_path = repo_root / ".powdrr" / "procedrr-runs" / f"{execution_id}.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert first_code == WORKFLOW_CHAT_WAITING_FOR_INPUT, first_stderr.getvalue()
    assert manifest["status"] == "suspended"
    assert manifest["pending_question"] == "Which region should I use?"
    assert manifest["checkpoint"]
    assert "Started: Configure the deployment." in first_stdout.getvalue()
    assert "Which region should I use?" in first_stdout.getvalue()
    assert [
        event.kind
        for event in first_events
        if event.kind
        in {
            WorkflowChatEventKind.ASSISTANT_MESSAGE,
            WorkflowChatEventKind.QUESTION,
            WorkflowChatEventKind.OUTCOME,
        }
    ] == [
        WorkflowChatEventKind.ASSISTANT_MESSAGE,
        WorkflowChatEventKind.QUESTION,
        WorkflowChatEventKind.OUTCOME,
    ]
    assert first_events[-1].data["status"] == "waiting_for_input"
    chat_event_path = (
        repo_root / ".powdrr" / "procedrr-runs" / f"{execution_id}.chat-events.jsonl"
    )
    persisted_events = [
        json.loads(line) for line in chat_event_path.read_text().splitlines()
    ]
    assert [event["sequence"] for event in persisted_events] == list(
        range(len(first_events))
    )

    resumed_stdout = StringIO()
    resumed_stderr = StringIO()
    resumed_events: list[WorkflowChatEvent] = []
    resumed_code = run_procedrr_workflow_chat(
        WorkflowChatConfig(
            processes_dir=process_dir,
            repo_root=repo_root,
            provider="local",
            execution_id=execution_id,
            conversation_id="chat-one",
            defer_user_input=True,
            resume_value="us-west-2",
        ),
        procedrr_client_factory=lambda _role: client,
        input_func=lambda: pytest.fail("resuming must not collect new inputs"),
        stdout=resumed_stdout,
        stderr=resumed_stderr,
        event_callback=resumed_events.append,
    )

    assert resumed_code == 0, resumed_stderr.getvalue()
    assert "Selected: us-west-2" in resumed_stdout.getvalue()
    final_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert final_manifest["status"] == "succeeded"
    assert "checkpoint" not in final_manifest
    assert resumed_events[0].sequence == len(first_events)
    assert resumed_events[-1].kind is WorkflowChatEventKind.OUTCOME
    assert resumed_events[-1].data["status"] == "succeeded"
    assert final_manifest["chat_event_cursor"] == len(first_events) + len(
        resumed_events
    )


def test_chat_suspends_and_resumes_required_process_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    repo_root = tmp_path / ".worktrees" / "chat"
    repo_root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
    shutil.copy(Path("docs/procedrr/processes/select-process.yaml"), process_dir)
    process_path = process_dir / "echo-process.yaml"
    process_path.write_text(
        """\
version: 1
name: echo-process
description: Collect one required input before running.
inputs:
  - {name: user_request, type: string, required: true}
  - {name: workflow_context, type: object, required: true}
  - {name: language_labels, type: array, required: true}
  - name: work_item_name
    type: string
    required: true
    question: "What should we call this work item?"
steps:
  - operation:
      tool: respond
      parameters: {text: "Ready: ${work_item_name}"}
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
    monkeypatch.setattr(
        procedrr_chat, "_persist_workflow_context", lambda *_a, **_k: None
    )
    execution_id = "durable-process-input-test"
    first_stderr = StringIO()
    first_code = run_procedrr_workflow_chat(
        WorkflowChatConfig(
            processes_dir=process_dir,
            repo_root=repo_root,
            provider="local",
            execution_id=execution_id,
            conversation_id="chat-one",
            initial_message="Please build the billing page.",
            defer_user_input=True,
        ),
        procedrr_client_factory=lambda _role: client,
        input_func=lambda: pytest.fail("required process input must be deferred"),
        stdout=StringIO(),
        stderr=first_stderr,
    )
    manifest_path = repo_root / ".powdrr" / "procedrr-runs" / f"{execution_id}.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert first_code == WORKFLOW_CHAT_WAITING_FOR_INPUT, first_stderr.getvalue()
    assert manifest["suspension_kind"] == "process_input"
    assert manifest["pending_input_name"] == "work_item_name"

    second_stdout = StringIO()
    second_stderr = StringIO()
    second_code = run_procedrr_workflow_chat(
        WorkflowChatConfig(
            processes_dir=process_dir,
            repo_root=repo_root,
            provider="local",
            execution_id=execution_id,
            conversation_id="chat-one",
            defer_user_input=True,
            resume_value="Billing page",
        ),
        procedrr_client_factory=lambda _role: client,
        input_func=lambda: pytest.fail("resuming process input must not block"),
        stdout=second_stdout,
        stderr=second_stderr,
    )
    assert second_code == 0, second_stderr.getvalue()
    assert "Ready: Billing page" in second_stdout.getvalue()


def test_workflow_context_persists_separately_per_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "worktree"
    (worktree / ".git").mkdir(parents=True)
    monkeypatch.setattr(
        chat_context,
        "_workflow_context_runtime",
        lambda *_args: SimpleNamespace(without_action_contract=nullcontext),
    )

    def invoke(
        _tool: str, parameters: Mapping[str, Any], **_kwargs: Any
    ) -> dict[str, Any]:
        if parameters["operation"] == "branch_current":
            return {"stdout": "feature/chat-context"}
        return {"returncode": 1, "stdout": ""}

    monkeypatch.setattr(chat_context, "invoke_intrinsic_capability", invoke)

    chat_context._persist_workflow_context(
        tmp_path,
        worktree,
        process_id="design-interview",
        request="First request",
        conversation_id="chat-one",
    )
    chat_context._persist_workflow_context(
        tmp_path,
        worktree,
        process_id="design-interview",
        request="Second request",
        conversation_id="chat-two",
    )

    first = chat_context._load_workflow_context(tmp_path, conversation_id="chat-one")
    second = chat_context._load_workflow_context(tmp_path, conversation_id="chat-two")
    assert first is not None and first.request == "First request"
    assert second is not None and second.request == "Second request"
    assert chat_context._workflow_context_path(
        tmp_path, conversation_id="chat-one"
    ) != chat_context._workflow_context_path(tmp_path, conversation_id="chat-two")
    assert not (tmp_path / ".powdrr" / "workflow-context.json").exists()


def test_workflow_context_rejects_unsafe_conversation_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="safe path component"):
        chat_context._workflow_context_path(tmp_path, conversation_id="../shared")


def test_chat_jev_provider_uses_the_decision_classifier(
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
