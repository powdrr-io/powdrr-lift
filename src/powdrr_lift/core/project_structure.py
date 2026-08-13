from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.change_log_template import _resolve_repo_root
from powdrr_lift.core.specification_v1 import validate_module_tool_sections

PROJECT_STRUCTURE_TEMPLATE = """# Project structure specification template.
#
# Instructions:
# - Describe the project-wide modules and tools discovered from repository evidence.
# - Use only sections needed by the project structure; all specification
#   sections are optional.
# - Add `pr-prep` to testing, linting, and formatting tools unless repository
#   evidence shows the tool is ad hoc.
# - Delete these instructions and replace them with this comment at the top:
#   "# This file is read-only and should never be edited by a tool or agent."
#
schema: https://powdrr.io/schemas/specification-v1
id: null
title: null
modules:
  - id: null
    action: null
    parent_module: null
    relative_location: null
    related_modules: []
    purpose: null
tools:
  - id: null
    action: null
    related_module: null
    related_modules: []
    labels: []
    when_to_use: null
    template: null
    how_to_use: null
    evidence: null
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
    _check_unknown_fields(data, _ROOT_FIELDS, "", issues)
    if any(
        marker in path.read_text(encoding="utf-8")
        for marker in (
            "# Project structure specification template.",
            "# - Delete these instructions and replace them with this comment "
            "at the top:",
        )
    ):
        issues.append(
            ProjectStructureValidationIssue(
                "template_boilerplate_not_removed",
                "Remove the template instructions before validating the "
                "project structure.",
            )
        )
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
        if section not in data:
            continue
        if not isinstance(data.get(section), list):
            issues.append(
                ProjectStructureValidationIssue(
                    "invalid_section", f"{section} must be a list.", section
                )
            )
    modules = data.get("modules", [])
    tools = data.get("tools", [])
    if isinstance(modules, list) and isinstance(tools, list):
        shared_result = validate_module_tool_sections(modules, tools)
        issues.extend(
            ProjectStructureValidationIssue(issue.code, issue.message, issue.path)
            for issue in shared_result.issues
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
