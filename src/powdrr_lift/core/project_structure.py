from __future__ import annotations

from pathlib import Path

from powdrr_lift.change_log_template import _resolve_repo_root

PROJECT_STRUCTURE_TEMPLATE = """schema: project-structure-v1
modules: []
tools: []
relationships: []
evidence: []
"""


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
