from __future__ import annotations

import json
from pathlib import Path

from powdrr_lift.core import (
    Skill,
    SkillStep,
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
                details="Record the user-visible outcome first.",
            ),
            SkillStep(
                description="Pull in the system context.",
                details="Use the system spec and related context.",
                uses_skills=("specify-system",),
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
                "details": "Record the user-visible outcome first.",
            },
            {
                "description": "Pull in the system context.",
                "details": "Use the system spec and related context.",
                "uses_skills": ["specify-system"],
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
            {"description": "Summarize the result."},
        ],
    }


def test_skill_file_helpers_round_trip(tmp_path: Path) -> None:
    skill = Skill(
        name="clarify-intent",
        when_to_use=("When the user needs a quick synchronous clarification flow.",),
        steps=(SkillStep(description="Ask for the intent."),),
    )

    output_path = save_skill(skill, tmp_path / "clarify-intent.json")
    assert output_path.exists()
    assert load_skill(output_path) == skill


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
        "Prompt the user to review the result.",
        "Incorporate the user's approved feedback into the feature plan.",
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
        "evaluate-system-specification",
        "--work-item-name",
        "<work-item-name>",
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
    assert "choose `next_step` immediately" in (skill.steps[9].details or "")
    assert skill.steps[4].tool_invocations[0].command == (
        "powdrr-lift",
        "evaluate-architecture-specification",
        "--work-item-name",
        "<work-item-name>",
        "--entity-type",
        "<type>",
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
    assert [invocation.command for invocation in skill.steps[9].tool_invocations] == [
        (
            "powdrr-lift",
            "evaluate-system-specification",
            "--work-item-name",
            "<work-item-name>",
        ),
        (
            "powdrr-lift",
            "evaluate-architecture-specification",
            "--work-item-name",
            "<work-item-name>",
            "--entity-type",
            "<type>",
        ),
        (
            "powdrr-lift",
            "evaluate-implementation-specification",
            "--work-item-name",
            "<work-item-name>",
        ),
    ]
    assert [invocation.command for invocation in skill.steps[11].tool_invocations] == [
        ("powdrr-lift", "repository-state"),
        ("git", "add", "docs/proposals/<work-item-name>"),
    ]
    assert skill.steps[12].uses_skills == ("finish-pr-prep",)
    assert "create-pull-request" in (skill.steps[13].details or "")
    assert skill.steps[13].uses_skills == ("create-pull-request",)


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
        "review-architecture",
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
            for step in skill.steps
        )


def test_create_pull_request_skill_has_prescribed_flow() -> None:
    skills_dir = Path(__file__).resolve().parents[1] / "skill-definitions"
    skill = load_skill(skills_dir / "create-pull-request.yaml")

    assert skill.name == "create-pull-request"
    assert [step.description for step in skill.steps] == [
        "Generate the pull request description template.",
        "Fill in the pull request description template.",
        "Commit the validated changes.",
        "Push the committed changes.",
        "Create a draft pull request when none exists.",
        "Update the existing pull request.",
    ]
    assert skill.steps[0].tool_invocations[0].command == (
        "powdrr-lift",
        "pull-request-description",
        "--kind",
        "<pr-kind>",
    )
    assert "do not print" in (skill.steps[0].details or "").lower()
    assert "do not print" in (skill.steps[1].details or "").lower()
    assert skill.steps[2].tool_invocations[-1].command == (
        "git",
        "commit",
        "-m",
        "<commit-message>",
    )
    assert skill.steps[3].tool_invocations[0].command == (
        "git",
        "push",
        "-u",
        "origin",
        "HEAD",
    )
    assert skill.steps[4].tool_invocations[0].command[:3] == (
        "gh",
        "pr",
        "create",
    )
    assert skill.steps[5].tool_invocations[0].command[:3] == (
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
        "Confirm the staged pull request scope and validation commands.",
        "Run the final formatting, lint, type-check, and test passes.",
        "Leave the branch ready for pull request creation.",
    ]
    assert [
        (invocation.tool, invocation.label)
        for invocation in skill.steps[0].tool_invocations
    ][:1] == [("ref", "pr-prep")]
    assert skill.steps[1].tool_invocations == ()


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
    assert "already exist" in implementation_step_details
    validation_step_details = skill.steps[3].details
    assert validation_step_details is not None
    assert "spec-v1" in validation_step_details
    assert "yaml_edit" in validation_step_details
    assert "Do not instantiate workflows" in validation_step_details
    workflow_step_details = skill.steps[4].details
    assert workflow_step_details is not None
    assert "templates/execute-proposed-pr.yaml" in workflow_step_details
    pr_step_details = skill.steps[8].details
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
        "Create implementation specifications for the proposed PRs.",
        "Validate every spec-v1 document before creating execution workflows.",
        "Instantiate an execution workflow for every proposed PR.",
        "Review and approve the implementation plan and workflows.",
        "Stage the approved artifacts for pull request preparation.",
        "Run finish-pr-prep before creating the draft pull request.",
        "Commit the approved artifacts and open a draft pull request.",
        "Hand the draft pull request to the user for review.",
    ]
    assert [step.llm_type for step in skill.steps] == [
        "standard_reasoning",
        "standard_reasoning",
        "high_reasoning",
        "fast_iteration",
        "simple_task",
        "high_reasoning",
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
    assert skill.steps[3].tool_invocations[0].command == (
        "powdrr-lift",
        "evaluate",
        "docs/proposals/<feature-name>",
    )
    assert skill.steps[4].tool_invocations[0].command == (
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
    assert "dependencies" in (skill.steps[4].details or "")
    assert "If an execution workflow already exists for every proposed PR" in (
        skill.steps[4].details or ""
    )
    assert "invoke the workflow instantiation tool" in (skill.steps[4].details or "")
    assert [invocation.command for invocation in skill.steps[6].tool_invocations] == [
        ("powdrr-lift", "repository-state"),
        ("git", "add", "docs/proposals/<feature-name>", "docs/workflows"),
    ]
    assert skill.steps[7].uses_skills == ("finish-pr-prep",)
    assert skill.steps[8].uses_skills == ("create-pull-request",)


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
        "Invoke the listed system validator exactly once after the specification "
        "edits. If it succeeds, use its result to confirm that no inconsistencies "
        "remain and choose `next_step` immediately. If it reports validation "
        "errors, edit the specification to address them before invoking the "
        "validator again; never repeat the same validation command unchanged."
    )
    assert skill.steps[3].tool_invocations[0].command == (
        "powdrr-lift",
        "evaluate-system-specification",
        "--work-item-name",
        "<work-item-name>",
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
        "Invoke the listed architecture validator exactly once after the "
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
        "evaluate-architecture-specification",
        "--work-item-name",
        "<work-item-name>",
        "--entity-type",
        "<type>",
    )
