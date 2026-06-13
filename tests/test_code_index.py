from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

import powdrr_lift.core.code_index as code_index_store
import powdrr_lift.core.index as core_index
from powdrr_lift import (
    Span,
    code_index_db_path,
    lookup_code_provenance,
    refresh_code_index,
)


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


def test_refresh_code_index_uses_pr_description_when_changelog_is_missing(
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

    _git(repo_root, "checkout", "-b", "feature/code-index")
    (repo_root / "src").mkdir()
    (repo_root / "src" / "app.py").write_text(
        "print('hello')\nprint('world')\n",
        encoding="utf-8",
    )
    _git(repo_root, "add", "src/app.py")
    _git(repo_root, "commit", "-m", "Add application scaffold")

    def _fake_fetch_pr_metadata(
        repo_root_arg: Path,
        *gh_args: str,
    ) -> dict[str, object]:
        assert repo_root_arg == repo_root
        assert gh_args in {
            (
                "list",
                "--head",
                "feature/code-index",
                "--base",
                "main",
                "--state",
                "all",
                "--limit",
                "1",
            ),
            ("view", "21"),
        }
        return {
            "number": 21,
            "title": "Add application scaffold",
            "body": "Introduce the app file without a changelog artifact.",
        }

    monkeypatch.setattr(core_index, "_fetch_pr_metadata", _fake_fetch_pr_metadata)

    index = refresh_code_index(
        branch_name="feature/code-index",
        parent_branch="main",
        repo_root=repo_root,
    )

    assert index.documents[0].changelog_path.name == "PR-21-description.md"
    assert index.provenance_for("src/app.py", 1).pr_number == 21
    assert index.provenance_for("src/app.py", 1).title == "Add application scaffold"
    assert (
        index.provenance_for("src/app.py", 1).intent_goal
        == "Introduce the app file without a changelog artifact."
    )


def test_refresh_code_index_uses_commit_body_when_pr_is_missing(
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
    (repo_root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    _git(repo_root, "add", "src/app.py")
    _git(
        repo_root,
        "commit",
        "-m",
        "Add helper",
        "-m",
        "Track intent in the commit comment.",
    )

    index = refresh_code_index(
        branch_name="feature/code-index",
        parent_branch="main",
        repo_root=repo_root,
    )

    provenance = index.provenance_for("src/app.py", 1)
    assert provenance is not None
    assert provenance.kind == "commented"
    assert provenance.title == "Add helper"
    assert provenance.intent_goal == "Track intent in the commit comment."
    assert lookup_code_provenance(
        "src/app.py",
        1,
        branch_name="feature/code-index",
        parent_branch="main",
        repo_root=repo_root,
    ).kind == "commented"


def test_refresh_code_index_uses_sparse_spans_from_pr_description(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")

    (repo_root / "README.md").write_text("initial\n", encoding="utf-8")
    (repo_root / "src").mkdir()
    (repo_root / "src" / "app.py").write_text(
        "\n".join(
            [
                "line 1",
                "line 2",
                "line 3",
                "line 4",
                "line 5",
                "line 6",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo_root, "add", "README.md")
    _git(repo_root, "add", "src/app.py")
    _git(repo_root, "commit", "-m", "Initial commit")

    _git(repo_root, "checkout", "-b", "feature/code-index")
    (repo_root / "src" / "app.py").write_text(
        "\n".join(
            [
                "line 1",
                "line 3",
                "line 4",
                "line 5",
                "line 6",
                "line 7",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo_root, "add", "src/app.py")
    _git(repo_root, "commit", "-m", "Sparse app changes (#22)")

    def _fake_fetch_pr_metadata(
        repo_root_arg: Path,
        *gh_args: str,
    ) -> dict[str, object]:
        assert repo_root_arg == repo_root
        assert gh_args in {
            (
                "list",
                "--head",
                "feature/code-index",
                "--base",
                "main",
                "--state",
                "all",
                "--limit",
                "1",
            ),
            ("view", "22"),
        }
        return {
            "number": 22,
            "title": "Sparse app changes",
            "body": "Skip a line and append a new one without a changelog artifact.",
        }

    monkeypatch.setattr(core_index, "_fetch_pr_metadata", _fake_fetch_pr_metadata)

    index = refresh_code_index(
        branch_name="feature/code-index",
        parent_branch="main",
        repo_root=repo_root,
    )

    assert index.changes[0].span == Span(start_line=2, end_line=6)


def test_backfill_line_state_from_blame_fills_missing_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    sha = "a" * 40
    provenance = core_index.ProvenanceRecord(
        kind="declared",
        pr_number=7,
        commit_sha=sha,
        commit_timestamp=1,
        changelog_path="docs/changelogs/PR-7-changelog.yaml",
        title="Add changelog provenance index",
        change_id="7",
        intent_problem="Missing provenance lines.",
        intent_goal="Backfill missing provenance from blame.",
        file="src/app.py",
        span=Span(start_line=2, end_line=2),
        summary="Backfill missing provenance.",
        rationale="Use local blame data to fill gaps.",
        change_index=0,
    )
    line_state: dict[str, list[core_index.ProvenanceRecord | None]] = {
        "src/app.py": [None, None, None]
    }
    changes_by_commit_and_file = {(sha, "src/app.py"): [provenance]}

    def _fake_git_output(repo_root_arg: Path, *args: str) -> str:
        assert repo_root_arg == repo_root
        assert args == ("blame", "--line-porcelain", "--", "src/app.py")
        return "\n".join(
            [
                f"{sha} 1 1 3",
                "\tline 1",
                f"{sha} 2 2 1",
                "\tline 2",
                f"{sha} 3 3 1",
                "\tline 3",
            ]
        )

    monkeypatch.setattr(core_index, "_git_output", _fake_git_output)

    core_index._backfill_line_state_from_blame(  # noqa: SLF001
        repo_root,
        line_state,
        changes_by_commit_and_file,
    )

    assert line_state["src/app.py"][0] == provenance
    assert line_state["src/app.py"][1] == provenance
    assert line_state["src/app.py"][2] == provenance


def test_refresh_code_index_rebuilds_when_parent_snapshot_changes(
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
    (repo_root / "docs" / "changelogs" / "PR-30-changelog.yaml").write_text(
        """
        version: 1
        change_id: 30
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
    _git(repo_root, "add", "src/app.py", "docs/changelogs/PR-30-changelog.yaml")
    _git(repo_root, "commit", "-m", "Add application scaffold (#30)")

    index = refresh_code_index(
        branch_name="feature/code-index",
        parent_branch="main",
        repo_root=repo_root,
    )
    assert index.provenance_for("src/app.py", 1).pr_number == 30

    db_path = code_index_db_path(repo_root)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE branch_state SET index_version = 0 WHERE branch_name = ?",
            ("main",),
        )
        connection.execute(
            """
            UPDATE branch_state
            SET parent_index_version = 0
            WHERE branch_name = ?
            """,
            ("feature/code-index",),
        )
        connection.commit()

    stale_feature_state = refresh_code_index(
        branch_name="feature/code-index",
        parent_branch="main",
        repo_root=repo_root,
    )

    assert stale_feature_state.provenance_for("src/app.py", 1).pr_number == 30
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        feature_row = connection.execute(
            """
            SELECT index_version, parent_index_version
            FROM branch_state
            WHERE branch_name = ?
            """,
            ("feature/code-index",),
        ).fetchone()
        main_row = connection.execute(
            """
            SELECT index_version
            FROM branch_state
            WHERE branch_name = ?
            """,
            ("main",),
        ).fetchone()

    assert feature_row["index_version"] == code_index_store.INDEX_CACHE_VERSION
    assert feature_row["parent_index_version"] == code_index_store.INDEX_CACHE_VERSION
    assert main_row["index_version"] == code_index_store.INDEX_CACHE_VERSION


def _git(repo_root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
