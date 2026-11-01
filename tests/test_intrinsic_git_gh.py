from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from powdrr_lift import intrinsic_git_gh
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.intrinsic_git_gh import (
    execute_intrinsic_git_gh_tool,
    intrinsic_command,
)
from powdrr_lift.workrr.task_agent import _git_operation_parameters


def test_git_intrinsic_operations_have_expected_commands() -> None:
    assert intrinsic_command({"operation": "status"}, tool="git") == [
        "status",
        "--short",
    ]
    review_threads = intrinsic_command(
        {
            "operation": "pr_review_threads",
            "repository": "powdrr-io/powdrr-lift",
            "pr_reference": "432",
        },
        tool="gh",
    )
    assert review_threads[:2] == ["api", "graphql"]
    assert "reviewThreads(first: 100, after: $after)" in review_threads[3]
    assert "owner=powdrr-io" in review_threads
    assert "name=powdrr-lift" in review_threads
    assert "number=432" in review_threads
    reply = intrinsic_command(
        {
            "operation": "pr_review_reply",
            "thread_id": "PRRT_thread-id",
            "body": "This is addressed in the latest push.",
        },
        tool="gh",
    )
    assert reply[-2:] == ["-f", "body=This is addressed in the latest push."]
    resolve = intrinsic_command(
        {"operation": "pr_review_resolve", "thread_id": "PRRT_thread-id"},
        tool="gh",
    )
    assert resolve[-2:] == ["-f", "threadId=PRRT_thread-id"]
    assert intrinsic_command({"operation": "head"}, tool="git") == [
        "rev-parse",
        "HEAD",
    ]
    assert _git_operation_parameters(["rev-parse", "HEAD"]) == {"operation": "head"}
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
        {"operation": "pr_view", "json_fields": ["number", "url"]}, tool="gh"
    ) == ["pr", "view", "--json", "number,url"]
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
    assert intrinsic_command(
        {
            "operation": "pr_publish_clean",
            "title": "Add feature",
            "body": "Summary",
            "base": "main",
            "publication_branch": "powdrr/feature-implementation",
            "files_to_publish": ["src/a.py"],
            "commit_message": "Implement feature",
        },
        tool="gh",
    )[-4:] == [
        "--base",
        "main",
        "--head",
        "powdrr/feature-implementation",
    ]


def test_gh_edit_rejects_a_model_owned_target(tmp_path: Path) -> None:
    with pytest.raises(PowdrrExecutionError, match="pr_reference"):
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


def test_gh_create_rejects_model_branch_overrides(tmp_path: Path) -> None:
    with pytest.raises(PowdrrExecutionError, match="base, head"):
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


def test_empty_git_commit_is_an_idempotent_success(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test User"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "initial"],
        check=True,
        capture_output=True,
        text=True,
    )

    result = execute_intrinsic_git_gh_tool(
        "git",
        {"operation": "commit", "message": "initial"},
        worktree_root=tmp_path,
    )

    assert result["returncode"] == 0
    assert result["no_op"] is True
    assert "No changes to commit" in result["stdout"]


def test_existing_pull_request_makes_create_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if args[:3] == ["git", "branch", "--show-current"]:
            return subprocess.CompletedProcess(args, 0, "workflow/demo\n", "")
        if args[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                args,
                0,
                '{"url":"https://github.com/example/repo/pull/7","state":"OPEN"}',
                "",
            )
        raise AssertionError(args)

    monkeypatch.setattr("powdrr_lift.intrinsic_git_gh.subprocess.run", fake_run)
    result = execute_intrinsic_git_gh_tool(
        "gh",
        {"operation": "pr_create", "title": "Title", "body": "Body"},
        worktree_root=tmp_path,
    )

    assert result["returncode"] == 0
    assert result["no_op"] is True
    assert result["stdout"] == "https://github.com/example/repo/pull/7\n"


def test_review_thread_query_collects_every_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def page(
        thread_id: str, *, has_next_page: bool, end_cursor: str | None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["gh", "api", "graphql"],
            0,
            json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "id": "PR_kwDOA",
                                "number": 432,
                                "title": "Add feature",
                                "body": "Proposed PR: feature-part-2",
                                "state": "OPEN",
                                "baseRefName": "main",
                                "headRefName": "feature/part-2",
                                "headRefOid": "abc123",
                                "reviewThreads": {
                                    "nodes": [
                                        {
                                            "id": thread_id,
                                            "isResolved": False,
                                            "isOutdated": False,
                                            "path": "src/example.py",
                                            "line": 12,
                                            "comments": {
                                                "nodes": [],
                                                "pageInfo": {
                                                    "hasNextPage": False,
                                                    "endCursor": None,
                                                },
                                            },
                                        }
                                    ],
                                    "pageInfo": {
                                        "hasNextPage": has_next_page,
                                        "endCursor": end_cursor,
                                    },
                                },
                            }
                        }
                    }
                }
            ),
            "",
        )

    pages = [
        page("THREAD_1", has_next_page=True, end_cursor="cursor-1"),
        page("THREAD_2", has_next_page=False, end_cursor=None),
    ]
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return pages.pop(0)

    monkeypatch.setattr(intrinsic_git_gh.subprocess, "run", fake_run)
    result = execute_intrinsic_git_gh_tool(
        "gh",
        {
            "operation": "pr_review_threads",
            "repository": "powdrr-io/powdrr-lift",
            "pr_reference": "432",
        },
        worktree_root=tmp_path,
    )

    payload = json.loads(result["stdout"])
    assert result["returncode"] == 0
    assert payload["pull_request"]["headRefOid"] == "abc123"
    assert [thread["id"] for thread in payload["review_threads"]] == [
        "THREAD_1",
        "THREAD_2",
    ]
    assert commands[0][:3] == ["gh", "api", "graphql"]
    assert commands[1][-2:] == ["-f", "after=cursor-1"]


def test_review_thread_query_collects_all_replies_in_a_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread_page = {
        "data": {
            "repository": {
                "pullRequest": {
                    "id": "PR_kwDOA",
                    "number": 432,
                    "title": "Add feature",
                    "body": "",
                    "state": "OPEN",
                    "baseRefName": "main",
                    "headRefName": "feature/part-2",
                    "headRefOid": "abc123",
                    "reviewThreads": {
                        "nodes": [
                            {
                                "id": "THREAD_1",
                                "isResolved": False,
                                "isOutdated": False,
                                "path": "src/example.py",
                                "line": 12,
                                "comments": {
                                    "nodes": [{"id": "COMMENT_1"}],
                                    "pageInfo": {
                                        "hasNextPage": True,
                                        "endCursor": "comment-cursor-1",
                                    },
                                },
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            }
        }
    }
    comment_page = {
        "data": {
            "node": {
                "comments": {
                    "nodes": [{"id": "COMMENT_2"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
    }
    responses = [thread_page, comment_page]
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(responses.pop(0)), "")

    monkeypatch.setattr(intrinsic_git_gh.subprocess, "run", fake_run)
    result = execute_intrinsic_git_gh_tool(
        "gh",
        {
            "operation": "pr_review_threads",
            "repository": "powdrr-io/powdrr-lift",
            "pr_reference": 432,
        },
        worktree_root=tmp_path,
    )

    payload = json.loads(result["stdout"])
    comments = payload["review_threads"][0]["comments"]["nodes"]
    assert result["returncode"] == 0
    assert [comment["id"] for comment in comments] == ["COMMENT_1", "COMMENT_2"]
    assert "threadId=THREAD_1" in commands[1]
    assert "after=comment-cursor-1" in commands[1]
