from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from powdrr_lift.cli import main
from powdrr_lift.core.project_structure import (
    PROJECT_STRUCTURE_TEMPLATE,
    create_project_structure_template,
    validate_project_structure_yaml,
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
    assert (
        "schema: https://powdrr.io/schemas/specification-v1"
        in PROJECT_STRUCTURE_TEMPLATE
    )
    template_lines = PROJECT_STRUCTURE_TEMPLATE.splitlines()
    assert "relationships:" not in template_lines
    assert "evidence:" not in template_lines


def test_project_structure_validator_accepts_completed_specification_v1(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "project-structure.yaml"
    output_path.write_text(
        """# This file is read-only and should never be edited by a tool or agent.
schema: https://powdrr.io/schemas/specification-v1
id: project-structure
""",
        encoding="utf-8",
    )

    report = validate_project_structure_yaml(output_path)

    assert report.validation_successful is True
    assert report.issues == []


def test_project_structure_validator_rejects_unremoved_template_instructions(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "project-structure.yaml"
    output_path.write_text(PROJECT_STRUCTURE_TEMPLATE, encoding="utf-8")

    report = validate_project_structure_yaml(output_path)

    assert report.validation_successful is False
    assert any(
        issue.code == "template_boilerplate_not_removed" for issue in report.issues
    )


def test_project_structure_validator_rejects_removed_sections_and_module_evidence(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "project-structure.yaml"
    output_path.write_text(
        """schema: https://powdrr.io/schemas/specification-v1
id: project-structure
entities: []
modules:
  - id: app
    action: added
    relative_location: src/app.py
    purpose: Application.
    evidence: source evidence
tools: []
entity_relationships: []
invariants: []
guidance: []
features: []
decisions: []
intent: {}
relationships: []
evidence: []
""",
        encoding="utf-8",
    )

    report = validate_project_structure_yaml(output_path)

    assert report.validation_successful is False
    assert {issue.path for issue in report.issues} >= {"modules[0].evidence"}


def test_project_structure_cli_validates_template(tmp_path: Path) -> None:
    output_path = tmp_path / "project-structure.yaml"
    output_path.write_text(
        """# This file is read-only and should never be edited by a tool or agent.
schema: https://powdrr.io/schemas/specification-v1
id: project-structure
""",
        encoding="utf-8",
    )

    assert main(["validate-project-structure", "--input", str(output_path)]) == 0


def test_validate_pr_files_runs_project_structure_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_path = tmp_path / "docs" / "project_structure" / "project-structure.yaml"
    output_path.parent.mkdir(parents=True)
    output_path.write_text(
        "schema: https://powdrr.io/schemas/specification-v1\nid: project-structure\n",
        encoding="utf-8",
    )

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        assert command == ["gh", "pr", "diff", "123", "--name-only"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="docs/project_structure/project-structure.yaml\n",
            stderr="",
        )

    monkeypatch.setattr("powdrr_lift.cli.resolve_repo_root", lambda _: tmp_path)
    monkeypatch.setattr("powdrr_lift.cli.subprocess.run", fake_run)

    assert (
        main(
            [
                "validate-pr-files",
                "--pr-number",
                "123",
                "--repo-root",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert '"validator": "validate_project_structure_yaml"' in capsys.readouterr().out


def test_validate_pr_files_fails_for_invalid_changed_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "docs" / "project_structure" / "project-structure.yaml"
    output_path.parent.mkdir(parents=True)
    output_path.write_text("id: invalid\n", encoding="utf-8")

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="docs/project_structure/project-structure.yaml\n",
            stderr="",
        )

    monkeypatch.setattr("powdrr_lift.cli.resolve_repo_root", lambda _: tmp_path)
    monkeypatch.setattr("powdrr_lift.cli.subprocess.run", fake_run)

    assert main(["validate-pr-files", "--pr-number", "123"]) == 1


def test_project_structure_validator_normalizes_explicit_empty_values(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "project-structure.yaml"
    output_path.write_text(
        """# keep this comment
schema: https://powdrr.io/schemas/specification-v1
id: project-structure
modules: []
tools:
  - id: tools
    action: added
    when_to_use: null
    parent_module: root
    related_module: root
""",
        encoding="utf-8",
    )

    report = validate_project_structure_yaml(output_path)

    assert report.validation_successful is False
    rewritten = output_path.read_text(encoding="utf-8")
    assert "modules: []" not in rewritten
    assert "when_to_use: null" not in rewritten
    assert "# keep this comment" in rewritten
    assert {issue.path for issue in report.issues} >= {
        "tools[0].parent_module",
        "tools[0].related_module",
    }


def test_project_structure_normalization_preserves_comments_and_reindexes_errors(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "project-structure.yaml"
    output_path.write_text(
        """# Keep this comment.
schema: https://powdrr.io/schemas/specification-v1
id: project-structure
tools:
  - id: first
    action: added
    when_to_use: null
  - id: second
    action: added
    when_to_use: Run it.
    labels: []
""",
        encoding="utf-8",
    )

    report = validate_project_structure_yaml(output_path)

    assert output_path.read_text(encoding="utf-8").startswith("# Keep this comment.")
    assert [
        issue.path for issue in report.issues if issue.code == "missing_tool_labels"
    ] == ["tools[0].labels", "tools[1].labels"]


def test_project_structure_validator_requires_labels_on_every_tool(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "project-structure.yaml"
    output_path.write_text(
        """schema: https://powdrr.io/schemas/specification-v1
id: project-structure
tools:
  - id: test
    action: added
    when_to_use: Run tests.
    template: pytest
    how_to_use: Run pytest.
""",
        encoding="utf-8",
    )

    report = validate_project_structure_yaml(output_path)

    assert report.validation_successful is False
    assert any(
        issue.code == "missing_tool_labels" and issue.path == "tools[0].labels"
        for issue in report.issues
    )


def test_create_project_structure_template_preserves_existing_file(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "docs" / "project_structure" / "project-structure.yaml"
    output_path.parent.mkdir(parents=True)
    output_path.write_text("schema: customized\n", encoding="utf-8")

    create_project_structure_template(output_path=output_path, repo_root=tmp_path)

    assert output_path.read_text(encoding="utf-8") == "schema: customized\n"


def test_project_structure_cli_creates_default_template(tmp_path: Path) -> None:
    output_path = tmp_path / "docs" / "project_structure" / "project-structure.yaml"

    exit_code = main(["project-structure", "--repo-root", str(tmp_path)])

    assert exit_code == 0
    assert output_path.is_file()
