from __future__ import annotations

import json
from pathlib import Path

from powdrr_lift.core.workflow_task_specification import (
    TaskComplexity,
    TaskStatus,
    WorkflowTask,
    build_workflow_task_directory_validation_report,
    build_workflow_task_validation_report,
    load_workflow_task,
    load_workflow_tasks,
    save_workflow_task,
    select_ready_workflow_tasks,
    validate_workflow_task_directory,
    workflow_task_from_json,
    workflow_task_to_json,
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
        "output_state_type": "state",
        "description": "Prepare the deployment environment.",
    }


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

    assert load_workflow_tasks(tmp_path) == (task_a, task_b)


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
                "output_state_type": "state",
                "description": "Task one.",
                "unexpected": "field",
            }
        )
    )

    assert report.validation_successful is False
    assert [issue.code for issue in report.issues] == ["unknown_key"]
    assert report.issues[0].path == "unexpected"


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
