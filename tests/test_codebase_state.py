from __future__ import annotations

import io
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from powdrr_lift import build_codebase_state_report, codebase_state_default_output_path
from powdrr_lift.cli import main


def test_build_codebase_state_report_collates_current_state(tmp_path: Path) -> None:
    repo_root = _create_repo_with_state_branch(tmp_path)

    report = build_codebase_state_report(
        branch_name="feature/state",
        parent_branch="main",
        repo_root=repo_root,
    )

    assert report.branch_name == "feature/state"
    assert report.parent_branch == "main"
    assert [entity.id for entity in report.entities] == ["Alpha"]
    assert report.entities[0].type == "Service"
    assert report.entities[0].last_action == "modified"
    assert report.entities[0].source.pr_number == 2
    assert report.relationships == []
    assert [item.id for item in report.invariants] == ["inv-1"]
    assert report.invariants[0].description == (
        "Keep the service boundary explicit and current."
    )
    assert report.invariants[0].last_action == "altered"
    assert report.guidance == []
    assert [decision.key for decision in report.decisions] == ["DEC-001"]
    assert len(report.decisions[0].sources) == 2
    assert report.decisions[0].summary == "Define the current codebase snapshot."
    assert [intent.problem for intent in report.intents] == [
        "Keep the state description up to date."
    ]
    assert len(report.intents[0].sources) == 2


def test_cli_codebase_state_writes_default_file(tmp_path: Path) -> None:
    repo_root = _create_repo_with_state_branch(tmp_path)
    expected_output_path = codebase_state_default_output_path(repo_root)

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "codebase-state",
                "feature/state",
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
    assert [entity["id"] for entity in report["entities"]] == ["Alpha"]
    assert [decision["key"] for decision in report["decisions"]] == ["DEC-001"]
    assert len(report["decisions"][0]["sources"]) == 2


def _create_repo_with_state_branch(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")

    (repo_root / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo_root, "add", "README.md")
    _git(repo_root, "commit", "-m", "Initial commit")

    (repo_root / "docs").mkdir()
    (repo_root / "docs" / "changelogs").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "changelogs" / "PR-1-changelog.yaml").write_text(
        """
        version: 2
        change_id: 1
        title: Describe the initial state

        intent:
          problem: Keep the state description up to date.
          goal: Describe the codebase using changelog-derived data.

        decisions:
          - id: DEC-001
            summary: Define the current codebase snapshot.

        entities:
          - id: Alpha
            type: Service
            action: added
          - id: Beta
            type: Service
            action: added

        entity_relationships:
          - id: rel-1
            source: Alpha
            target: Beta
            relationship: depends_on
            rationale: Alpha depends on Beta.
            action: added

        invariants:
          - id: inv-1
            description: Keep the service boundary explicit.
            action: added

        guidance:
          - id: guide-1
            description: Mention the shared service in status updates.
            action: added
        """,
        encoding="utf-8",
    )
    _git(repo_root, "add", "docs/changelogs/PR-1-changelog.yaml")
    _git(repo_root, "commit", "-m", "Describe the initial state (#1)")

    _git(repo_root, "checkout", "-b", "feature/state")
    (repo_root / "docs" / "changelogs" / "PR-2-changelog.yaml").write_text(
        """
        version: 2
        change_id: 2
        title: Refine the current state

        intent:
          problem: Keep the state description up to date.
          goal: Describe the codebase using changelog-derived data.

        decisions:
          - id: DEC-001
            summary: Define the current codebase snapshot.

        entities:
          - id: Alpha
            type: Service
            action: modified
          - id: Beta
            type: Service
            action: deleted

        entity_relationships:
          - id: rel-1
            source: Alpha
            target: Beta
            relationship: depends_on
            rationale: Remove the dependency once Beta is retired.
            action: removed

        invariants:
          - id: inv-1
            description: Keep the service boundary explicit and current.
            action: altered

        guidance:
          - id: guide-1
            description: Mention the shared service in status updates.
            action: removed
        """,
        encoding="utf-8",
    )
    _git(repo_root, "add", "docs/changelogs/PR-2-changelog.yaml")
    _git(repo_root, "commit", "-m", "Refine the current state (#2)")

    return repo_root


def _git(repo_root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
