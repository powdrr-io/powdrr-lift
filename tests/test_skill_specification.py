from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from powdrr_lift.core import (
    CodingLoopSpec,
    Skill,
    SkillStep,
    SkillStepCompletion,
    SkillStepGate,
    SkillStepInput,
    SkillStepOutput,
    SkillStepPreStep,
    SkillUsesSkill,
    SkillUsesSkillBinding,
    build_skill_directory_validation_report,
    build_skill_validation_report,
    load_skill,
    save_skill,
    skill_from_data,
    skill_from_json,
    skill_to_json,
    validate_skill_directory,
)


def uses_skill_name(step: SkillStep) -> str:
    contract = step.uses_skill
    assert contract is not None
    return contract.skill


def test_git_tool_invocation_package_expands_to_declared_operations() -> None:
    skill = skill_from_data(
        {
            "name": "git-packages",
            "when_to_use": ["Use Git."],
            "steps": [
                {
                    "description": "Use Git operations.",
                    "tool_invocation_packages": ["git_readonly_and_additive"],
                }
            ],
        }
    )

    step = skill.steps[0]
    assert step.tool_invocation_packages == ("git_readonly_and_additive",)
    assert {invocation.operation for invocation in step.tool_invocations} == {
        "status",
        "remote",
        "branch_current",
        "default_branch",
        "show_ref",
        "add",
        "commit",
        "push",
        "switch",
        "switch_create",
        "move",
        "rename",
    }


def test_gh_tool_invocation_packages_expand_to_read_and_write_operations() -> None:
    skill = skill_from_data(
        {
            "name": "gh-packages",
            "when_to_use": ["Use GitHub."],
            "steps": [
                {
                    "description": "Inspect and update a pull request.",
                    "tool_invocation_packages": ["gh_readonly_and_write"],
                }
            ],
        }
    )

    step = skill.steps[0]
    assert step.tool_invocation_packages == ("gh_readonly_and_write",)
    assert {invocation.operation for invocation in step.tool_invocations} == {
        "pr_view",
        "pr_diff",
        "pr_checks",
        "pr_comments",
        "pr_create",
        "pr_edit",
        "pr_review_comment",
    }


def test_unknown_tool_invocation_package_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported tool invocation package"):
        skill_from_data(
            {
                "name": "invalid-packages",
                "when_to_use": ["Use Git."],
                "steps": [
                    {
                        "description": "Use Git operations.",
                        "tool_invocation_packages": ["git_everything"],
                    }
                ],
            }
        )


def test_uses_skill_step_round_trips_explicit_handoff_bindings() -> None:
    skill = skill_from_data(
        {
            "name": "parent",
            "when_to_use": ["Call a nested skill."],
            "steps": [
                {
                    "description": "Run the child deterministically.",
                    "step_type": "uses_skill",
                    "uses_skill": {
                        "skill": "child",
                        "inputs": {"request": "feature_request"},
                        "outputs": {
                            "result": {
                                "ref": "child_result",
                                "schema": {"type": "object"},
                            }
                        },
                    },
                }
            ],
        }
    )

    contract = skill.steps[0].uses_skill
    assert isinstance(contract, SkillUsesSkill)
    assert contract.inputs == (SkillUsesSkillBinding("request", "feature_request"),)
    assert contract.outputs[0].ref == "child_result"
    assert skill.steps[0].to_data()["step_type"] == "uses_skill"


def test_uses_skill_step_rejects_model_actions() -> None:
    with pytest.raises(ValueError, match="cannot declare model actions"):
        skill_from_data(
            {
                "name": "invalid",
                "when_to_use": ["Call a nested skill."],
                "steps": [
                    {
                        "description": "Run the child.",
                        "step_type": "uses_skill",
                        "actions": ["invoke_skill"],
                        "uses_skill": {"skill": "child"},
                    }
                ],
            }
        )


def test_coding_loop_step_round_trips_with_typed_protocol() -> None:
    skill = skill_from_json(
        json.dumps(
            {
                "name": "coding-loop",
                "when_to_use": ["Implement and verify a code change."],
                "steps": [
                    {
                        "id": "implement",
                        "description": "Implement the requested change.",
                        "step_type": "coding_loop",
                        "coding_loop": {
                            "goal": "Make the targeted tests pass.",
                            "verification": [{"id": "tests", "command": ["pytest"]}],
                            "stopping_conditions": ["tests pass", "scope is complete"],
                            "max_iterations": 5,
                        },
                        "actions": ["read_document", "edit", "invoke_tool"],
                    }
                ],
            }
        )
    )

    assert isinstance(skill.steps[0].coding_loop, CodingLoopSpec)
    assert skill.steps[0].coding_loop.max_iterations == 5
    serialized = json.loads(skill_to_json(skill))
    assert serialized["steps"][0]["coding_loop"]["goal"] == (
        "Make the targeted tests pass."
    )
    assert build_skill_validation_report(skill_to_json(skill)).validation_successful


def test_coding_loop_step_rejects_invalid_iteration_budget() -> None:
    report = build_skill_validation_report(
        json.dumps(
            {
                "name": "invalid-coding-loop",
                "when_to_use": ["Test validation."],
                "steps": [
                    {
                        "description": "Implement.",
                        "step_type": "coding_loop",
                        "coding_loop": {"goal": "Implement", "max_iterations": 0},
                    }
                ],
            }
        )
    )

    assert not report.validation_successful
    assert any(
        issue.code == "invalid_coding_loop_max_iterations" for issue in report.issues
    )


def test_explicit_empty_actions_round_trip_as_closed_contract() -> None:
    skill = skill_from_json(
        json.dumps(
            {
                "name": "closed-step",
                "when_to_use": ["When no model action is permitted."],
                "steps": [
                    {
                        "id": "wait",
                        "description": "Wait for the engine-owned result.",
                        "actions": [],
                    }
                ],
            }
        )
    )

    step = skill.steps[0]
    assert step.actions == ()
    assert step.actions_declared is True
    serialized = skill_to_json(skill)
    assert '"actions": []' in serialized

    legacy = skill_from_json(
        json.dumps(
            {
                "name": "legacy-step",
                "when_to_use": ["Compatibility."],
                "steps": [{"description": "No contract."}],
            }
        )
    )
    assert legacy.steps[0].actions_declared is False


def test_skill_round_trips_through_json() -> None:
    skill = Skill(
        name="specify-a-feature",
        when_to_use=(
            "When the user wants to work through a feature synchronously.",
            (
                "When the flow should compose other skills instead of generating "
                "async tasks."
            ),
        ),
        steps=(
            SkillStep(
                description="Capture the feature goal.",
                id="capture-goal",
                details="Record the user-visible outcome first.",
                prompt_catalogs=(),
                actions=(),
            ),
            SkillStep(
                description="Pull in the system context.",
                details="Use the system spec and related context.",
                uses_skill=SkillUsesSkill("specify-system"),
            ),
            SkillStep(
                description="Summarize the result.",
                actions=(),
            ),
        ),
        adversarial=True,
    )

    json_text = skill_to_json(skill)
    parsed = skill_from_json(json_text)

    assert parsed == skill
    assert parsed.steps[2].prompt_catalogs == ()
    assert json.loads(json_text) == {
        "name": "specify-a-feature",
        "adversarial": True,
        "when_to_use": [
            "When the user wants to work through a feature synchronously.",
            (
                "When the flow should compose other skills instead of generating "
                "async tasks."
            ),
        ],
        "steps": [
            {
                "description": "Capture the feature goal.",
                "step_type": "governed",
                "id": "capture-goal",
                "details": "Record the user-visible outcome first.",
            },
            {
                "description": "Pull in the system context.",
                "step_type": "uses_skill",
                "details": "Use the system spec and related context.",
                "uses_skill": {"skill": "specify-system"},
            },
            {
                "description": "Summarize the result.",
                "step_type": "governed",
            },
        ],
    }


def test_skill_interaction_style_round_trips_and_validates() -> None:
    skill = Skill(
        name="review",
        when_to_use=("Review changes.",),
        interaction_style="observational_review",
        steps=(
            SkillStep(
                description="Challenge the proposal.",
                interaction_style="devils_advocate",
            ),
        ),
    )

    parsed = skill_from_json(skill_to_json(skill))

    assert parsed == skill
    assert parsed.interaction_style == "observational_review"
    assert parsed.steps[0].interaction_style == "devils_advocate"

    report = build_skill_validation_report(
        yaml.safe_dump(
            {
                "name": "invalid-style",
                "when_to_use": ["Review changes."],
                "interaction_style": "invented",
                "steps": [{"description": "Review."}],
            }
        ),
        source_path="skill.yaml",
    )
    assert not report.validation_successful
    assert any(issue.code == "invalid_interaction_style" for issue in report.issues)


def test_skill_round_trip_preserves_required_caller_inputs() -> None:
    skill = Skill(
        name="design-interview",
        when_to_use=("Create a feature proposal.",),
        inputs=(
            SkillStepInput("work_item_name", "string", True, "caller"),
            SkillStepInput("feature_description", "string", True, "caller"),
        ),
        steps=(SkillStep(description="Interview the caller."),),
    )

    parsed = skill_from_json(skill_to_json(skill))

    assert parsed.inputs == skill.inputs

    report = build_skill_validation_report(
        yaml.safe_dump(
            {
                "name": "requires-input",
                "when_to_use": ["Create a feature proposal."],
                "inputs": [{"name": "feature_description", "source": "caller"}],
                "steps": [{"description": "Interview the caller."}],
            }
        ),
        source_path="skill.yaml",
    )
    assert report.validation_successful


def test_checked_in_skill_and_workflow_steps_declare_prompt_catalogs() -> None:
    repository_root = Path(__file__).parents[1]
    definition_paths = sorted((repository_root / "skill-definitions").glob("*.yaml"))
    definition_paths += sorted((repository_root / "templates").glob("*.yaml"))

    assert definition_paths
    for path in definition_paths:
        document = yaml.safe_load(path.read_text())
        step_key = (
            "steps" if path.parent.name == "skill-definitions" else "task_templates"
        )
        for index, step in enumerate(document[step_key]):
            expected_invoke_tool_steps = {
                ("finish-pr-prep.yaml", 0),
                ("finish-pr-prep.yaml", 4),
                ("create-pull-request.yaml", 0),
                ("create-pull-request.yaml", 2),
                ("create-pull-request.yaml", 6),
                ("specify-system.yaml", 1),
                ("specify-system.yaml", 3),
                ("specify-system.yaml", 3),
                ("specify-architecture.yaml", 1),
                ("specify-architecture.yaml", 3),
                ("specify-architecture.yaml", 3),
                ("review-system.yaml", 2),
                ("review-system.yaml", 4),
                ("review-architecture.yaml", 2),
                ("review-architecture.yaml", 4),
                ("specify-implementation.yaml", 2),
                ("specify-implementation.yaml", 4),
                ("specify-implementation.yaml", 4),
                ("execute-proposed-pr.yaml", 0),
                ("run-tests-and-fix.yaml", 0),
                ("run-tests-and-fix.yaml", 4),
                ("execute-proposed-pr.yaml", 5),
                ("execute-proposed-pr.yaml", 6),
                ("execute-proposed-pr.yaml", 7),
                ("execute-proposed-pr.yaml", 10),
                ("execute-proposed-pr.yaml", 11),
                ("start-implementing-feature.yaml", 1),
                ("start-implementing-feature.yaml", 2),
                ("start-implementing-feature.yaml", 3),
                ("start-implementing-feature.yaml", 8),
                ("start-implementing-feature.yaml", 10),
                ("run-tests-and-fix.yaml", 1),
                ("run-tests-and-fix.yaml", 7),
                ("start-implementing-feature.yaml", 17),
                ("start-implementing-feature.yaml", 20),
                ("start-implementing-feature.yaml", 22),
                ("start-implementing-feature.yaml", 21),
                ("specify-a-feature.yaml", 3),
                ("design-interview.yaml", 20),
                ("design-interview.yaml", 22),
                ("design-interview.yaml", 23),
                ("bootstrap-code-structure.yaml", 5),
                ("review-skill-workflow.yaml", 8),
            }
            expected_gate_steps = {
                ("create-pull-request.yaml", 4),
                ("specify-system.yaml", 5),
                ("specify-architecture.yaml", 5),
                ("specify-implementation.yaml", 6),
                ("start-implementing-feature.yaml", 6),
                ("start-implementing-feature.yaml", 12),
                ("review-system.yaml", 6),
                ("review-architecture.yaml", 6),
                ("run-tests-and-fix.yaml", 6),
                ("run-tests-and-fix.yaml", 8),
                ("design-interview.yaml", 25),
            }
            expected_predicated_steps = {
                ("run-tests-and-fix.yaml", 3),
                ("run-tests-and-fix.yaml", 5),
                ("start-implementing-feature.yaml", 24),
                ("specify-a-feature.yaml", 1),
            } | {("design-interview.yaml", index) for index in range(20)}
            expected_step_type = (
                "coding_loop"
                if (path.name, index) == ("execute-proposed-pr.yaml", 2)
                else "invoke_tool"
                if (path.name, index) in expected_invoke_tool_steps
                else "gate"
                if (path.name, index) in expected_gate_steps
                else "predicated"
                if (path.name, index) in expected_predicated_steps
                else "uses_skill"
                if "uses_skill" in step
                else "governed"
            )
            assert step["step_type"] == expected_step_type, (
                f"{path}:{step_key}[{index}]"
            )
            if "prompt_catalogs" in step:
                assert step["prompt_catalogs"], f"{path}:{step_key}[{index}]"
                assert set(step["prompt_catalogs"]) <= {
                    "context_types",
                    "skills",
                    "actions",
                }


def test_actions_round_trip_and_default_to_next_step() -> None:
    skill = Skill(
        name="action-contract",
        when_to_use=("Test step action restrictions.",),
        steps=(
            SkillStep(
                description="Inspect then hand off.",
                actions=("read_document",),
            ),
        ),
    )

    parsed = skill_from_json(skill_to_json(skill))
    assert parsed == skill

    invalid = yaml.safe_load(skill_to_json(skill))
    invalid["steps"][0]["actions"] = ["not-an-action"]
    with pytest.raises(ValueError, match="unsupported action"):
        skill_from_json(json.dumps(invalid))


def test_step_actions_round_trip_with_local_instructions() -> None:
    skill = Skill(
        name="action-instructions",
        when_to_use=("Test local action instructions.",),
        steps=(
            SkillStep(
                description="Inspect then hand off.",
                actions=("read_document",),
            ),
        ),
    )

    parsed = skill_from_json(skill_to_json(skill))

    assert parsed == skill
    assert json.loads(skill_to_json(skill))["steps"][0]["actions"] == ["read_document"]


def test_skill_step_contracts_round_trip_and_validate() -> None:
    skill = Skill(
        name="handoff-test",
        when_to_use=("Test explicit step handoffs.",),
        steps=(
            SkillStep(
                id="produce",
                description="Produce a result.",
                outputs=(
                    SkillStepOutput(
                        name="validation_result",
                        type="validation_result",
                        required_for_next_step=True,
                        schema={
                            "type": "object",
                            "properties": {"valid": {"type": "boolean"}},
                            "required": ["valid"],
                            "additionalProperties": False,
                        },
                    ),
                ),
            ),
            SkillStep(
                id="consume",
                description="Consume the result.",
                inputs=(
                    SkillStepInput(
                        name="validation_result",
                        type="validation_result",
                    ),
                ),
            ),
        ),
    )

    parsed = skill_from_json(skill_to_json(skill))

    assert parsed == skill
    assert parsed.steps[0].outputs[0].schema == {
        "type": "object",
        "properties": {"valid": {"type": "boolean"}},
        "required": ["valid"],
        "additionalProperties": False,
    }
    report = build_skill_validation_report(skill_to_json(skill))
    assert report.validation_successful is True


def test_skill_step_contracts_reject_duplicate_names() -> None:
    report = build_skill_validation_report(
        json.dumps(
            {
                "name": "invalid-handoff",
                "when_to_use": ["Test invalid contracts."],
                "steps": [
                    {
                        "description": "Produce values.",
                        "outputs": [
                            {"name": "result"},
                            {"name": "result"},
                        ],
                    }
                ],
            }
        ),
    )

    assert report.validation_successful is False
    assert any(issue.code == "duplicate_output_name" for issue in report.issues)


def test_skill_step_output_schema_rejects_unknown_schema_fields() -> None:
    report = build_skill_validation_report(
        json.dumps(
            {
                "name": "invalid-output-schema",
                "when_to_use": ["Test output validation."],
                "steps": [
                    {
                        "description": "Produce output.",
                        "outputs": [
                            {
                                "name": "result",
                                "schema": {"type": "object", "mystery": True},
                            }
                        ],
                    }
                ],
            }
        )
    )

    assert report.validation_successful is False
    assert any(issue.code == "invalid_output_schema" for issue in report.issues)


def test_skill_file_helpers_round_trip(tmp_path: Path) -> None:
    skill = Skill(
        name="clarify-intent",
        when_to_use=("When the user needs a quick synchronous clarification flow.",),
        steps=(SkillStep(description="Ask for the intent."),),
    )

    output_path = save_skill(skill, tmp_path / "clarify-intent.json")
    assert output_path.exists()
    assert load_skill(output_path) == skill


def test_skill_validation_rejects_duplicate_step_ids() -> None:
    report = build_skill_validation_report(
        "name: repeated\n"
        "when_to_use: [review]\n"
        "steps:\n"
        "- id: repeat\n"
        "  description: First\n"
        "  actions: []\n"
        "- id: repeat\n"
        "  description: Second\n"
        "  actions: []\n",
        source_path=Path("repeated.yaml"),
    )

    assert report.validation_successful is False
    assert [issue.code for issue in report.issues] == ["duplicate_step_id"]


def test_skill_validation_accepts_invoke_tool_step_with_gather_pre_step() -> None:
    report = build_skill_validation_report(
        yaml.safe_dump(
            {
                "name": "filter-context",
                "when_to_use": ["When gathered context needs filtering."],
                "steps": [
                    {
                        "description": "Filter gathered requirements.",
                        "step_type": "invoke_tool",
                        "pre_step": {
                            "action": "gather_context",
                            "template": {
                                "feature_id": "<feature-id>",
                                "types": ["requirements"],
                            },
                        },
                        "outputs": [
                            {
                                "name": "filtered_requirements",
                                "required_for_next_step": True,
                            }
                        ],
                    }
                ],
            }
        ),
        source_path=Path("filter-context.yaml"),
    )

    assert report.validation_successful is True


def test_skill_validation_rejects_explicit_universal_actions() -> None:
    report = build_skill_validation_report(
        "name: universal-actions\n"
        "when_to_use: [review]\n"
        "steps: [{description: review, actions: [prompt_user, next_step]}]\n",
        source_path=Path("universal-actions.yaml"),
    )

    assert report.validation_successful is False
    assert [issue.code for issue in report.issues] == ["invalid_actions"]
    assert "universal action" in report.issues[0].message


def test_gate_step_round_trips_and_validates() -> None:
    skill = Skill(
        name="gated-work",
        when_to_use=("When work needs an automated verification loop.",),
        steps=(
            SkillStep(id="repair", description="Repair the result."),
            SkillStep(
                id="verify",
                description="Verify the result.",
                step_type="gate",
                pre_step=SkillStepPreStep(
                    action="invoke_tool",
                    template={"tool": "shell", "command": ["true"]},
                ),
                gate=SkillStepGate(
                    outcome={"path": "returncode", "equals": 0},
                    goto_step="repair",
                    retry_context="Repair the result and run the verification again.",
                ),
            ),
        ),
    )

    parsed = skill_from_json(skill_to_json(skill))

    assert parsed == skill
    assert build_skill_validation_report(skill_to_json(skill)).validation_successful


def test_dynamic_validation_gate_round_trips() -> None:
    skill = Skill(
        name="dynamic-validation",
        when_to_use=("When all discovered checks must pass.",),
        steps=(
            SkillStep(
                description="Run every discovered check.",
                validation_gate={
                    "id": "checks",
                    "discovery": {
                        "action": {"kind": "gather_context", "types": ["tools"]}
                    },
                    "obligations": {
                        "source": "matches",
                        "filter": {"section": "tools"},
                        "id": "item.id",
                        "action": "item.validation_action",
                    },
                },
            ),
        ),
    )

    parsed = skill_from_json(skill_to_json(skill))

    assert parsed == skill
    assert build_skill_validation_report(skill_to_json(skill)).validation_successful


def test_skill_validation_rejects_invoke_tool_without_pre_step() -> None:
    report = build_skill_validation_report(
        yaml.safe_dump(
            {
                "name": "missing-tool",
                "when_to_use": ["When a tool is required."],
                "steps": [
                    {
                        "description": "Invoke the tool.",
                        "step_type": "invoke_tool",
                    }
                ],
            }
        ),
        source_path=Path("missing-tool.yaml"),
    )

    assert report.validation_successful is False
    assert [issue.code for issue in report.issues] == ["missing_pre_step"]


def test_skill_validation_rejects_unknown_step_type() -> None:
    report = build_skill_validation_report(
        "name: unknown-type\n"
        "when_to_use: [review]\n"
        "steps:\n"
        "- description: Review\n"
        "  step_type: unknown\n",
        source_path=Path("unknown-type.yaml"),
    )

    assert report.validation_successful is False
    assert [issue.code for issue in report.issues] == ["invalid_step_type_value"]


def test_predicated_step_requires_declared_completion_outputs() -> None:
    skill = skill_from_data(
        {
            "name": "predicated",
            "when_to_use": ["review"],
            "steps": [
                {
                    "id": "produce-result",
                    "description": "Produce the result.",
                    "step_type": "predicated",
                    "completion": {"required_outputs": ["result"]},
                    "outputs": [{"name": "result", "type": "object"}],
                }
            ],
        }
    )

    assert skill.steps[0].completion == SkillStepCompletion(("result",))
    assert skill.steps[0].to_data()["completion"] == {"required_outputs": ["result"]}


def test_predicated_step_rejects_missing_completion() -> None:
    report = build_skill_validation_report(
        "name: predicated\n"
        "when_to_use: [review]\n"
        "steps:\n"
        "- description: Produce the result.\n"
        "  step_type: predicated\n",
        source_path=Path("predicated.yaml"),
    )

    assert report.validation_successful is False
    assert [issue.code for issue in report.issues] == ["missing_completion"]


def test_skill_validation_rejects_empty_prompt_catalogs() -> None:
    report = build_skill_validation_report(
        "name: empty-catalogs\n"
        "when_to_use: [review]\n"
        "steps:\n"
        "- description: Review\n"
        "  prompt_catalogs: []\n",
        source_path=Path("empty-catalogs.yaml"),
    )

    assert report.validation_successful is False
    assert [issue.code for issue in report.issues] == ["empty_prompt_catalogs"]


def test_skill_directory_validation_accepts_references(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skill-definitions"
    skills_dir.mkdir()
    save_skill(
        Skill(
            name="specify-system",
            when_to_use=("When the system context should be gathered first.",),
            steps=(SkillStep(description="Capture the system shape."),),
        ),
        skills_dir / "specify-system.json",
    )
    save_skill(
        Skill(
            name="specify-a-feature",
            when_to_use=("When a feature should be walked through synchronously.",),
            steps=(
                SkillStep(
                    description="Gather the system context.",
                    uses_skill=SkillUsesSkill("specify-system"),
                ),
            ),
        ),
        skills_dir / "specify-a-feature.json",
    )

    report = build_skill_directory_validation_report(skills_dir)

    assert report.validation_successful is True
    assert report.skill_names == ["specify-a-feature", "specify-system"]
    assert json.loads(validate_skill_directory(skills_dir)) == {
        "validation_successful": True,
        "skill_names": ["specify-a-feature", "specify-system"],
        "skill_paths": [
            str(skills_dir / "specify-a-feature.json"),
            str(skills_dir / "specify-system.json"),
        ],
        "issues": [],
    }


def test_skill_validation_reports_yaml_parse_errors_as_yaml(tmp_path: Path) -> None:
    skill_path = tmp_path / "broken.yaml"
    report = build_skill_validation_report(
        "name: [\n",
        source_path=skill_path,
    )

    assert report.validation_successful is False
    assert report.issues[0].code == "invalid_yaml"
    assert "Could not parse YAML skill document" in report.issues[0].message


def test_skill_validation_accepts_adversarial_values() -> None:
    report = build_skill_validation_report(
        "name: adversarial\n"
        "adversarial: true\n"
        "when_to_use: [review]\n"
        "steps: [{description: challenge, actions: []}]\n",
        source_path=Path("adversarial.yaml"),
    )

    assert report.validation_successful is True

    assert (
        load_skill(
            Path(__file__).resolve().parents[1]
            / "skill-definitions"
            / "adversarial-pr-review.yaml"
        ).adversarial
        is True
    )


def test_skill_validation_accepts_inherited_adversarial_value() -> None:
    report = build_skill_validation_report(
        "name: inherited\n"
        "adversarial: null\n"
        "when_to_use: [review]\n"
        "steps: [{description: challenge, actions: []}]\n",
        source_path=Path("inherited.yaml"),
    )

    assert report.validation_successful is True


def test_skill_validation_rejects_non_boolean_adversarial_option() -> None:
    report = build_skill_validation_report(
        "name: adversarial\n"
        "adversarial: 'yes'\n"
        "when_to_use: [review]\n"
        "steps: [{description: challenge}]\n",
        source_path=Path("adversarial.yaml"),
    )

    assert report.validation_successful is False
    assert [issue.code for issue in report.issues] == ["invalid_adversarial_type"]


def test_skill_directory_validation_rejects_unknown_reference(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skill-definitions"
    skills_dir.mkdir()
    save_skill(
        Skill(
            name="specify-a-feature",
            when_to_use=("When a feature should be walked through synchronously.",),
            steps=(
                SkillStep(
                    description="Gather the system context.",
                    uses_skill=SkillUsesSkill("specify-system"),
                ),
            ),
        ),
        skills_dir / "specify-a-feature.json",
    )

    report = build_skill_directory_validation_report(skills_dir)

    assert report.validation_successful is False
    assert [issue.code for issue in report.issues] == ["missing_skill_reference"]
    assert report.issues[0].path == (
        f"{skills_dir / 'specify-a-feature.json'}.steps[0].uses_skill.skill"
    )


def test_specify_feature_skill_file_is_checked_in() -> None:
    skill_path = (
        Path(__file__).resolve().parents[1]
        / "skill-definitions"
        / "specify-a-feature.yaml"
    )
    skill = load_skill(skill_path)

    assert skill.name == "specify-a-feature"
    assert skill.when_to_use == (
        ("When the user needs a concrete feature plan."),
        ("When the flow must gather context and drive implementation."),
    )
    steps_by_id = {step.id: step for step in skill.steps if step.id is not None}

    def step(step_id: str) -> SkillStep:
        return steps_by_id[step_id]

    def command(step_id: str) -> list[str]:
        pre_step = step(step_id).pre_step
        assert pre_step is not None
        return list(pre_step.template["command"])

    assert step("capture-feature-name").step_type == "governed"
    assert step("capture-feature-name").outputs[0].name == "work_item_name"
    assert step("capture-feature-context").outputs[0].name == "feature_description"
    interview = step("conduct-design-interview")
    assert interview.step_type == "uses_skill"
    assert uses_skill_name(interview) == "design-interview"
    assert interview.uses_skill is not None
    assert [(binding.name, binding.ref) for binding in interview.uses_skill.inputs] == [
        ("work_item_name", "work_item_name"),
        ("feature_description", "feature_description"),
    ]
    stage = step("stage-specification-artifacts")
    assert stage.pre_step is not None
    assert stage.pre_step.template == {
        "tool": "git",
        "operation": "add",
        "paths": ["docs/proposals/<work-item-name>"],
    }
    assert uses_skill_name(step("prepare-pull-request")) == "finish-pr-prep"
    assert uses_skill_name(step("create-feature-pull-request")) == "create-pull-request"


def test_checked_in_skill_definitions_directory_is_valid() -> None:
    skills_dir = Path(__file__).resolve().parents[1] / "skill-definitions"
    report = build_skill_directory_validation_report(skills_dir)

    assert report.validation_successful is True
    assert report.skill_names == [
        "address-review-comments",
        "adversarial-pr-review",
        "bootstrap-code-structure",
        "create-pull-request",
        "dead-code-review",
        "design-interview",
        "feature-functionality-review",
        "feature-test-coverage-review",
        "finish-pr-prep",
        "fix-ci-failures",
        "fix-merge-conflicts",
        "handle-ad-hoc",
        "independent-skill-workflow-review",
        "review-architecture",
        "review-skill-workflow",
        "review-system",
        "run-tests-and-fix",
        "security-review",
        "spec-v1-design-review",
        "specify-a-feature",
        "specify-architecture",
        "specify-implementation",
        "specify-system",
        "start-implementing-feature",
    ]


def test_run_tests_and_fix_uses_deterministic_test_enrichment() -> None:
    skill = load_skill(
        Path(__file__).resolve().parents[1]
        / "skill-definitions"
        / "run-tests-and-fix.yaml"
    )
    steps = {step.id: step for step in skill.steps if step.id is not None}

    assert [step.id for step in skill.steps] == [
        "run-all-tests",
        "enrich-test-results",
        "diagnose-test-results",
        "produce-repair-edit",
        "validate-repair-edit",
        "repair-invalid-edit",
        "validate-repair-edit-gate",
        "apply-repair-edit",
        "rerun-all-tests",
    ]
    assert steps["run-all-tests"].pre_step is not None
    assert steps["run-all-tests"].outputs[0].name == "test_tool_result"
    assert steps["run-all-tests"].outputs[0].required_for_next_step
    assert steps["enrich-test-results"].pre_step is not None
    assert steps["enrich-test-results"].pre_step.template == {
        "tool": "enrich",
        "format": "pytest",
        "tool_output": {"source": "handoff", "name": "test_tool_result"},
    }
    assert steps["enrich-test-results"].inputs[0].name == "test_tool_result"
    assert steps["enrich-test-results"].outputs[0].name == "enriched_test_result"
    assert steps["enrich-test-results"].outputs[0].required_for_next_step
    assert steps["diagnose-test-results"].inputs[0].name == "enriched_test_result"
    assert steps["diagnose-test-results"].step_type == "governed"
    assert steps["diagnose-test-results"].completion is None
    assert steps["diagnose-test-results"].outputs[0].name == "test_diagnosis"
    assert steps["diagnose-test-results"].outputs[0].required_for_next_step
    assert steps["produce-repair-edit"].inputs[0].name == "test_diagnosis"
    assert steps["produce-repair-edit"].step_type == "predicated"
    assert steps["produce-repair-edit"].completion is not None
    assert steps["produce-repair-edit"].completion.required_outputs == ("repair_edit",)
    assert steps["produce-repair-edit"].outputs[0].name == "repair_edit"
    assert steps["produce-repair-edit"].outputs[0].required_for_next_step
    assert steps["validate-repair-edit"].pre_step is not None
    assert steps["validate-repair-edit"].pre_step.template == {
        "tool": "validate_edit",
        "edit": "<repair_edit>",
    }
    assert steps["validate-repair-edit"].outputs[0].name == "edit_validation"
    assert steps["validate-repair-edit"].outputs[0].required_for_next_step
    assert steps["repair-invalid-edit"].inputs[0].name == "repair_edit"
    assert steps["repair-invalid-edit"].inputs[1].name == "edit_validation"
    assert steps["repair-invalid-edit"].step_type == "predicated"
    assert steps["repair-invalid-edit"].completion is not None
    assert steps["repair-invalid-edit"].completion.required_outputs == ("repair_edit",)
    assert steps["repair-invalid-edit"].outputs[0].name == "repair_edit"
    assert steps["validate-repair-edit-gate"].gate is not None
    assert steps["validate-repair-edit-gate"].gate.goto_step == "repair-invalid-edit"
    assert steps["validate-repair-edit-gate"].pre_step is not None
    assert steps["validate-repair-edit-gate"].pre_step.template == {
        "tool": "validate_edit",
        "edit": "<repair_edit>",
    }
    assert steps["apply-repair-edit"].pre_step is not None
    assert steps["apply-repair-edit"].pre_step.template == {
        "tool": "apply_edit",
        "edit": "<repair_edit>",
    }
    assert steps["rerun-all-tests"].gate is not None
    assert steps["rerun-all-tests"].gate.goto_step == "enrich-test-results"
    assert steps["run-all-tests"].details is None
    assert steps["enrich-test-results"].details is None
    repair_details = steps["produce-repair-edit"].details or ""
    assert "read_document for every file" in repair_details
    assert "Do not guess line 1" in repair_details
    assert '"kind":"replace","start_line":12,"end_line":14' in repair_details


def test_repository_state_invocations_use_internal_tool() -> None:
    skills_dir = Path(__file__).resolve().parents[1] / "skill-definitions"

    for skill_path in sorted(skills_dir.glob("*.yaml")):
        skill = load_skill(skill_path)
        for step in skill.steps:
            for invocation in step.tool_invocations:
                if invocation.command == ("powdrr-lift", "repository-state"):
                    assert invocation.tool == "internal", skill_path


def test_checked_in_review_skill_definitions_exist() -> None:
    skills_dir = Path(__file__).resolve().parents[1] / "skill-definitions"
    assert (skills_dir / "address-review-comments.yaml").is_file()
    assert (skills_dir / "finish-pr-prep.yaml").is_file()
    assert (skills_dir / "feature-test-coverage-review.yaml").is_file()
    assert (skills_dir / "dead-code-review.yaml").is_file()
    assert (skills_dir / "adversarial-pr-review.yaml").is_file()
    assert (skills_dir / "review-architecture.yaml").is_file()
    assert (skills_dir / "review-system.yaml").is_file()


def test_review_skill_workflow_ends_with_pull_request_creation() -> None:
    skills_dir = Path(__file__).resolve().parents[1] / "skill-definitions"
    skill = load_skill(skills_dir / "review-skill-workflow.yaml")

    assert [step.description for step in skill.steps[-5:]] == [
        "Apply and validate the accepted definition.",
        "Confirm the reviewed definition changed before preparing a pull request.",
        "Stage the reviewed definition for pull-request preparation.",
        "Run final pull-request preparation checks.",
        "Create or update the pull request for the reviewed definition.",
    ]
    assert skill.steps[-4].tool_invocations[0].command == (
        "git",
        "diff",
        "--name-only",
        "--",
        "<target-definition-path>",
    )
    assert "choose `complete`" in (skill.steps[-4].details or "")
    assert skill.steps[-3].pre_step is not None
    assert skill.steps[-3].pre_step.template == {
        "tool": "git",
        "operation": "add",
        "paths": ["<target-definition-path>"],
    }
    assert uses_skill_name(skill.steps[-2]) == "finish-pr-prep"
    assert uses_skill_name(skill.steps[-1]) == "create-pull-request"
    assert "skill-workflow-review" in (skill.steps[-1].details or "")
    assert "pull-request URL" in (skill.steps[-1].details or "")


def test_pr_description_generators_are_used_by_pr_skills() -> None:
    skills_dir = Path(__file__).resolve().parents[1] / "skill-definitions"
    expected_kinds = {
        "specify-a-feature": "feature",
        "start-implementing-feature": "feature",
        "bootstrap-code-structure": "project-structure",
        "fix-ci-failures": "ci-fix",
        "fix-merge-conflicts": "merge-conflict",
        "address-review-comments": "review-comments",
    }

    for skill_name, kind in expected_kinds.items():
        skill = load_skill(skills_dir / f"{skill_name}.yaml")
        assert any(
            step.uses_skill is not None
            and uses_skill_name(step) == "create-pull-request"
            and kind in (step.details or "")
            and "files_to_publish" in (step.details or "")
            for step in skill.steps
        )


def test_create_pull_request_skill_has_prescribed_flow() -> None:
    skills_dir = Path(__file__).resolve().parents[1] / "skill-definitions"
    skill = load_skill(skills_dir / "create-pull-request.yaml")

    assert skill.name == "create-pull-request"
    assert [step.description for step in skill.steps] == [
        "Generate the pull request description template.",
        "Fill in the pull request description template.",
        "Inspect repository state before staging the pull request.",
        "Remove unintended files before staging.",
        "Confirm the worktree is clean after cleanup.",
        "Stage the exact files that belong in the pull request.",
        "Verify the staged pull request file set.",
        "Commit the validated changes.",
        "Push the committed changes.",
        "Create a draft pull request when none exists.",
        "Update the existing pull request.",
    ]
    gate = skill.steps[4]
    assert gate.pre_step is not None
    assert gate.pre_step.template["command"] == [
        "sh",
        "-c",
        'test -z "$(git diff --name-only)$(git ls-files --others --exclude-standard)"',
    ]
    assert "staged changes are allowed" in (gate.details or "").lower()
    assert skill.steps[0].pre_step is not None
    assert skill.steps[0].pre_step.action == "invoke_tool"
    assert skill.steps[0].pre_step.template["command"] == [
        "powdrr-lift",
        "pull-request-description",
        "--kind",
        "feature",
    ]
    assert skill.steps[0].details is None
    assert "do not print" in (skill.steps[1].details or "").lower()
    assert skill.steps[2].pre_step is not None
    assert skill.steps[2].pre_step.template["command"] == ["git", "status", "--short"]
    assert '"action":"delete_file"' in (skill.steps[3].details or "")
    assert skill.steps[5].tool_invocation_packages == ("git_readonly_and_additive",)
    assert any(
        invocation.operation == "add" for invocation in skill.steps[5].tool_invocations
    )
    assert skill.steps[6].pre_step is not None
    assert skill.steps[6].pre_step.template["command"] == [
        "git",
        "diff",
        "--cached",
        "--name-only",
    ]
    assert skill.steps[7].tool_invocations[-1].command == (
        "git",
        "commit",
        "-m",
        "<commit-message>",
    )
    assert skill.steps[8].tool_invocations[0].command == (
        "git",
        "push",
        "-u",
        "origin",
        "HEAD",
    )
    assert any(
        invocation.operation == "pr_create"
        for invocation in skill.steps[9].tool_invocations
    )
    assert any(
        invocation.operation == "pr_edit"
        for invocation in skill.steps[10].tool_invocations
    )


def test_checked_in_handle_ad_hoc_skill_matches_flow() -> None:
    skills_dir = Path(__file__).resolve().parents[1] / "skill-definitions"
    skill = load_skill(skills_dir / "handle-ad-hoc.yaml")

    assert skill.name == "handle-ad-hoc"
    assert [step.description for step in skill.steps] == [
        "Handle what the user asked for.",
        "Run finish-pr-prep when files changed.",
    ]
    assert "invoke finish-pr-prep" in (skill.steps[1].details or "")


def test_checked_in_address_review_comments_skill_matches_flow() -> None:
    skill_path = (
        Path(__file__).resolve().parents[1]
        / "skill-definitions"
        / "address-review-comments.yaml"
    )
    skill = load_skill(skill_path)

    assert skill.name == "address-review-comments"
    assert [step.description for step in skill.steps] == [
        "Inspect the pull request and collect its review comments.",
        "Classify each comment against the feature contract.",
        "Update the v1 specification for design-level feedback.",
        "Implement and test every actionable review correction.",
        "Run finish-pr-prep on the final staged changes.",
        "Commit and push the addressed changes to the existing pull request.",
    ]
    assert "resolved, outdated, and current comments" in (skill.steps[0].details or "")
    assert "design, entities, relationships" in (skill.steps[1].details or "")
    assert "system-specification" in (skill.steps[2].details or "")
    assert uses_skill_name(skill.steps[4]) == "finish-pr-prep"
    assert uses_skill_name(skill.steps[5]) == "create-pull-request"


def test_checked_in_feature_test_coverage_review_skill_matches_review_flow() -> None:
    skill_path = (
        Path(__file__).resolve().parents[1]
        / "skill-definitions"
        / "feature-test-coverage-review.yaml"
    )
    skill = load_skill(skill_path)

    assert skill.name == "feature-test-coverage-review"
    assert [step.description for step in skill.steps] == [
        "Discover the referenced pull request and its feature relationship.",
        "Select the feature test scope for the current pull request.",
        "Build the requested-test coverage matrix.",
        "Post every actionable test coverage finding inline.",
        "Verify the posted coverage review and report the audit result.",
    ]
    assert "every requested test" in (skill.steps[2].details or "")
    assert "unjustified scope finding" in (skill.steps[2].details or "")
    assert skill.steps[3].tool_invocations[0].command == (
        "gh",
        "api",
        "repos/<owner>/<repo>/pulls/<number>/comments",
        "--paginate",
    )


def test_checked_in_finish_pr_prep_skill_definition_matches_flow() -> None:
    skill_path = (
        Path(__file__).resolve().parents[1]
        / "skill-definitions"
        / "finish-pr-prep.yaml"
    )
    skill = load_skill(skill_path)

    assert skill.name == "finish-pr-prep"
    assert skill.when_to_use == (
        (
            "When staged changes must be validated immediately before creating "
            "or updating a pull request."
        ),
        (
            "When tests, formatting, lint, and type checks must be rerun after "
            "the final staged file set is known."
        ),
    )
    assert [step.description for step in skill.steps] == [
        "Deterministically inspect the staged file set.",
        "Confirm the staged pull request scope.",
        "Discover validation tools for the staged languages.",
        "Run the final formatting, lint, type-check, and test passes.",
        "Leave the branch ready for pull request creation.",
    ]
    assert skill.steps[0].pre_step is not None
    assert skill.steps[0].pre_step.template == {
        "tool": "git",
        "operation": "status",
    }
    assert skill.steps[1].tool_invocations[0].tool == "shell"
    assert skill.steps[1].tool_invocations[0].command == (
        "git",
        "diff",
        "--cached",
        "--stat",
    )
    assert skill.steps[2].tool_invocations == ()
    assert skill.steps[3].inputs[0].name == "validation_tool_obligations"
    assert skill.steps[3].validation_gate is not None
    assert skill.steps[4].pre_step is not None
    assert skill.steps[4].pre_step.action == "invoke_tool"


def test_checked_in_start_implementing_feature_skill_definition_matches_flow() -> None:
    skills_dir = Path(__file__).resolve().parents[1] / "skill-definitions"
    skill = load_skill(skills_dir / "start-implementing-feature.yaml")

    assert skill.name == "start-implementing-feature"
    steps = {step.id: step for step in skill.steps if step.id is not None}

    def step(step_id: str) -> SkillStep:
        return steps[step_id]

    def pre_step_command(step_id: str) -> tuple[str, ...]:
        pre_step = step(step_id).pre_step
        assert pre_step is not None
        return tuple(pre_step.template["command"])

    assert step("capture-feature-query").outputs[0].name == "feature_query"
    assert step("capture-feature-query").outputs[0].required_for_next_step
    assert pre_step_command("discover-proposed-feature") == (
        "fuzzy-match",
        "docs/proposals",
        "-name",
        "<feature-query>",
        "-type",
        "d",
        "-maxdepth",
        "2",
        "-print",
    )
    assert pre_step_command("discover-current-feature") == (
        "fuzzy-match",
        "docs/current",
        "-name",
        "<feature-query>",
        "-type",
        "d",
        "-maxdepth",
        "2",
        "-print",
    )
    assert pre_step_command("discover-feature-workflows") == (
        "fuzzy-match",
        "docs/workflows",
        "-name",
        "<feature-query>",
        "-type",
        "d",
        "-maxdepth",
        "3",
        "-print",
    )
    for step_id, output_name in (
        ("discover-proposed-feature", "proposed_feature_candidates"),
        ("discover-current-feature", "current_feature_candidates"),
        ("discover-feature-workflows", "workflow_candidates"),
    ):
        discovery_step = step(step_id)
        assert discovery_step.step_type == "invoke_tool"
        assert discovery_step.actions == ()
        assert discovery_step.actions_declared
        assert discovery_step.pre_step is not None
        assert discovery_step.pre_step.template["tool"] == "fuzzy-match"
        assert discovery_step.outputs[0].name == output_name
        assert discovery_step.outputs[0].required_for_next_step
    assert step("select-feature-context").step_type == "governed"
    assert step("select-feature-context").outputs[0].name == "feature_name"
    assert step("select-feature-context").outputs[0].required_for_next_step
    assert [output.name for output in step("select-feature-context").outputs] == [
        "feature_name",
        "specification_documents",
        "workflow_documents",
        "missing_documents",
    ]
    select_details = step("select-feature-context").details
    assert select_details is not None
    assert "directory's basename as the canonical feature_name" in select_details
    assert "docs/workflows/<canonical-feature-name>" in select_details
    assert "do not ask the user which workflow path to use" in select_details
    assert step("bootstrap-project-structure").uses_skill is None
    assert step("bootstrap-project-structure").pre_step is not None
    assert step("generate-proposed-pr-specification").step_type == "invoke_tool"
    assert step("plan-proposed-prs").outputs[0].name == "proposed_pr_names"
    assert step("plan-proposed-prs").outputs[0].required_for_next_step
    assert step("plan-proposed-prs").actions == ("invoke_tool", "read_document")
    plan_details = step("plan-proposed-prs").details
    assert plan_details is not None
    assert "planning-only step" in plan_details
    assert "Do not create, edit, rename, or delete" in plan_details
    assert "basedpyright-structure" in plan_details
    assert "basedpyright-symbol" in plan_details
    assert step("generate-proposed-pr-specification").pre_step is not None
    assert pre_step_command("generate-proposed-pr-specification") == (
        "powdrr-lift",
        "pr-specification",
        "--work-item-name",
        "<feature-name>",
        "--output",
        "docs/proposals/<feature-name>/proposed-pr-specification.yaml",
    )
    planning_step = step("plan-proposed-pr-specification")
    assert planning_step.actions == ("read_document",)
    assert planning_step.outputs[0].name == "proposed_pr_plan"
    assert planning_step.outputs[0].required_for_next_step
    assert planning_step.outputs[0].schema is not None
    assert planning_step.outputs[0].schema["additionalProperties"] is False
    assert planning_step.details is not None
    assert "planning-only judgment" in planning_step.details
    assert "Do not edit YAML" in planning_step.details
    assert "authoritative effects" in planning_step.details
    load_effects_step = step("load-authoritative-pr-effects")
    assert load_effects_step.step_type == "invoke_tool"
    assert pre_step_command("load-authoritative-pr-effects") == (
        "powdrr-lift",
        "authoritative-pr-effects",
        "--work-item-name",
        "<feature-name>",
    )
    assert load_effects_step.outputs[0].name == "authoritative_effects"
    allocation_step = step("allocate-proposed-pr-effects")
    assert allocation_step.outputs[0].name == "effect_allocation"
    assert allocation_step.outputs[0].schema is not None
    assert allocation_step.outputs[0].schema["additionalProperties"] is False
    assert allocation_step.details is not None
    assert "Never return, copy, or invent section, id, action" in (
        allocation_step.details
    )
    repair_step = step("repair-proposed-pr-specification")
    assert repair_step.actions == ("read_document", "goto_step")
    assert repair_step.next_step_override == "evaluate-proposed-pr-specification"
    assert repair_step.details is not None
    assert "Do not repair YAML" in repair_step.details
    assert "plan-proposed-pr-specification" in repair_step.details
    assert "allocate-proposed-pr-effects" in repair_step.details
    assert step("evaluate-proposed-pr-specification").step_type == "gate"
    assert pre_step_command("evaluate-proposed-pr-specification") == (
        "powdrr-lift",
        "evaluate",
        "docs/proposals/<feature-name>",
    )
    gate = step("evaluate-proposed-pr-specification").gate
    assert gate is not None
    assert gate.goto_step == "repair-proposed-pr-specification"
    assert gate.success_goto_step == "plan-workflow-instantiation"
    assert step("instantiate-execution-workflows").tool_invocations[0].command == (
        "powdrr-lift",
        "instantiate-workflow",
        "--work-item-name",
        "<feature-name>",
        "--workflow-instance-name",
        "<proposed-pr-name>",
        "--template-value",
        "proposed-pr-id=<proposed-pr-name>",
        "--template-value",
        "verification-command=<verification-command>",
        "--template",
        "templates/execute-proposed-pr.yaml",
    )
    instantiate_details = step("instantiate-execution-workflows").details
    assert instantiate_details is not None
    assert "Do not pass workflow dependencies manually" in instantiate_details
    assert "unified proposed-PR specification" in instantiate_details
    assert "workflow_instantiation_report" in instantiate_details
    assert '"outputs":{"workflow_instantiation_report"' in instantiate_details
    instantiate_output = step("instantiate-execution-workflows").outputs[0]
    assert instantiate_output.name == "workflow_instantiation_report"
    assert instantiate_output.required_for_next_step
    assert instantiate_output.schema is not None
    assert instantiate_output.schema["required"] == ["workflows"]
    workflow_output_schema = instantiate_output.schema["properties"]["workflows"]
    assert workflow_output_schema["items"]["required"] == [
        "workflow_id",
        "workflow_directory",
        "integration_branch",
        "integration_worktree",
        "task_count",
        "reused",
    ]
    dependency_step = step("verify-workflow-dependencies")
    assert dependency_step.step_type == "governed"
    assert dependency_step.inputs[0].name == "workflow_instantiation_report"
    assert dependency_step.actions == ("read_document", "goto_step")
    assert [output.name for output in dependency_step.outputs] == [
        "workflow_dependency_report"
    ]
    dependency_output = dependency_step.outputs[0]
    assert dependency_output.required_for_next_step
    assert dependency_output.schema is not None
    assert dependency_output.schema["additionalProperties"] is False
    assert dependency_output.schema["required"] == ["workflows", "all_match"]
    workflow_schema = dependency_output.schema["properties"]["workflows"]
    assert workflow_schema["type"] == "array"
    workflow_item_schema = workflow_schema["items"]
    assert workflow_item_schema["required"] == [
        "workflow_id",
        "path",
        "expected_dependencies",
        "actual_dependencies",
        "matches",
    ]
    dependency_details = dependency_step.details
    assert dependency_details is not None
    assert "depends_on_workflows" in dependency_details
    assert "Do not infer dependencies from ordering" in dependency_details
    assert "step_id `plan-workflow-instantiation`" in dependency_details
    assert "workflow_dependency_report" in dependency_details
    assert '"outputs":{"workflow_dependency_report"' in dependency_details
    review_details = step("review-workflow-proposed-pr-links").details
    assert review_details is not None
    assert "every proposed PR has exactly one matching workflow" in review_details
    assert "every workflow has exactly one matching proposed PR" in review_details
    assert "proposed PR -> workflow" in review_details
    assert "workflow -> proposed PR" in review_details
    assert "do not infer matches from ordering" in review_details
    assert '"action":"goto_step"' in review_details
    assert '"step_id":"plan-workflow-instantiation"' in review_details
    assert '"step_id":"plan-proposed-prs"' in review_details
    assert '"action":"next_step"' in review_details
    assert pre_step_command("inspect-workflow-repository-state") == (
        "powdrr-lift",
        "repository-state",
    )
    stage_step = step("stage-validated-artifacts")
    assert stage_step.step_type == "invoke_tool"
    assert stage_step.actions == ()
    assert stage_step.pre_step is not None
    assert stage_step.pre_step.template == {
        "tool": "git",
        "operation": "add",
        "paths": [
            "docs/proposals/<feature-name>",
            "docs/workflows",
        ],
    }
    assert pre_step_command("verify-staged-artifacts") == (
        "powdrr-lift",
        "repository-state",
    )
    prepare_step = step("prepare-feature-pull-request")
    assert prepare_step.step_type == "predicated"
    assert prepare_step.actions == ()
    assert prepare_step.completion is not None
    assert prepare_step.completion.required_outputs == (
        "final_repository_state",
        "readiness_report",
    )
    assert [output.name for output in prepare_step.outputs] == [
        "final_repository_state",
        "readiness_report",
    ]
    assert all(output.required_for_next_step for output in prepare_step.outputs)
    assert prepare_step.outputs[0].schema == {
        "type": "object",
        "required": ["clean", "files"],
        "properties": {
            "root": {"type": "string"},
            "branch": {"type": "string"},
            "upstream": {"type": "string"},
            "ahead": {"type": "integer"},
            "behind": {"type": "integer"},
            "clean": {"type": "boolean"},
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "path",
                        "staged",
                        "unstaged",
                        "untracked",
                        "conflicted",
                    ],
                    "properties": {
                        "path": {"type": "string"},
                        "index_status": {"type": "string"},
                        "worktree_status": {"type": "string"},
                        "staged": {"type": "boolean"},
                        "unstaged": {"type": "boolean"},
                        "untracked": {"type": "boolean"},
                        "conflicted": {"type": "boolean"},
                    },
                },
            },
        },
    }
    assert prepare_step.outputs[1].schema == {
        "type": "object",
        "additionalProperties": False,
        "required": ["ready", "reasons", "satisfied_requirements"],
        "properties": {
            "ready": {"type": "boolean"},
            "reasons": {"type": "array", "items": {"type": "string"}},
            "satisfied_requirements": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
    }
    prepare_details = prepare_step.details
    assert prepare_details is not None
    assert '"action":"emit_outputs"' in prepare_details
    assert "copying the exact values" in prepare_details
    assert uses_skill_name(step("create-feature-pull-request")) == "create-pull-request"


def test_checked_in_bootstrap_skill_verifies_discovered_tools() -> None:
    skills_dir = Path(__file__).resolve().parents[1] / "skill-definitions"
    skill = load_skill(skills_dir / "bootstrap-code-structure.yaml")

    discovery_details = skill.steps[1].details
    assert discovery_details is not None
    assert "try its command with the smallest safe invocation" in discovery_details
    assert "correct the command, and retry it" in discovery_details
    assert "Do not pass an untested or failing command" in discovery_details
    populate_details = skill.steps[3].details
    assert populate_details is not None
    assert "Verify every tool command against the successful invocation evidence" in (
        populate_details
    )


def test_checked_in_bootstrap_skill_reuses_existing_artifact_before_discovery() -> None:
    skills_dir = Path(__file__).resolve().parents[1] / "skill-definitions"
    skill = load_skill(skills_dir / "bootstrap-code-structure.yaml")

    existence_check = skill.steps[0]
    assert existence_check.description == (
        "Check whether the project-structure artifact already exists."
    )
    assert existence_check.details is not None
    assert "choose complete immediately" in existence_check.details
    assert "choose next_step" in existence_check.details
    assert "docs/project_structure/project-structure.yaml" in existence_check.details
    assert skill.steps[1].description == (
        "Discover the project-wide modules, tools, tests, and development commands."
    )


def test_checked_in_review_system_skill_definition_matches_review_flow() -> None:
    skill_path = (
        Path(__file__).resolve().parents[1] / "skill-definitions" / "review-system.yaml"
    )
    skill = load_skill(skill_path)

    assert skill.name == "review-system"
    assert skill.when_to_use == (
        "When new needs may require updating the current system specification.",
        (
            "When the TUI should evaluate whether the system requirements and "
            "approach still fit."
        ),
    )
    steps_by_id = {step.id: step for step in skill.steps if step.id is not None}
    assert steps_by_id["generate-system"].step_type == "invoke_tool"
    assert steps_by_id["fill-system"].step_type == "governed"
    assert steps_by_id["evaluate-system"].step_type == "invoke_tool"
    assert steps_by_id["gate-system"].step_type == "gate"
    assert steps_by_id["generate-system"].pre_step is not None
    assert tuple(steps_by_id["generate-system"].pre_step.template["command"]) == (
        "powdrr-lift",
        "system-specification",
        "--work-item-name",
        "<work-item-name>",
    )
    assert steps_by_id["evaluate-system"].pre_step is not None
    assert tuple(steps_by_id["evaluate-system"].pre_step.template["command"]) == (
        "powdrr-lift",
        "evaluate",
        "docs/proposals/<work-item-name>/system-specification.yaml",
    )


def test_checked_in_review_architecture_skill_definition_matches_review_flow() -> None:
    skill_path = (
        Path(__file__).resolve().parents[1]
        / "skill-definitions"
        / "review-architecture.yaml"
    )
    skill = load_skill(skill_path)

    assert skill.name == "review-architecture"
    assert skill.when_to_use == (
        "When new needs may require updating the current architecture specification.",
        (
            "When the TUI should evaluate whether the architecture still fits "
            "the system needs."
        ),
    )
    steps_by_id = {step.id: step for step in skill.steps if step.id is not None}
    assert steps_by_id["generate-architecture"].step_type == "invoke_tool"
    assert steps_by_id["fill-architecture"].step_type == "governed"
    assert steps_by_id["evaluate-architecture"].step_type == "invoke_tool"
    assert steps_by_id["gate-architecture"].step_type == "gate"
    assert steps_by_id["generate-architecture"].pre_step is not None
    assert tuple(steps_by_id["generate-architecture"].pre_step.template["command"]) == (
        "powdrr-lift",
        "architecture-specification",
        "--work-item-name",
        "<work-item-name>",
        "--all-entity-types",
    )
    assert steps_by_id["evaluate-architecture"].pre_step is not None
    assert tuple(steps_by_id["evaluate-architecture"].pre_step.template["command"]) == (
        "powdrr-lift",
        "evaluate",
        "docs/proposals/<work-item-name>/architecture-specification.yaml",
    )
