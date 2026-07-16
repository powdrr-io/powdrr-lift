from __future__ import annotations

import json
from pathlib import Path

from powdrr_lift.core import (
    Skill,
    SkillStep,
    SkillToolInvocation,
    build_skill_directory_validation_report,
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
                        tool="shell",
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
    )

    json_text = skill_to_json(skill)
    parsed = skill_from_json(json_text)

    assert parsed == skill
    assert json.loads(json_text) == {
        "name": "specify-a-feature",
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
                        "tool": "shell",
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
        / "specify-a-feature.json"
    )
    skill = load_skill(skill_path)

    assert skill.name == "specify-a-feature"
    assert skill.when_to_use == (
        ("When the user needs a concrete feature plan."),
        ("When the flow must gather context and drive implementation."),
    )
    assert [step.description for step in skill.steps] == [
        "Capture the feature goal and success criteria.",
        "Generate the system template and fill it out.",
        "Review the system context before deciding the feature shape.",
        "Generate the architecture template and fill it out.",
        "Review architecture before implementation.",
        "Generate the implementation template and fill it out.",
        "Decide on proposed PRs and fill each template.",
        "Prompt the user to review the result.",
    ]
    for step in skill.steps:
        assert step.details is not None
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


def test_checked_in_skill_definitions_directory_is_valid() -> None:
    skills_dir = Path(__file__).resolve().parents[1] / "skill-definitions"
    report = build_skill_directory_validation_report(skills_dir)

    assert report.validation_successful is True
    assert report.skill_names == [
        "review-architecture",
        "review-system",
        "specify-a-feature",
        "specify-architecture",
        "specify-implementation",
        "specify-system",
    ]


def test_checked_in_review_skill_definitions_exist() -> None:
    skills_dir = Path(__file__).resolve().parents[1] / "skill-definitions"
    assert (skills_dir / "review-architecture.json").is_file()
    assert (skills_dir / "review-system.json").is_file()
