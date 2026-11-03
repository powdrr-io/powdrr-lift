from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from powdrr_service.specifications import implementation as _implementation_service
from powdrr_service.specifications.implementation import (
    ArchitectureSpecificationSummary,
    ImplementationSpecificationValidationIssue,
    ImplementationSpecificationValidationReport,
    summarize_architecture_specification_yaml,
)

from powdrr_lift.core.repo import resolve_repo_root as _resolve_repo_root
from powdrr_lift.core.spec_paths import (
    SPECIFICATION_SCHEMA_URL,
    existing_specification_path,
    implementation_specification_path,
)
from powdrr_lift.core.template_generation import merge_existing_template_content
from powdrr_lift.core.validation_messages import (
    ValidationError,
    validation_error_to_data,
)

__all__ = [
    "ImplementationSpecificationValidationIssue",
    "ImplementationSpecificationValidationReport",
    "build_implementation_specification_validation_report",
    "create_implementation_specification_template",
    "implementation_specification_default_output_path",
    "render_implementation_specification_template",
    "validate_implementation_specification_yaml",
]


def implementation_specification_default_output_path(
    work_item_name: str,
    repo_root: str | Path | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    return implementation_specification_path(repo_root_path, work_item_name)


def render_implementation_specification_template(
    *,
    work_item_name: str,
    architecture_specification_path: str | Path | None = None,
    title: str | None = None,
    repo_root: str | Path | None = None,
) -> str:
    repo_root_path = _resolve_repo_root(repo_root)
    architecture_summary = _load_architecture_specification_summary(
        repo_root_path,
        architecture_specification_path=architecture_specification_path,
        work_item_name=work_item_name,
        issues=None,
    )
    if architecture_summary is None:
        raise ValueError(
            "The architecture specification could not be loaded. "
            "Create the architecture specification first and then retry."
        )
    architecture_id = architecture_summary.architecture_id
    if architecture_id is None:
        raise ValueError(
            "The architecture specification must include an id before you can "
            "generate an implementation specification template."
        )

    lines = [
        "# Implementation specification template.",
        "#",
        "# Instructions:",
        f"# - Use the proposal folder `docs/proposals/{work_item_name.strip()}`.",
        "# - Keep `architecture_id` aligned with the architecture specification.",
        "# - Copy entity ids and relationship ids only from the architecture",
        "#   specification listed below.",
        "# - Give every entity and entity relationship an `action` of `added`",
        "#   or `removed`.",
        "# - Give every module and tool an `action` of `added`, `removed`,",
        "#   or `changed`, and copy their ids from the architecture specification.",
        "# - related_modules values must reference module ids from the",
        "#   architecture specification; omit related_modules when empty.",
        "# - Include `supercedes` only when it lists one or more ids; omit the",
        "#   field when it would be empty.",
        "# - Treat entity relationships with the same item shape as entities:",
        "#   `id`, `action`, `rationale`, and optional `supercedes`.",
        "# - Give each feature and decision an `action` of `added` or",
        "#   `removed`.",
        "# - Include `supercedes` only when it lists one or more ids; omit the",
        "#   field when it would be empty.",
        "# - Give each feature a unique id, description, and functional",
        "#   requirements.",
        "# - Give each decision a unique id and description.",
        "# - Scope decisions and other entities to every affected module with",
        "#   related_modules; omit it for a genuinely global item.",
        '# - For a module-targeted context lookup use filters: {module_id: "<id>"}.',
        "# - Delete these instructions and replace them with this comment at the top:",
        '#   "# This file is read-only and should never be edited by a tool or agent."',
        "#",
        f"schema: {SPECIFICATION_SCHEMA_URL}",
        f"# Architecture id: {architecture_id}",
        "# Available entities:",
        *[f"# - {entity_id}" for entity_id in architecture_summary.entity_ids],
        "# Available relationships:",
        *[
            f"# - {relationship_id}"
            for relationship_id in architecture_summary.relationship_ids
        ],
        "# Available modules:",
        *[f"# - {module_id}" for module_id in architecture_summary.module_ids],
        "# Available tools:",
        *[f"# - {tool_id}" for tool_id in architecture_summary.tool_ids],
        "title: null",
        f"architecture_id: {json.dumps(architecture_id)}",
        "entities:",
        "  - id: null",
        "    action: null",
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
        "    when_to_use: null",
        "    template: null",
        "    how_to_use: null",
        "    related_modules: []",
        "entity_relationships:",
        "  - id: null",
        "    action: null",
        "    rationale: null",
        "    related_modules: []",
        "features:",
        "  - id: null",
        "    action: null",
        "    description: null",
        "    supercedes: null",
        "    functional_requirements:",
        "      - null",
        "    related_modules: []",
        "decisions:",
        "  - id: null",
        "    action: null",
        "    description: null",
        "    supercedes: null",
        "    related_modules: []",
        "",
    ]

    if title is not None:
        lines[lines.index("title: null")] = f"title: {json.dumps(title)}"

    return "\n".join(lines)


def create_implementation_specification_template(
    *,
    work_item_name: str,
    architecture_specification_path: str | Path | None = None,
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
            render_implementation_specification_template(
                work_item_name=work_item_name,
                architecture_specification_path=architecture_specification_path,
                title=title,
                repo_root=repo_root_path,
            ),
            existing_content,
        ),
        encoding="utf-8",
    )
    return resolved_output_path


def validate_implementation_specification_yaml(
    proposed_implementation_specification_yaml: str,
    *,
    work_item_name: str,
    architecture_specification_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    file_path: str | Path | None = None,
) -> str:
    report = build_implementation_specification_validation_report(
        proposed_implementation_specification_yaml,
        work_item_name=work_item_name,
        architecture_specification_path=architecture_specification_path,
        repo_root=repo_root,
    )
    return yaml.safe_dump(_report_to_data(report, file_path=file_path), sort_keys=False)


def build_implementation_specification_validation_report(
    proposed_implementation_specification_yaml: str,
    *,
    work_item_name: str,
    architecture_specification_path: str | Path | None = None,
    repo_root: str | Path | None = None,
) -> ImplementationSpecificationValidationReport:
    repo_root_path = _resolve_repo_root(repo_root)
    resolved_path = _resolve_input_path(
        repo_root_path,
        architecture_specification_path,
        default_path=existing_specification_path(
            repo_root_path,
            work_item_name,
            "architecture-specification.yaml",
        ),
    )
    architecture_yaml: str | None = None
    architecture_read_error: str | None = None
    try:
        architecture_yaml = resolved_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError) as exc:
        architecture_read_error = str(exc)

    try:
        evidence_path = resolved_path.relative_to(repo_root_path).as_posix()
    except ValueError:
        evidence_path = resolved_path.name

    return _implementation_service.build_implementation_specification_validation_report(
        proposed_implementation_specification_yaml,
        architecture_specification_yaml=architecture_yaml,
        architecture_specification_path=evidence_path,
        architecture_specification_read_error=architecture_read_error,
    )


def _load_architecture_specification_summary(
    repo_root: Path,
    *,
    architecture_specification_path: str | Path | None,
    work_item_name: str,
    issues: list[ImplementationSpecificationValidationIssue] | None,
) -> ArchitectureSpecificationSummary | None:
    resolved_path = _resolve_input_path(
        repo_root,
        architecture_specification_path,
        default_path=existing_specification_path(
            repo_root,
            work_item_name,
            "architecture-specification.yaml",
        ),
    )
    try:
        architecture_yaml = resolved_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        architecture_yaml = None
        read_error = None
    except (OSError, UnicodeError) as exc:
        architecture_yaml = None
        read_error = str(exc)
    else:
        read_error = None

    try:
        evidence_path = resolved_path.relative_to(repo_root).as_posix()
    except ValueError:
        evidence_path = resolved_path.name

    result = summarize_architecture_specification_yaml(
        architecture_yaml,
        architecture_specification_path=evidence_path,
        architecture_specification_read_error=read_error,
    )
    if issues is not None:
        issues.extend(result.issues)
    return result.summary


def _resolve_output_path(
    repo_root: Path,
    *,
    work_item_name: str,
    output_path: str | Path | None,
) -> Path:
    if output_path is None:
        return implementation_specification_path(repo_root, work_item_name)

    resolved_output_path = Path(output_path)
    if not resolved_output_path.is_absolute():
        resolved_output_path = repo_root / resolved_output_path

    return resolved_output_path


def _resolve_input_path(
    repo_root: Path,
    input_path: str | Path | None,
    *,
    default_path: Path,
) -> Path:
    if input_path is None:
        if default_path.is_absolute():
            return default_path
        return repo_root / default_path

    resolved_input_path = Path(input_path)
    if not resolved_input_path.is_absolute():
        resolved_input_path = repo_root / resolved_input_path

    return resolved_input_path


def _report_to_data(
    report: ImplementationSpecificationValidationReport,
    *,
    file_path: str | Path | None = None,
) -> Mapping[str, Any]:
    return {
        "validation_successful": report.validation_successful,
        "architecture_id": report.architecture_id,
        "available_entity_ids": report.available_entity_ids,
        "available_relationship_ids": report.available_relationship_ids,
        "issues": [
            validation_error_to_data(
                ValidationError(issue.code, issue.message, issue.path),
                file_path=None if file_path is None else str(file_path),
            )
            for issue in report.issues
        ],
    }
