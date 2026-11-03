from __future__ import annotations

import json
from pathlib import Path

import yaml

from powdrr_lift.core.repo import resolve_repo_root as _resolve_repo_root
from powdrr_lift.core.spec_paths import (
    SPECIFICATION_SCHEMA_URL,
    system_specification_path,
)
from powdrr_lift.core.template_generation import merge_existing_template_content
from powdrr_lift.core.validation_messages import (
    ValidationError,
    validation_error_to_data,
)


def validate_system_specification_yaml(
    proposed_system_specification_yaml: str,
    *,
    file_path: str | Path | None = None,
) -> str:
    """Validate supplied system-spec content and format the local CLI report."""
    from powdrr_service.specifications.system import (
        build_system_specification_validation_report,
    )

    report = build_system_specification_validation_report(
        proposed_system_specification_yaml
    )
    return yaml.safe_dump(
        {
            "validation_successful": report.validation_successful,
            "system_id": report.system_id,
            "requirement_ids": report.requirement_ids,
            "approach_ids": report.approach_ids,
            "issues": [
                validation_error_to_data(
                    ValidationError(issue.code, issue.message, issue.path),
                    file_path=None if file_path is None else str(file_path),
                )
                for issue in report.issues
            ],
        },
        sort_keys=False,
    )


def system_specification_default_output_path(
    work_item_name: str,
    repo_root: str | Path | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    return system_specification_path(repo_root_path, work_item_name)


def render_system_specification_template(
    *,
    work_item_name: str,
    title: str | None = None,
) -> str:
    normalized_work_item_name = work_item_name.strip()
    if not normalized_work_item_name:
        raise ValueError("work_item_name must not be empty.")

    lines = [
        "# System specification template.",
        "#",
        "# Instructions:",
        f"# - Use the proposal folder `docs/proposals/{normalized_work_item_name}`.",
        "# - Fill in the requirements and approach sections.",
        "# - Delete these instructions and replace them with this comment at the top:",
        '#   "# This file is read-only and should never be edited by a tool or agent."',
        "# - Set `id` to a unique identifier for this system description.",
        "# - Use `state: added` for new items and include a description.",
        "# - Use `state: removed` for retired items and leave description empty.",
        "# - Use `state: supercedes` when an item replaces same-section ids.",
        "# - `supercedes` is optional; omit it unless the item replaces ids.",
        "# - Keep ids unique across both sections.",
        "# - Reference only same-section ids in `supercedes`.",
        "#",
        f"schema: {SPECIFICATION_SCHEMA_URL}",
        "# Optional title:",
        "# - Set `title` if a human-readable summary is helpful.",
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
        "",
    ]

    if title is not None:
        lines[lines.index("title: null")] = f"title: {json.dumps(title)}"

    return "\n".join(lines)


def create_system_specification_template(
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
            render_system_specification_template(
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
        return system_specification_path(repo_root, work_item_name)
    resolved_output_path = Path(output_path)
    if not resolved_output_path.is_absolute():
        return repo_root / resolved_output_path
    return resolved_output_path
