from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from powdrr_lift.change_log_template import (
    _git_output,
    _resolve_default_branch,
    _resolve_repo_root,
)
from powdrr_lift.core.codebase_state import build_current_state_specification_report
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
    repo_root: str | Path | None = None,
    branch_name: str | None = None,
    parent_branch: str | None = None,
) -> str:
    normalized_work_item_name = work_item_name.strip()
    if not normalized_work_item_name:
        raise ValueError("work_item_name must not be empty.")

    repo_root_path = _resolve_repo_root(repo_root)
    resolved_branch_name = branch_name or _current_branch(repo_root_path)
    resolved_parent_branch = parent_branch or _resolve_default_branch(
        repo_root_path,
        resolved_branch_name,
    )
    compact_instructions = _should_use_compact_system_map_instructions(
        repo_root_path,
        resolved_branch_name,
        resolved_parent_branch,
    )
    report = build_current_state_specification_report(
        branch_name=resolved_branch_name,
        parent_branch=resolved_parent_branch,
        repo_root=repo_root_path,
    )
    report.pop("proposed_prs", None)
    report["id"] = _system_map_report_id(resolved_branch_name)
    report["title"] = title or f"System map synthesized for {resolved_branch_name}"

    return _render_system_map_specification(
        normalized_work_item_name=normalized_work_item_name,
        report=report,
        compact_instructions=compact_instructions,
    )


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
            repo_root=repo_root_path,
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


def _render_system_map_specification(
    *,
    normalized_work_item_name: str,
    report: dict[str, Any],
    compact_instructions: bool,
) -> str:
    lines = [
        "# System map specification template.",
        "#",
        "# Instructions:",
        *_system_map_instruction_lines(
            normalized_work_item_name=normalized_work_item_name,
            compact_instructions=compact_instructions,
        ),
        "#",
    ]
    rendered_report = _render_system_map_report(report)
    return "\n".join([*lines, rendered_report, ""])


def _system_map_instruction_lines(
    *,
    normalized_work_item_name: str,
    compact_instructions: bool,
) -> list[str]:
    if compact_instructions:
        return [
            "# - This file is already complete, delete this line and then move "
            "on to the next step",
        ]

    return [
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
    ]


def _render_system_map_report(report: dict[str, Any]) -> str:
    import yaml

    return yaml.safe_dump(report, sort_keys=False).rstrip()


def _system_map_report_id(branch_name: str) -> str:
    normalized_branch = branch_name.strip().replace("/", "-")
    return f"system-map-{normalized_branch}"


def _should_use_compact_system_map_instructions(
    repo_root: Path,
    branch_name: str,
    parent_branch: str,
) -> bool:
    recent_branches = _recent_feature_branches(repo_root, branch_name)
    if not recent_branches:
        return False

    qualifying_branches = 0
    for recent_branch in recent_branches:
        if _branch_has_changelog(repo_root, recent_branch) or (
            _branch_source_line_changes(repo_root, recent_branch, parent_branch) < 100
        ):
            qualifying_branches += 1

    return qualifying_branches * 2 >= len(recent_branches)


def _recent_feature_branches(
    repo_root: Path,
    current_branch_name: str,
    *,
    limit: int = 10,
) -> list[str]:
    try:
        output = _git_output(
            repo_root,
            "for-each-ref",
            "--sort=-committerdate",
            "--format=%(refname:short)",
            "refs/heads",
        )
    except subprocess.CalledProcessError:
        return []

    recent_branches: list[str] = []
    for raw_branch_name in output.splitlines():
        branch_name = raw_branch_name.strip()
        if not branch_name or branch_name == current_branch_name:
            continue
        if branch_name in {"main", "master", "trunk", "develop"}:
            continue

        recent_branches.append(branch_name)
        if len(recent_branches) >= limit:
            break

    return recent_branches


def _branch_has_changelog(repo_root: Path, branch_name: str) -> bool:
    try:
        output = _git_output(
            repo_root,
            "ls-tree",
            "-r",
            "--name-only",
            branch_name,
            "docs/changelogs",
        )
    except subprocess.CalledProcessError:
        return False

    return any(
        path.startswith("docs/changelogs/PR-") and path.endswith("-changelog.yaml")
        for path in output.splitlines()
    )


def _branch_source_line_changes(
    repo_root: Path,
    branch_name: str,
    parent_branch: str,
) -> int:
    try:
        output = _git_output(
            repo_root,
            "diff",
            "--numstat",
            f"{parent_branch}...{branch_name}",
        )
    except subprocess.CalledProcessError:
        return 0

    total_changed_lines = 0
    for raw_line in output.splitlines():
        parts = raw_line.split("\t")
        if len(parts) < 3:
            continue

        added_text, deleted_text = parts[0], parts[1]
        changed_path = parts[2:]
        if not any(
            path.startswith("src/") or path.startswith("tests/")
            for path in changed_path
        ):
            continue

        if added_text != "-":
            total_changed_lines += int(added_text)
        if deleted_text != "-":
            total_changed_lines += int(deleted_text)

    return total_changed_lines


def _current_branch(repo_root: Path) -> str:
    return _git_output(repo_root, "branch", "--show-current").strip()


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
