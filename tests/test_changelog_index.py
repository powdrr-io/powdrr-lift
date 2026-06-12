from __future__ import annotations

import subprocess
from pathlib import Path

from powdrr_lift import build_changelog_index


def test_build_changelog_index_tracks_lineage_across_later_insertions(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")

    (repo_root / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo_root, "add", "README.md")
    _git(repo_root, "commit", "-m", "Initial commit")

    (repo_root / "src").mkdir()
    (repo_root / "src" / "app.py").write_text(
        "print('hello')\nprint('world')\n",
        encoding="utf-8",
    )
    (repo_root / "docs").mkdir()
    (repo_root / "docs" / "changelogs").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "changelogs" / "PR-1-changelog.yaml").write_text(
        """
        version: 1
        change_id: 1
        title: Add application scaffold

        intent:
          problem: The repository had no application entry point.
          goal: Introduce the initial application file.

        changes:
          - file: src/app.py
            span:
              start_line: 1
              end_line: 2
            summary: Add the initial application body.
            affects: []
            rationale: Bootstrap the app file.
        """,
        encoding="utf-8",
    )
    _git(repo_root, "add", "src/app.py", "docs/changelogs/PR-1-changelog.yaml")
    _git(repo_root, "commit", "-m", "Add application scaffold (#1)")

    (repo_root / "src" / "app.py").write_text(
        "print('intro')\nprint('hello')\nprint('world')\n",
        encoding="utf-8",
    )
    (repo_root / "docs" / "changelogs" / "PR-2-changelog.yaml").write_text(
        """
        version: 1
        change_id: 2
        title: Add app intro

        intent:
          problem: The application output should start with a short intro.
          goal: Prepend an intro line to the app.

        changes:
          - file: src/app.py
            span:
              start_line: 1
              end_line: 1
            summary: Insert an intro line.
            affects: []
            rationale: Make the output start with an intro.
        """,
        encoding="utf-8",
    )
    _git(repo_root, "add", "src/app.py", "docs/changelogs/PR-2-changelog.yaml")
    _git(repo_root, "commit", "-m", "Add app intro (#2)")

    index = build_changelog_index(repo_root)

    assert index.documents[0].commit_sha is not None
    assert index.documents[1].commit_sha is not None
    assert index.provenance_for("src/app.py", 1).pr_number == 2
    assert index.provenance_for("src/app.py", 1).kind == "declared"
    assert index.provenance_for("src/app.py", 2).pr_number == 1
    assert (
        index.provenance_for("src/app.py", 2).summary
        == "Add the initial application body."
    )
    assert index.provenance_for("src/app.py", 3).pr_number == 1
    assert index.provenance_for("src/app.py", 3).rationale == "Bootstrap the app file."
    assert (
        index.provenance_for("docs/changelogs/PR-1-changelog.yaml", 1).kind
        == "artifact"
    )
    assert index.provenance_for("docs/changelogs/PR-1-changelog.yaml", 1).pr_number == 1


def _git(repo_root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
