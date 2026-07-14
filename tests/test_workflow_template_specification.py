from __future__ import annotations

import json
from pathlib import Path

from powdrr_lift.core.workflow_task_specification import TaskComplexity
from powdrr_lift.core.workflow_template_specification import (
    WorkflowTaskTemplate,
    WorkflowTaskTemplateGeneration,
    WorkflowTemplate,
    build_workflow_template_validation_report,
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
            ),
        ),
    )

    output_path = save_workflow_template(template, tmp_path / "workflow.json")
    assert output_path.exists()
    assert load_workflow_template(output_path) == template


def test_specify_feature_workflow_template_file_is_checked_in() -> None:
    template_path = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "specs"
        / "specify-a-feature"
        / "workflow-template.json"
    )
    template = load_workflow_template(template_path)

    assert template.when_to_use == (
        "When a feature needs to move from an idea to implementation-ready work.",
        "When the work should gather requirements, supporting context, intent, and "
        "proposed PRs in a single workflow.",
    )
    assert template.how_to_fill_this_out == (
        "Fill in the steps in order and keep each step focused on the named output.",
        "Use the generation block to fan out follow-up tasks when one step needs "
        "to produce several related tasks.",
        "Keep dependencies explicit so later steps only rely on completed upstream "
        "templates.",
    )
    assert [task.description for task in template.task_templates] == [
        "Gather the requirements and approach.",
        "Gather the entities, invariants, and guidance.",
        "Gather the features and decisions.",
        "Gather the intent and reasoning.",
        "Specify proposed PRs.",
    ]
    assert template.task_templates[2].generation == WorkflowTaskTemplateGeneration(
        for_each="each feature or decision that needs dedicated follow-up",
        downstream_task_template_indexes=(3, 4),
    )
    assert (
        build_workflow_template_validation_report(
            template.to_json()
        ).validation_successful
        is True
    )
