from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from powdrr_lift.core import (
    AgentRole,
    AssigneeType,
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
