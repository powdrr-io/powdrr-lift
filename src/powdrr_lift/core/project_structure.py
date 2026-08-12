from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.change_log_template import _resolve_repo_root

PROJECT_STRUCTURE_TEMPLATE = """schema: https://powdrr.io/schemas/specification-v1
id: project-structure
title: Project structure
entities: []
modules: []
tools: []
entity_relationships: []
invariants: []
guidance: []
features: []
decisions: []
intent: {}
"""


@dataclass(frozen=True, slots=True)
class ProjectStructureValidationIssue:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectStructureValidationReport:
    validation_successful: bool
    issues: list[ProjectStructureValidationIssue] = field(default_factory=list)


_ROOT_FIELDS = {
    "schema",
    "id",
    "title",
    "entities",
    "modules",
    "tools",
    "entity_relationships",
    "invariants",
    "guidance",
    "features",
    "decisions",
    "intent",
    "requirements",
    "approach",
    "architecture_id",
    "human-decisions",
}
_REQUIRED_ROOT_FIELDS = {
    "schema",
    "id",
    "entities",
    "modules",
    "tools",
    "entity_relationships",
    "invariants",
    "guidance",
    "features",
    "decisions",
    "intent",
}
_MODULE_FIELDS = {
    "id",
    "action",
    "parent_module",
    "relative_location",
    "related_modules",
    "purpose",
}
_REQUIRED_MODULE_FIELDS = {"id", "action", "relative_location", "purpose"}
_TOOL_FIELDS = {
    "id",
    "action",
    "related_module",
    "related_modules",
    "labels",
    "when_to_use",
    "template",
    "how_to_use",
    "evidence",
}
_REQUIRED_TOOL_FIELDS = {"id", "action", "when_to_use", "template", "how_to_use"}


def validate_project_structure_yaml(
    input_path: str | Path,
) -> ProjectStructureValidationReport:
    """Validate the project structure as a specification-v1 document."""
    path = Path(input_path)
    issues: list[ProjectStructureValidationIssue] = []
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        return ProjectStructureValidationReport(
            validation_successful=False,
            issues=[ProjectStructureValidationIssue("invalid_yaml", str(error))],
        )

    if not isinstance(data, dict):
        issues.append(
            ProjectStructureValidationIssue("invalid_root", "Root must be a mapping.")
        )
        return ProjectStructureValidationReport(False, issues)
    _check_required_fields(data, _REQUIRED_ROOT_FIELDS, "", issues)
    _check_unknown_fields(data, _ROOT_FIELDS, "", issues)
    if data.get("schema") != "https://powdrr.io/schemas/specification-v1":
        issues.append(
            ProjectStructureValidationIssue(
                "invalid_schema",
                "schema must be https://powdrr.io/schemas/specification-v1.",
                "schema",
            )
        )
    if not isinstance(data.get("id"), str) or not data["id"].strip():
        issues.append(
            ProjectStructureValidationIssue(
                "missing_field", "id must be a non-empty string.", "id"
            )
        )
    for section in (
        "entities",
        "modules",
        "tools",
        "entity_relationships",
        "invariants",
        "guidance",
        "features",
        "decisions",
    ):
        if not isinstance(data.get(section), list):
            issues.append(
                ProjectStructureValidationIssue(
                    "invalid_section", f"{section} must be a list.", section
                )
            )
    for section in ("modules", "tools"):
        entries = data.get(section)
        if not isinstance(entries, list):
            issues.append(
                ProjectStructureValidationIssue(
                    "invalid_section", f"{section} must be a list.", section
                )
            )
            continue
        allowed = _MODULE_FIELDS if section == "modules" else _TOOL_FIELDS
        for index, entry in enumerate(entries):
            entry_path = f"{section}[{index}]"
            if not isinstance(entry, dict):
                issues.append(
                    ProjectStructureValidationIssue(
                        "invalid_entry", "Entry must be a mapping.", entry_path
                    )
                )
                continue
            required = (
                _REQUIRED_MODULE_FIELDS
                if section == "modules"
                else _REQUIRED_TOOL_FIELDS
            )
            _check_required_fields(entry, required, entry_path, issues)
            _check_unknown_fields(entry, allowed, entry_path, issues)
            for field_name in ("id", "action"):
                if (
                    not isinstance(entry.get(field_name), str)
                    or not entry[field_name].strip()
                ):
                    issues.append(
                        ProjectStructureValidationIssue(
                            "missing_field",
                            f"{field_name} must be a non-empty string.",
                            f"{entry_path}.{field_name}",
                        )
                    )
            if not isinstance(entry.get("related_modules", []), list):
                issues.append(
                    ProjectStructureValidationIssue(
                        "invalid_field",
                        "related_modules must be a list.",
                        f"{entry_path}.related_modules",
                    )
                )
    return ProjectStructureValidationReport(not issues, issues)


def _check_unknown_fields(
    data: dict[str, Any],
    allowed: set[str],
    path: str,
    issues: list[ProjectStructureValidationIssue],
) -> None:
    for key in sorted(set(data) - allowed):
        issues.append(
            ProjectStructureValidationIssue(
                "unknown_field",
                f"Unknown field {key!r}.",
                f"{path}.{key}" if path else key,
            )
        )


def _check_required_fields(
    data: dict[str, Any],
    required: set[str],
    path: str,
    issues: list[ProjectStructureValidationIssue],
) -> None:
    for key in sorted(required - set(data)):
        issues.append(
            ProjectStructureValidationIssue(
                "missing_field",
                f"Missing required field {key!r}.",
                f"{path}.{key}" if path else key,
            )
        )


def create_project_structure_template(
    *,
    output_path: str | Path = "docs/project_structure/project-structure.yaml",
    repo_root: str | Path | None = None,
) -> Path:
    """Create the project-structure template and its parent directories."""
    repo_root_path = _resolve_repo_root(repo_root)
    resolved_output_path = Path(output_path)
    if not resolved_output_path.is_absolute():
        resolved_output_path = repo_root_path / resolved_output_path
    resolved_output_path = resolved_output_path.resolve()
    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
    if not resolved_output_path.exists():
        resolved_output_path.write_text(PROJECT_STRUCTURE_TEMPLATE, encoding="utf-8")
    return resolved_output_path
