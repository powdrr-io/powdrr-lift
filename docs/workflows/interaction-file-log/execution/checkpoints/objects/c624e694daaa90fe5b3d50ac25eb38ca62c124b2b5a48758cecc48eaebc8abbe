from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from powdrr_lift.pr_workflow_record import (
    is_pull_request_create_command,
    pull_request_number,
    record_pull_request_workflow,
)


def _git(repo: Path, *arguments: str) -> None:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_pull_request_record_is_committed_and_accumulates_tool_calls(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "branch", "-M", "main")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    _git(repo, "switch", "-c", "feature/pr-record")

    path = record_pull_request_workflow(
        repo,
        42,
        branch="feature/pr-record",
        base_branch="main",
        title="Example PR",
        workflow_name="create-pull-request",
        workflow_path="docs/workflows/example",
        steps=[{"id": "stage", "description": "Stage changes"}],
        events=[
            {
                "kind": "invoke_tool",
                "tool": "shell",
                "parameters": {"command": ["git", "add", "src"]},
                "result": {"returncode": 0},
                "decisions_and_context": "Stage only approved files.",
            }
        ],
        explanation="The record makes the automated PR auditable.",
    )

    assert path == repo / "docs" / "prs" / "42.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert document["pull_request"] == 42
    assert document["workflow"]["skills"] == ["create-pull-request"]
    assert document["tool_calls"][0]["why"] == "Stage only approved files."
    assert (
        "Add workflow record for PR 42"
        in subprocess.run(
            ["git", "log", "-1", "--pretty=%s"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )

    record_pull_request_workflow(
        repo,
        42,
        branch="feature/pr-record",
        base_branch="main",
        title="Example PR",
        workflow_name="create-pull-request",
        workflow_path="docs/workflows/example",
        steps=[],
        events=[
            {
                "kind": "invoke_tool",
                "tool": "shell",
                "parameters": {"command": ["git", "push"]},
                "result": {"returncode": 0},
            }
        ],
        explanation="The record makes the automated PR auditable.",
    )
    updated = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert len(updated["tool_calls"]) == 2


def test_pull_request_command_and_url_parsing() -> None:
    assert is_pull_request_create_command(["gh", "pr", "create", "--draft"])
    assert is_pull_request_create_command(["rtk", "gh", "pr", "create"])
    assert not is_pull_request_create_command(["gh", "pr", "edit", "42"])
    assert pull_request_number("https://github.com/example/repo/pull/42\n") == 42
    assert pull_request_number("no pull request") is None


def test_pull_request_record_preserves_root_workflow_and_nested_skill_events(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "branch", "-M", "main")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    _git(repo, "switch", "-c", "feature/root-workflow")

    record_pull_request_workflow(
        repo,
        43,
        branch="feature/root-workflow",
        base_branch="main",
        title="Example PR",
        workflow_name="specify-a-feature",
        workflow_path="skill-definitions/specify-a-feature.yaml",
        steps=[{"id": "capture-goal", "description": "Capture the goal"}],
        events=[
            {"kind": "invoke_skill", "skill": "review-system"},
            {
                "kind": "invoke_tool",
                "tool": "shell",
                "parameters": {"command": ["git", "add", "docs"]},
                "result": {"returncode": 0},
            },
        ],
        explanation="The record makes the automated PR auditable.",
    )

    document = yaml.safe_load(
        (repo / "docs" / "prs" / "43.yaml").read_text(encoding="utf-8")
    )
    assert document["workflow"]["name"] == "specify-a-feature"
    assert document["workflow"]["steps"] == [
        {"id": "capture-goal", "description": "Capture the goal"}
    ]
    assert document["workflow"]["skills"] == [
        "specify-a-feature",
        "review-system",
    ]
    assert document["tool_calls"][0]["parameters"]["command"] == [
        "git",
        "add",
        "docs",
    ]
