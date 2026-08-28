from __future__ import annotations

import json
from pathlib import Path

import pytest

from powdrr_lift.core.skill_specification import SkillToolInvocation
from powdrr_lift.core.workflow_task_specification import (
    AgentRole,
    AssigneeType,
    HumanRole,
    TaskComplexity,
    TaskStatus,
    WorkflowInstance,
    WorkflowTask,
    build_workflow_task_directory_validation_report,
    build_workflow_task_validation_report,
    load_ready_workflow_tasks,
    load_workflow_task,
    load_workflow_tasks,
    save_workflow_task,
    select_ready_workflow_tasks,
    validate_workflow_task_directory,
    workflow_task_from_json,
    workflow_task_from_yaml,
    workflow_task_to_json,
    workflow_task_to_yaml,
)


def test_workflow_task_round_trips_through_json() -> None:
    task = WorkflowTask(
        task_id="task-1",
        status=TaskStatus.OPEN,
        upstream_task_ids=("task-0",),
        dependent_state=("state-a", "state-b"),
        complexity=TaskComplexity.MEDIUM,
        input_state={"environment": "staging"},
        description="Prepare the deployment environment.",
    )

    json_text = workflow_task_to_json(task)
    parsed = workflow_task_from_json(json_text)

    assert parsed == task
    assert json.loads(json_text) == {
        "task_id": "task-1",
        "status": "open",
        "upstream_task_ids": ["task-0"],
        "dependent_state": ["state-a", "state-b"],
        "complexity": "medium",
        "input_state": {"environment": "staging"},
        "assignee_type": "agent",
        "assignee_role": "coder",
        "output_state_type": "state",
        "description": "Prepare the deployment environment.",
        "step_type": "freeform",
        "actions": [
            {
                "name": "next_step",
                "instructions": "Advance after this task is complete.",
            }
        ],
    }


def test_workflow_task_round_trips_through_yaml() -> None:
    task = WorkflowTask(
        task_id="task-1",
        status=TaskStatus.OPEN,
        upstream_task_ids=(),
        dependent_state=(),
        complexity=TaskComplexity.LOW,
        input_state={"environment": "staging"},
        description="Prepare the deployment environment.",
    )

    yaml_text = workflow_task_to_yaml(task)
    parsed = workflow_task_from_yaml(yaml_text)

    assert parsed == task
    assert "task_id: task-1" in yaml_text
    assert "step_type: freeform" in yaml_text


def test_workflow_task_round_trips_executable_step_fields() -> None:
    task = WorkflowTask(
        task_id="task-1",
        status=TaskStatus.OPEN,
        upstream_task_ids=(),
        dependent_state=(),
        complexity=TaskComplexity.MEDIUM,
        input_state={},
        description="Run the implementation step.",
        details="Use the approved design and preserve the recorded decisions.",
        llm_type="standard_reasoning",
        uses_skills=("review-context",),
        tool_invocations=(SkillToolInvocation(tool="shell", command=("pytest", "-q")),),
    )

    parsed = workflow_task_from_json(workflow_task_to_json(task))

    assert parsed == task
    assert parsed.to_data()["tool_invocations"] == [
        {"tool": "shell", "command": ["pytest", "-q"]}
    ]


def test_workflow_task_interaction_style_round_trips_and_validates() -> None:
    task = WorkflowTask(
        task_id="review-task",
        status=TaskStatus.OPEN,
        complexity=TaskComplexity.MEDIUM,
        input_state={"scope": "the proposed change"},
        description="Challenge the proposed change.",
        interaction_style="devils_advocate",
    )

    parsed = workflow_task_from_json(workflow_task_to_json(task))

    assert parsed == task
    assert parsed.to_data()["interaction_style"] == "devils_advocate"

    invalid_report = build_workflow_task_validation_report(
        json.dumps(task.to_data() | {"interaction_style": "invented"})
    )

    assert invalid_report.validation_successful is False
    assert [issue.code for issue in invalid_report.issues] == [
        "invalid_interaction_style"
    ]


def test_workflow_task_directory_loader_reads_all_json_files(
    tmp_path: Path,
) -> None:
    task_a = WorkflowTask(
        task_id="task-a",
        status=TaskStatus.OPEN,
        upstream_task_ids=(),
        dependent_state=("state-a",),
        complexity=TaskComplexity.LOW,
        input_state={"ready": True},
        description="First task.",
    )
    task_b = WorkflowTask(
        task_id="task-b",
        status=TaskStatus.LOCKED,
        upstream_task_ids=("task-a",),
        dependent_state=("state-b",),
        complexity=TaskComplexity.HIGH,
        input_state={"ready": False},
        description="Second task.",
    )

    save_workflow_task(task_b, tmp_path / "b.json")
    save_workflow_task(task_a, tmp_path / "a.json")
    (tmp_path / "feature-workflow.yaml").write_text(
        "integration_branch: powdrr/feature\n",
        encoding="utf-8",
    )
    assert load_workflow_tasks(tmp_path) == (task_a, task_b)


def test_workflow_pauses_for_human_input_then_resumes_agent_task(
    tmp_path: Path,
) -> None:
    """A running workflow can insert a human task and resume after its output."""
    agent_request = WorkflowTask(
        task_id="agent-request",
        status=TaskStatus.OPEN,
        upstream_task_ids=(),
        dependent_state=(),
        complexity=TaskComplexity.MEDIUM,
        input_state={"feature": "Add the new API"},
        description="Determine the API version for the implementation.",
        assignee_type=AssigneeType.AGENT,
        assignee_role=AgentRole.CODER,
    )
    human_decision = WorkflowTask(
        task_id="human-decision",
        status=TaskStatus.OPEN,
        upstream_task_ids=("agent-request",),
        dependent_state=(),
        complexity=TaskComplexity.LOW,
        input_state={
            "question": "Which API version should this use?",
            "context": "The agent could not determine this from the repository.",
        },
        description="Choose the API version for the implementation.",
        assignee_type=AssigneeType.HUMAN,
        assignee_role=HumanRole.DECIDER,
    )
    implementation = WorkflowTask(
        task_id="implementation",
        status=TaskStatus.OPEN,
        upstream_task_ids=("human-decision",),
        dependent_state=(),
        complexity=TaskComplexity.MEDIUM,
        input_state={"use_decision_from": "human-decision"},
        description="Implement the API using the selected version.",
        assignee_type=AssigneeType.AGENT,
        assignee_role=AgentRole.CODER,
    )

    workflow = WorkflowInstance.create(tmp_path / "feature", (agent_request,))
    assert workflow.ready_tasks() == (agent_request,)

    # The agent completes its first task, realizes it needs a decision, and
    # mutates the running workflow with the human handoff and continuation.
    workflow.complete_task(
        "agent-request",
        output_state={"question": "Which API version should this use?"},
    )
    workflow.add_task(human_decision)
    workflow.add_task(implementation)

    assert workflow.ready_tasks(assignee_type=AssigneeType.HUMAN) == (human_decision,)
    assert workflow.ready_tasks(assignee_type=AssigneeType.AGENT) == ()

    workflow.complete_task(
        "human-decision",
        output_state={"decision": "Use v2 because the existing clients support it."},
    )

    assert workflow.ready_tasks(assignee_type=AssigneeType.AGENT) == (implementation,)
    assert workflow.task_context("implementation")["upstream_outputs"] == {
        "human-decision": {
            "decision": "Use v2 because the existing clients support it."
        }
    }
    assert WorkflowInstance.from_directory(tmp_path / "feature").ready_tasks() == (
        implementation,
    )


def test_workflow_termination_closes_remaining_tasks(tmp_path: Path) -> None:
    first = WorkflowTask(
        task_id="first",
        status=TaskStatus.OPEN,
        complexity=TaskComplexity.LOW,
        input_state={},
        description="Check whether the proposal is superseded.",
    )
    second = WorkflowTask(
        task_id="second",
        status=TaskStatus.OPEN,
        upstream_task_ids=("first",),
        complexity=TaskComplexity.LOW,
        input_state={},
        description="Do the implementation.",
    )
    workflow = WorkflowInstance.create(tmp_path / "superseded", (first,))
    workflow.add_task(second)
    workflow.claim_task("first")

    terminated = workflow.terminate_workflow("first", {"superseded": True})

    assert terminated.status is TaskStatus.COMPLETED
    persisted = WorkflowInstance.from_directory(tmp_path / "superseded")
    assert persisted.tasks[1].status is TaskStatus.CLOSED
    assert persisted.is_finished()


def test_workflow_materializes_upstream_output_contract_when_task_is_claimed(
    tmp_path: Path,
) -> None:
    upstream = WorkflowTask(
        task_id="plan",
        status=TaskStatus.OPEN,
        upstream_task_ids=(),
        dependent_state=("plan-created",),
        complexity=TaskComplexity.HIGH,
        input_state={"request": "Plan the change."},
        description="Create a plan.",
        output_state_type="execution-plan-state",
    )
    downstream = WorkflowTask(
        task_id="implement",
        status=TaskStatus.OPEN,
        upstream_task_ids=("plan",),
        dependent_state=("implementation-created",),
        complexity=TaskComplexity.HIGH,
        input_state={"plan": "plan.execution-plan-state"},
        description="Implement the plan.",
        output_state_type="implementation-state",
    )
    workflow = WorkflowInstance.create(tmp_path / "workflow", (upstream,))
    workflow.add_task(downstream)

    workflow.complete_task(
        "plan",
        output_state={"steps": ["add the behavior", "validate it"]},
    )
    claimed_task = workflow.claim_task("implement")

    assert claimed_task.input_state == {
        "plan": {"steps": ["add the behavior", "validate it"]},
    }
    persisted = WorkflowInstance.from_directory(tmp_path / "workflow").tasks
    persisted_downstream = next(
        task for task in persisted if task.task_id == "implement"
    )
    assert persisted_downstream.input_state == claimed_task.input_state


def test_workflow_cannot_complete_without_declared_output_state(tmp_path: Path) -> None:
    workflow = WorkflowInstance.create(
        tmp_path / "workflow",
        (
            WorkflowTask(
                task_id="plan",
                status=TaskStatus.OPEN,
                upstream_task_ids=(),
                dependent_state=("plan-created",),
                complexity=TaskComplexity.HIGH,
                input_state={"request": "Plan the change."},
                description="Create a plan.",
                output_state_type="execution-plan-state",
            ),
        ),
    )

    with pytest.raises(ValueError, match="must provide a non-null output_state"):
        workflow.complete_task("plan")


def test_workflow_task_directory_validation_accepts_known_dependencies(
    tmp_path: Path,
) -> None:
    save_workflow_task(
        WorkflowTask(
            task_id="task-1",
            status=TaskStatus.OPEN,
            upstream_task_ids=(),
            dependent_state=("state-a",),
            complexity=TaskComplexity.LOW,
            input_state={"ready": True},
            description="First task.",
        ),
        tmp_path / "task-1.json",
    )
    save_workflow_task(
        WorkflowTask(
            task_id="task-2",
            status=TaskStatus.OPEN,
            upstream_task_ids=("task-1",),
            dependent_state=("state-b",),
            complexity=TaskComplexity.MEDIUM,
            input_state={"ready": False},
            description="Second task.",
        ),
        tmp_path / "task-2.json",
    )

    report = build_workflow_task_directory_validation_report(tmp_path)

    assert report.validation_successful is True
    assert report.task_ids == ["task-1", "task-2"]
    assert report.issues == []
    assert json.loads(validate_workflow_task_directory(tmp_path)) == {
        "validation_successful": True,
        "task_ids": ["task-1", "task-2"],
        "task_paths": [
            str(tmp_path / "task-1.json"),
            str(tmp_path / "task-2.json"),
        ],
        "issues": [],
    }


def test_workflow_task_directory_validation_rejects_missing_upstream_task(
    tmp_path: Path,
) -> None:
    save_workflow_task(
        WorkflowTask(
            task_id="task-2",
            status=TaskStatus.ABANDONED,
            upstream_task_ids=("missing-task",),
            dependent_state=("state-b",),
            complexity=TaskComplexity.HIGH,
            input_state={"ready": False},
            description="Second task.",
        ),
        tmp_path / "task-2.json",
    )

    report = build_workflow_task_directory_validation_report(tmp_path)

    assert report.validation_successful is False
    assert [issue.code for issue in report.issues] == ["missing_upstream_task"]
    assert "missing-task" in report.issues[0].message
    assert report.issues[0].path == f"{tmp_path / 'task-2.json'}.upstream_task_ids[0]"


def test_workflow_task_directory_validation_rejects_malformed_json(
    tmp_path: Path,
) -> None:
    (tmp_path / "task-1.json").write_text(
        "{ invalid json",
        encoding="utf-8",
    )

    report = build_workflow_task_directory_validation_report(tmp_path)

    assert report.validation_successful is False
    assert [issue.code for issue in report.issues] == ["invalid_json"]
    assert report.issues[0].path == str(tmp_path / "task-1.json")


def test_workflow_task_validation_reports_unknown_keys() -> None:
    report = build_workflow_task_validation_report(
        json.dumps(
            {
                "task_id": "task-1",
                "status": "completed",
                "upstream_task_ids": [],
                "dependent_state": ["state-a"],
                "complexity": "low",
                "input_state": {"ready": True},
                "assignee_type": "agent",
                "assignee_role": "coder",
                "output_state_type": "state",
                "description": "Task one.",
                "unexpected": "field",
            }
        )
    )

    assert report.validation_successful is False
    assert [issue.code for issue in report.issues] == ["unknown_key"]
    assert report.issues[0].path == "unexpected"


def test_workflow_task_validation_rejects_empty_prompt_catalogs() -> None:
    report = build_workflow_task_validation_report(
        json.dumps(
            {
                "task_id": "task-1",
                "status": "completed",
                "upstream_task_ids": [],
                "dependent_state": ["state-a"],
                "complexity": "low",
                "input_state": {"ready": True},
                "assignee_type": "agent",
                "assignee_role": "coder",
                "output_state_type": "state",
                "description": "Task one.",
                "prompt_catalogs": [],
            }
        )
    )

    assert report.validation_successful is False
    assert [issue.code for issue in report.issues] == ["empty_prompt_catalogs"]


def test_workflow_task_validation_rejects_invalid_status() -> None:
    report = build_workflow_task_validation_report(
        json.dumps(
            {
                "task_id": "task-1",
                "status": "in-progress",
                "upstream_task_ids": [],
                "dependent_state": ["state-a"],
                "complexity": "low",
                "input_state": {"ready": True},
                "assignee_type": "agent",
                "assignee_role": "coder",
                "output_state_type": "state",
                "description": "Task one.",
            }
        )
    )

    assert report.validation_successful is False
    assert [issue.code for issue in report.issues] == ["invalid_status"]
    assert report.issues[0].path == "status"


def test_workflow_task_validation_accepts_closed_status() -> None:
    report = build_workflow_task_validation_report(
        json.dumps(
            {
                "task_id": "task-1",
                "status": "closed",
                "upstream_task_ids": [],
                "dependent_state": ["state-a"],
                "complexity": "low",
                "input_state": {"ready": True},
                "assignee_type": "agent",
                "assignee_role": "coder",
                "output_state_type": "state",
                "description": "Task one.",
            }
        )
    )

    assert report.validation_successful is True
    assert report.issues == []


def test_workflow_task_file_helpers_round_trip(tmp_path: Path) -> None:
    task = WorkflowTask(
        task_id="task-1",
        status=TaskStatus.COMPLETED,
        upstream_task_ids=(),
        dependent_state=(),
        complexity=TaskComplexity.LOW,
        input_state={"ready": True},
        description="Task one.",
    )

    output_path = save_workflow_task(task, tmp_path / "task-1.json")
    assert output_path.exists()
    assert load_workflow_task(output_path) == task


def test_select_ready_workflow_tasks_returns_ready_open_tasks() -> None:
    task_a = WorkflowTask(
        task_id="task-a",
        status=TaskStatus.COMPLETED,
        upstream_task_ids=(),
        dependent_state=("state-a",),
        complexity=TaskComplexity.LOW,
        input_state={"ready": True},
        description="Completed upstream.",
    )
    task_b = WorkflowTask(
        task_id="task-b",
        status=TaskStatus.OPEN,
        upstream_task_ids=("task-a",),
        dependent_state=("state-b",),
        complexity=TaskComplexity.MEDIUM,
        input_state={"ready": False},
        description="Ready to run.",
    )
    task_c = WorkflowTask(
        task_id="task-c",
        status=TaskStatus.OPEN,
        upstream_task_ids=("task-b",),
        dependent_state=("state-c",),
        complexity=TaskComplexity.HIGH,
        input_state={"ready": False},
        description="Blocked by open upstream.",
    )
    task_d = WorkflowTask(
        task_id="task-d",
        status=TaskStatus.LOCKED,
        upstream_task_ids=("task-a",),
        dependent_state=("state-d",),
        complexity=TaskComplexity.LOW,
        input_state={"ready": False},
        description="Not open.",
    )

    ready_tasks = select_ready_workflow_tasks((task_a, task_b, task_c, task_d))

    assert ready_tasks == (task_b,)


def test_select_ready_workflow_tasks_excludes_missing_upstreams() -> None:
    task = WorkflowTask(
        task_id="task-a",
        status=TaskStatus.OPEN,
        upstream_task_ids=("missing-task",),
        dependent_state=("state-a",),
        complexity=TaskComplexity.LOW,
        input_state={"ready": True},
        description="Blocked by missing upstream.",
    )

    assert select_ready_workflow_tasks((task,)) == ()


def test_select_ready_workflow_tasks_filters_by_assignee_type_and_role() -> None:
    agent_task = WorkflowTask(
        task_id="agent-task",
        status=TaskStatus.OPEN,
        upstream_task_ids=(),
        dependent_state=(),
        complexity=TaskComplexity.MEDIUM,
        input_state={},
        description="Agent task.",
        assignee_type=AssigneeType.AGENT,
        assignee_role=AgentRole.ARCHITECT,
    )
    human_task = WorkflowTask(
        task_id="human-task",
        status=TaskStatus.OPEN,
        upstream_task_ids=(),
        dependent_state=(),
        complexity=TaskComplexity.MEDIUM,
        input_state={},
        description="Human task.",
        assignee_type=AssigneeType.HUMAN,
        assignee_role=HumanRole.DECIDER,
    )

    assert select_ready_workflow_tasks(
        (agent_task, human_task), assignee_type=AssigneeType.AGENT
    ) == (agent_task,)
    assert select_ready_workflow_tasks(
        (agent_task, human_task), assignee_role="decider"
    ) == (human_task,)
    assert select_ready_workflow_tasks(
        (agent_task, human_task),
        assignee_type=AssigneeType.AGENT,
        assignee_role=AgentRole.ARCHITECT,
    ) == (agent_task,)


def test_workflow_task_rejects_role_for_the_wrong_assignee_type() -> None:
    with pytest.raises(ValueError, match="assignee_role for agent"):
        WorkflowTask(
            task_id="invalid-task",
            status=TaskStatus.OPEN,
            upstream_task_ids=(),
            dependent_state=(),
            complexity=TaskComplexity.LOW,
            input_state={},
            description="Invalid assignment.",
            assignee_type=AssigneeType.AGENT,
            assignee_role=HumanRole.DECIDER,
        )


def test_load_ready_workflow_tasks_scans_all_work_items(tmp_path: Path) -> None:
    for work_item_name, task_id in (("feature-a", "task-a"), ("feature-b", "task-b")):
        work_item_directory = tmp_path / work_item_name
        save_workflow_task(
            WorkflowTask(
                task_id=task_id,
                status=TaskStatus.OPEN,
                upstream_task_ids=(),
                dependent_state=("ready",),
                complexity=TaskComplexity.LOW,
                input_state={},
                description=f"Ready task for {work_item_name}.",
            ),
            work_item_directory / f"{task_id}.json",
        )

    ready_tasks = load_ready_workflow_tasks(tmp_path)

    assert [(item.work_item_name, item.task.task_id) for item in ready_tasks] == [
        ("feature-a", "task-a"),
        ("feature-b", "task-b"),
    ]


def test_workflow_task_directory_validation_flags_open_task_with_closed_downstream(
    tmp_path: Path,
) -> None:
    save_workflow_task(
        WorkflowTask(
            task_id="task-open",
            status=TaskStatus.OPEN,
            upstream_task_ids=(),
            dependent_state=("state-open",),
            complexity=TaskComplexity.LOW,
            input_state={"ready": True},
            description="Open upstream task.",
        ),
        tmp_path / "task-open.json",
    )
    save_workflow_task(
        WorkflowTask(
            task_id="task-closed",
            status=TaskStatus.CLOSED,
            upstream_task_ids=("task-open",),
            dependent_state=("state-closed",),
            complexity=TaskComplexity.MEDIUM,
            input_state={"ready": False},
            description="Closed downstream task.",
        ),
        tmp_path / "task-closed.json",
    )

    report = build_workflow_task_directory_validation_report(tmp_path)

    assert report.validation_successful is False
    assert [issue.code for issue in report.issues].count(
        "open_task_has_closed_downstream_task"
    ) == 1
    assert any("task-open" in issue.message for issue in report.issues)
    assert any(
        issue.path == str(tmp_path / "task-open.json") for issue in report.issues
    )
