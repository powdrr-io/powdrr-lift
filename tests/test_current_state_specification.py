from __future__ import annotations

import io
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from powdrr_lift import (
    build_codebase_state_report,
    build_current_state_specification_report,
    current_state_specification_default_output_path,
)
from powdrr_lift.cli import main


def test_build_current_state_specification_report_synthesizes_indexed_specs(
    tmp_path: Path,
) -> None:
    repo_root = _create_repo_with_structured_specs_and_changelogs(tmp_path)

    report = build_current_state_specification_report(
        branch_name="feature/current-state",
        parent_branch="main",
        repo_root=repo_root,
    )

    assert report["schema"] == "https://powdrr.io/schemas/specification-v1"
    assert [item["id"] for item in report["requirements"]] == ["req-1"]
    assert report["requirements"][0]["state"] == "added"
    assert [item["id"] for item in report["approach"]] == ["app-1"]
    assert [item["id"] for item in report["entities"]] == [
        "Alpha",
        "Beta",
        "Gamma",
    ]
    assert [item["id"] for item in report["entity_relationships"]] == [
        "rel-1",
        "rel-2",
    ]
    assert [item["id"] for item in report["features"]] == [
        "feature-1",
        "feature-2",
    ]
    assert report["features"][0]["action"] == "added"
    assert [item["id"] for item in report["decisions"]] == ["decision-1"]
    assert [item["id"] for item in report["invariants"]] == ["inv-1"]
    assert [item["id"] for item in report["guidance"]] == ["guide-1"]
    assert [item["id"] for item in report["proposed_prs"]] == ["pr-101"]
    assert report["proposed_prs"][0]["state"] == "in_progress"
    assert len(report["intents"]) == 1

    codebase_state = build_codebase_state_report(
        branch_name="feature/current-state",
        parent_branch="main",
        repo_root=repo_root,
    )
    assert [feature.id for feature in codebase_state.features] == [
        "feature-1",
        "feature-2",
    ]
    assert [proposed_pr.id for proposed_pr in codebase_state.proposed_prs] == [
        "pr-101",
        "pr-102",
    ]
    assert codebase_state.proposed_prs[1].state == "completed"


def test_cli_synthesize_current_state_writes_default_file(tmp_path: Path) -> None:
    repo_root = _create_repo_with_structured_specs_and_changelogs(tmp_path)
    expected_output_path = current_state_specification_default_output_path(repo_root)

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "synthesize-current-state",
                "feature/current-state",
                "--repo-root",
                str(repo_root),
                "--parent-branch",
                "main",
            ]
        )

    assert exit_code == 0
    assert expected_output_path.exists()
    assert str(expected_output_path) in stdout.getvalue()

    report = yaml.safe_load(expected_output_path.read_text(encoding="utf-8"))
    assert [item["id"] for item in report["requirements"]] == ["req-1"]
    assert [item["id"] for item in report["proposed_prs"]] == ["pr-101"]
    assert report["proposed_prs"][0]["state"] == "in_progress"


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

        prs:
          - id: pr-101
            state: in_progress
          - id: pr-102
            state: completed
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
        entity_relationships:
          - id: rel-1
            source: Alpha
            target: Beta
            relationship: depends_on
            description: Alpha depends on Beta.
            rationale: "req-1"
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
        architecture_id: "2026-06-23-current-state-architecture"
        title: "Current state implementation"
        entities:
          - id: Gamma
            action: added
            rationale: "req-1"
        entity_relationships:
          - id: rel-2
            action: added
            source: Gamma
            target: Alpha
            relationship: depends_on
            rationale: "req-1"
        features:
          - id: feature-1
            action: added
            description: Keep the current state report coherent.
            functional_requirements:
              - The report must come from indexed docs.
          - id: feature-2
            action: added
            description: Hide completed proposed PRs from the report.
            functional_requirements:
              - Completed proposed PRs must be omitted.
        decisions:
          - id: decision-1
            action: added
            description: Build the current state report from indexed docs.
        """,
        encoding="utf-8",
    )


def _git(repo_root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
