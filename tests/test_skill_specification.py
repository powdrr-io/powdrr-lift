from __future__ import annotations

import json
from pathlib import Path

from powdrr_lift.core import (
    Skill,
    SkillStep,
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
            SkillStep(description="Capture the feature goal."),
            SkillStep(
                description="Pull in the system context.",
                uses_skills=("specify-system",),
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
            {"description": "Capture the feature goal."},
            {
                "description": "Pull in the system context.",
                "uses_skills": ["specify-system"],
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
        (
            "When the user wants to work through a feature in the TUI and leave "
            "with a concrete, synchronous plan."
        ),
        (
            "When the feature needs a guided sequence of clarifying questions "
            "instead of an async task bundle."
        ),
    )
    assert [step.description for step in skill.steps] == [
        "Capture the feature goal and what success looks like.",
        "Gather the relevant system context, entities, invariants, and guidance.",
        "List the concrete feature decisions and the parts that need to change.",
        "Record the intent and reasoning behind the chosen approach.",
        "Summarize the result into a concise implementation-ready note.",
    ]
    assert skill.steps[1].uses_skills == ("specify-system",)
