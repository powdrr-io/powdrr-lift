from __future__ import annotations

import json
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from powdrr_lift.cli import main
from powdrr_lift.workflow_definition_analysis import (
    analyze_workflow_definition,
    analyze_workflow_definitions,
    apply_liveness_baseline,
    apply_warning_budget,
    discover_workflow_definitions,
    render_skill_prompt_snapshots,
    warning_report_data,
)


def test_definition_analysis_uses_runtime_action_parser_and_input_contract(
    tmp_path: Path,
) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: inspect
when_to_use: [Inspect files.]
steps:
  - id: inspect
    description: Inspect the feature.
    inputs:
      - name: feature_id
    details: >-
      Use <feature-id>, then return
      {"action":"complete","text":"Inspected the feature."}.
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert report.validation_successful


def test_definition_analysis_rejects_undeclared_invoke_tool_action(
    tmp_path: Path,
) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: undeclared-tool
when_to_use: [Plan work.]
steps:
  - id: plan
    description: Plan the work.
    actions: [invoke_tool]
    details: Return the plan as a structured output.
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert not report.validation_successful
    issue = next(
        issue
        for issue in report.issues
        if issue.code == "undeclared_invoke_tool_action"
    )
    assert "declares no model-owned tool" in issue.message


def test_definition_analysis_allows_runner_owned_gather_context(
    tmp_path: Path,
) -> None:
    definition = tmp_path / "workflow.yaml"
    definition.write_text(
        """\
id: workflow
when_to_use: [Execute work.]
how_to_fill_this_out: [Use the task contract.]
task_templates:
  - description: Gather context.
    step_type: invoke_tool
    actions: []
    pre_step:
      action: gather_context
      template: {feature_id: example, types: [requirements]}
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert "pre_step_action_not_declared" not in {issue.code for issue in report.issues}


def test_definition_analysis_rejects_multiple_model_tool_invocations(
    tmp_path: Path,
) -> None:
    definition = tmp_path / "workflow.yaml"
    definition.write_text(
        """\
id: workflow
when_to_use: [Execute work.]
how_to_fill_this_out: [Use the task contract.]
task_templates:
  - description: Plan work.
    step_type: governed
    actions: [invoke_tool]
    tool_invocations:
      - {tool: basedpyright-structure, operation: inspect_structure}
      - {tool: basedpyright-symbol, operation: resolve_symbol}
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert "multiple_model_tool_invocations" in {issue.code for issue in report.issues}


def test_definition_analysis_rejects_details_on_deterministic_task(
    tmp_path: Path,
) -> None:
    definition = tmp_path / "workflow.yaml"
    definition.write_text(
        """\
id: workflow
when_to_use: [Execute work.]
how_to_fill_this_out: [Use the task contract.]
task_templates:
  - description: Run the command.
    step_type: invoke_tool
    actions: []
    details: This prose is not consumed by the deterministic runner.
    pre_step:
      action: invoke_tool
      template: {tool: shell, command: [true]}
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert "deterministic_task_details_unused" in {
        issue.code for issue in report.issues
    }


def test_definition_analysis_covers_instantiated_tasks_and_workflow_metadata(
    tmp_path: Path,
) -> None:
    workflow_root = tmp_path / "docs/workflows/interaction-file-log"
    workflow_root.mkdir(parents=True)
    task = workflow_root / "interaction-file-log-core-task-001.yaml"
    task.write_text(
        """\
task_id: interaction-file-log-core-task-001
status: open
upstream_task_ids: []
dependent_state: [proposed-pr-context-gathered]
complexity: high
input_state:
  proposed_pr: interaction-file-log-core
  feature_id: interaction-file-log
assignee_type: agent
assignee_role: architect
output_state_type: proposed-pr-context-state
description: Gather context about the proposed PR
workflow_template: execute-proposed-pr
phase_type: intake
persona_id: architect
step_type: invoke_tool
pre_step:
  action: gather_context
  template:
    feature_id: <feature_id>
    types: [proposed_prs]
""",
        encoding="utf-8",
    )
    workflow = workflow_root / "interaction-file-log-core-workflow.yaml"
    workflow.write_text(
        """\
proposed_pr_id: interaction-file-log-core
base_branch: main
integration_branch: powdrr/interaction-file-log-core
workflow_relative_directory: docs/workflows/interaction-file-log
invariants:
  - id: workflow-implements-target
    relationship: implements
    source_type: workflow
    target_type: proposed_pr
    target_id: interaction-file-log-core
    cardinality: exactly_one
relationships:
  - relationship: implements
    source_type: workflow
    target_type: proposed_pr
    cardinality: exactly_one
    invariant_id: workflow-implements-target
    source_id: interaction-file-log-core
""",
        encoding="utf-8",
    )

    task_report = analyze_workflow_definition(task)
    workflow_report = analyze_workflow_definition(workflow)

    assert task_report.kind == "workflow_task"
    assert task_report.validation_successful
    assert workflow_report.kind == "workflow_instance"
    assert workflow_report.validation_successful


def test_definition_analysis_reports_invalid_examples_and_unbound_placeholders(
    tmp_path: Path,
) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: inspect
when_to_use: [Inspect files.]
steps:
  - id: inspect
    description: Inspect the feature.
    outputs:
      - name: result
        type: string
    inputs:
      - name: feature_id
    details: >-
      Use <missing-id>, then return {"action":"not-a-real-action"}.
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert not report.validation_successful
    assert {issue.code for issue in report.issues} == {
        "invalid_action_example",
        "unbound_placeholder",
    }


def test_definition_analysis_requires_examples_for_actions_requested_in_prose(
    tmp_path: Path,
) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: inspect
when_to_use: [Inspect files.]
steps:
  - id: inspect
    description: Inspect the feature.
    actions: [read_document]
    outputs:
      - name: result
        type: string
    details: Return the result with exactly one next_step action.
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert "missing_action_example" in {issue.code for issue in report.issues}
    issue = next(
        issue for issue in report.issues if issue.code == "missing_action_example"
    )
    assert "next_step" in issue.message


def test_definition_analysis_accepts_exact_action_example_for_prose_request(
    tmp_path: Path,
) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: inspect
when_to_use: [Inspect files.]
steps:
  - id: inspect
    description: Inspect the feature.
    actions: [read_document]
    outputs:
      - name: result
        type: string
    details: >-
      Return the result with exactly one next_step action:
      {"action":"next_step","output_state":{"result":"inspected"}}.
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert "missing_action_example" not in {issue.code for issue in report.issues}


def test_prompt_snapshots_use_production_builder_and_normalize_repository_root(
    tmp_path: Path,
) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: inspect
when_to_use: [Inspect files.]
steps:
  - id: inspect
    description: Inspect files.
    outputs:
      - name: result
        type: string
""",
        encoding="utf-8",
    )
    output_dir = tmp_path / "snapshots"

    paths = render_skill_prompt_snapshots(
        definition,
        output_dir=output_dir,
        repo_root=tmp_path,
    )

    assert [path.name for path in paths] == ["001-inspect.json"]
    snapshot = json.loads(paths[0].read_text(encoding="utf-8"))
    assert snapshot["messages"][0]["role"] == "system"
    assert "<root-intent>" in snapshot["messages"][1]["content"]
    assert str(tmp_path) not in paths[0].read_text(encoding="utf-8")


def test_definition_validation_cli_emits_machine_readable_report(
    tmp_path: Path,
) -> None:
    from powdrr_lift.cli import main

    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: inspect
when_to_use: [Inspect files.]
steps:
  - id: inspect
    description: Inspect files.
    outputs:
      - name: result
        type: string
""",
        encoding="utf-8",
    )
    stdout = StringIO()

    with redirect_stdout(stdout):
        exit_code = main(["validate-workflow-definition", str(definition), "--json"])

    assert exit_code == 0
    assert json.loads(stdout.getvalue())["validation_successful"] is True


def test_definition_discovery_recurses_and_ignores_unsupported_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "skill.yaml").write_text("name: x\nsteps: []\n", encoding="utf-8")
    (tmp_path / "nested" / "template.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not a definition", encoding="utf-8")

    assert discover_workflow_definitions([tmp_path]) == (
        tmp_path / "nested" / "template.json",
        tmp_path / "skill.yaml",
    )


def test_batch_definition_analysis_reports_all_definitions(tmp_path: Path) -> None:
    valid = tmp_path / "valid.yaml"
    valid.write_text(
        """\
name: inspect
when_to_use: [Inspect files.]
steps:
  - id: inspect
    description: Inspect files.
""",
        encoding="utf-8",
    )
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("steps: not-a-list\n", encoding="utf-8")

    report = analyze_workflow_definitions([tmp_path])

    assert len(report.reports) == 2
    assert not report.validation_successful
    assert report.reports[0].definition == invalid
    assert report.reports[1].definition == valid


def test_batch_definition_validation_cli_fails_for_any_invalid_definition(
    tmp_path: Path,
) -> None:
    (tmp_path / "valid.yaml").write_text(
        "name: inspect\nwhen_to_use: [Inspect.]\nsteps:\n  - description: Inspect.\n",
        encoding="utf-8",
    )
    (tmp_path / "invalid.yaml").write_text("steps: invalid\n", encoding="utf-8")
    stdout = StringIO()

    with redirect_stdout(stdout):
        exit_code = main(["validate-workflow-definitions", str(tmp_path), "--json"])

    assert exit_code == 1
    report = json.loads(stdout.getvalue())
    assert report["definition_count"] == 2
    assert report["validation_successful"] is False


def test_prompt_snapshots_support_workflow_templates(tmp_path: Path) -> None:
    definition = tmp_path / "template.yaml"
    definition.write_text(
        """\
id: execute
task_templates:
  - description: Gather context.
    step_type: invoke_tool
    input_state: {proposal: <proposal-id>}
    details: Assign the result.
    output_state_type: context-state
""",
        encoding="utf-8",
    )

    paths = render_skill_prompt_snapshots(
        definition, output_dir=tmp_path / "snapshots", repo_root=tmp_path
    )

    assert [path.name for path in paths] == ["001-gather-context.json"]
    snapshot = json.loads(paths[0].read_text(encoding="utf-8"))
    assert snapshot["workflow_template"] == "execute"
    assert snapshot["step_type"] == "invoke_tool"


def test_definition_analysis_reports_missing_handoff_and_schema(tmp_path: Path) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: handoff
when_to_use: [Test handoffs.]
steps:
  - id: produce
    description: Produce a result.
    outputs:
      - name: result
        required_for_next_step: true
  - id: consume
    description: Consume the result.
    inputs:
      - name: missing
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert {issue.code for issue in report.issues} >= {
        "missing_output_schema",
        "missing_handoff_input",
    }


def test_definition_analysis_reports_unreachable_steps(tmp_path: Path) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: control-flow
when_to_use: [Test control flow.]
steps:
  - id: first
    description: Jump over the second step.
    next_step_override: last
  - id: skipped
    description: This step is unreachable.
  - id: last
    description: Finish.
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert "unreachable_step" in {issue.code for issue in report.issues}


def test_definition_analysis_validates_examples_against_step_contract(
    tmp_path: Path,
) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: predicated
when_to_use: [Test predicated actions.]
steps:
  - id: inspect
    description: Inspect.
    step_type: predicated
    completion:
      required_outputs: [answer]
    outputs:
      - name: answer
        schema: {type: string}
    outputs:
      - name: answer
        required_for_next_step: true
        schema: {type: string}
    details: 'Return {"action":"next_step"}.'
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert "invalid_action_example" in {issue.code for issue in report.issues}


def test_definition_analysis_warns_on_model_owned_idempotent_git_add(
    tmp_path: Path,
) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: stage
when_to_use: [Stage files.]
steps:
  - id: stage
    description: Stage the files.
    tool_invocations:
      - tool: git
        command: [add, docs/proposal.yaml]
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert not report.validation_successful
    issues = {issue.code: issue for issue in report.issues}
    assert issues["model_owned_deterministic_action"].severity == "error"
    assert issues["idempotent_action_without_auto_advance"].severity == "error"


def test_definition_analysis_rejects_empty_repair_action_space(tmp_path: Path) -> None:
    definition = tmp_path / "empty-actions.yaml"
    definition.write_text(
        """\
name: empty-actions
when_to_use: [Test repairability.]
steps:
  - id: blocked
    description: No legal recovery action.
    actions: []
    actions_declared: true
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert "empty_repair_action_space" in {issue.code for issue in report.issues}


def test_definition_analysis_rejects_read_only_step_without_discrete_outcome(
    tmp_path: Path,
) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: read-only
when_to_use: [Read a document.]
steps:
  - id: inspect
    description: Inspect the document and continue.
    actions: [read_document]
    details: Return next_step after reviewing the document.
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert "missing_discrete_outcome" in {issue.code for issue in report.issues}


def test_definition_analysis_warns_on_non_progress_cycle(tmp_path: Path) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: loop
when_to_use: [Loop.]
steps:
  - id: loop
    description: Repeat the same observation.
    next_step_override: loop
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert "non_progress_cycle" in {issue.code for issue in report.issues}


def test_definition_validation_cli_prints_liveness_warnings(
    tmp_path: Path,
) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: stage
when_to_use: [Stage files.]
steps:
  - id: stage
    description: Stage the files.
    tool_invocations:
      - tool: git
        command: [add, docs/proposal.yaml]
""",
        encoding="utf-8",
    )
    stdout = StringIO()
    stderr = StringIO()

    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(["validate-workflow-definition", str(definition)])

    assert exit_code == 1
    assert "Workflow definition invalid" in stderr.getvalue()
    assert "idempotent_action_without_auto_advance" in stderr.getvalue()


def test_definition_analysis_warns_on_repeated_read_cycle(tmp_path: Path) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: read-loop
when_to_use: [Read repeatedly.]
steps:
  - id: read
    description: Read the same document.
    actions: [read_document]
    next_step_override: read
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert "non_progress_cycle" in {issue.code for issue in report.issues}


def test_definition_analysis_reports_unobservable_completion(tmp_path: Path) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: missing-output
when_to_use: [Complete.]
steps:
  - id: complete
    description: Complete only when a missing output exists.
    step_type: predicated
    completion:
      required_outputs: [answer]
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert "unobservable_completion" in {issue.code for issue in report.issues}
    assert "terminal_state_without_completion" in {
        issue.code for issue in report.issues
    }


def test_definition_analysis_reports_unbounded_coding_loop(tmp_path: Path) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: loop
when_to_use: [Loop.]
steps:
  - id: loop
    description: Keep coding forever.
    step_type: coding_loop
    coding_loop:
      goal: Make changes.
      verification: [{id: check, command: check}]
      stopping_conditions: [verified]
      max_iterations: 0
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert "unbounded_coding_loop" in {issue.code for issue in report.issues}


def test_definition_analysis_reports_unknown_shell_effect(tmp_path: Path) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: shell
when_to_use: [Run a command.]
steps:
  - id: run
    description: Run a command.
    actions: [invoke_tool]
    outputs:
      - name: result
        type: string
    tool_invocations:
      - tool: shell
        command: [custom-command]
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    issue = next(
        issue for issue in report.issues if issue.code == "unknown_shell_effect"
    )
    assert issue.severity == "warning"


def test_definition_analysis_resolves_known_shell_effect_metadata(
    tmp_path: Path,
) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: shell
when_to_use: [Inspect files.]
steps:
  - id: inspect
    description: Inspect files.
    tool_invocations:
      - tool: shell
        command: [rg, -n, pattern, src]
""",
        encoding="utf-8",
    )

    report = analyze_workflow_definition(definition)

    assert "unknown_shell_effect" not in {issue.code for issue in report.issues}


def test_liveness_baseline_suppresses_advisory_diagnostics_only(tmp_path: Path) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: shell
when_to_use: [Run a command.]
steps:
  - id: run
    description: Run a command.
    outputs:
      - name: result
        type: string
    tool_invocations:
      - tool: shell
        command: [custom-command]
""",
        encoding="utf-8",
    )
    report = analyze_workflow_definitions([definition])
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "version": 1,
                "issues": [
                    {
                        "definition": str(definition),
                        "code": "unknown_shell_effect",
                        "path": f"{definition}.steps[0].tool_invocations",
                        "owner": "workflow-platform",
                        "reason": "Legacy shell capability rollout.",
                        "expires": "2099-12-31",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    filtered = apply_liveness_baseline(report, baseline)

    assert filtered.validation_successful
    assert filtered.reports[0].issues == ()


def test_warning_budget_fails_on_new_advisory(tmp_path: Path) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: shell
when_to_use: [Run a command.]
steps:
  - id: run
    description: Run a command.
    tool_invocations:
      - tool: shell
        command: [custom-command]
""",
        encoding="utf-8",
    )
    report = analyze_workflow_definitions([definition])
    budget = tmp_path / "budget.json"
    budget.write_text(json.dumps({"version": 1, "counts": {}}), encoding="utf-8")

    checked = apply_warning_budget(report, budget)

    assert not checked.validation_successful
    assert "liveness_warning_budget_exceeded" in {
        issue.code for issue in checked.reports[0].issues
    }
    assert warning_report_data(report)["counts"] == {"unknown_shell_effect": 1}
