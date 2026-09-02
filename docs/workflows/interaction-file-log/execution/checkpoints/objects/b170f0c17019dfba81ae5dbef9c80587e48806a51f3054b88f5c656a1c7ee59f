from __future__ import annotations

import io
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from powdrr_lift import (
    lookup_entity_decisions,
    lookup_entity_references,
    lookup_entity_relationships,
)
from powdrr_lift.cli import main


def test_lookup_entity_decisions_returns_matching_pr_decisions(
    tmp_path: Path,
) -> None:
    repo_root = _create_repo_with_entity_branch(tmp_path)

    report = lookup_entity_decisions(
        "AppService",
        branch_name="feature/entity-context",
        parent_branch="main",
        repo_root=repo_root,
    )

    assert report.entity_name == "AppService"
    assert [decision.pr_number for decision in report.decisions] == [1, 2]
    assert [decision.decision_id for decision in report.decisions] == [
        "ARCH-001",
        "ARCH-002",
    ]
    assert [decision.decision_summary for decision in report.decisions] == [
        "Bootstrap the shared service.",
        "Extend the shared service.",
    ]


def test_lookup_entity_references_returns_matching_file_spans(tmp_path: Path) -> None:
    repo_root = _create_repo_with_entity_branch(tmp_path)

    report = lookup_entity_references(
        "AppService",
        branch_name="feature/entity-context",
        parent_branch="main",
        repo_root=repo_root,
    )

    assert report.entity_name == "AppService"
    assert [reference.pr_number for reference in report.references] == [1, 2]
    assert [reference.file for reference in report.references] == [
        "src/app.py",
        "src/app.py",
    ]
    assert [reference.span.start_line for reference in report.references] == [1, 2]
    assert report.references[0].affects == ("AppService",)


def test_lookup_entity_relationships_returns_context(tmp_path: Path) -> None:
    repo_root = _create_repo_with_entity_branch(tmp_path)

    report = lookup_entity_relationships(
        "AppService",
        branch_name="feature/entity-context",
        parent_branch="main",
        repo_root=repo_root,
    )

    assert report.entity_name == "AppService"
    assert [occurrence.pr_number for occurrence in report.entity_occurrences] == [
        1,
        2,
    ]
    assert [relationship.relationship for relationship in report.relationships] == [
        "reads_from",
        "writes_to",
    ]


def test_cli_entity_lookup_commands_emit_yaml(tmp_path: Path) -> None:
    repo_root = _create_repo_with_entity_branch(tmp_path)

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "entity-references",
                "feature/entity-context",
                "--repo-root",
                str(repo_root),
                "--parent-branch",
                "main",
                "--entity",
                "AppService",
            ]
        )

    assert exit_code == 0
    report = yaml.safe_load(stdout.getvalue())
    assert report["entity_name"] == "AppService"
    assert [entry["pr_number"] for entry in report["references"]] == [1, 2]

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "entity-decisions",
                "feature/entity-context",
                "--repo-root",
                str(repo_root),
                "--parent-branch",
                "main",
                "--entity",
                "AppService",
            ]
        )

    assert exit_code == 0
    report = yaml.safe_load(stdout.getvalue())
    assert report["entity_name"] == "AppService"
    assert [entry["pr_number"] for entry in report["decisions"]] == [1, 2]

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "entity-relationships",
                "feature/entity-context",
                "--repo-root",
                str(repo_root),
                "--parent-branch",
                "main",
                "--entity",
                "AppService",
            ]
        )

    assert exit_code == 0
    report = yaml.safe_load(stdout.getvalue())
    assert report["entity_name"] == "AppService"
    assert [entry["relationship"] for entry in report["relationships"]] == [
        "reads_from",
        "writes_to",
    ]


def _create_repo_with_entity_branch(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")

    (repo_root / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo_root, "add", "README.md")
    _git(repo_root, "commit", "-m", "Initial commit")

    (repo_root / "src").mkdir()
    (repo_root / "src" / "app.py").write_text("alpha\nbeta\n", encoding="utf-8")
    (repo_root / "docs").mkdir()
    (repo_root / "docs" / "changelogs").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "changelogs" / "PR-1-changelog.yaml").write_text(
        """
        version: 1
        change_id: 1
        title: Introduce AppService

        intent:
          problem: AppService does not exist.
          goal: Introduce the shared service.

        decisions:
          - id: ARCH-001
            summary: Bootstrap the shared service.

        entities:
          - id: AppService
            type: Service
            action: added
          - id: RedisCache
            type: Cache
            action: added

        changes:
          - file: src/app.py
            span:
              start_line: 1
              end_line: 1
            summary: Introduce AppService.
            affects:
              - AppService
            rationale: Bootstrap the shared service.

        relationship_changes:
          - action: add
            source: AppService
            target: RedisCache
            relationship: reads_from
            rationale: AppService depends on RedisCache.
        """,
        encoding="utf-8",
    )
    _git(
        repo_root,
        "add",
        "src/app.py",
        "docs/changelogs/PR-1-changelog.yaml",
    )
    _git(repo_root, "commit", "-m", "Introduce AppService (#1)")

    _git(repo_root, "checkout", "-b", "feature/entity-context")
    (repo_root / "src" / "app.py").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    (repo_root / "docs" / "changelogs" / "PR-2-changelog.yaml").write_text(
        """
        version: 1
        change_id: 2
        title: Update AppService

        intent:
          problem: AppService needs a second change.
          goal: Update the app service implementation.

        decisions:
          - id: ARCH-002
            summary: Extend the shared service.

        entities:
          - id: AppService
            type: Service
          - id: QueueService
            type: Service
            action: added

        changes:
          - file: src/app.py
            span:
              start_line: 2
              end_line: 2
            summary: Update AppService.
            affects:
              - AppService
            rationale: Extend the shared service.

        relationship_changes:
          - action: add
            source: AppService
            target: QueueService
            relationship: writes_to
            rationale: AppService now writes to QueueService.
        """,
        encoding="utf-8",
    )
    _git(
        repo_root,
        "add",
        "src/app.py",
        "docs/changelogs/PR-2-changelog.yaml",
    )
    _git(repo_root, "commit", "-m", "Update AppService (#2)")

    return repo_root


def _git(repo_root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
