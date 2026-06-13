from __future__ import annotations

import subprocess
from pathlib import Path

from powdrr_lift import blame_view_state_to_data, build_blame_view_state


def test_build_blame_view_state_groups_lines_by_provenance_and_builds_tree(
    tmp_path: Path,
) -> None:
    repo_root = _create_repo_with_blame_branch(tmp_path)

    state = build_blame_view_state(
        repo_root=repo_root,
        branch_name="feature/blame-view",
        parent_branch="main",
        selected_file="src/app.py",
    )

    assert state.selected_file == "src/app.py"
    assert state.file_view is not None
    assert state.file_view.path == "src/app.py"
    assert [chunk.provenance_ref for chunk in state.file_view.chunks] == [1, 2, 3]
    assert [provenance.pr_number for provenance in state.file_view.provenances] == [
        3,
        2,
        1,
    ]
    assert state.file_view.selected_chunk_ref == 1
    assert any(node.path == "src" and node.kind == "dir" for node in state.tree)

    data = blame_view_state_to_data(state)
    assert data["selected_file"] == "src/app.py"
    assert data["file_view"]["chunks"][0]["start_line"] == 1
    assert data["file_view"]["provenances"][0]["pr_number"] == 3


def _create_repo_with_blame_branch(tmp_path: Path) -> Path:
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

    _git(repo_root, "checkout", "-b", "feature/blame-view")
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
