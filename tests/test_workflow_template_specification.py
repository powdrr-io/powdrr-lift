from __future__ import annotations

import json
from pathlib import Path

from powdrr_lift.process.task_step import ToolInvocation
from powdrr_lift.process.tasks import TaskComplexity, WorkflowInstance
from powdrr_lift.process.templates import (
    WorkflowTaskTemplate,
    WorkflowTaskTemplateGeneration,
    WorkflowTemplate,
    build_workflow_template_validation_report,
    instantiate_procedrr_process_workflow,
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
                "step_type": "governed",
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
                "step_type": "governed",
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
                "when_to_use": ["When a workflow invokes a nested process."],
                "how_to_fill_this_out": ["Declare the nested process."],
                "task_templates": [
                    {
                        "description": "Run the nested validation process.",
                        "step_type": "governed",
                        "uses_processes": ["some-process"],
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
    assert "uses_processes_with_pre_step" in issue_codes
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


def test_execute_proposed_pr_is_a_procedrr_workflow_process() -> None:
    from procedrr import ProcessCatalog

    catalog = ProcessCatalog.load(
        Path(__file__).resolve().parents[1] / "docs/procedrr/processes"
    )
    process = catalog.get("execute-proposed-pr-workflow").document
    steps = process["steps"]
    assert "tool_calls" not in process["limits"]
    assert "llm_activations" not in process["limits"]
    assert steps[0]["call"]["process"] == "execute-proposed-pr"
    branch = steps[1]["branch"]
    assert branch["subject"] == "implementation_result.passed"
    assert branch["cases"][True][0]["call"]["process"] == "finish-implementation-pr"
    failure_steps = branch["default"]
    execute_process = catalog.get("execute-proposed-pr").document
    execute_output = execute_process["outputs"]["coding_attempt"]
    assert "required" not in execute_output
    assert execute_output["properties"]["error"] == {"type": ["string", "null"]}
    assert execute_output["properties"]["retryable"] == {"type": "boolean"}
    assert failure_steps[0]["judge"]["output"]["schema"]["properties"][
        "classification"
    ]["enum"] == ["plan_defect", "implementation_defect", "unclear"]
    assert (
        failure_steps[1]["branch"]["cases"]["plan_defect"][0]["call"]["process"]
        == "amend-plan-and-open-pr"
    )
    process_calls = (
        "execute-proposed-pr",
        "finish-implementation-pr",
        "amend-plan-and-open-pr",
        "finish-pr-prep",
        "create-pull-request",
    )
    for process_name in process_calls:
        limits = catalog.get(process_name).document.get("limits", {})
        assert "tool_calls" not in limits
        assert "llm_activations" not in limits
    assert steps[0]["call"]["outputs"] == {"implementation_result": "coding_attempt"}
    implementation_process = catalog.get("finish-implementation-pr").document
    implementation_steps = implementation_process["steps"]
    assert (
        implementation_steps[0]["gate"]["subject"]
        == "implementation_result.structrr.passed"
    )
    assert (
        implementation_steps[1]["gate"]["subject"]
        == "implementation_result.integration_tests.passed"
    )
    assert implementation_steps[2]["judge"]["output"]["name"] == "publication_scope"
    assert (
        "files_to_publish"
        not in implementation_steps[2]["judge"]["output"]["schema"]["required"]
    )
    assert implementation_steps[3]["call"]["inputs"]["files_to_publish"] == {
        "type": "reference",
        "value": "implementation_result.changed_paths",
    }
    assert implementation_steps[3]["call"]["outputs"] == {
        "validation_results": "validation_results"
    }
    assert implementation_steps[4]["judge"]["output"]["name"] == "pull_request_request"
    title_judge = implementation_steps[4]["judge"]
    assert any(
        "task_context.proposed_pr_context.title" in instruction
        and "Prefer the reviewed item title verbatim" in instruction
        for instruction in title_judge["instructions"]
    )
    assert any(
        "Never use the plan ID" in instruction
        for instruction in title_judge["instructions"]
    )
    assert (
        title_judge["output"]["schema"]["properties"]["workflow_context"]["properties"][
            "feature-name"
        ]["maxLength"]
        == 100
    )
    assert implementation_steps[5]["call"]["inputs"]["files_to_publish"] == {
        "type": "reference",
        "value": "implementation_result.changed_paths",
    }
    assert implementation_steps[5]["call"]["outputs"] == {
        "pull_request_url": "pull_request_url"
    }
    assert process["outputs"]["pull_request_url"]["pattern"].endswith("/pull/[0-9]+$")
    amendment_process = catalog.get("amend-plan-and-open-pr").document
    amendment_call = amendment_process["steps"][0]["operation"]["parameters"]
    assert amendment_call["parameters"]["mode"] == "plan-amendment"
    assert amendment_process["steps"][1]["gate"]["subject"] == "amendment_result.passed"


def test_create_pull_request_preserves_runtime_state_and_accepts_committed_files() -> (
    None
):
    from procedrr import ProcessCatalog

    process = (
        ProcessCatalog.load(
            Path(__file__).resolve().parents[1] / "docs/procedrr/processes"
        )
        .get("create-pull-request")
        .document
    )
    operation = process["steps"][0]["operation"]
    parameters = operation["parameters"]["parameters"]
    assert parameters["operation"] == "pr_publish_clean"
    assert parameters["files_to_publish"]["value"] == "files_to_publish"
    assert (
        parameters["publication_branch"]["value"]
        == "workflow_context.publication-branch"
    )
    assert process["outputs"]["pull_request_url"]["pattern"].endswith("/pull/[0-9]+$")


def test_instantiate_procedrr_process_workflow_creates_task(tmp_path: Path) -> None:
    process_directory = Path(__file__).resolve().parents[1] / "docs/procedrr/processes"
    inputs = {
        "work_item_name": "Example Feature",
        "proposed_pr_id": "example-pr",
        "feature_description": "Deliver the example feature.",
        "proposed_pr_context": {"objective": "Deliver the example feature."},
    }
    output_directory, tasks = instantiate_procedrr_process_workflow(
        process_id="execute-proposed-pr-workflow",
        process_directory=process_directory,
        input_state=inputs,
        work_item_name="Example Feature",
        output_root=tmp_path / "workflows",
    )

    assert output_directory == tmp_path / "workflows" / "example-feature"
    assert len(tasks) == 1
    assert tasks[0].process_id == "execute-proposed-pr-workflow"
    assert tasks[0].input_state == inputs
    assert tasks[0].status.value == "open"


def test_instantiate_procedrr_process_workflow_reuses_task(
    tmp_path: Path,
) -> None:
    process_directory = Path(__file__).resolve().parents[1] / "docs/procedrr/processes"
    output_root = tmp_path / "workflows"
    inputs = {"work_item_name": "Example", "proposed_pr_id": "example-pr"}

    first_directory, first_tasks = instantiate_procedrr_process_workflow(
        process_id="execute-proposed-pr-workflow",
        process_directory=process_directory,
        input_state=inputs,
        work_item_name="Example Feature",
        workflow_instance_name="example-pr",
        output_root=output_root,
    )
    second_directory, second_tasks = instantiate_procedrr_process_workflow(
        process_id="execute-proposed-pr-workflow",
        process_directory=process_directory,
        input_state=inputs,
        work_item_name="Example Feature",
        workflow_instance_name="example-pr",
        output_root=output_root,
    )

    assert second_directory == first_directory
    assert second_tasks == first_tasks


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
                    ToolInvocation(
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


def test_fully_review_pr_is_a_sequential_procedrr_process() -> None:
    from procedrr import ProcessCatalog

    processes_path = Path(__file__).resolve().parents[1] / "docs/procedrr/processes"
    process = ProcessCatalog.load(processes_path).get("fully-review-pr").document
    assert [item["name"] for item in process["inputs"]] == [
        "user_request",
        "pull_request_id",
        "workflow_context",
    ]
    steps = process["steps"]
    assert len(steps) == 2
    question = steps[0]["specialize"]["question"]
    assert (
        question.index("`feature-functionality-review`")
        < question.index("`feature-test-coverage-review`")
        < question.index("`security-review`")
    )
    assert "passing the functionality report as context" in question
    assert "passing both earlier review reports as context" in question
