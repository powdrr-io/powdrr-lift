from __future__ import annotations

from pathlib import Path

from powdrr_lift.core.project_structure import (
    PROJECT_STRUCTURE_TEMPLATE,
    create_project_structure_template,
)


def test_create_project_structure_template_creates_parent_directories(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "docs" / "project_structure" / "project-structure.yaml"

    rendered_path = create_project_structure_template(
        output_path=output_path,
        repo_root=tmp_path,
    )

    assert rendered_path == output_path.resolve()
    assert output_path.read_text(encoding="utf-8") == PROJECT_STRUCTURE_TEMPLATE


def test_create_project_structure_template_preserves_existing_file(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "docs" / "project_structure" / "project-structure.yaml"
    output_path.parent.mkdir(parents=True)
    output_path.write_text("schema: customized\n", encoding="utf-8")

    create_project_structure_template(output_path=output_path, repo_root=tmp_path)

    assert output_path.read_text(encoding="utf-8") == "schema: customized\n"
