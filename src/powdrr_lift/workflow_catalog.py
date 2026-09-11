"""Catalog loading shared by workflow agents."""

from __future__ import annotations

from pathlib import Path
from typing import TextIO

from powdrr_lift.core import build_skill_directory_validation_report, load_skills
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.process.catalog import SkillCatalogEntry


def load_skill_catalog(
    skills_dir: Path,
    *,
    stderr: TextIO,
) -> tuple[SkillCatalogEntry, ...]:
    """Load and validate all skill definitions in a directory."""
    resolved_dir = skills_dir.expanduser().resolve()
    if not resolved_dir.exists():
        print(f"Skill directory does not exist: {resolved_dir}", file=stderr)
        return ()
    if not resolved_dir.is_dir():
        print(f"Skill path is not a directory: {resolved_dir}", file=stderr)
        return ()

    report = build_skill_directory_validation_report(resolved_dir)
    if not report.validation_successful:
        for issue in report.issues:
            print(f"{issue.path}: {issue.code}: {issue.message}", file=stderr)
        return ()

    skill_paths = tuple(
        skill_path
        for pattern in ("*.yaml", "*.yml", "*.json")
        for skill_path in sorted(resolved_dir.glob(pattern))
        if skill_path.is_file()
    )
    skills = load_skills(resolved_dir)
    return tuple(
        SkillCatalogEntry(path=skill_path, skill=skill)
        for skill_path, skill in zip(skill_paths, skills, strict=False)
    )


def load_workflow_template_catalog(
    templates_dir: Path,
    *,
    stderr: TextIO,
) -> tuple[SkillCatalogEntry, ...]:
    return load_skill_catalog(templates_dir, stderr=stderr)


def find_skill_by_name(
    catalog: tuple[SkillCatalogEntry, ...] | list[SkillCatalogEntry],
    skill_name: str,
) -> SkillCatalogEntry:
    normalized_name = skill_name.strip().casefold()
    for entry in catalog:
        if entry.skill.name.casefold() == normalized_name:
            return entry
    raise PowdrrExecutionError(f"Could not find referenced skill {skill_name!r}.")
