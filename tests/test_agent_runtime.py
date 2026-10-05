from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from powdrr_lift.agent_runtime import prepare_agent_worktree
from powdrr_lift.errors import PowdrrExecutionError


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _repository(root: Path) -> Path:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("def run():\n    return 'ok'\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "initial")
    return root


def test_prepare_agent_worktree_uses_dedicated_branch_and_keeps_checkout_clean(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo")
    start = _git(root, "rev-parse", "HEAD")

    task = prepare_agent_worktree(root, "sample task", run_id="attempt-1")

    assert task.starting_commit == start
    assert task.branch == "powdrr/sample-task-attempt-1"
    assert task.worktree.is_dir()
    assert _git(task.worktree, "branch", "--show-current") == task.branch
    assert _git(task.worktree, "rev-parse", "HEAD") == start
    assert _git(root, "status", "--porcelain") == ""

    with pytest.raises(PowdrrExecutionError, match="already exists"):
        prepare_agent_worktree(root, "sample task", run_id="attempt-1")


def test_prepare_agent_worktree_refuses_to_clean_or_stash_user_changes(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo")
    (root / "notes.txt").write_text("preserve me")

    with pytest.raises(PowdrrExecutionError, match="notes.txt"):
        prepare_agent_worktree(root, "sample task", run_id="attempt-1")

    assert (root / "notes.txt").read_text() == "preserve me"
    assert not (root / ".git" / "powdrr-lift" / "worktrees").exists()
