from __future__ import annotations

import io
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from powdrr_lift import lookup_edit_context
from powdrr_lift.cli import main


def test_lookup_edit_context_preserves_prior_intent_and_line_refs(
    tmp_path: Path,
) -> None:
    repo_root = _create_repo_with_edit_context_branch(tmp_path)

    report = lookup_edit_context(
        "src/app.py",
        [(1, 2), (3, 5)],
        branch_name="feature/edit-context",
        parent_branch="main",
        repo_root=repo_root,
    )

    assert report.branch_name == "feature/edit-context"
    assert report.parent_branch == "main"
    assert report.file_path == "src/app.py"
    assert len(report.matching_changes) == 3
    assert [
        requested_range.start_line for requested_range in report.requested_ranges
    ] == [1, 3]

    first_range = report.requested_ranges[0]
    second_range = report.requested_ranges[1]

    first_line_ref = first_range.lines[0].provenance_ref
    second_line_ref = first_range.lines[1].provenance_ref
    third_line_ref = second_range.lines[0].provenance_ref

    assert first_line_ref is not None
    assert second_line_ref is not None
    assert third_line_ref is not None

    assert report.matching_changes[first_line_ref - 1].pr_number == 3
    assert report.matching_changes[second_line_ref - 1].pr_number == 2
    assert report.matching_changes[third_line_ref - 1].pr_number == 1
    assert (
        report.matching_changes[first_line_ref - 1].rationale
        == "Add the new banner line."
    )
    assert (
        report.matching_changes[second_line_ref - 1].intent_goal
        == "Insert the intro line above the app scaffold."
    )


def test_cli_edit_context_emits_yaml_report(tmp_path: Path) -> None:
    repo_root = _create_repo_with_edit_context_branch(tmp_path)

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "edit-context",
                "feature/edit-context",
                "--repo-root",
                str(repo_root),
                "--parent-branch",
                "main",
                "--file",
                "src/app.py",
                "--range",
                "1:2",
            ]
        )

    assert exit_code == 0
    report = yaml.safe_load(stdout.getvalue())
    assert report["file_path"] == "src/app.py"
    assert report["matching_changes"][0]["pr_number"] == 3
    assert report["requested_ranges"][0]["lines"][0]["provenance_ref"] == 1


def _create_repo_with_edit_context_branch(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")

    (repo_root / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo_root, "add", "README.md")
    _git(repo_root, "commit", "-m", "Initial commit")

    (repo_root / "src").mkdir()
    (repo_root / "docs" / "changelogs").mkdir(parents=True, exist_ok=True)
    (repo_root / "src" / "app.py").write_text(
        "alpha\nbeta\ngamma\n",
        encoding="utf-8",
    )
    (repo_root / "docs" / "changelogs" / "PR-1-changelog.yaml").write_text(
        """
        version: 1
        change_id: 1
        title: Add the app scaffold

        intent:
          problem: The repository has no application scaffold.
          goal: Add the initial application file.

        changes:
          - file: src/app.py
            span:
              start_line: 1
              end_line: 3
            summary: Add the initial application scaffold.
            affects: []
            rationale: Bootstrap the application file.
        """,
        encoding="utf-8",
    )
    _git(
        repo_root,
        "add",
        "src/app.py",
        "docs/changelogs/PR-1-changelog.yaml",
    )
    _git(repo_root, "commit", "-m", "Add app scaffold (#1)")

    (repo_root / "src" / "app.py").write_text(
        "intro\nalpha\nbeta\ngamma\n",
        encoding="utf-8",
    )
    (repo_root / "docs" / "changelogs" / "PR-2-changelog.yaml").write_text(
        """
        version: 1
        change_id: 2
        title: Add the intro line

        intent:
          problem: The app scaffold needs an introductory line.
          goal: Insert the intro line above the app scaffold.

        changes:
          - file: src/app.py
            span:
              start_line: 1
              end_line: 1
            summary: Add the intro line.
            affects: []
            rationale: Make the application start with an intro line.
        """,
        encoding="utf-8",
    )
    _git(
        repo_root,
        "add",
        "src/app.py",
        "docs/changelogs/PR-2-changelog.yaml",
    )
    _git(repo_root, "commit", "-m", "Add intro line (#2)")

    _git(repo_root, "checkout", "-b", "feature/edit-context")
    (repo_root / "src" / "app.py").write_text(
        "banner\nintro\nalpha\nbeta\ngamma\n",
        encoding="utf-8",
    )
    (repo_root / "docs" / "changelogs" / "PR-3-changelog.yaml").write_text(
        """
        version: 1
        change_id: 3
        title: Add the banner line

        intent:
          problem: The app needs a banner before the intro line.
          goal: Add the new banner line.

        changes:
          - file: src/app.py
            span:
              start_line: 1
              end_line: 1
            summary: Add the banner line.
            affects: []
            rationale: Add the new banner line.
        """,
        encoding="utf-8",
    )
    _git(
        repo_root,
        "add",
        "src/app.py",
        "docs/changelogs/PR-3-changelog.yaml",
    )
    _git(repo_root, "commit", "-m", "Add banner line (#3)")

    return repo_root


def _git(repo_root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
