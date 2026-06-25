from __future__ import annotations

import json
from pathlib import Path

from powdrr_lift.change_log_template import _resolve_repo_root
from powdrr_lift.core.spec_paths import (
    SPECIFICATION_SCHEMA_URL,
    feature_pr_specification_path,
    system_map_specification_path,
)


def system_map_specification_default_output_path(
    work_item_name: str,
    repo_root: str | Path | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    return system_map_specification_path(repo_root_path, work_item_name)


def feature_pr_specification_default_output_path(
    work_item_name: str,
    repo_root: str | Path | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    return feature_pr_specification_path(repo_root_path, work_item_name)


def render_system_map_specification_template(
    *,
    work_item_name: str,
    title: str | None = None,
) -> str:
    normalized_work_item_name = work_item_name.strip()
    if not normalized_work_item_name:
        raise ValueError("work_item_name must not be empty.")

    lines = [
        "# System map specification template.",
        "#",
        "# Instructions:",
        f"# - Use the work item folder `docs/specs/{normalized_work_item_name}`.",
        "# - Analyze the full codebase deeply before writing anything.",
        "# - Fill out one section at a time in this order:",
        "#   requirements, approach, entities, entity_relationships,",
        "#   invariants, guidance, features, decisions.",
        "# - Double-check each section before moving to the next one.",
        "# - Remove these instructions once the template is complete.",
        "# - Use `state` for requirements and approach items.",
        "# - Use `action` for features and decisions.",
        "# - Keep ids unique across the whole document.",
        "#",
        f"schema: {SPECIFICATION_SCHEMA_URL}",
        "id: null",
        "title: null",
        "requirements:",
        "  - id: null",
        "    description: null",
        "    state: null",
        "approach:",
        "  - id: null",
        "    description: null",
        "    state: null",
        "entities:",
        "  - id: null",
        "    type: null",
        "    summary: null",
        "    rationale: null",
        "entity_relationships:",
        "  - id: null",
        "    source: null",
        "    target: null",
        "    relationship: null",
        "    description: null",
        "    rationale: null",
        "invariants:",
        "  - id: null",
        "    description: null",
        "    rationale: null",
        "    related:",
        "      entities: []",
        "      entity_relationships: []",
        "guidance:",
        "  - id: null",
        "    description: null",
        "    rationale: null",
        "    related:",
        "      entities: []",
        "      entity_relationships: []",
        "features:",
        "  - id: null",
        "    action: null",
        "    description: null",
        "    supercedes: null",
        "    functional_requirements:",
        "      - null",
        "decisions:",
        "  - id: null",
        "    action: null",
        "    description: null",
        "    supercedes: null",
        "",
    ]

    if title is not None:
        lines[lines.index("title: null")] = f"title: {json.dumps(title)}"

    return "\n".join(lines)


def render_feature_pr_specification_template(
    *,
    work_item_name: str,
    title: str | None = None,
) -> str:
    normalized_work_item_name = work_item_name.strip()
    if not normalized_work_item_name:
        raise ValueError("work_item_name must not be empty.")

    lines = [
        "# Feature and PR specification template.",
        "#",
        "# Instructions:",
        f"# - Use the work item folder `docs/specs/{normalized_work_item_name}`.",
        "# - Start from the filled system map template and the requested feature.",
        "# - Fill out one section at a time in this order:",
        "#   requirements, approach, entities, entity_relationships,",
        "#   invariants, guidance, features, decisions, feature_ids, intent,",
        "#   acceptance_criteria, expected_tests, expected_outcomes,",
        "#   non_goals, risks.",
        "# - For each section, capture only the changes needed to implement the",
        "#   feature and the validation conditions that prove it is done.",
        "# - Double-check that nothing requested by the feature is missing.",
        "# - Remove these instructions once the template is complete.",
        "# - Use `state` for requirements and approach items.",
        "# - Use `action` for features and decisions.",
        "# - Keep ids unique across the whole document.",
        "#",
        f"schema: {SPECIFICATION_SCHEMA_URL}",
        "id: null",
        "title: null",
        "requirements:",
        "  - id: null",
        "    description: null",
        "    state: null",
        "approach:",
        "  - id: null",
        "    description: null",
        "    state: null",
        "entities:",
        "  - id: null",
        "    type: null",
        "    summary: null",
        "    rationale: null",
        "entity_relationships:",
        "  - id: null",
        "    source: null",
        "    target: null",
        "    relationship: null",
        "    description: null",
        "    rationale: null",
        "invariants:",
        "  - id: null",
        "    description: null",
        "    rationale: null",
        "    related:",
        "      entities: []",
        "      entity_relationships: []",
        "guidance:",
        "  - id: null",
        "    description: null",
        "    rationale: null",
        "    related:",
        "      entities: []",
        "      entity_relationships: []",
        "features:",
        "  - id: null",
        "    action: null",
        "    description: null",
        "    supercedes: null",
        "    functional_requirements:",
        "      - null",
        "decisions:",
        "  - id: null",
        "    action: null",
        "    description: null",
        "    supercedes: null",
        "feature_ids:",
        "  - null",
        "intent:",
        "  goal: null",
        "  reasoning: null",
        "acceptance_criteria:",
        "  - id: null",
        "    description: null",
        "expected_tests:",
        "  - id: null",
        "    description: null",
        "expected_outcomes:",
        "  - id: null",
        "    description: null",
        "non_goals:",
        "  - id: null",
        "    description: null",
        "risks:",
        "  - id: null",
        "    description: null",
        "",
    ]

    if title is not None:
        lines[lines.index("title: null")] = f"title: {json.dumps(title)}"

    return "\n".join(lines)


def create_system_map_specification_template(
    *,
    work_item_name: str,
    output_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    title: str | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    resolved_output_path = (
        Path(output_path)
        if output_path is not None
        else system_map_specification_path(repo_root_path, work_item_name)
    )
    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_output_path.write_text(
        render_system_map_specification_template(
            work_item_name=work_item_name,
            title=title,
        ),
        encoding="utf-8",
    )
    return resolved_output_path


def create_feature_pr_specification_template(
    *,
    work_item_name: str,
    output_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    title: str | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    resolved_output_path = (
        Path(output_path)
        if output_path is not None
        else feature_pr_specification_path(repo_root_path, work_item_name)
    )
    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_output_path.write_text(
        render_feature_pr_specification_template(
            work_item_name=work_item_name,
            title=title,
        ),
        encoding="utf-8",
    )
    return resolved_output_path


def start_planning_feature(
    *,
    work_item_name: str,
    repo_root: str | Path | None = None,
) -> str:
    repo_root_path = _resolve_repo_root(repo_root)
    normalized_work_item_name = work_item_name.strip()
    if not normalized_work_item_name:
        raise ValueError("work_item_name must not be empty.")

    skill_path = repo_root_path / "skills" / "plan-and-implement-feature" / "SKILL.md"
    if not skill_path.is_file():
        raise FileNotFoundError(
            f"Could not find plan-and-implement-feature skill at {skill_path}"
        )

    skill_text = skill_path.read_text(encoding="utf-8")
    return skill_text.replace("<work-item-name>", normalized_work_item_name)
