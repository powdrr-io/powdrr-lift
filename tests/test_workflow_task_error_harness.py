from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from powdrr_lift.core import (
    AgentRole,
    AssigneeType,
    HumanRole,
    TaskComplexity,
    TaskStatus,
    WorkflowInstance,
    WorkflowTask,
    WorkflowTaskTemplate,
    WorkflowTemplate,
    save_workflow_template,
)


def _harness_module() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "workflow-task-error-harness.py"
    specification = importlib.util.spec_from_file_location(
        "workflow_task_harness", path
    )
    assert specification is not None
    module = importlib.util.module_from_spec(specification)
    assert specification.loader is not None
    specification.loader.exec_module(module)
    return module


def _task(status: TaskStatus = TaskStatus.OPEN) -> WorkflowTask:
    return WorkflowTask(
        task_id="task-001",
        status=status,
        description="Do the task",
        complexity=TaskComplexity.MEDIUM,
        input_state={"feature_id": "demo"},
        assignee_type=AssigneeType.AGENT,
        assignee_role=AgentRole.CODER,
    )


def test_reopen_task_preserves_repairable_task_content(tmp_path: Path) -> None:
    module = _harness_module()
    workflow = WorkflowInstance.create(tmp_path / "workflow", (_task(),))
    claimed = workflow.claim_task("task-001")

    module._reopen_task(workflow.directory, claimed.task_id)

    reopened = WorkflowInstance.from_directory(workflow.directory).tasks[0]
    assert reopened.status is TaskStatus.OPEN
    assert reopened.description == claimed.description
    assert reopened.input_state == claimed.input_state


def test_infer_template_matches_instantiated_task_descriptions(tmp_path: Path) -> None:
    module = _harness_module()
    workflow_dir = tmp_path / "docs" / "workflows" / "demo"
    WorkflowInstance.create(workflow_dir, (_task(),))
    template_path = tmp_path / "templates" / "demo.yaml"
    save_workflow_template(
        WorkflowTemplate(
            when_to_use=("Use this workflow.",),
            how_to_fill_this_out=("Complete the task.",),
            task_templates=(
                WorkflowTaskTemplate(
                    description="Do the task",
                    complexity=TaskComplexity.MEDIUM,
                    input_state={"feature_id": "<work-item-name>"},
                ),
            ),
        ),
        template_path,
    )

    assert module._infer_template(tmp_path, workflow_dir) == template_path


def test_workflow_state_reports_human_handoff_until_all_tasks_complete(
    tmp_path: Path,
) -> None:
    module = _harness_module()
    workflow = WorkflowInstance.create(tmp_path / "workflow", (_task(),))
    workflow.complete_task("task-001", {"result": "ready"})
    workflow.add_task(
        WorkflowTask(
            task_id="human-review",
            status=TaskStatus.OPEN,
            description="Review the result.",
            complexity=TaskComplexity.LOW,
            input_state={"result": "ready"},
            assignee_type=AssigneeType.HUMAN,
            assignee_role=HumanRole.REVIEWER,
            upstream_task_ids=("task-001",),
        )
    )

    state = module._workflow_state(workflow.directory)

    assert state["outcome"] == "human_handoff"
    assert state["ready_human_task_ids"] == ["human-review"]
    assert state["ready_agent_task_ids"] == []

    workflow.complete_task("human-review", {"answer": "approved"})
    assert module._workflow_state(workflow.directory)["outcome"] == "completed"


def test_workflow_state_and_fingerprint_detect_locked_agent_without_hanging(
    tmp_path: Path,
) -> None:
    module = _harness_module()
    workflow = WorkflowInstance.create(tmp_path / "workflow", (_task(),))
    workflow.claim_task("task-001")

    state = module._workflow_state(workflow.directory)

    assert state["outcome"] == "agent_task_locked"
    assert state["locked_task_ids"] == ["task-001"]
    first_fingerprint = module._state_fingerprint(state)
    assert module._state_fingerprint(module._workflow_state(workflow.directory)) == (
        first_fingerprint
    )

    module._reopen_locked_tasks(
        tmp_path,
        workflow.directory,
        state["locked_task_ids"],
    )
    reopened = module._workflow_state(workflow.directory)
    assert reopened["outcome"] == "agent_work_remaining"
    assert module._state_fingerprint(reopened) != first_fingerprint


def test_full_workflow_run_does_not_select_a_single_task() -> None:
    module = _harness_module()
    args = SimpleNamespace(provider="auto", max_roundtrips=None, verbose=False)

    command = module._build_task_command(
        repo_root=Path("."),
        workflow_dir=Path("workflow"),
        task=None,
        args=args,
    )

    assert "--task-id" not in command


def test_repair_command_timeout_kills_the_process_group(tmp_path: Path) -> None:
    module = _harness_module()

    return_code = module._run_repair_command(
        f'{sys.executable} -c "import time; time.sleep(1)"',
        repo_root=tmp_path,
        workflow_dir=tmp_path / "workflow",
        task=None,
        template_path=tmp_path / "template.yaml",
        error_log=tmp_path / "errors.jsonl",
        result={"transcript": str(tmp_path / "transcript.log")},
        iteration=1,
        timeout=0.01,
    )

    assert return_code == 124
