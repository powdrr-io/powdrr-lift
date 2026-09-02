from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from powdrr_lift.errors import PowdrrExecutionError
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
    assert intrinsic_command(
        {"operation": "switch_create", "branch": "workflow/demo"}, tool="git"
    ) == ["switch", "-c", "workflow/demo"]
    assert intrinsic_command(
        {"operation": "push", "branch": "workflow/demo"}, tool="git"
    ) == ["push", "--set-upstream", "origin", "workflow/demo"]


def test_git_intrinsic_rejects_worktree_escape() -> None:
    with pytest.raises(PowdrrExecutionError, match="stay inside the worktree"):
        intrinsic_command(
            {"operation": "add", "paths": ["../outside.txt"]},
            tool="git",
        )


def test_intrinsics_require_structured_operations() -> None:
    with pytest.raises(PowdrrExecutionError, match="structured operation"):
        intrinsic_command({"command": ["status", "--short"]}, tool="git")
    with pytest.raises(PowdrrExecutionError, match="structured operation"):
        intrinsic_command({"command": ["pr", "view", "394"]}, tool="gh")


def test_gh_intrinsic_operations_have_expected_commands() -> None:
    assert intrinsic_command(
        {"operation": "pr_view", "pr_reference": "394"}, tool="gh"
    ) == ["pr", "view", "394"]
    assert intrinsic_command(
        {
            "operation": "pr_view",
            "pr_reference": "394",
            "json_fields": ["number", "url", "headRefOid"],
        },
        tool="gh",
    ) == ["pr", "view", "394", "--json", "number,url,headRefOid"]
    assert intrinsic_command(
        {
            "operation": "pr_create",
            "title": "Add feature",
            "body": "Summary",
        },
        tool="gh",
    ) == ["pr", "create", "--draft", "--title", "Add feature", "--body", "Summary"]
    assert intrinsic_command(
        {
            "operation": "pr_create",
            "title": "Add feature",
            "body": "Summary",
            "draft": False,
        },
        tool="gh",
    ) == ["pr", "create", "--draft", "--title", "Add feature", "--body", "Summary"]
    assert intrinsic_command(
        {
            "operation": "pr_review_comment",
            "repository": "powdrr-io/powdrr-lift",
            "pr_reference": "432",
            "body": "Fix the design scope.",
            "commit_id": "abc123",
            "path": "docs/proposal.yaml",
            "line": 12,
            "side": "RIGHT",
        },
        tool="gh",
    ) == [
        "api",
        "repos/powdrr-io/powdrr-lift/pulls/432/comments",
        "--method",
        "POST",
        "-f",
        "body=Fix the design scope.",
        "-f",
        "commit_id=abc123",
        "-f",
        "path=docs/proposal.yaml",
        "-F",
        "line=12",
        "-f",
        "side=RIGHT",
    ]
    assert intrinsic_command(
        {
            "operation": "pr_create",
            "title": "Add feature",
            "body": "Summary",
            "base": "main",
            "head": "workflow/demo",
        },
        tool="gh",
    )[-4:] == ["--base", "main", "--head", "workflow/demo"]


def test_gh_edit_uses_the_current_branch_as_runtime_owned_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(command)
        if command[:3] == ["git", "branch", "--show-current"]:
            return SimpleNamespace(returncode=0, stdout="workflow/current\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("powdrr_lift.intrinsic_git_gh.subprocess.run", fake_run)
    execute_intrinsic_git_gh_tool(
        "gh",
        {
            "operation": "pr_edit",
            "pr_reference": "PR-40",
            "title": "Update",
            "body": "Body",
        },
        worktree_root=tmp_path,
    )

    assert calls[-1] == [
        "gh",
        "pr",
        "edit",
        "workflow/current",
        "--title",
        "Update",
        "--body",
        "Body",
    ]


def test_gh_create_does_not_use_model_branch_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("powdrr_lift.intrinsic_git_gh.subprocess.run", fake_run)
    execute_intrinsic_git_gh_tool(
        "gh",
        {
            "operation": "pr_create",
            "title": "Create",
            "body": "Body",
            "base": "main",
            "head": "PR-40",
        },
        worktree_root=tmp_path,
    )

    assert calls[-1][-4:] == ["--title", "Create", "--body", "Body"]


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
