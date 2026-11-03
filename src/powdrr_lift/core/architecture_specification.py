from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml
from powdrr_service.specifications.architecture import (
    ArchitectureSpecificationValidationIssue,
    ArchitectureSpecificationValidationReport,
    normalize_architecture_entity_types,
)
from powdrr_service.specifications.architecture import (
    build_architecture_specification_validation_report as _validate_architecture_policy,
)

from powdrr_lift.core.repo import resolve_repo_root as _resolve_repo_root
from powdrr_lift.core.spec_paths import (
    SPECIFICATION_SCHEMA_URL,
    architecture_specification_path,
    existing_specification_path,
)
from powdrr_lift.core.template_generation import merge_existing_template_content
from powdrr_lift.core.validation_messages import (
    ValidationError,
    validation_error_to_data,
)

__all__ = [
    "ArchitectureSpecificationValidationIssue",
    "ArchitectureSpecificationValidationReport",
    "architecture_specification_default_output_path",
    "build_architecture_specification_validation_report",
    "create_architecture_specification_template",
    "render_architecture_specification_template",
    "validate_architecture_specification_yaml",
]


def build_architecture_specification_validation_report(
    proposed_architecture_specification_yaml: str,
    *,
    entity_types: Sequence[str],
    work_item_name: str,
    repo_root: str | Path | None = None,
) -> ArchitectureSpecificationValidationReport:
    """Collect local system-spec evidence and call service validation policy."""
    repo_root_path = _resolve_repo_root(repo_root)
    system_specification_path = existing_specification_path(
        repo_root_path, work_item_name, "system-specification.yaml"
    )
    relative_path = str(system_specification_path.relative_to(repo_root_path))
    system_specification_yaml: str | None = None
    system_specification_read_error: str | None = None
    try:
        if system_specification_path.exists():
            system_specification_yaml = system_specification_path.read_text(
                encoding="utf-8"
            )
    except (OSError, UnicodeError) as exc:
        system_specification_read_error = str(exc)

    return _validate_architecture_policy(
        proposed_architecture_specification_yaml,
        entity_types=entity_types,
        system_specification_yaml=system_specification_yaml,
        system_specification_path=relative_path,
        system_specification_read_error=system_specification_read_error,
    )


def validate_architecture_specification_yaml(
    proposed_architecture_specification_yaml: str,
    *,
    entity_types: Sequence[str],
    work_item_name: str,
    repo_root: str | Path | None = None,
    file_path: str | Path | None = None,
) -> str:
    report = build_architecture_specification_validation_report(
        proposed_architecture_specification_yaml,
        entity_types=entity_types,
        work_item_name=work_item_name,
        repo_root=repo_root,
    )
    return yaml.safe_dump(_report_to_data(report, file_path=file_path), sort_keys=False)


def _report_to_data(
    report: ArchitectureSpecificationValidationReport,
    *,
    file_path: str | Path | None = None,
) -> dict[str, Any]:
    return {
        "validation_successful": report.validation_successful,
        "title": report.title,
        "allowed_entity_types": report.allowed_entity_types,
        "module_ids": report.module_ids,
        "tool_ids": report.tool_ids,
        "issues": [
            validation_error_to_data(
                ValidationError(issue.code, issue.message, issue.path),
                file_path=None if file_path is None else str(file_path),
            )
            for issue in report.issues
        ],
    }


def architecture_specification_default_output_path(
    work_item_name: str,
    repo_root: str | Path | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    return architecture_specification_path(repo_root_path, work_item_name)


def render_architecture_specification_template(
    entity_types: Sequence[str],
    *,
    work_item_name: str,
    title: str | None = None,
) -> str:
    normalized_entity_types = normalize_architecture_entity_types(entity_types)
    if not normalized_entity_types:
        raise ValueError("At least one entity type must be provided.")
    normalized_work_item_name = work_item_name.strip()
    if not normalized_work_item_name:
        raise ValueError("work_item_name must not be empty.")

    lines = [
        "# Architecture specification template.",
        "#",
        "# Instructions:",
        f"# - Use the proposal folder `docs/proposals/{normalized_work_item_name}`.",
        "# - Fill in the sections below with the intended architecture.",
        "# - Set `id` to a date-based identifier, for example 2026-06-19.",
        "# - Choose each entity type from the allowed entity types listed here.",
        "# - Write each rationale in English, but quote the requirement or",
        '#   approach ids it relies on, for example "req-single-command-install".',
        "# - Every entity and every entity relationship rationale must cite at",
        "#   least one current requirement or approach id in quotes.",
        "# - Every invariant and guidance rationale must cite at least one",
        "#   current requirement or approach id in quotes.",
        "# - Use `related.entities` and `related.entity_relationships` in",
        "#   invariants and guidance whenever they refer to a specific entity",
        "#   or relationship, and keep at least one current entity or",
        "#   relationship listed in each item's related block.",
        "# - Keep every entity mentioned in a relationship, invariant, or",
        "#   guidance item present in the `entities` section.",
        "# - Use `action: added`, `removed`, or `changed` for every entity,",
        "#   module, and tool.",
        "# - Scope entities, relationships, invariants, and guidance with",
        "#   related_modules when they apply to one or more specific modules.",
        "# - Set each module's quality_tier to prototyping, internal_beta, or",
        "#   production_ready, inheriting the initiative tier unless overridden.",
        "# - Record preserved or intentionally changed existing behavior in",
        "#   legacy_behavior for each module.",
        "# - Delete these instructions and replace them with this comment at the top:",
        '#   "# This file is read-only and should never be edited by a tool or agent."',
        "#",
        f"schema: {SPECIFICATION_SCHEMA_URL}",
        "# Allowed entity types:",
        *[f"# - {entity_type}" for entity_type in normalized_entity_types],
        "id: null",
        "title: null",
        "entities:",
        "  - id: null",
        "    type: null",
        "    action: null",
        "    summary: null",
        "    rationale: null",
        "    related_modules: []",
        "modules:",
        "  - id: null",
        "    action: null",
        "    relative_location: null",
        "    purpose: null",
        "    quality_tier: null",
        "    legacy_behavior: null",
        "    related_modules: []",
        "tools:",
        "  - id: null",
        "    action: null",
        "    related_modules: []",
        "    when_to_use: null",
        "    template: null",
        "    how_to_use: null",
        "entity_relationships:",
        "  - id: null",
        "    source: null",
        "    target: null",
        "    relationship: null",
        "    description: null",
        "    rationale: null",
        "    related_modules: []",
        "invariants:",
        "  - id: null",
        "    description: null",
        "    rationale: null",
        "    related:",
        "      entities: []",
        "      entity_relationships: []",
        "    related_modules: []",
        "guidance:",
        "  - id: null",
        "    description: null",
        "    rationale: null",
        "    related:",
        "      entities: []",
        "      entity_relationships: []",
        "    related_modules: []",
        "",
    ]

    if title is not None:
        lines[lines.index("title: null")] = f"title: {json.dumps(title)}"

    return "\n".join(lines)


def create_architecture_specification_template(
    entity_types: Sequence[str],
    *,
    work_item_name: str,
    output_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    title: str | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    resolved_output_path = _resolve_output_path(
        repo_root_path,
        work_item_name=work_item_name,
        output_path=output_path,
    )
    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
    existing_content = (
        resolved_output_path.read_text(encoding="utf-8")
        if resolved_output_path.is_file()
        else None
    )
    resolved_output_path.write_text(
        merge_existing_template_content(
            render_architecture_specification_template(
                entity_types,
                work_item_name=work_item_name,
                title=title,
            ),
            existing_content,
        ),
        encoding="utf-8",
    )
    return resolved_output_path


def _resolve_output_path(
    repo_root: Path,
    *,
    work_item_name: str,
    output_path: str | Path | None,
) -> Path:
    if output_path is None:
        return architecture_specification_path(repo_root, work_item_name)

    resolved_output_path = Path(output_path)
    if not resolved_output_path.is_absolute():
        resolved_output_path = repo_root / resolved_output_path

    return resolved_output_path
