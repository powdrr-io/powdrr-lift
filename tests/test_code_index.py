from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from powdrr_lift import code_index_db_path, lookup_code_provenance, refresh_code_index


def test_refresh_code_index_persists_branch_snapshot_and_updates_on_new_commits(
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

    _git(repo_root, "checkout", "-b", "feature/code-index")
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
    _git(repo_root, "commit", "-m", "Add application scaffold")

    index = refresh_code_index(
        branch_name="feature/code-index",
        parent_branch="main",
        repo_root=repo_root,
    )
    db_path = code_index_db_path(repo_root)

    assert db_path.exists()
    assert index.provenance_for("src/app.py", 1).pr_number == 1
    assert index.provenance_for("src/app.py", 2).rationale == "Bootstrap the app file."

    (repo_root / "src" / "app.py").write_text(
        "print('intro')\nprint('hello')\nprint('world')\n",
        encoding="utf-8",
    )
    _git(repo_root, "add", "src/app.py")
    _git(repo_root, "commit", "-m", "Add app intro")

    refreshed = refresh_code_index(
        branch_name="feature/code-index",
        parent_branch="main",
        repo_root=repo_root,
    )

    assert refreshed.provenance_for("src/app.py", 1).pr_number == 1
    assert (
        refreshed.provenance_for("src/app.py", 3).rationale == "Bootstrap the app file."
    )
    assert (
        lookup_code_provenance(
            "src/app.py",
            3,
            branch_name="feature/code-index",
            parent_branch="main",
            repo_root=repo_root,
        ).rationale
        == "Bootstrap the app file."
    )

    with sqlite3.connect(db_path) as connection:
        branch_head_sha = connection.execute(
            "SELECT branch_head_sha FROM branch_state WHERE branch_name = ?",
            ("feature/code-index",),
        ).fetchone()[0]
        assert branch_head_sha is not None


def _git(repo_root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
