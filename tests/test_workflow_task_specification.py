from __future__ import annotations

import json
from pathlib import Path

from powdrr_lift.core.workflow_task_specification import (
    TaskComplexity,
    WorkflowTask,
    WorkflowTaskDocument,
    build_workflow_task_validation_report,
    load_workflow_task_document,
    save_workflow_task_document,
    validate_workflow_task_json,
    workflow_task_document_from_json,
    workflow_task_document_to_json,
)


def test_workflow_task_document_round_trips_through_json() -> None:
    document = WorkflowTaskDocument(
        tasks=(
            WorkflowTask(
                task_id="task-1",
                upstream_task_ids=(),
                dependent_state=("state-a", "state-b"),
                complexity=TaskComplexity.MEDIUM,
                input_state={"environment": "staging"},
                description="Prepare the deployment environment.",
            ),
            WorkflowTask(
                task_id="task-2",
                upstream_task_ids=("task-1",),
                dependent_state=("state-c",),
                complexity=TaskComplexity.HIGH,
                input_state=["artifact-a", "artifact-b"],
                description="Promote the release artifacts.",
            ),
        ),
    )

    json_text = workflow_task_document_to_json(document)
    parsed = workflow_task_document_from_json(json_text)

    assert parsed == document
    assert json.loads(json_text) == {
        "tasks": [
            {
                "task_id": "task-1",
                "upstream_task_ids": [],
                "dependent_state": ["state-a", "state-b"],
                "complexity": "medium",
                "input_state": {"environment": "staging"},
                "description": "Prepare the deployment environment.",
            },
            {
                "task_id": "task-2",
                "upstream_task_ids": ["task-1"],
                "dependent_state": ["state-c"],
                "complexity": "high",
                "input_state": ["artifact-a", "artifact-b"],
                "description": "Promote the release artifacts.",
            },
        ]
    }


def test_workflow_task_document_validation_accepts_known_dependencies() -> None:
    json_text = """
    {
      "tasks": [
        {
          "task_id": "task-1",
          "upstream_task_ids": [],
          "dependent_state": ["state-a"],
          "complexity": "low",
          "input_state": {"ready": true},
          "description": "First task."
        },
        {
          "task_id": "task-2",
          "upstream_task_ids": ["task-1"],
          "dependent_state": ["state-b"],
          "complexity": "medium",
          "input_state": {"ready": false},
          "description": "Second task."
        }
      ]
    }
    """

    report = build_workflow_task_validation_report(json_text)

    assert report.validation_successful is True
    assert report.task_ids == ["task-1", "task-2"]
    assert report.issues == []
    assert json.loads(validate_workflow_task_json(json_text)) == {
        "validation_successful": True,
        "task_ids": ["task-1", "task-2"],
        "issues": [],
    }


def test_workflow_task_document_validation_rejects_missing_upstream_task() -> None:
    json_text = """
    {
      "tasks": [
        {
          "task_id": "task-2",
          "upstream_task_ids": ["missing-task"],
          "dependent_state": ["state-b"],
          "complexity": "high",
          "input_state": {"ready": false},
          "description": "Second task."
        }
      ]
    }
    """

    report = build_workflow_task_validation_report(json_text)

    assert report.validation_successful is False
    assert [issue.code for issue in report.issues] == ["missing_upstream_task"]
    assert "missing-task" in report.issues[0].message
    assert report.issues[0].path == "tasks[0].upstream_task_ids[0]"


def test_workflow_task_document_validation_rejects_malformed_tasks_array() -> None:
    report = build_workflow_task_validation_report(
        json.dumps(
            {
                "tasks": [
                    {
                        "task_id": "task-1",
                        "upstream_task_ids": [],
                        "dependent_state": ["state-a"],
                        "complexity": "low",
                        "input_state": {"ready": True},
                        "description": "Task one.",
                        "unexpected": "field",
                    }
                ]
            }
        )
    )

    assert report.validation_successful is False
    assert [issue.code for issue in report.issues] == ["unknown_key"]
    assert report.issues[0].path == "tasks[0].unexpected"


def test_workflow_task_document_file_helpers_round_trip(tmp_path: Path) -> None:
    document = WorkflowTaskDocument(
        tasks=(
            WorkflowTask(
                task_id="task-1",
                upstream_task_ids=(),
                dependent_state=(),
                complexity=TaskComplexity.LOW,
                input_state={"ready": True},
                description="Task one.",
            ),
        ),
    )

    output_path = save_workflow_task_document(document, tmp_path / "tasks.json")
    assert output_path.exists()
    assert load_workflow_task_document(output_path) == document
