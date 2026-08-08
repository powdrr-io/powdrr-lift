from __future__ import annotations

import json
from pathlib import Path

from powdrr_lift.core.workflow_task_specification import TaskComplexity
from powdrr_lift.core.workflow_template_specification import (
    WorkflowTaskTemplate,
    WorkflowTaskTemplateGeneration,
    WorkflowTemplate,
    build_workflow_template_validation_report,
    instantiate_workflow_template,
    load_workflow_template,
    save_workflow_template,
    validate_workflow_template_json,
    workflow_template_from_json,
    workflow_template_to_json,
)


def test_workflow_template_round_trips_through_json() -> None:
    template = WorkflowTemplate(
        when_to_use=(
            "When the work can be decomposed into ordered tasks.",
            "When a fan-out task feeds a downstream integration task.",
        ),
        how_to_fill_this_out=(
            "Describe the work as a reusable workflow pattern.",
            "Use the generation block for fan-out tasks.",
        ),
        task_templates=(
            WorkflowTaskTemplate(
                description="Generate one task per changed file.",
                complexity=TaskComplexity.MEDIUM,
                input_state={"files": []},
                upstream_task_template_indexes=(),
                dependent_state=("files-discovered",),
                generation=WorkflowTaskTemplateGeneration(
                    for_each="each changed file",
                    downstream_task_template_indexes=(1,),
                ),
            ),
            WorkflowTaskTemplate(
                description="Validate the aggregated results.",
                complexity=TaskComplexity.HIGH,
                input_state={"ready": True},
                upstream_task_template_indexes=(0,),
                dependent_state=("validation-ready",),
            ),
        ),
    )

    json_text = workflow_template_to_json(template)
    parsed = workflow_template_from_json(json_text)

    assert parsed == template
    assert json.loads(json_text) == {
        "when_to_use": [
            "When the work can be decomposed into ordered tasks.",
            "When a fan-out task feeds a downstream integration task.",
        ],
        "how_to_fill_this_out": [
            "Describe the work as a reusable workflow pattern.",
            "Use the generation block for fan-out tasks.",
        ],
        "task_templates": [
            {
                "description": "Generate one task per changed file.",
                "complexity": "medium",
                "input_state": {"files": []},
                "assignee_type": "agent",
                "assignee_role": "coder",
                "output_state_type": "state",
                "upstream_task_template_indexes": [],
                "dependent_state": ["files-discovered"],
                "generation": {
                    "for_each": "each changed file",
                    "downstream_task_template_indexes": [1],
                },
            },
            {
                "description": "Validate the aggregated results.",
                "complexity": "high",
                "input_state": {"ready": True},
                "assignee_type": "agent",
                "assignee_role": "coder",
                "output_state_type": "state",
                "upstream_task_template_indexes": [0],
                "dependent_state": ["validation-ready"],
            },
        ],
    }


def test_workflow_template_validation_accepts_generation_and_dependencies() -> None:
    json_text = json.dumps(
        {
            "when_to_use": ["When a workflow has a fan-out phase."],
            "how_to_fill_this_out": ["Fill the fan-out task first."],
            "task_templates": [
                {
                    "description": "Generate one task per item.",
                    "complexity": "low",
                    "input_state": {"items": []},
                    "assignee_type": "agent",
                    "assignee_role": "coder",
                    "output_state_type": "state",
                    "upstream_task_template_indexes": [],
                    "dependent_state": ["items-ready"],
                    "generation": {
                        "for_each": "each item",
                        "downstream_task_template_indexes": [1],
                    },
                },
                {
                    "description": "Aggregate generated results.",
                    "complexity": "high",
                    "input_state": {"ready": True},
                    "assignee_type": "agent",
                    "assignee_role": "coder",
                    "output_state_type": "state",
                    "upstream_task_template_indexes": [0],
                    "dependent_state": ["aggregation-ready"],
                },
            ],
        }
    )

    report = build_workflow_template_validation_report(json_text)

    assert report.validation_successful is True
    assert report.task_template_count == 2
    assert report.issues == []
    assert json.loads(validate_workflow_template_json(json_text)) == {
        "validation_successful": True,
        "task_template_count": 2,
        "issues": [],
    }


def test_workflow_template_validation_rejects_unknown_generation_target() -> None:
    json_text = json.dumps(
        {
            "when_to_use": ["When a workflow has a fan-out phase."],
            "how_to_fill_this_out": ["Fill the fan-out task first."],
            "task_templates": [
                {
                    "description": "Generate one task per item.",
                    "complexity": "low",
                    "input_state": {"items": []},
                    "assignee_type": "agent",
                    "assignee_role": "coder",
                    "output_state_type": "state",
                    "upstream_task_template_indexes": [],
                    "dependent_state": ["items-ready"],
                    "generation": {
                        "for_each": "each item",
                        "downstream_task_template_indexes": [2],
                    },
                },
                {
                    "description": "Aggregate generated results.",
                    "complexity": "high",
                    "input_state": {"ready": True},
                    "assignee_type": "agent",
                    "assignee_role": "coder",
                    "output_state_type": "state",
                    "upstream_task_template_indexes": [0],
                    "dependent_state": ["aggregation-ready"],
                },
            ],
        }
    )

    report = build_workflow_template_validation_report(json_text)

    assert report.validation_successful is False
    assert [issue.code for issue in report.issues] == [
        "missing_downstream_task_template"
    ]
    assert report.issues[0].path == "task_templates[0]"


def test_workflow_template_file_helpers_round_trip(tmp_path: Path) -> None:
    template = WorkflowTemplate(
        when_to_use=("When the workflow is simple.",),
        how_to_fill_this_out=("Describe the workflow steps.",),
        task_templates=(
            WorkflowTaskTemplate(
                description="Do the work.",
                complexity=TaskComplexity.LOW,
                input_state={"ready": True},
                output_state_type="state",
            ),
        ),
    )

    output_path = save_workflow_template(template, tmp_path / "workflow.json")
    assert output_path.exists()
    assert load_workflow_template(output_path) == template


def test_implement_feature_workflow_template_file_is_checked_in() -> None:
    template_path = (
        Path(__file__).resolve().parents[1] / "templates" / "implement-a-feature.yaml"
    )
    template = load_workflow_template(template_path)

    assert template.when_to_use == (
        "When a feature has plan documents and is ready to become implementation work.",
        "When the work should confirm requirements before splitting the plan into "
        "proposed PRs.",
    )
    assert template.how_to_fill_this_out == (
        "Review the existing plan documents before making implementation decisions.",
        "Keep the confirmed requirements explicit in the task state.",
        "Generate one execute-proposed-pr workflow for each approved proposed PR.",
    )
    assert [task.description for task in template.task_templates] == [
        "Review plan documents",
        "Confirm requirements",
        "Generate proposed PRs",
    ]
    assert [task.output_state_type for task in template.task_templates] == [
        "reviewed-plan-documents-state",
        "confirmed-requirements-state",
        "proposed-prs-and-execution-workflows-state",
    ]
    assert template.task_templates[0].input_state == {
        "plan_documents": [
            "docs/specs/<feature-name>/system-specification.yaml",
            "docs/specs/<feature-name>/architecture-specification.yaml",
            "docs/specs/<feature-name>/implementation-specification.yaml",
            "docs/specs/<feature-name>/proposed-pr-specification.yaml",
        ]
    }
    assert [
        (task.assignee_type.value, task.assignee_role.value)
        for task in template.task_templates
    ] == [
        ("agent", "architect"),
        ("human", "decider"),
        ("agent", "architect"),
    ]
    assert [task.llm_type for task in template.task_templates] == [
        "high_reasoning",
        "standard_reasoning",
        "high_reasoning",
    ]
    assert all(task.details for task in template.task_templates)
    assert "workflow-instance context" in (template.task_templates[2].details or "")
    assert "actual proposed PR" in (template.task_templates[2].details or "")
    assert template.task_templates[2].tool_invocations[0].command == (
        "powdrr-lift",
        "instantiate-workflow",
        "--work-item-name",
        "<work-item-name>",
        "--workflow-instance-name",
        "<workflow-instance-name>",
        "--template",
        "templates/execute-proposed-pr.yaml",
    )
    assert (
        build_workflow_template_validation_report(
            template.to_json()
        ).validation_successful
        is True
    )


def test_execute_proposed_pr_workflow_template_file_is_checked_in() -> None:
    template_path = (
        Path(__file__).resolve().parents[1] / "templates" / "execute-proposed-pr.yaml"
    )
    template = load_workflow_template(template_path)

    assert [task.description for task in template.task_templates] == [
        "Review proposed PR plan",
        "Review overall plan",
        "Confirm requirements",
        "Generate test diffs",
        "Generate functionality diffs",
        "Confirm completeness",
        "Confirm goals",
        "Lint and cleanup",
        "Create PR",
    ]
    proposed_pr_input = template.task_templates[0].input_state["proposed_pr"]
    assert proposed_pr_input["workflow_context"] == {}
    assert (
        "Find the actual proposed PR specification" in proposed_pr_input["instructions"]
    )
    assert "actual proposed PR" in " ".join(template.how_to_fill_this_out)
    assert "specification path" in (template.task_templates[0].details or "")
    assert [
        (task.assignee_type.value, task.assignee_role.value)
        for task in template.task_templates
    ] == [
        ("agent", "architect"),
        ("agent", "architect"),
        ("human", "decider"),
        ("agent", "coder"),
        ("agent", "coder"),
        ("human", "reviewer"),
        ("human", "decider"),
        ("agent", "reviewer"),
        ("human", "reviewer"),
    ]
    assert build_workflow_template_validation_report(
        template.to_json()
    ).validation_successful


def test_instantiate_workflow_template_creates_first_ready_task(tmp_path: Path) -> None:
    template_path = (
        Path(__file__).resolve().parents[1] / "templates" / "implement-a-feature.yaml"
    )

    output_directory, tasks = instantiate_workflow_template(
        template_path=template_path,
        work_item_name="Example Feature",
        output_root=tmp_path / "workflows",
    )

    assert output_directory == tmp_path / "workflows" / "example-feature"
    assert len(tasks) == 3
    assert tasks[0].task_id == "task-001"
    assert tasks[1].upstream_task_ids == ("task-001",)
    assert all(task.status.value == "open" for task in tasks)


def test_instantiate_workflow_template_namespaces_instances_in_shared_directory(
    tmp_path: Path,
) -> None:
    template_path = (
        Path(__file__).resolve().parents[1] / "templates" / "implement-a-feature.yaml"
    )
    output_root = tmp_path / "workflows"

    first_directory, first_tasks = instantiate_workflow_template(
        template_path=template_path,
        work_item_name="Example Feature",
        workflow_instance_name="first-pr",
        output_root=output_root,
    )
    second_directory, second_tasks = instantiate_workflow_template(
        template_path=template_path,
        work_item_name="Example Feature",
        workflow_instance_name="second-pr",
        output_root=output_root,
    )

    assert first_directory == second_directory == output_root / "example-feature"
    assert first_tasks[0].task_id == "first-pr-task-001"
    assert second_tasks[0].task_id == "second-pr-task-001"
    assert (first_directory / "first-pr-task-001.json").is_file()
    assert (second_directory / "second-pr-task-001.json").is_file()


def test_instantiate_execute_proposed_pr_fills_proposed_pr_context(
    tmp_path: Path,
) -> None:
    template_path = (
        Path(__file__).resolve().parents[1] / "templates" / "execute-proposed-pr.yaml"
    )

    _, tasks = instantiate_workflow_template(
        template_path=template_path,
        work_item_name="interaction-file-log",
        workflow_instance_name="interaction-file-log-pr",
        output_root=tmp_path / "workflows",
    )

    proposed_pr_input = tasks[0].input_state["proposed_pr"]
    assert (
        "Find the actual proposed PR specification" in proposed_pr_input["instructions"]
    )
    assert proposed_pr_input["workflow_context"] == {
        "work_item_name": "interaction-file-log",
        "workflow_instance_name": "interaction-file-log-pr",
    }
