from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from powdrr_lift.process.model import (
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


def test_checked_in_workflow_templates_declare_prompt_catalogs() -> None:
    repository_root = Path(__file__).parents[1]
    definition_paths = sorted((repository_root / "templates").glob("*.yaml"))

    assert definition_paths
    for path in definition_paths:
        document = yaml.safe_load(path.read_text())
        step_key = "task_templates"
        for index, step in enumerate(document[step_key]):
            assert step.get("step_type"), f"{path}:{step_key}[{index}]"
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


def test_checked_in_procedrr_catalog_contains_migrated_root_workflows() -> None:
    from procedrr import ProcessCatalog

    process_dir = (
        Path(__file__).resolve().parents[1] / "docs" / "procedrr" / "processes"
    )
    catalog = ProcessCatalog.load(process_dir)
    names = {process.name for process in catalog.list()}

    assert {
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
    } <= names


def test_branch_step_is_typed_and_round_trips() -> None:
    skill = skill_from_data(
        {
            "name": "routing",
            "when_to_use": ["route a decision"],
            "steps": [
                {
                    "id": "route",
                    "description": "Route the typed decision.",
                    "step_type": "branch",
                    "branch": {
                        "cases": [
                            {
                                "path": "decision.kind",
                                "equals": "resolved",
                                "goto_step": "done",
                            }
                        ],
                        "default_goto_step": "ask",
                    },
                },
                {"id": "done", "description": "Done."},
                {"id": "ask", "description": "Ask."},
            ],
        }
    )
    branch = skill.steps[0].branch
    assert branch is not None
    assert branch.cases[0].goto_step == "done"
    assert skill_from_data(skill.to_data()).steps[0].branch == branch
