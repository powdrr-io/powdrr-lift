from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from powdrr_lift.core import (
    AgentRole,
    AssigneeType,
    HumanRole,
    Skill,
    SkillStep,
    SkillStepPreStep,
    TaskComplexity,
    TaskStatus,
    WorkflowInstance,
    WorkflowTask,
    save_skill,
)
from powdrr_lift.core.spec_context import (
    gather_specification_context,
    render_gather_context_report,
)
from powdrr_lift.workflow_chat_agent import (
    LLMModelLimits,
    _action_system_prompt,
    _parse_action_response,
)
from powdrr_lift.workflow_git import (
    WorkflowGitInconsistency,
    WorkflowGitState,
    save_workflow_git_state,
)
from powdrr_lift.workflow_llm import (
    WorkflowAction,
    WorkflowLLMHTTPError,
    complete_json_with_timeout_retry,
)
from powdrr_lift.workflow_task_agent import (
    WorkflowTaskAgentConfig,
    _build_task_messages,
    _build_workflow_client,
    _handle_exhausted_timeout,
    _publish_workflow_progress,
    _read_task_document,
    _select_ready_workflow_git_state,
    _task_events_for_prompt,
    _validate_workflow_task_state,
    _workflow_file_command_error,
    run_workflow_task,
)


class _FakeClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = iter(responses)
        self.messages: list[list[dict[str, str]]] = []

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, object]:
        self.messages.append(messages)
        return next(self.responses)


def _workflow(tmp_path: Path) -> WorkflowInstance:
    return WorkflowInstance.create(
        tmp_path / "workflow",
        (
            WorkflowTask(
                task_id="agent-task",
                status=TaskStatus.OPEN,
                upstream_task_ids=(),
                dependent_state=(),
                complexity=TaskComplexity.MEDIUM,
                input_state={"request": "Choose an API version."},
                description="Choose an API version.",
                assignee_type=AssigneeType.AGENT,
                assignee_role=AgentRole.ARCHITECT,
                llm_type="simple_task",
            ),
        ),
    )


def test_select_ready_workflow_skips_workflow_with_incomplete_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_directory = tmp_path / "workflow"
    base_task = _workflow(tmp_path).tasks[0]
    WorkflowInstance.create(
        workflow_directory,
        (
            replace(base_task, task_id="tool-workflow-task-001"),
            replace(base_task, task_id="writer-workflow-task-001"),
        ),
    )
    save_workflow_git_state(
        workflow_directory,
        WorkflowGitState(
            proposed_pr_id="tool",
            base_branch="main",
            integration_branch="powdrr/tool",
            workflow_relative_directory="workflow",
            depends_on_workflows=("writer",),
        ),
    )
    save_workflow_git_state(
        workflow_directory,
        WorkflowGitState(
            proposed_pr_id="writer",
            base_branch="main",
            integration_branch="powdrr/writer",
            workflow_relative_directory="workflow",
        ),
    )

    monkeypatch.setattr(
        "powdrr_lift.workflow_task_agent.workflow_dependencies_completion",
        lambda _repo_root, state: (not state.depends_on_workflows, ()),
    )

    state, workflow_id = _select_ready_workflow_git_state(
        workflow_directory,
        tmp_path,
    )

    assert state is not None
    assert state.proposed_pr_id == "writer"
    assert workflow_id == "writer"


@pytest.mark.parametrize("verbose", [False, True])
def test_process_workflow_task_completes_claimed_agent_task(
    tmp_path: Path,
    verbose: bool,
) -> None:
    workflow = _workflow(tmp_path)
    client = _FakeClient([{"kind": "complete", "output_state": {"version": "v2"}}])
    stderr = io.StringIO()
    stdout = io.StringIO()

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
            verbose=verbose,
        ),
        client=client,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    completed = WorkflowInstance.from_directory(workflow.directory).tasks[0]
    assert completed.status is TaskStatus.COMPLETED
    assert completed.output_state == {"version": "v2"}
    prompt = client.messages[0][1]["content"]
    assert client.messages[0][0]["content"].startswith(_action_system_prompt())
    assert (
        "Use `next_step` when this task is finished" in client.messages[0][0]["content"]
    )
    assert '"execution_mode":"process_workflow_task"' in prompt
    assert json.loads(prompt)["workflow_dir"] == "workflow"
    assert str(tmp_path) not in prompt
    assert "\n" not in prompt
    displayed = stderr.getvalue()
    assert ("Workflow task LLM input:" in displayed) is verbose
    assert ("Workflow task LLM output:" in displayed) is verbose
    assert ('"kind": "complete"' in displayed) is verbose
    assert "received streamed LLM data" not in displayed
    assert ("Workflow task LLM action:" in stdout.getvalue()) is verbose
    assert "Workflow task roundtrip 1: complete" in stdout.getvalue()


def test_deterministic_gather_context_must_be_persisted_exactly(
    tmp_path: Path,
) -> None:
    specs = tmp_path / "docs" / "proposals" / "feature"
    specs.mkdir(parents=True)
    (specs / "proposed-pr-specification.yaml").write_text(
        "schema: https://powdrr.io/schemas/proposed-pr-specification-v1\n"
        "id: feature\n"
        "feature_ids: [feature-id]\n"
        "proposed_prs:\n"
        "- id: feature-pr\n"
        "  intent: Capture the feature context.\n"
        "  justification: Required for the workflow.\n",
        encoding="utf-8",
    )
    workflow = WorkflowInstance.create(
        tmp_path / "workflow",
        (
            WorkflowTask(
                task_id="feature-task",
                status=TaskStatus.OPEN,
                upstream_task_ids=(),
                dependent_state=(),
                complexity=TaskComplexity.MEDIUM,
                input_state={"feature_id": "feature"},
                description="Gather feature context.",
                assignee_type=AssigneeType.AGENT,
                assignee_role=AgentRole.ARCHITECT,
                output_state_type="context-state",
                step_type="invoke_tool",
                pre_step=SkillStepPreStep(
                    action="gather_context",
                    template={
                        "feature_id": "<feature_id>",
                        "types": ["proposed_prs"],
                    },
                ),
            ),
        ),
    )
    expected_context = json.loads(
        render_gather_context_report(
            gather_specification_context(
                tmp_path,
                types=["proposed_prs"],
                feature_id="feature",
            )
        )
    )
    client = _FakeClient(
        [
            {"kind": "complete", "output_state": {"context-state": "summary"}},
            {
                "kind": "complete",
                "output_state": {"context-state": expected_context},
            },
        ]
    )

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(workflow_dir=workflow.directory, repo_root=tmp_path),
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    assert len(client.messages) == 2
    assert "exact deterministic pre-step result" in client.messages[1][1]["content"]


def test_process_workflow_task_resolves_input_placeholders_before_llm(
    tmp_path: Path,
) -> None:
    workflow = WorkflowInstance.create(
        tmp_path / "workflow",
        (
            WorkflowTask(
                task_id="agent-task",
                status=TaskStatus.OPEN,
                upstream_task_ids=(),
                dependent_state=(),
                complexity=TaskComplexity.MEDIUM,
                input_state={
                    "feature_id": "interaction-file-logging",
                    "proposed_pr": "pr-interaction-capture-17",
                },
                description="Resolve the proposed PR.",
                details=(
                    "Use docs/proposals/<work-item-name>/ for "
                    "input_state.feature_id and input_state.proposed_pr."
                ),
                assignee_type=AssigneeType.AGENT,
                assignee_role=AgentRole.ARCHITECT,
                llm_type="simple_task",
            ),
        ),
    )
    client = _FakeClient([{"kind": "complete", "output_state": {"ok": True}}])

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    prompt = client.messages[0][1]["content"]
    assert "docs/proposals/interaction-file-logging/" in prompt
    assert "pr-interaction-capture-17" in prompt
    assert "<work-item-name>" not in prompt
    assert "input_state.feature_id" not in prompt
    assert "input_state.proposed_pr" not in prompt


def test_workflow_task_prompt_includes_task_interaction_style(
    tmp_path: Path,
) -> None:
    task = WorkflowTask(
        task_id="review-task",
        status=TaskStatus.OPEN,
        complexity=TaskComplexity.MEDIUM,
        input_state={"scope": "the proposed change"},
        description="Challenge the proposed change.",
        interaction_style="observational_review",
        assignee_type=AssigneeType.AGENT,
        assignee_role=AgentRole.REVIEWER,
    )
    workflow = WorkflowInstance.create(tmp_path / "workflow", (task,))

    messages = _build_task_messages(workflow, task, [], repo_root=tmp_path)

    assert "Interaction style: observational_review." in messages[0]["content"]
    assert (
        "Separate observations, inferences, risks, and recommendations."
        in (messages[0]["content"])
    )
    assert "Use `next_step` when this task is finished" in messages[0]["content"]
    assert json.loads(messages[1]["content"])["task"]["interaction_style"] == (
        "observational_review"
    )


def test_task_prompt_marks_deterministic_pre_step_as_authoritative(
    tmp_path: Path,
) -> None:
    task = WorkflowTask(
        task_id="context-task",
        status=TaskStatus.OPEN,
        complexity=TaskComplexity.HIGH,
        input_state={"feature_id": "fixture-feature"},
        description="Gather context about the proposed PR.",
        output_state_type="proposed-pr-context-state",
        assignee_type=AssigneeType.AGENT,
        assignee_role=AgentRole.ARCHITECT,
    )
    workflow = WorkflowInstance.create(tmp_path / "workflow", (task,))

    messages = _build_task_messages(
        workflow,
        task,
        [
            {
                "kind": "deterministic_pre_step",
                "action": "gather_context",
                "result": {"matches": []},
            }
        ],
        repo_root=tmp_path,
    )

    prompt = json.loads(messages[1]["content"])
    assert prompt["deterministic_pre_step"]["status"] == "already_completed"
    assert prompt["deterministic_pre_step"]["result"] == {"matches": []}
    assert prompt["deterministic_pre_step"]["required_output_state"] == {
        "proposed-pr-context-state": {"matches": []}
    }
    assert (
        "Do not search for, rediscover, reinterpret"
        in prompt["deterministic_pre_step"]["instructions"]
    )


def test_task_prompt_keeps_latest_result_without_repeating_old_results() -> None:
    events: list[dict[str, Any]] = [
        {
            "kind": "invoke_tool",
            "parameters": {"command": "cat a very large file"},
            "result": {"stdout": "old output"},
        },
        {
            "kind": "action_error",
            "action_kind": "edit",
            "error": "the requested range is outside the document",
        },
        {
            "kind": "read_document",
            "file_path": "docs/example.md",
            "result": {"content": "current output"},
        },
    ]

    prompt_events = _task_events_for_prompt(events)

    assert prompt_events["recent"][0] == {
        "kind": "invoke_tool",
    }
    assert prompt_events["recent"][1]["error"] == (
        "the requested range is outside the document"
    )
    assert prompt_events["latest_result"]["value"] == {"content": "current output"}
    assert "parameters" not in prompt_events["recent"][0]
    assert "result" not in prompt_events["recent"][0]


def test_task_prompt_preserves_latest_failed_result_separately() -> None:
    prompt_events = _task_events_for_prompt(
        [
            {
                "kind": "invoke_tool",
                "result": {"returncode": 2, "stderr": "validation failed"},
            },
            {"kind": "next_step"},
        ]
    )

    assert prompt_events["latest_failure"]["value"] == {
        "returncode": 2,
        "stderr": "validation failed",
    }


def test_task_prompt_bounds_event_metadata() -> None:
    prompt_events = _task_events_for_prompt(
        [{"kind": "read_document", "result": {"line": index}} for index in range(20)]
    )

    assert len(prompt_events["recent"]) == 12
    assert prompt_events["omitted_event_count"] == 8
    assert prompt_events["latest_result"]["value"] == {"line": 19}


def test_process_workflow_task_runs_nested_skill_in_same_worktree(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skill-definitions"
    save_skill(
        Skill(
            name="nested-skill",
            when_to_use=("Run nested work.",),
            steps=(SkillStep(description="Perform nested work."),),
        ),
        skills_dir / "nested-skill.yaml",
    )
    workflow = _workflow(tmp_path)
    client = _FakeClient(
        [
            {"kind": "invoke_skill", "skill": "nested-skill"},
            {"kind": "complete", "text": "Nested work complete."},
            {"kind": "complete", "output_state": {"ok": True}},
        ]
    )

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    assert WorkflowInstance.from_directory(workflow.directory).tasks[
        0
    ].output_state == {"ok": True}
    assert len(client.messages) == 3


def test_nested_skill_repairs_malformed_edit_action_in_place(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skill-definitions"
    save_skill(
        Skill(
            name="nested-skill",
            when_to_use=("Run nested work.",),
            steps=(SkillStep(description="Perform nested work."),),
        ),
        skills_dir / "nested-skill.yaml",
    )
    workflow = _workflow(tmp_path)
    client = _FakeClient(
        [
            {"kind": "invoke_skill", "skill": "nested-skill"},
            {
                "action": "edit",
                "file_path": "notes.txt",
                "edits": [
                    {
                        "kind": {"operation": "replace"},
                        "start_line": 1,
                        "end_line": 1,
                        "text": "invalid",
                    }
                ],
            },
            {"action": "complete", "text": "Nested work complete."},
            {"kind": "complete", "output_state": {"ok": True}},
        ]
    )
    stderr = io.StringIO()

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 0
    assert len(client.messages) == 4
    assert (
        "Workflow edit action edit kind must be a string"
        in client.messages[2][1]["content"]
    )
    assert "Nested skill action response needs repair" in stderr.getvalue()
    error_records = [
        json.loads(line)
        for line in (tmp_path / "workflow-llm-errors.jsonl").read_text().splitlines()
    ]
    assert error_records[-1]["phase"] == "nested_skill_llm_output_parse"
    assert error_records[-1]["context"]["skill_name"] == "nested-skill"


def test_process_workflow_task_passes_clean_nested_skill_context_between_skills(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skill-definitions"
    save_skill(
        Skill(
            name="review-skill",
            when_to_use=("Review a skill.",),
            steps=(SkillStep(description="Delegate the independent review."),),
        ),
        skills_dir / "review-skill.yaml",
    )
    save_skill(
        Skill(
            name="independent-review",
            when_to_use=("Review supplied material.",),
            steps=(SkillStep(description="Review the supplied material."),),
        ),
        skills_dir / "independent-review.yaml",
    )
    workflow = _workflow(tmp_path)
    client = _FakeClient(
        [
            {
                "kind": "invoke_skill",
                "skill": "review-skill",
                "clean": True,
                "context": [
                    "Target skill: review-skill-workflow",
                    "Original step: obtain an independent review",
                    "Proposed step: invoke the adversarial reviewer with context",
                ],
            },
            {
                "kind": "invoke_skill",
                "skill": "independent-review",
                "provider_role": "adversarial",
                "clean": True,
                "context": [
                    "Target skill: review-skill-workflow",
                    "Original step: obtain an independent review",
                    "Proposed step: invoke the adversarial reviewer with context",
                ],
            },
            {"kind": "complete", "text": "Independent review complete."},
            {"kind": "complete", "text": "Review complete."},
            {"kind": "complete", "output_state": {"ok": True}},
        ]
    )

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    review_prompt = json.loads(client.messages[1][1]["content"])
    assert review_prompt["step_context"] == [
        "Target skill: review-skill-workflow",
        "Original step: obtain an independent review",
        "Proposed step: invoke the adversarial reviewer with context",
    ]
    assert review_prompt["transcript"] == []
    assert review_prompt["execution_events"] == []
    nested_prompt = json.loads(client.messages[2][1]["content"])
    assert nested_prompt["selected_skill"]["name"] == "independent-review"
    assert nested_prompt["step_context"] == [
        "Target skill: review-skill-workflow",
        "Original step: obtain an independent review",
        "Proposed step: invoke the adversarial reviewer with context",
    ]
    assert nested_prompt["transcript"] == []


def test_process_workflow_task_uses_workflow_integration_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_root = tmp_path / "repo"
    primary_root.mkdir()
    for command in (
        ("git", "init", "-b", "main"),
        ("git", "config", "user.email", "test@example.com"),
        ("git", "config", "user.name", "Test User"),
    ):
        subprocess.run(
            list(command),
            cwd=primary_root,
            check=True,
            capture_output=True,
            text=True,
        )
    (primary_root / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=primary_root,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=primary_root,
        check=True,
        capture_output=True,
        text=True,
    )
    source_root = primary_root / ".worktrees" / "source"
    subprocess.run(
        ["git", "worktree", "add", "-b", "source", str(source_root), "main"],
        cwd=primary_root,
        check=True,
        capture_output=True,
        text=True,
    )
    workflow_dir = source_root / "docs" / "workflows" / "feature"
    WorkflowInstance.create(
        workflow_dir,
        (
            WorkflowTask(
                task_id="feature-17-task-001",
                status=TaskStatus.OPEN,
                upstream_task_ids=(),
                dependent_state=(),
                complexity=TaskComplexity.MEDIUM,
                input_state={"request": "Choose an API version."},
                description="Choose an API version.",
                assignee_type=AssigneeType.AGENT,
                assignee_role=AgentRole.ARCHITECT,
                llm_type="simple_task",
            ),
        ),
    )
    state = WorkflowGitState(
        proposed_pr_id="feature-17",
        base_branch="main",
        integration_branch="powdrr/feature-17",
        workflow_relative_directory="docs/workflows/feature",
    )
    save_workflow_git_state(workflow_dir, state)
    subprocess.run(
        ["git", "add", "docs/workflows/feature"],
        cwd=source_root,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "initialize workflow"],
        cwd=source_root,
        check=True,
        capture_output=True,
        text=True,
    )
    integration_root = primary_root / ".worktrees" / "powdrr" / "feature-17"
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "-b",
            "powdrr/feature-17",
            str(integration_root),
            "source",
        ],
        cwd=primary_root,
        check=True,
        capture_output=True,
        text=True,
    )
    published_roots: list[Path] = []

    monkeypatch.setattr(
        "powdrr_lift.workflow_task_agent._open_final_workflow_pull_request",
        lambda *_args, **_kwargs: None,
    )

    def _record_publish(
        repo_root: Path,
        published_workflow: WorkflowInstance,
        *,
        workflow_id: str | None = None,
        reason: str,
        stdout: object,
        open_pull_request: bool = True,
        events: Sequence[Mapping[str, object]] = (),
    ) -> None:
        del workflow_id, reason, stdout, open_pull_request, events
        published_roots.append(repo_root)
        assert published_workflow.directory == (
            integration_root / "docs" / "workflows" / "feature"
        )

    monkeypatch.setattr(
        "powdrr_lift.workflow_task_agent._publish_workflow_progress",
        _record_publish,
    )

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow_dir,
            repo_root=primary_root,
        ),
        client=_FakeClient([{"kind": "complete", "output_state": {"ok": True}}]),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    assert published_roots == [integration_root, integration_root]
    assert WorkflowInstance.from_directory(
        integration_root / "docs" / "workflows" / "feature"
    ).tasks[0].status is (TaskStatus.COMPLETED)
    assert (
        subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=integration_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "powdrr/feature-17"
    )
    assert not (integration_root / "tasks").exists()


def test_publish_workflow_progress_pushes_clean_worktree_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = WorkflowInstance.create(tmp_path / "workflow")
    save_workflow_git_state(
        workflow.directory,
        WorkflowGitState(
            proposed_pr_id="feature-17",
            base_branch="main",
            integration_branch="powdrr/feature-17",
            workflow_relative_directory="workflow",
        ),
    )
    git_calls: list[list[str]] = []

    monkeypatch.setattr(
        "powdrr_lift.workflow_task_agent._is_git_worktree",
        lambda _repo_root: True,
    )
    monkeypatch.setattr(
        "powdrr_lift.workflow_task_agent._git_output",
        lambda _repo_root, _arguments: "powdrr/feature-17",
    )
    monkeypatch.setattr(
        "powdrr_lift.workflow_task_agent._git_result",
        lambda _repo_root, _arguments: subprocess.CompletedProcess(
            [], 0, stdout="", stderr=""
        ),
    )

    def _record_git_call(_repo_root: Path, arguments: list[str]) -> str:
        git_calls.append(arguments)
        return ""

    monkeypatch.setattr("powdrr_lift.workflow_task_agent._run_git", _record_git_call)

    _publish_workflow_progress(
        tmp_path,
        workflow,
        workflow_id="feature-17",
        reason="complete feature-17-task-001",
        stdout=io.StringIO(),
        open_pull_request=False,
    )

    assert ["add", "--all"] in git_calls
    assert ["push", "--set-upstream", "origin", "powdrr/feature-17"] in git_calls


def test_process_workflow_task_persists_output_for_downstream_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _workflow(tmp_path)
    downstream = WorkflowTask(
        task_id="next-task",
        status=TaskStatus.OPEN,
        upstream_task_ids=("agent-task",),
        dependent_state=("next-input-ready",),
        complexity=TaskComplexity.MEDIUM,
        input_state={"plan": "agent-task.state"},
        description="Use the completed plan.",
        output_state_type="implementation-state",
    )
    workflow.add_task(downstream)
    published_reasons: list[str] = []

    def _record_publish(
        repo_root: Path,
        published_workflow: WorkflowInstance,
        *,
        workflow_id: str | None = None,
        reason: str,
        stdout: object,
        open_pull_request: bool = True,
        events: Sequence[Mapping[str, object]] = (),
    ) -> None:
        del workflow_id, open_pull_request, events
        assert repo_root == tmp_path
        assert published_workflow.directory == workflow.directory
        published_reasons.append(reason)

    monkeypatch.setattr(
        "powdrr_lift.workflow_task_agent._publish_workflow_progress",
        _record_publish,
    )
    client = _FakeClient(
        [
            {"kind": "next_step", "output_state": {"plan": ["step"]}},
            {"kind": "complete", "output_state": {"result": "done"}},
        ]
    )

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    persisted = WorkflowInstance.from_directory(workflow.directory)
    completed_task = next(
        task for task in persisted.tasks if task.task_id == "agent-task"
    )
    next_task = next(task for task in persisted.tasks if task.task_id == "next-task")
    assert exit_code == 0
    assert completed_task.output_state == {"plan": ["step"]}
    assert next_task.input_state == {"plan": {"plan": ["step"]}}
    assert next_task.status is TaskStatus.COMPLETED
    assert next_task.output_state == {"result": "done"}
    assert published_reasons == [
        "claim agent-task",
        "next_step agent-task",
        "claim next-task",
        "terminate next-task",
    ]


def test_locked_workflow_task_reports_recovery_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = replace(_workflow(tmp_path).tasks[0], status=TaskStatus.LOCKED)
    workflow = WorkflowInstance.create(tmp_path / "workflow", (task,))
    state = WorkflowGitState(
        proposed_pr_id="feature-17",
        base_branch="main",
        integration_branch="powdrr/feature-17",
        workflow_relative_directory="workflow",
    )
    monkeypatch.setattr(
        "powdrr_lift.workflow_task_agent.inspect_workflow_run",
        lambda _repo_root, _proposed_pr_id: {
            "claim_refs": [
                "refs/agents/claims/feature-17/agent-task",
            ]
        },
    )

    with pytest.raises(WorkflowGitInconsistency) as raised:
        _validate_workflow_task_state(workflow, state, tmp_path)

    message = str(raised.value)
    assert "agent-task is locked" in message
    assert "refs/agents/claims/feature-17/agent-task" in message
    assert (
        "powdrr-lift workflow-recovery --proposed-pr-id feature-17 --cleanup" in message
    )


def test_process_workflow_task_stops_when_human_task_becomes_ready(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)
    workflow.add_task(
        WorkflowTask(
            task_id="human-task",
            status=TaskStatus.OPEN,
            upstream_task_ids=("agent-task",),
            dependent_state=(),
            complexity=TaskComplexity.LOW,
            input_state={"decision": "required"},
            description="Make the final decision.",
            assignee_type=AssigneeType.HUMAN,
            assignee_role=HumanRole.REVIEWER,
        )
    )
    stdout = io.StringIO()

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=_FakeClient(
            [{"kind": "next_step", "output_state": {"result": "ready"}}]
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    persisted = WorkflowInstance.from_directory(workflow.directory)
    assert exit_code == 0
    assert persisted.tasks[0].status is TaskStatus.COMPLETED
    assert persisted.tasks[1].status is TaskStatus.OPEN
    assert "Workflow waiting on human task: human-task" in stdout.getvalue()


def test_process_workflow_task_repairs_invalid_json_response(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)

    class _InvalidThenCompleteClient(_FakeClient):
        def __init__(self) -> None:
            super().__init__([])
            self.calls = 0

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, object]:
            self.messages.append(messages)
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError(
                    "OpenAI response content was not valid JSON: Expecting value"
                )
            return {"kind": "complete", "output_state": {"version": "v2"}}

    client = _InvalidThenCompleteClient()
    stderr = io.StringIO()
    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 0
    assert client.calls == 2
    assert "response needs repair" in stderr.getvalue()
    assert "<no parsed response; client error:" in stderr.getvalue()
    assert "response_correction" not in client.messages[1][1]["content"]
    assert "Expecting value" in client.messages[1][1]["content"]
    assert "not valid JSON" in client.messages[1][1]["content"]
    assert WorkflowInstance.from_directory(workflow.directory).tasks[0].status is (
        TaskStatus.COMPLETED
    )


def test_workflow_task_accepts_workflow_chat_action_field() -> None:
    action = _parse_action_response(
        {"action": "complete", "output_state": {"result": "shared contract"}}
    )

    assert action.kind == "complete"
    assert action.output_state == {"result": "shared contract"}


def test_process_workflow_task_compacts_context_before_exceeding_model_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _workflow(tmp_path)
    client = _FakeClient(
        [
            {"compacted_context": {"necessary": ["keep this"]}},
            {"kind": "complete", "output_state": {"version": "v2"}},
        ]
    )

    def _estimate(messages: list[dict[str, str]]) -> int:
        if "compacting context" in messages[0]["content"]:
            return 10
        if len(messages) == 1:
            return 10
        payload = json.loads(messages[1]["content"])
        return 10 if "compacted_context" in payload else 100

    monkeypatch.setattr(
        "powdrr_lift.workflow_task_agent._estimate_message_tokens",
        _estimate,
    )
    limit_calls = 0

    def _limits(*_args: object, **_kwargs: object) -> LLMModelLimits:
        nonlocal limit_calls
        limit_calls += 1
        return LLMModelLimits(
            context_window=50 if limit_calls == 1 else 50_000,
            max_output_tokens=50,
        )

    monkeypatch.setattr("powdrr_lift.workflow_task_agent._model_limits_for", _limits)
    stderr = io.StringIO()

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 0
    assert len(client.messages) == 2
    compaction_prompt = json.loads(client.messages[0][1]["content"])
    assert compaction_prompt["task_description"] == "Choose an API version."
    assert compaction_prompt["task_details"] is None
    compacted_prompt = json.loads(client.messages[1][1]["content"])
    assert compacted_prompt["compacted_context"]["necessary"] == ["keep this"]
    assert "latest_actionable" in compacted_prompt["compacted_context"]
    status = stderr.getvalue()
    assert "Compacting workflow task context" in status
    assert "waiting for context compaction LLM response" in status
    assert "Compacted workflow task context" in status


def test_process_workflow_task_compacts_at_proactive_threshold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _workflow(tmp_path)
    client = _FakeClient(
        [
            {"compacted_context": {"necessary": ["keep this"]}},
            {"kind": "complete", "output_state": {"version": "v2"}},
        ]
    )

    def _estimate(messages: list[dict[str, str]]) -> int:
        if "compacting context" in messages[0]["content"]:
            return 10
        if len(messages) == 1:
            return 10
        payload = json.loads(messages[1]["content"])
        return 10 if "compacted_context" in payload else 4_000

    monkeypatch.setattr(
        "powdrr_lift.workflow_task_agent._estimate_message_tokens", _estimate
    )
    monkeypatch.setattr(
        "powdrr_lift.workflow_task_agent._model_limits_for",
        lambda *_args, **_kwargs: LLMModelLimits(
            context_window=5_000,
            max_output_tokens=500,
        ),
    )

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
            context_compaction_threshold=0.75,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    assert len(client.messages) == 2


def test_process_workflow_task_retries_llm_timeouts_with_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _workflow(tmp_path)
    client = _FakeClient([])
    calls = 0
    sleeps: list[float] = []

    def _complete(messages: list[dict[str, str]]) -> dict[str, object]:
        nonlocal calls
        client.messages.append(messages)
        calls += 1
        if calls < 3:
            raise RuntimeError("OpenAI-compatible request timed out")
        return {"kind": "complete", "output_state": {"version": "v2"}}

    client.complete_json = _complete  # type: ignore[method-assign]
    monkeypatch.setattr("powdrr_lift.workflow_llm.time.sleep", sleeps.append)
    stderr = io.StringIO()

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
            max_timeout_retries=2,
            timeout_backoff_seconds=1.5,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 0
    assert calls == 3
    assert sleeps == [1.5, 3.0]
    assert stderr.getvalue().count("retrying in") == 2


def test_process_workflow_task_retries_provider_overload_with_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _workflow(tmp_path)
    client = _FakeClient([])
    calls = 0
    sleeps: list[float] = []

    def _complete(messages: list[dict[str, str]]) -> dict[str, object]:
        nonlocal calls
        client.messages.append(messages)
        calls += 1
        if calls < 3:
            raise WorkflowLLMHTTPError(
                "OpenAI",
                429,
                '{"error":{"code":"engine_overloaded"}}',
            )
        return {"kind": "complete", "output_state": {"version": "v2"}}

    client.complete_json = _complete  # type: ignore[method-assign]
    monkeypatch.setattr("powdrr_lift.workflow_llm.time.sleep", sleeps.append)
    stderr = io.StringIO()

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
            max_timeout_retries=2,
            timeout_backoff_seconds=1.5,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 0
    assert calls == 3
    assert sleeps == [1.5, 3.0]
    assert stderr.getvalue().count("provider is overloaded") == 2


def test_workflow_retries_dropped_provider_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient([])
    calls = 0
    sleeps: list[float] = []

    def _complete(messages: list[dict[str, str]]) -> dict[str, object]:
        nonlocal calls
        client.messages.append(messages)
        calls += 1
        if calls < 3:
            raise RuntimeError(
                "OpenAI request connection dropped: "
                "Remote end closed connection without response"
            )
        return {"action": "next_step"}

    client.complete_json = _complete  # type: ignore[method-assign]
    monkeypatch.setattr("powdrr_lift.workflow_llm.time.sleep", sleeps.append)

    result = complete_json_with_timeout_retry(
        client,
        [{"role": "user", "content": "hello"}],
        model="test-model",
        stderr=io.StringIO(),
        max_timeout_retries=2,
        timeout_backoff_seconds=1.5,
    )

    assert result == {"action": "next_step"}
    assert calls == 3
    assert sleeps == [1.5, 3.0]


def test_exhausted_timeout_keeps_workflow_worktree_for_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted: list[Path] = []
    monkeypatch.setattr(
        "powdrr_lift.workflow_task_agent._delete_workflow_task_worktree",
        lambda path, *, stderr: deleted.append(path),
    )
    task = _workflow(tmp_path).tasks[0]
    dedicated_worktree = tmp_path / ".worktrees" / "timed-out-task"

    result = _handle_exhausted_timeout(
        dedicated_worktree,
        task,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        error=RuntimeError("timed out"),
    )

    assert result == 2
    assert deleted == []


def test_process_workflow_task_prints_invalid_response_before_repair(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)
    stderr = io.StringIO()
    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=_FakeClient(
            [
                {"action": None, "diagnostic": "inspect me"},
                {"action": "complete", "output_state": {"version": "v2"}},
            ]
        ),
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 0
    output = stderr.getvalue()
    assert "Workflow task LLM response requiring repair:" in output
    assert '"action": null' in output
    assert '"diagnostic": "inspect me"' in output


def test_process_workflow_task_supports_fuzzy_match_tool(tmp_path: Path) -> None:
    workflow = _workflow(tmp_path)
    (tmp_path / "candidate-spec.yaml").write_text("name: candidate\n", encoding="utf-8")
    client = _FakeClient(
        [
            {
                "action": "invoke_tool",
                "tool": "fuzzy-match",
                "parameters": {
                    "command": [
                        "fuzzy-match",
                        ".",
                        "-name",
                        "candidate",
                        "-type",
                        "f",
                        "-print",
                    ]
                },
            },
            {"kind": "complete", "output_state": {"found": True}},
        ]
    )

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    assert "fuzzy-match" in client.messages[0][1]["content"]
    assert "candidate-spec.yaml" in client.messages[1][1]["content"]
    assert "available_tools" in client.messages[0][1]["content"]


def test_process_workflow_task_repairs_fuzzy_match_tool_error(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)
    (tmp_path / "candidate-spec.yaml").write_text("name: candidate\n", encoding="utf-8")
    client = _FakeClient(
        [
            {
                "action": "invoke_tool",
                "tool": "fuzzy-match",
                "parameters": {"command": ["fuzzy-match", "."]},
            },
            {
                "kind": "invoke_tool",
                "tool": "fuzzy-match",
                "parameters": {
                    "command": [
                        "fuzzy-match",
                        ".",
                        "-name",
                        "candidate",
                        "-type",
                        "f",
                    ]
                },
            },
            {"kind": "complete", "output_state": {"found": True}},
        ]
    )

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    assert len(client.messages) == 3
    correction = client.messages[1][1]["content"]
    assert "fuzzy-match requires -name <query>" in correction
    assert "corrected JSON action" in correction
    assert "tool_error" in correction


def test_process_workflow_task_supports_gather_context_action(tmp_path: Path) -> None:
    workflow = _workflow(tmp_path)
    specs = tmp_path / "docs" / "specs" / "example"
    specs.mkdir(parents=True)
    (specs / "proposed-pr-specification.yaml").write_text(
        "proposed_prs:\n- id: example-pr\n  state: proposed\n",
        encoding="utf-8",
    )
    client = _FakeClient(
        [
            {
                "kind": "gather_context",
                "types": ["proposed_prs"],
                "keywords": ["example-pr"],
            },
            {"kind": "complete", "output_state": {"found": True}},
        ]
    )

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    assert "example-pr" in client.messages[1][1]["content"]
    assert "gather_context" in client.messages[0][0]["content"]


def test_process_workflow_task_can_advance_after_empty_gather_context(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)
    client = _FakeClient(
        [
            {"kind": "gather_context", "types": ["proposed_prs"]},
            {"kind": "next_step"},
            {"kind": "complete", "output_state": {"found": False}},
        ]
    )

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0


def test_process_workflow_task_repairs_read_document_range_error(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)
    (tmp_path / "specification.yaml").write_text("first\nsecond\n", encoding="utf-8")
    client = _FakeClient(
        [
            {
                "kind": "read_document",
                "file_path": "specification.yaml",
                "start_line": 1,
                "end_line": 10,
            },
            {"kind": "complete", "output_state": {"read": True}},
        ]
    )

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    assert len(client.messages) == 2


def test_read_task_document_clamps_end_line_to_document_length(
    tmp_path: Path,
) -> None:
    (tmp_path / "specification.yaml").write_text("first\nsecond\n", encoding="utf-8")

    result = _read_task_document(
        WorkflowAction(
            kind="read_document",
            file_path="specification.yaml",
            start_line=1,
            end_line=50,
        ),
        tmp_path,
    )

    assert result["end_line"] == 2
    assert [line["text"] for line in result["lines"]] == ["first", "second"]


def test_process_workflow_task_repairs_guessed_workflow_filename_suffix(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)
    guessed_path = workflow.directory / "agent-task."
    client = _FakeClient(
        [
            {
                "action": "invoke_tool",
                "tool": "shell",
                "parameters": {
                    "command": f"cat {guessed_path}",
                },
            },
            {"action": "complete", "output_state": {"version": "v2"}},
        ]
    )
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert "Corrected malformed workflow filename suffix" in stderr.getvalue()
    assert "Rejected workflow shell command" not in stderr.getvalue()
    assert "agent-task.yaml" in stdout.getvalue()
    assert "workflow_files" in client.messages[0][1]["content"]
    assert "agent-task.yaml" in client.messages[0][1]["content"]
    assert WorkflowInstance.from_directory(workflow.directory).tasks[0].status is (
        TaskStatus.COMPLETED
    )


def test_workflow_file_command_error_is_not_reported_for_exact_filename(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)

    assert (
        _workflow_file_command_error(
            {"command": f"cat {workflow.directory / 'agent-task.yaml'}"},
            workflow.directory,
        )
        is None
    )


def test_workflow_task_client_defaults_to_deepinfra_cheap_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _workflow(tmp_path)
    captured: dict[str, str] = {}
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("DEEPINFRA_API_TOKEN", "test-token")

    class _FakeOpenAIClient:
        def __init__(
            self,
            *,
            model: str,
            api_key: str,
            base_url: str,
            limits: object,
            progress_stream: object = None,
        ) -> None:
            captured.update(
                {
                    "model": model,
                    "api_key": api_key,
                    "base_url": base_url,
                }
            )

    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent.OpenAIChatClient",
        _FakeOpenAIClient,
    )
    monkeypatch.setattr(
        "powdrr_lift.workflow_task_agent._resolve_credentials",
        lambda provider, api_key, base_url: type(
            "Credentials",
            (),
            {"api_key": "test-token", "base_url": "https://example.test"},
        )(),
    )

    _build_workflow_client(
        WorkflowTaskAgentConfig(workflow_dir=workflow.directory),
        workflow.tasks[0],
    )

    assert captured == {
        "model": "deepseek-ai/DeepSeek-V4-Flash-0731",
        "api_key": "test-token",
        "base_url": "https://example.test",
    }


def test_process_workflow_task_blocks_with_human_handoff_and_follow_up(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)
    client = _FakeClient(
        [
            {
                "kind": "get-human-input",
                "human_input": {
                    "human_task": {
                        "description": "Choose v1 or v2.",
                        "role": "decider",
                        "input_state": {"options": ["v1", "v2"]},
                        "output_state_type": "api-decision",
                    },
                    "incorporation_instructions": (
                        "Use the human decision in the implementation."
                    ),
                    "follow_up_task": {
                        "description": "Implement the selected API version.",
                        "role": "coder",
                        "input_state": {"source": "human-input-1"},
                        "output_state_type": "implementation-state",
                    },
                },
            }
        ]
    )

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    blocked_workflow = WorkflowInstance.from_directory(workflow.directory)
    assert exit_code == 0
    assert blocked_workflow.tasks[0].status is TaskStatus.LOCKED
    assert [task.task_id for task in blocked_workflow.ready_tasks()] == [
        "human-input-1"
    ]
    follow_up = next(
        task
        for task in blocked_workflow.tasks
        if task.task_id == "human-input-1-follow-up"
    )
    assert follow_up.upstream_task_ids == ("human-input-1",)
    assert follow_up.details == "Use the human decision in the implementation."

    blocked_workflow.complete_task(
        "human-input-1",
        output_state={"decision": "v2"},
    )
    assert [task.task_id for task in blocked_workflow.ready_tasks()] == [
        "human-input-1-follow-up"
    ]
