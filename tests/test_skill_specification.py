from __future__ import annotations

import json
from pathlib import Path

import yaml

from powdrr_lift.core import (
    Skill,
    SkillStep,
    SkillStepGate,
    SkillStepInput,
    SkillStepOutput,
    SkillStepPreStep,
    SkillToolInvocation,
    build_skill_directory_validation_report,
    build_skill_validation_report,
    load_skill,
    save_skill,
    skill_from_json,
    skill_to_json,
    validate_skill_directory,
)


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
            ),
            SkillStep(
                description="Pull in the system context.",
                details="Use the system spec and related context.",
                uses_skills=("specify-system",),
                prompt_catalogs=("context_types", "skills"),
                tool_invocations=(
                    SkillToolInvocation(
                        tool="internal",
                        command=(
                            "powdrr-lift",
                            "system-specification",
                            "--work-item-name",
                            "<work-item-name>",
                        ),
                    ),
                ),
            ),
            SkillStep(description="Summarize the result."),
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
                "step_type": "freeform",
                "id": "capture-goal",
                "details": "Record the user-visible outcome first.",
            },
            {
                "description": "Pull in the system context.",
                "step_type": "freeform",
                "details": "Use the system spec and related context.",
                "uses_skills": ["specify-system"],
                "prompt_catalogs": ["context_types", "skills"],
                "tool_invocations": [
                    {
                        "tool": "internal",
                        "command": [
                            "powdrr-lift",
                            "system-specification",
                            "--work-item-name",
                            "<work-item-name>",
                        ],
                    }
                ],
            },
            {
                "description": "Summarize the result.",
                "step_type": "freeform",
            },
        ],
    }


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
                ("finish-pr-prep.yaml", 3),
                ("create-pull-request.yaml", 0),
                ("specify-system.yaml", 1),
                ("specify-system.yaml", 3),
                ("specify-system.yaml", 3),
                ("specify-architecture.yaml", 1),
                ("specify-architecture.yaml", 3),
                ("specify-architecture.yaml", 3),
                ("specify-implementation.yaml", 1),
                ("specify-implementation.yaml", 3),
                ("specify-implementation.yaml", 3),
                ("execute-proposed-pr.yaml", 0),
                ("specify-architecture.yaml", 3),
            }
            expected_gate_steps = {("specify-architecture.yaml", 5)}
            expected_gate_steps = {
                ("specify-system.yaml", 5),
                ("specify-architecture.yaml", 5),
                ("specify-implementation.yaml", 5),
            }
            expected_step_type = (
                "invoke_tool"
                if (path.name, index) in expected_invoke_tool_steps
                else "gate"
                if (path.name, index) in expected_gate_steps
                else "gate"
                if (path.name, index) in expected_gate_steps
                else "freeform"
            )
            assert step["step_type"] == expected_step_type, (
                f"{path}:{step_key}[{index}]"
            )
            if "prompt_catalogs" in step:
                assert step["prompt_catalogs"], f"{path}:{step_key}[{index}]"
                assert set(step["prompt_catalogs"]) <= {"context_types", "skills"}


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
        "- id: repeat\n"
        "  description: Second\n",
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
                    "obligations": {"action_field": "validation_action"},
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
                    uses_skills=("specify-system",),
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
        "steps: [{description: challenge}]\n",
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
        "steps: [{description: challenge}]\n",
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
                    uses_skills=("specify-system",),
                ),
            ),
        ),
        skills_dir / "specify-a-feature.json",
    )

    report = build_skill_directory_validation_report(skills_dir)

    assert report.validation_successful is False
    assert [issue.code for issue in report.issues] == ["missing_skill_reference"]
    assert report.issues[0].path == (
        f"{skills_dir / 'specify-a-feature.json'}.steps[0].uses_skills[0]"
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
    assert [step.description for step in skill.steps] == [
        "Capture the feature name, goal, and success criteria.",
        "Generate the system template and fill it out.",
        "Review the system context before deciding the feature shape.",
        "Generate the architecture template and fill it out.",
        "Review architecture before implementation.",
        "Generate the implementation template and fill it out.",
        "Decide on proposed PRs and fill each template.",
        "Validate every generated specification before implementation.",
        (
            "If any specification validation fails, fix it and return to the "
            "validation step."
        ),
        "Stage the validated specification artifacts for pull request preparation.",
        "Run finish-pr-prep before creating the pull request.",
        "Create or update the pull request for the validated feature.",
    ]
    for step in skill.steps:
        assert step.details is not None
    first_step_details = skill.steps[0].details
    assert first_step_details is not None
    assert "exact feature name as the work-item name" in first_step_details
    assert skill.steps[2].uses_skills == ("review-system",)
    assert skill.steps[1].tool_invocations[0].command == (
        "powdrr-lift",
        "system-specification",
        "--work-item-name",
        "<work-item-name>",
    )
    assert skill.steps[2].tool_invocations[0].command == (
        "powdrr-lift",
        "evaluate",
        "docs/proposals/<work-item-name>",
    )
    assert skill.steps[3].tool_invocations[0].command == (
        "powdrr-lift",
        "architecture-specification",
        "--work-item-name",
        "<work-item-name>",
        "--entity-type",
        "<type>",
    )
    assert skill.steps[4].uses_skills == ("review-architecture",)
    assert "choose `next_step` immediately" in (skill.steps[2].details or "")
    assert "choose `next_step` immediately" in (skill.steps[4].details or "")
    assert "invoke its validator once" in (skill.steps[5].details or "")
    assert "choose `next_step` immediately" in (skill.steps[7].details or "")
    assert skill.steps[4].tool_invocations[0].command == (
        "powdrr-lift",
        "evaluate",
        "docs/proposals/<work-item-name>",
    )
    assert skill.steps[5].tool_invocations[0].command == (
        "powdrr-lift",
        "implementation-specification",
        "--work-item-name",
        "<work-item-name>",
    )
    assert skill.steps[6].tool_invocations[0].command == (
        "powdrr-lift",
        "pr-specification",
        "--work-item-name",
        "<work-item-name>",
    )
    assert [invocation.command for invocation in skill.steps[7].tool_invocations] == [
        (
            "powdrr-lift",
            "evaluate",
            "docs/proposals/<work-item-name>",
        ),
    ]
    assert [invocation.command for invocation in skill.steps[9].tool_invocations] == [
        ("powdrr-lift", "repository-state"),
        ("git", "add", "docs/proposals/<work-item-name>"),
    ]
    assert skill.steps[10].uses_skills == ("finish-pr-prep",)
    assert "invoke_skill" in (skill.steps[10].details or "")
    assert "create-pull-request" in (skill.steps[11].details or "")
    assert skill.steps[11].uses_skills == ("create-pull-request",)


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
        "security-review",
        "specify-a-feature",
        "specify-architecture",
        "specify-implementation",
        "specify-system",
        "start-implementing-feature",
    ]


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
    assert skill.steps[-3].tool_invocations[0].command == (
        "git",
        "add",
        "<target-definition-path>",
    )
    assert skill.steps[-2].uses_skills == ("finish-pr-prep",)
    assert skill.steps[-1].uses_skills == ("create-pull-request",)
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
            "create-pull-request" in (step.uses_skills or ())
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
        "Stage the exact files that belong in the pull request.",
        "Commit the validated changes.",
        "Push the committed changes.",
        "Create a draft pull request when none exists.",
        "Update the existing pull request.",
    ]
    assert skill.steps[0].pre_step is not None
    assert skill.steps[0].pre_step.action == "invoke_tool"
    assert skill.steps[0].pre_step.template["command"] == [
        "powdrr-lift",
        "pull-request-description",
        "--kind",
        "feature",
    ]
    assert "do not print" in (skill.steps[0].details or "").lower()
    assert "do not print" in (skill.steps[1].details or "").lower()
    assert "files_to_publish" in (skill.steps[2].details or "")
    assert "git diff --cached --name-only" in (skill.steps[2].details or "")
    assert skill.steps[2].tool_invocations[0].command == (
        "git",
        "add",
        "<files-to-publish>",
    )
    assert skill.steps[3].tool_invocations[-1].command == (
        "git",
        "commit",
        "-m",
        "<commit-message>",
    )
    assert skill.steps[4].tool_invocations[0].command == (
        "git",
        "push",
        "-u",
        "origin",
        "HEAD",
    )
    assert skill.steps[5].tool_invocations[0].command[:3] == (
        "gh",
        "pr",
        "create",
    )
    assert skill.steps[6].tool_invocations[0].command[:3] == (
        "gh",
        "pr",
        "edit",
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
    assert skill.steps[4].uses_skills == ("finish-pr-prep",)
    assert skill.steps[5].uses_skills == ("create-pull-request",)


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
        "Confirm the staged pull request scope and validation commands.",
        "Run the final formatting, lint, type-check, and test passes.",
        "Leave the branch ready for pull request creation.",
    ]
    assert skill.steps[0].pre_step is not None
    assert skill.steps[0].pre_step.template["command"] == [
        "git",
        "diff",
        "--cached",
        "--name-only",
    ]
    assert [
        (invocation.tool, invocation.label)
        for invocation in skill.steps[1].tool_invocations
    ][:1] == [("ref", "pr-prep")]
    assert skill.steps[2].tool_invocations == ()
    assert skill.steps[3].pre_step is not None
    assert skill.steps[3].pre_step.action == "invoke_tool"


def test_checked_in_start_implementing_feature_skill_definition_matches_flow() -> None:
    skills_dir = Path(__file__).resolve().parents[1] / "skill-definitions"
    skill = load_skill(skills_dir / "start-implementing-feature.yaml")

    assert skill.name == "start-implementing-feature"
    first_step_details = skill.steps[0].details
    assert first_step_details is not None
    assert "docs/proposals" in first_step_details
    assert "canonical feature name" in first_step_details
    assert "fuzzy-match tool" in first_step_details
    bootstrap_step_details = skill.steps[1].details
    assert bootstrap_step_details is not None
    assert "bootstrap-code-structure" in bootstrap_step_details
    assert skill.steps[1].uses_skills == ("bootstrap-code-structure",)
    assert "Filter the results to the best matching" in first_step_details
    assert "If the best match is uncertain" in first_step_details
    assert "Do not ask whether documents exist before searching" in first_step_details
    implementation_step_details = skill.steps[2].details
    assert implementation_step_details is not None
    assert "only generates the files" in implementation_step_details
    fill_step_details = skill.steps[3].details
    assert fill_step_details is not None
    assert "yaml_edit" in fill_step_details
    assert "Remove all generator instructions" in fill_step_details
    validation_step_details = skill.steps[4].details
    assert validation_step_details is not None
    assert "spec-v1" in validation_step_details
    assert "yaml_edit" in validation_step_details
    workflow_step_details = skill.steps[5].details
    assert workflow_step_details is not None
    assert "templates/execute-proposed-pr.yaml" in workflow_step_details
    assert "active worktree" in workflow_step_details
    verify_step_details = skill.steps[6].details
    assert verify_step_details is not None
    assert "only below .worktrees/" in verify_step_details
    pr_step_details = skill.steps[9].details
    assert pr_step_details is not None
    assert "must invoke create-pull-request" in pr_step_details
    assert "returned PR URL" in pr_step_details

    assert skill.steps[0].tool_invocations[0].tool == "fuzzy-match"
    assert skill.steps[0].tool_invocations[0].command == (
        "fuzzy-match",
        "docs/proposals",
        "-name",
        "<feature-name>",
        "-type",
        "d",
        "-maxdepth",
        "2",
        "-print",
    )
    assert skill.steps[0].tool_invocations[1].command == (
        "fuzzy-match",
        "docs/current",
        "-name",
        "<feature-name>",
        "-type",
        "d",
        "-maxdepth",
        "2",
        "-print",
    )
    assert skill.steps[0].tool_invocations[2].command == (
        "fuzzy-match",
        "docs/workflows",
        "-name",
        "<feature-name>",
        "-type",
        "d",
        "-maxdepth",
        "3",
        "-print",
    )
    assert [step.description for step in skill.steps] == [
        "Discover the feature specification and execution workflows.",
        "Bootstrap the project-wide module and tool structure from codebase evidence.",
        "Generate implementation specification templates for the proposed PRs.",
        "Fill every generated implementation specification completely.",
        (
            "Validate every implementation specification before creating "
            "execution workflows."
        ),
        "Instantiate an execution workflow for every validated proposed PR.",
        "Verify workflow artifacts are in the active worktree.",
        "Stage the validated artifacts for pull request preparation.",
        "Run finish-pr-prep before creating the draft pull request.",
        "Commit the validated artifacts and open a draft pull request.",
        "Hand the draft pull request to the user for review.",
    ]
    assert [step.llm_type for step in skill.steps] == [
        "standard_reasoning",
        "standard_reasoning",
        "high_reasoning",
        "high_reasoning",
        "fast_iteration",
        "simple_task",
        "fast_iteration",
        "simple_task",
        "fast_iteration",
        "simple_task",
        "standard_reasoning",
    ]
    assert skill.steps[2].tool_invocations[0].command == (
        "powdrr-lift",
        "implementation-specification",
        "--work-item-name",
        "<feature-name>",
        "--output",
        "docs/proposals/<feature-name>/<proposed-pr-name>-implementation-specification.yaml",
    )
    assert skill.steps[4].tool_invocations[0].command == (
        "powdrr-lift",
        "evaluate",
        "docs/proposals/<feature-name>",
    )
    assert skill.steps[5].tool_invocations[0].command == (
        "powdrr-lift",
        "instantiate-workflow",
        "--work-item-name",
        "<feature-name>",
        "--workflow-instance-name",
        "<proposed-pr-name>",
        "--template-value",
        "proposed-pr-id=<proposed-pr-name>",
        "--template",
        "templates/execute-proposed-pr.yaml",
    )
    assert "dependencies" in (skill.steps[5].details or "")
    assert "If an execution workflow already exists for every proposed PR" in (
        skill.steps[5].details or ""
    )
    assert "invoke the workflow instantiation tool" in (skill.steps[5].details or "")
    assert [invocation.command for invocation in skill.steps[7].tool_invocations] == [
        ("powdrr-lift", "repository-state"),
        ("git", "add", "docs/proposals/<feature-name>", "docs/workflows"),
    ]
    assert skill.steps[8].uses_skills == ("finish-pr-prep",)
    assert skill.steps[9].uses_skills == ("create-pull-request",)


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
    assert [step.description for step in skill.steps] == [
        "Gather the requirements and approach context.",
        "Decide whether the current system specification needs to change.",
        (
            "Generate and fill the system specification template only when "
            "changes are required."
        ),
        "Validate the updated system specification and confirm it is still consistent.",
    ]
    assert skill.steps[0].details == (
        "Use the requirements and approach context to understand the new needs "
        "before judging the system specification."
    )
    assert skill.steps[1].details == (
        "Compare the gathered requirements and approach against the new needs. "
        "If the existing spec already covers them, report that no update is "
        "needed and stop."
    )
    assert skill.steps[2].tool_invocations[0].command == (
        "powdrr-lift",
        "system-specification",
        "--work-item-name",
        "<work-item-name>",
    )
    assert skill.steps[3].details == (
        "Invoke the generic evaluator exactly once after the specification "
        "edits. If it succeeds, use its result to confirm that no inconsistencies "
        "remain and choose `next_step` immediately. If it reports validation "
        "errors, edit the specification to address them before invoking the "
        "validator again; never repeat the same validation command unchanged."
    )
    assert skill.steps[3].tool_invocations[0].command == (
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
    assert [step.description for step in skill.steps] == [
        "Gather the entities, entity relationships, invariants, and guidance context.",
        "Decide whether the current architecture specification needs to change.",
        (
            "Generate and fill the architecture specification template only "
            "when changes are required."
        ),
        (
            "Validate the updated architecture specification and confirm it is "
            "still consistent."
        ),
    ]
    assert skill.steps[0].details == (
        "Use the architecture context to understand the current model before "
        "judging whether it needs to change."
    )
    assert skill.steps[1].details == (
        "Compare the gathered entity model against the new needs. If the "
        "existing spec already covers them, report that no update is needed "
        "and stop."
    )
    assert skill.steps[3].details == (
        "Invoke the generic evaluator exactly once after the "
        "specification edits. If it succeeds, use its result to confirm that "
        "no inconsistencies remain and choose `next_step` immediately. If it "
        "reports validation errors, edit the specification to address them "
        "before invoking the validator again; never repeat the same validation "
        "command unchanged."
    )
    assert skill.steps[2].tool_invocations[0].command == (
        "powdrr-lift",
        "architecture-specification",
        "--work-item-name",
        "<work-item-name>",
        "--entity-type",
        "<type>",
    )
    assert skill.steps[3].tool_invocations[0].command == (
        "powdrr-lift",
        "evaluate",
        "docs/proposals/<work-item-name>/architecture-specification.yaml",
    )
