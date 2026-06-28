from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest
import yaml

from powdrr_lift.cli import main
from powdrr_lift.core import (
    feature_planning_specification as feature_planning,
)
from powdrr_lift.core import (
    feature_pr_specification_default_output_path,
    system_map_specification_default_output_path,
)


def test_create_system_map_specification_template_prepopulates_current_index(
    tmp_path: Path,
) -> None:
    repo_root = _create_repo_with_structured_specs_and_changelogs(tmp_path)
    output_path = system_map_specification_default_output_path("powdrr-lift", repo_root)

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "system-map-specification",
                "--work-item-name",
                "powdrr-lift",
                "--repo-root",
                str(repo_root),
            ]
        )

    assert exit_code == 0
    assert output_path.exists()
    assert str(output_path) in stdout.getvalue()
    template_text = output_path.read_text(encoding="utf-8")
    assert "# System map specification template." in template_text
    assert (
        "# - Analyze the full codebase deeply before writing anything." in template_text
    )
    assert (
        "# - Delete these instructions and replace with a comment saying that"
        in template_text
    )
    assert (
        "#   this file is read-only and should never be editted by a tool or"
        in template_text
    )
    assert "system-map-feature-current-state" in template_text
    assert "System map synthesized for feature/current-state" in template_text
    assert "req-1" in template_text
    assert "app-1" in template_text
    assert "Alpha" in template_text
    assert "rel-1" in template_text
    assert "inv-1" in template_text
    assert "guide-1" in template_text
    assert "feature-1" in template_text
    assert "decision-1" in template_text
    assert "proposed_prs:" not in template_text
    assert "feature_ids:" not in template_text

    rendered_template = yaml.safe_load(template_text)
    assert rendered_template["id"] == "system-map-feature-current-state"
    assert rendered_template["title"] == (
        "System map synthesized for feature/current-state"
    )
    assert [item["id"] for item in rendered_template["requirements"]] == ["req-1"]
    assert [item["id"] for item in rendered_template["approach"]] == ["app-1"]
    assert [item["id"] for item in rendered_template["entities"]] == [
        "Alpha",
        "Beta",
        "Gamma",
    ]
    assert [item["id"] for item in rendered_template["entity_relationships"]] == [
        "rel-1",
        "rel-2",
    ]
    assert [item["id"] for item in rendered_template["invariants"]] == ["inv-1"]
    assert [item["id"] for item in rendered_template["guidance"]] == ["guide-1"]
    assert [item["id"] for item in rendered_template["features"]] == [
        "feature-1",
        "feature-2",
    ]
    assert [item["id"] for item in rendered_template["decisions"]] == ["decision-1"]
    assert [section for section in rendered_template] == [
        "schema",
        "id",
        "title",
        "requirements",
        "approach",
        "entities",
        "entity_relationships",
        "invariants",
        "guidance",
        "features",
        "decisions",
    ]


def test_create_system_map_specification_template_uses_compact_instructions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")
    (repo_root / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo_root, "add", "README.md")
    _git(repo_root, "commit", "-m", "Initial commit")

    monkeypatch.setattr(
        feature_planning,
        "_should_use_compact_system_map_instructions",
        lambda *args, **kwargs: True,
    )
    output_path = system_map_specification_default_output_path("powdrr-lift", repo_root)
    exit_code = main(
        [
            "system-map-specification",
            "--work-item-name",
            "powdrr-lift",
            "--repo-root",
            str(repo_root),
        ]
    )

    assert exit_code == 0
    template_text = output_path.read_text(encoding="utf-8")
    assert (
        "# - This file is already complete, delete this line and then move "
        "on to the next step" in template_text
    )
    assert (
        "# - Delete these instructions and replace with a comment saying that"
        in template_text
    )
    assert (
        "#   this file is read-only and should never be editted by a tool or"
        in template_text
    )
    assert (
        "# - Analyze the full codebase deeply before writing anything."
        not in template_text
    )


def test_create_feature_pr_specification_template_writes_default_file(
    tmp_path: Path,
) -> None:
    output_path = feature_pr_specification_default_output_path(
        "powdrr-lift",
        tmp_path,
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "feature-pr-specification",
                "--work-item-name",
                "powdrr-lift",
                "--repo-root",
                str(tmp_path),
            ]
        )

    assert exit_code == 0
    assert output_path.exists()
    assert str(output_path) in stdout.getvalue()
    template_text = output_path.read_text(encoding="utf-8")
    assert "# Feature and PR specification template." in template_text
    assert (
        "# - Start from the filled system map template and the requested feature."
        in template_text
    )
    assert "requirements:" in template_text
    assert "approach:" in template_text
    assert "entities:" in template_text
    assert "entity_relationships:" in template_text
    assert "invariants:" in template_text
    assert "guidance:" in template_text
    assert "features:" in template_text
    assert "decisions:" in template_text
    assert "feature_ids:" in template_text
    assert "intent:" in template_text
    assert "acceptance_criteria:" in template_text
    assert "expected_tests:" in template_text
    assert "expected_outcomes:" in template_text
    assert "non_goals:" in template_text
    assert "risks:" in template_text

    rendered_template = yaml.safe_load(template_text)
    assert [section for section in rendered_template] == [
        "schema",
        "id",
        "title",
        "requirements",
        "approach",
        "entities",
        "entity_relationships",
        "invariants",
        "guidance",
        "features",
        "decisions",
        "feature_ids",
        "intent",
        "acceptance_criteria",
        "expected_tests",
        "expected_outcomes",
        "non_goals",
        "risks",
    ]


def _create_repo_with_structured_specs_and_changelogs(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")

    (repo_root / "README.md").write_text("initial\n", encoding="utf-8")
    (repo_root / "docs").mkdir()
    (repo_root / "docs" / "specs").mkdir(parents=True, exist_ok=True)
    _write_structured_specs(repo_root)
    _git(repo_root, "add", "README.md", "docs/specs")
    _git(repo_root, "commit", "-m", "Add structured specs")

    _git(repo_root, "checkout", "-b", "feature/current-state")
    (repo_root / "docs" / "changelogs").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "changelogs" / "PR-1-changelog.yaml").write_text(
        """
        schema: https://powdrr.io/schemas/changelog-v1
        version: 2
        change_id: 1
        title: Seed current state

        intent:
          problem: Keep the report coherent.
          goal: Track the current state through indexed specs.

        invariants:
          - id: inv-1
            description: Keep the service boundary explicit.
            action: added

        guidance:
          - id: guide-1
            description: Mention the synthesized current state in reviews.
            action: added

        features:
          - id: feature-1
            state: in_progress
          - id: feature-2
            state: completed
          - id: feature-3
            state: removed

        decisions:
          - id: decision-1
            description: Keep the current state report aligned with indexed specs.
            action: added
        """,
        encoding="utf-8",
    )
    _git(repo_root, "add", "docs/changelogs/PR-1-changelog.yaml")
    _git(repo_root, "commit", "-m", "Seed current state (#1)")

    return repo_root


def _write_structured_specs(repo_root: Path) -> None:
    (repo_root / "docs" / "specs" / "powdrr-lift").mkdir(parents=True, exist_ok=True)
    (
        repo_root / "docs" / "specs" / "powdrr-lift" / "system-specification.yaml"
    ).write_text(
        """
        schema: https://powdrr.io/schemas/specification-v1
        id: "2026-06-23-current-state-system"
        title: "Current state system"
        requirements:
          - id: req-1
            description: Keep the current state report coherent.
            state: added
        approach:
          - id: app-1
            description: Build the current state report from indexed docs.
            state: added
        """,
        encoding="utf-8",
    )
    (
        repo_root / "docs" / "specs" / "powdrr-lift" / "architecture-specification.yaml"
    ).write_text(
        """
        schema: https://powdrr.io/schemas/specification-v1
        id: "2026-06-23-current-state-architecture"
        title: "Current state architecture"
        entities:
          - id: Alpha
            type: Skill
            summary: The alpha skill.
            rationale: "req-1"
          - id: Beta
            type: Skill
            summary: The beta skill.
            rationale: "req-1"
          - id: Gamma
            type: Skill
            summary: The gamma skill.
            rationale: "req-1"
        entity_relationships:
          - id: rel-1
            source: Alpha
            target: Beta
            relationship: depends_on
            description: Alpha depends on Beta.
            rationale: "req-1"
          - id: rel-2
            source: Beta
            target: Gamma
            relationship: depends_on
            description: Beta depends on Gamma.
            rationale: "req-1"
        invariants:
          - id: inv-1
            description: Keep the alpha boundary explicit.
            rationale: "req-1"
            related:
              entities:
                - Alpha
              entity_relationships:
                - rel-1
        guidance:
          - id: guide-1
            description: Mention the current state in reviews.
            rationale: "req-1"
            related:
              entities:
                - Beta
              entity_relationships:
                - rel-2
        """,
        encoding="utf-8",
    )
    (
        repo_root
        / "docs"
        / "specs"
        / "powdrr-lift"
        / "implementation-specification.yaml"
    ).write_text(
        """
        schema: https://powdrr.io/schemas/specification-v1
        id: "2026-06-23-current-state-implementation"
        title: "Current state implementation"
        architecture_id: "2026-06-23-current-state-architecture"
        features:
          - id: feature-1
            action: added
            description: Build the current state report.
            functional_requirements:
              - req-1
          - id: feature-2
            action: added
            description: Keep the state report stable.
            functional_requirements:
              - req-1
        decisions:
          - id: decision-1
            action: added
            description: Keep the current state reporting deterministic.
        """,
        encoding="utf-8",
    )


def _git(repo_root: Path, *args: str) -> None:
    import subprocess

    subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
