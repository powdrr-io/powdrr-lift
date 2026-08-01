from __future__ import annotations

import io
from pathlib import Path

import pytest

from powdrr_lift.core import (
    AgentRole,
    AssigneeType,
    TaskComplexity,
    TaskStatus,
    WorkflowInstance,
    WorkflowTask,
)
from powdrr_lift.workflow_task_agent import (
    WorkflowTaskAgentConfig,
    _build_zai_client,
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
    completed = WorkflowInstance.from_directory(workflow.directory).tasks[0]
    assert completed.status is TaskStatus.COMPLETED
    assert completed.output_state == {"version": "v2"}
    prompt = client.messages[0][1]["content"]
    assert "staff engineer" in client.messages[0][0]["content"]
    assert '"execution_mode": "process_workflow_task"' in prompt


def test_workflow_task_client_uses_task_llm_type_for_zai_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _workflow(tmp_path)
    captured: dict[str, str] = {}

    class _FakeOpenAIClient:
        def __init__(self, *, model: str, api_key: str, base_url: str) -> None:
            captured.update(model=model, api_key=api_key, base_url=base_url)

    monkeypatch.setenv("ZAI_API_KEY", "test-key")
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent.OpenAIChatClient",
        _FakeOpenAIClient,
    )

    _build_zai_client(
        WorkflowTaskAgentConfig(workflow_dir=workflow.directory),
        workflow.tasks[0],
    )

    assert captured == {
        "model": "glm-4.7-flash",
        "api_key": "test-key",
        "base_url": "https://api.z.ai/api/paas/v4/",
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
