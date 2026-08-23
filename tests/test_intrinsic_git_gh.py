from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from powdrr_lift.intrinsic_git_gh import (
    execute_intrinsic_git_gh_tool,
    intrinsic_command,
)


def test_git_intrinsic_operations_have_expected_commands() -> None:
    assert intrinsic_command({"operation": "status"}, tool="git") == [
        "status",
        "--short",
    ]
    assert intrinsic_command(
        {"operation": "add", "paths": ["docs/a.yaml", "docs/b.yaml"]},
        tool="git",
    ) == ["add", "docs/a.yaml", "docs/b.yaml"]
    assert intrinsic_command(
        {"operation": "move", "source": "old.yaml", "destination": "new.yaml"},
        tool="git",
    ) == ["mv", "old.yaml", "new.yaml"]


def test_git_intrinsic_rejects_worktree_escape() -> None:
    with pytest.raises(RuntimeError, match="stay inside the worktree"):
        intrinsic_command(
            {"operation": "add", "paths": ["../outside.txt"]},
            tool="git",
        )


def test_gh_intrinsic_operations_have_expected_commands() -> None:
    assert intrinsic_command(
        {"operation": "pr_view", "pr_reference": "394"}, tool="gh"
    ) == ["pr", "view", "394"]
    assert intrinsic_command(
        {
            "operation": "pr_create",
            "title": "Add feature",
            "body": "Summary",
            "draft": True,
        },
        tool="gh",
    ) == ["pr", "create", "--draft", "--title", "Add feature", "--body", "Summary"]


def test_git_intrinsic_executes_only_inside_the_worktree(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "old.yaml").write_text("value: true\n", encoding="utf-8")

    result = execute_intrinsic_git_gh_tool(
        "git",
        {"operation": "add", "paths": ["old.yaml"]},
        worktree_root=tmp_path,
    )

    assert result["returncode"] == 0
    assert result["command"] == ["git", "add", "old.yaml"]
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == "A  old.yaml\n"
