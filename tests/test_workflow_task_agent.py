from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from powdrr_lift.core import (
    AgentRole,
    AssigneeType,
    Skill,
    SkillStep,
    TaskComplexity,
    TaskStatus,
    WorkflowInstance,
    WorkflowTask,
    save_skill,
)
from powdrr_lift.workflow_chat_agent import LLMModelLimits, _action_system_prompt
from powdrr_lift.workflow_task_agent import (
    WorkflowTaskAgentConfig,
    _build_workflow_client,
    _handle_exhausted_timeout,
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


def test_process_workflow_task_completes_claimed_agent_task(tmp_path: Path) -> None:
    workflow = _workflow(tmp_path)
    client = _FakeClient([{"kind": "complete", "output_state": {"version": "v2"}}])
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
    completed = WorkflowInstance.from_directory(workflow.directory).tasks[0]
    assert completed.status is TaskStatus.COMPLETED
    assert completed.output_state == {"version": "v2"}
    prompt = client.messages[0][1]["content"]
    assert client.messages[0][0]["content"] == _action_system_prompt()
    assert '"execution_mode": "process_workflow_task"' in prompt
    displayed = stderr.getvalue()
    assert "Workflow task LLM input:" in displayed
    assert "Workflow task LLM output:" in displayed
    assert '"kind": "complete"' in displayed
    assert "received streamed LLM data" not in displayed


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


def test_process_workflow_task_relocates_execution_into_dedicated_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_root = tmp_path / "repo"
    worktree_root = primary_root / ".worktrees" / "workflow-task"
    workflow = WorkflowInstance.create(
        worktree_root / "docs" / "workflows" / "feature",
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
    published_roots: list[Path] = []

    monkeypatch.setattr(
        "powdrr_lift.workflow_task_agent._is_git_worktree",
        lambda _: True,
    )
    monkeypatch.setattr(
        "powdrr_lift.workflow_task_agent._resolve_worktree_context",
        lambda *_args, **_kwargs: worktree_root,
    )

    def _record_publish(
        repo_root: Path,
        published_workflow: WorkflowInstance,
        *,
        reason: str,
        stdout: object,
    ) -> None:
        del reason, stdout
        published_roots.append(repo_root)
        assert published_workflow.directory == workflow.directory

    monkeypatch.setattr(
        "powdrr_lift.workflow_task_agent._publish_workflow_progress",
        _record_publish,
    )

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=primary_root / "docs" / "workflows" / "feature",
            repo_root=primary_root,
        ),
        client=_FakeClient([{"kind": "complete", "output_state": {"ok": True}}]),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    assert published_roots == [worktree_root, worktree_root]
    assert WorkflowInstance.from_directory(workflow.directory).tasks[0].status is (
        TaskStatus.COMPLETED
    )


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
        reason: str,
        stdout: object,
    ) -> None:
        assert repo_root == tmp_path
        assert published_workflow.directory == workflow.directory
        published_reasons.append(reason)

    monkeypatch.setattr(
        "powdrr_lift.workflow_task_agent._publish_workflow_progress",
        _record_publish,
    )
    client = _FakeClient([{"kind": "complete", "output_state": {"plan": ["step"]}}])

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
    assert next_task.input_state == {"plan": "agent-task.state"}

    claimed_next_task = persisted.claim_task("next-task")
    assert claimed_next_task.input_state == {"plan": {"plan": ["step"]}}
    assert published_reasons == ["claim agent-task", "complete agent-task"]


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
    assert compacted_prompt["compacted_context"] == {"necessary": ["keep this"]}
    status = stderr.getvalue()
    assert "Compacting workflow task context" in status
    assert "waiting for context compaction LLM response" in status
    assert "Compacted workflow task context" in status


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
            raise RuntimeError(
                "OpenAI request failed with HTTP 429: "
                '{"error":{"code":"engine_overloaded"}}'
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


def test_exhausted_timeout_deletes_only_dedicated_worktree(
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
    assert deleted == [dedicated_worktree]


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
                {"kind": None, "diagnostic": "inspect me"},
                {"kind": "complete", "output_state": {"version": "v2"}},
            ]
        ),
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 0
    output = stderr.getvalue()
    assert "Workflow task LLM response requiring repair:" in output
    assert '"kind": null' in output
    assert '"diagnostic": "inspect me"' in output


def test_process_workflow_task_supports_fuzzy_match_tool(tmp_path: Path) -> None:
    workflow = _workflow(tmp_path)
    (tmp_path / "candidate-spec.yaml").write_text("name: candidate\n", encoding="utf-8")
    client = _FakeClient(
        [
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
                "kind": "invoke_tool",
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
            {
                "kind": "read_document",
                "file_path": "specification.yaml",
                "start_line": 1,
                "end_line": 2,
            },
            {"kind": "complete", "output_state": {"read": True}},
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
    assert len(client.messages) == 3
    correction = client.messages[1][1]["content"]
    assert "outside the document" in correction
    assert "Request a range from 1 through 2" in correction
    assert "corrected JSON action" in correction
    assert "action_error" in correction
    assert "needs correction" in stderr.getvalue()


def test_process_workflow_task_repairs_guessed_workflow_filename_suffix(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)
    guessed_path = workflow.directory / "agent-task."
    client = _FakeClient(
        [
            {
                "kind": "invoke_tool",
                "parameters": {
                    "command": f"cat {guessed_path}",
                },
            },
            {"kind": "complete", "output_state": {"version": "v2"}},
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
    assert "agent-task.json" in stdout.getvalue()
    assert "workflow_files" in client.messages[0][1]["content"]
    assert "agent-task.json" in client.messages[0][1]["content"]
    assert WorkflowInstance.from_directory(workflow.directory).tasks[0].status is (
        TaskStatus.COMPLETED
    )


def test_workflow_file_command_error_is_not_reported_for_exact_filename(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)

    assert (
        _workflow_file_command_error(
            {"command": f"cat {workflow.directory / 'agent-task.json'}"},
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
        "model": "deepseek-ai/DeepSeek-V4-Flash",
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
