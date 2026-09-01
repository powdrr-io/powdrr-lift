from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from powdrr_lift.core import WorkflowInstance
from powdrr_lift.core.delivery_profile import PhaseType
from powdrr_lift.core.skill_specification import SkillToolInvocation
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
                interaction_style="engineering",
                dependent_state=("files-discovered",),
                generation=WorkflowTaskTemplateGeneration(
                    for_each="each changed file",
                    downstream_task_template_indexes=(1,),
                ),
            ),
            WorkflowTaskTemplate(
                description="Validate the aggregated results.",
                complexity=TaskComplexity.HIGH,
                input_state={"ready": "<upstream-task-0>.state"},
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
                "step_type": "freeform",
                "interaction_style": "engineering",
                "complexity": "medium",
                "input_state": {"files": []},
                "assignee_type": "agent",
                "assignee_role": "coder",
                "output_state_type": "state",
                "dependent_state": ["files-discovered"],
                "generation": {
                    "for_each": "each changed file",
                    "downstream_task_template_indexes": [1],
                },
            },
            {
                "description": "Validate the aggregated results.",
                "step_type": "freeform",
                "complexity": "high",
                "input_state": {"ready": "<upstream-task-0>.state"},
                "assignee_type": "agent",
                "assignee_role": "coder",
                "output_state_type": "state",
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
                    "dependent_state": ["items-ready"],
                    "generation": {
                        "for_each": "each item",
                        "downstream_task_template_indexes": [1],
                    },
                },
                {
                    "description": "Aggregate generated results.",
                    "complexity": "high",
                    "input_state": {"ready": "<upstream-task-0>.state"},
                    "assignee_type": "agent",
                    "assignee_role": "coder",
                    "output_state_type": "state",
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


def test_workflow_template_validation_rejects_skill_and_pre_step_together() -> None:
    report = build_workflow_template_validation_report(
        json.dumps(
            {
                "when_to_use": ["When a workflow invokes a nested skill."],
                "how_to_fill_this_out": ["Declare the nested skill."],
                "task_templates": [
                    {
                        "description": "Run the nested validation skill.",
                        "step_type": "freeform",
                        "uses_skills": ["some-skill"],
                        "pre_step": {
                            "action": "invoke_tool",
                            "template": {"tool": "shell", "command": ["true"]},
                        },
                    }
                ],
            }
        )
    )

    assert report.validation_successful is False
    issue_codes = [issue.code for issue in report.issues]
    assert "uses_skills_with_pre_step" in issue_codes
    assert "unexpected_pre_step" in issue_codes


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
                    "dependent_state": ["items-ready"],
                    "generation": {
                        "for_each": "each item",
                        "downstream_task_template_indexes": [2],
                    },
                },
                {
                    "description": "Aggregate generated results.",
                    "complexity": "high",
                    "input_state": {"ready": "<upstream-task-0>.state"},
                    "assignee_type": "agent",
                    "assignee_role": "coder",
                    "output_state_type": "state",
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


def test_workflow_template_validation_rejects_undeclared_detail_placeholders() -> None:
    json_text = json.dumps(
        {
            "when_to_use": ["When a workflow needs an input."],
            "how_to_fill_this_out": ["Declare every detail input."],
            "task_templates": [
                {
                    "description": "Use the input.",
                    "complexity": "low",
                    "input_state": {"feature_id": "<feature-id>"},
                    "details": "Use <feature_id> and <missing_input>.",
                    "assignee_type": "agent",
                    "assignee_role": "coder",
                    "output_state_type": "state",
                    "dependent_state": [],
                }
            ],
        }
    )

    report = build_workflow_template_validation_report(json_text)

    assert report.validation_successful is False
    assert [issue.code for issue in report.issues] == ["undeclared_detail_input"]
    assert report.issues[0].path == "task_templates[0].details"
    assert "missing_input" in report.issues[0].message


def test_workflow_template_validation_rejects_empty_prompt_catalogs() -> None:
    report = build_workflow_template_validation_report(
        json.dumps(
            {
                "when_to_use": ["When a workflow needs review."],
                "how_to_fill_this_out": ["Describe each task."],
                "task_templates": [
                    {
                        "description": "Review the change.",
                        "complexity": "low",
                        "input_state": {},
                        "assignee_type": "agent",
                        "assignee_role": "reviewer",
                        "output_state_type": "state",
                        "dependent_state": [],
                        "prompt_catalogs": [],
                    }
                ],
            }
        )
    )

    assert report.validation_successful is False
    assert [issue.code for issue in report.issues] == ["empty_prompt_catalogs"]


def test_workflow_template_validation_accepts_declared_detail_placeholders() -> None:
    json_text = json.dumps(
        {
            "when_to_use": ["When a workflow has dependent tasks."],
            "how_to_fill_this_out": ["Declare detail inputs."],
            "task_templates": [
                {
                    "description": "Create the input.",
                    "complexity": "low",
                    "input_state": {"feature_id": "<feature-id>"},
                    "details": "Use <feature_id>.",
                    "assignee_type": "agent",
                    "assignee_role": "coder",
                    "output_state_type": "state",
                    "dependent_state": [],
                },
                {
                    "description": "Use the upstream result.",
                    "complexity": "low",
                    "input_state": {"result": "<upstream-task-0>.state"},
                    "details": "Use <upstream-task-0>.",
                    "assignee_type": "agent",
                    "assignee_role": "coder",
                    "output_state_type": "state",
                    "dependent_state": [],
                },
            ],
        }
    )

    report = build_workflow_template_validation_report(json_text)

    assert report.validation_successful is True
    assert report.issues == []


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


def test_execute_proposed_pr_workflow_template_file_is_checked_in() -> None:
    template_path = (
        Path(__file__).resolve().parents[1] / "templates" / "execute-proposed-pr.yaml"
    )
    template = load_workflow_template(template_path)

    assert [task.description for task in template.task_templates] == [
        "Gather context about the proposed PR",
        "Create the execution plan",
        "Generate the planned tests",
        "Implement the planned product code",
        "Run all tests and fix failures",
        "Review specification completeness",
        "Repair specification completeness gaps",
        "Run formatting checks",
        "Run lint checks",
        "Run type checks",
        "Review the final diff for scope",
        "Repair final diff scope issues",
        "Promote the feature documents",
        "Verify the pull request file set",
        "Finish pull request preparation",
    ]
    proposed_pr_input = template.task_templates[0].input_state["proposed_pr"]
    assert proposed_pr_input == "<proposed-pr-id>"
    assert template.task_templates[0].input_state["feature_id"] == "<work-item-name>"
    assert [
        cast(PhaseType, task.phase_type).value for task in template.task_templates
    ] == [
        "intake",
        "plan_pr",
        "build",
        "build",
        "validate",
        "review_specifications",
        "specify",
        "validate",
        "validate",
        "validate",
        "review_pr",
        "resolve_findings",
        "confirm_readiness",
        "confirm_readiness",
        "publish_pr",
    ]
    assert [task.persona_id for task in template.task_templates] == [
        "architect",
        "engineer",
        "engineer",
        "engineer",
        "engineer",
        "specification-reviewer",
        "architect",
        "engineer",
        "engineer",
        "engineer",
        "code-reviewer",
        "engineer",
        "code-reviewer",
        "code-reviewer",
        "engineering-manager",
    ]
    assert template.task_templates[0].llm_type == "long_context"
    assert "one responsibility" in " ".join(template.how_to_fill_this_out)
    assert template.task_templates[0].step_type == "invoke_tool"
    assert template.task_templates[0].pre_step is not None
    assert template.task_templates[0].pre_step.action == "gather_context"
    assert "Assign that exact result to proposed-pr-context-state" in (
        template.task_templates[0].details or ""
    )
    assert "<work-item-name>" not in (template.task_templates[0].details or "")
    assert "<proposed-pr-id>" not in (template.task_templates[0].details or "")
    report = build_workflow_template_validation_report(template.to_json())
    assert report.validation_successful is True
    assert report.issues == []
    assert [
        (task.assignee_type.value, task.assignee_role.value)
        for task in template.task_templates
    ] == [
        ("agent", "architect"),
        ("agent", "architect"),
        ("agent", "coder"),
        ("agent", "coder"),
        ("agent", "reviewer"),
        ("agent", "architect"),
        ("agent", "coder"),
        ("agent", "reviewer"),
        ("agent", "reviewer"),
        ("agent", "reviewer"),
        ("agent", "reviewer"),
        ("agent", "coder"),
        ("agent", "reviewer"),
        ("agent", "reviewer"),
        ("agent", "reviewer"),
    ]
    assert template.task_templates[0].tool_invocations == ()
    invoke_tool_indexes = (0, 7, 8, 9, 12, 13)
    assert [
        index
        for index, task in enumerate(template.task_templates)
        if task.step_type == "invoke_tool"
    ] == list(invoke_tool_indexes)
    freeform_details = "\n".join(
        task.details or ""
        for task in template.task_templates
        if task.step_type == "freeform"
    )
    assert freeform_details.count("Perform exactly one action") >= 7
    assert '"action":"edit"' in freeform_details
    assert '"action":"file_management"' in freeform_details
    assert '"action":"next_step"' in freeform_details
    for task in template.task_templates[1:]:
        assert "upstream_task_outputs" not in (task.details or "")
        assert "runtime task ID" not in (task.details or "")
    assert template.task_templates[1].input_state["proposed_pr_context"] == (
        "<upstream-task-0>.proposed-pr-context-state"
    )
    assert template.task_templates[4].uses_skills == ("run-tests-and-fix",)
    assert template.task_templates[4].input_state["implementation_diffs"] == (
        "<upstream-task-3>.generated-product-code-state"
    )
    assert template.task_templates[5].description == (
        "Review specification completeness"
    )
    assert template.task_templates[11].input_state["scope_review"] == (
        "<upstream-task-10>.final-scope-review-state"
    )
    assert template.task_templates[14].uses_skills == ("finish-pr-prep",)
    assert template.task_templates[14].input_state["staged_changes"] == (
        "<upstream-task-13>.staged-pull-request-state"
    )
    plan_details = template.task_templates[1].details or ""
    assert '"action":"next_step"' in plan_details
    assert '"action":"complete"' not in plan_details
    assert '"detailed-execution-plan-state"' in plan_details
    assert build_workflow_template_validation_report(
        template.to_json()
    ).validation_successful


def test_instantiate_workflow_template_creates_first_ready_task(tmp_path: Path) -> None:
    template_path = (
        Path(__file__).resolve().parents[1] / "templates" / "execute-proposed-pr.yaml"
    )

    output_directory, tasks = instantiate_workflow_template(
        template_path=template_path,
        work_item_name="Example Feature",
        output_root=tmp_path / "workflows",
    )

    assert output_directory == tmp_path / "workflows" / "example-feature"
    assert len(tasks) == 15
    assert tasks[0].task_id == "task-001"
    assert tasks[1].upstream_task_ids == ("task-001",)
    assert all(task.status.value == "open" for task in tasks)
    assert all(task.workflow_template == template_path.stem for task in tasks)
    assert tasks[0].actions == ("next_step",)
    assert tasks[0].actions_declared is True
    assert tasks[1].actions == (
        "edit",
        "yaml_edit",
        "file_management",
        "read_document",
        "prompt_user",
        "next_step",
    )


def test_checked_in_execute_workflows_do_not_terminate_on_intermediate_tasks() -> None:
    workflow_root = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "workflows"
        / "interaction-file-log"
    )

    for task in WorkflowInstance.from_directory(workflow_root).tasks:
        if task.output_state_type == "pull-request-prep-state":
            continue
        assert '"action":"complete"' not in (task.details or ""), task.task_id


def test_instantiate_execute_proposed_pr_workflow_provides_resolution_context(
    tmp_path: Path,
) -> None:
    template_path = (
        Path(__file__).resolve().parents[1] / "templates" / "execute-proposed-pr.yaml"
    )

    _, tasks = instantiate_workflow_template(
        template_path=template_path,
        work_item_name="Interaction File Log",
        workflow_instance_name="interaction-file-log-pr-001",
        template_values={
            "proposed-pr-id": "interaction-file-log-pr-001",
            "feature-id": "feature-interaction-capture",
        },
        output_root=tmp_path / "workflows",
    )

    assert tasks[0].input_state["proposed_pr"] == "interaction-file-log-pr-001"
    assert tasks[0].input_state["feature_id"] == "Interaction File Log"
    assert tasks[1].input_state["proposed_pr_context"] == (
        "interaction-file-log-pr-001-task-001.proposed-pr-context-state"
    )
    assert tasks[4].input_state["implementation_diffs"] == (
        "interaction-file-log-pr-001-task-004.generated-product-code-state"
    )
    assert tasks[14].input_state["staged_changes"] == (
        "interaction-file-log-pr-001-task-014.staged-pull-request-state"
    )
    assert "interaction-file-log-pr-001" in (tasks[0].details or "")
    assert "Interaction File Log" in (tasks[0].details or "")


def test_instantiate_workflow_template_accepts_generic_input_values(
    tmp_path: Path,
) -> None:
    template = WorkflowTemplate(
        when_to_use=("When a custom input is required.",),
        how_to_fill_this_out=("Provide the custom value.",),
        task_templates=(
            WorkflowTaskTemplate(
                description="Use the custom input.",
                complexity=TaskComplexity.LOW,
                input_state={"custom": "<custom-value>"},
                tool_invocations=(
                    SkillToolInvocation(
                        tool="shell",
                        command=("echo", "<custom-value>"),
                        cwd="<custom-value>",
                        env=(("VALUE", "<custom-value>"),),
                    ),
                ),
            ),
        ),
    )
    template_path = save_workflow_template(template, tmp_path / "template.yaml")

    _, tasks = instantiate_workflow_template(
        template_path=template_path,
        work_item_name="Example",
        output_root=tmp_path / "workflows",
        template_values={"custom-value": "provided-value"},
    )

    assert tasks[0].input_state == {"custom": "provided-value"}
    assert tasks[0].tool_invocations[0].command == ("echo", "provided-value")
    assert tasks[0].tool_invocations[0].cwd == "provided-value"
    assert tasks[0].tool_invocations[0].env == (("VALUE", "provided-value"),)


def test_instantiate_workflow_template_namespaces_instances_in_shared_directory(
    tmp_path: Path,
) -> None:
    template_path = (
        Path(__file__).resolve().parents[1] / "templates" / "execute-proposed-pr.yaml"
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
    assert (first_directory / "first-pr-task-001.yaml").is_file()
    assert (second_directory / "second-pr-task-001.yaml").is_file()


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
        template_values={"proposed-pr-id": "interaction-file-log-pr"},
        output_root=tmp_path / "workflows",
    )

    proposed_pr_input = tasks[0].input_state["proposed_pr"]
    assert proposed_pr_input == "interaction-file-log-pr"
    assert "Instantiation context:" in (tasks[0].details or "")
    assert "interaction-file-log" in (tasks[0].details or "")
    assert "interaction-file-log-pr" in (tasks[0].details or "")


def test_fully_review_pr_template_runs_reviews_sequentially(
    tmp_path: Path,
) -> None:
    template_path = (
        Path(__file__).resolve().parents[1] / "templates" / "fully-review-pr.yaml"
    )

    template = load_workflow_template(template_path)
    assert [task.uses_skills for task in template.task_templates] == [
        ("feature-functionality-review",),
        ("feature-test-coverage-review",),
        ("security-review",),
    ]
    assert [task.llm_type for task in template.task_templates] == [
        "fast_iteration",
        "fast_iteration",
        "fast_iteration",
    ]
    assert [task.input_state["pull_request"] for task in template.task_templates] == [
        "<pull-request-id>",
        "<pull-request-id>",
        "<pull-request-id>",
    ]

    _, tasks = instantiate_workflow_template(
        template_path=template_path,
        work_item_name="Full Review",
        workflow_instance_name="full-review-pr-42",
        template_values={"pull-request-id": "42"},
        output_root=tmp_path / "workflows",
    )

    assert [task.upstream_task_ids for task in tasks] == [
        (),
        ("full-review-pr-42-task-001",),
        ("full-review-pr-42-task-001", "full-review-pr-42-task-002"),
    ]
    assert all(task.input_state["pull_request"] == "42" for task in tasks)
    assert tasks[1].input_state["functionality_review"] == (
        "full-review-pr-42-task-001.functionality-review-state"
    )
    assert tasks[2].input_state["test_coverage_review"] == (
        "full-review-pr-42-task-002.test-coverage-review-state"
    )
