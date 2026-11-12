from __future__ import annotations

import json
from argparse import Namespace
from hashlib import sha256
from types import SimpleNamespace
from typing import Any

from powdrr_agent.execution.github_actions import GitHubActionsSnapshot

from powdrr_lift import cli


def test_response_command_writes_idempotent_runner_failure_checkpoint(
    monkeypatch: Any, capsys: Any
) -> None:
    head_sha = "6c85b1e5c334a113b3db274b2c1b7a8f350b9707"
    comment_bodies: list[str] = []

    class FakeClient:
        def __init__(self, _repository: str) -> None:
            pass

        def observe_pull_request(self, _number: int) -> GitHubActionsSnapshot:
            return GitHubActionsSnapshot(
                repository="example/project",
                pull_request={
                    "html_url": "https://github.com/example/project/pull/270",
                    "head": {"sha": head_sha},
                },
                runs=(
                    {
                        "id": 9,
                        "run_attempt": 1,
                        "head_sha": head_sha,
                        "path": ".github/workflows/ci.yml",
                    },
                ),
                jobs_by_run={
                    "9": (
                        {
                            "id": 10,
                            "check_run_url": "https://api.github.test/check-runs/11",
                            "status": "completed",
                            "conclusion": "failure",
                            "runner_id": 0,
                            "labels": ["macos-14-arm64"],
                            "steps": [],
                        },
                    )
                },
                annotations_by_check_run={
                    "11": (
                        {"message": "The configuration 'macos-14' is not supported"},
                    )
                },
                changed_paths=(
                    ".github/workflows/ci.yml",
                    "src/behavior.py",
                    "tests/test_behavior.py",
                ),
            )

        def get_issue(self, _number: int) -> dict[str, str]:
            return {"body": "Tracking GitHub PR #270"}

        def list_issue_comments(self, _number: int) -> tuple[dict[str, str], ...]:
            return tuple({"body": body} for body in comment_bodies)

        def add_issue_comment(self, _number: int, body: str) -> None:
            comment_bodies.append(body)

    monkeypatch.setattr(cli, "GitHubActionsClient", FakeClient)
    monkeypatch.setattr(
        cli,
        "_prepare_ci_observation_worktree",
        lambda *_args, **_kwargs: (cli.Path.cwd(), cli.Path.cwd()),
    )
    args = Namespace(
        repository="example/project",
        pr=270,
        pr_url=None,
        issue=107,
    )

    assert cli._run_respond_to_pr_ci(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["checkpoint_written"] is True
    assert first["failures"][0]["phase"] == "runner_provisioning"
    assert (
        first["failures"][0]["local_reproduction"]["status"] == "not_executable_locally"
    )
    assert first["changed_paths"] == [
        ".github/workflows/ci.yml",
        "src/behavior.py",
        "tests/test_behavior.py",
    ]
    assert first["failures"][0]["workflow_path"] == ".github/workflows/ci.yml"
    assert first["failures"][0]["runner_labels"] == ["macos-14-arm64"]

    assert cli._run_respond_to_pr_ci(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["checkpoint_written"] is False
    assert len(comment_bodies) == 1

    assert cli._run_respond_to_pr_ci(args) == 0
    json.loads(capsys.readouterr().out)
    assert len(comment_bodies) == 1

    url_args = Namespace(
        repository="example/project",
        pr=None,
        pr_url="https://github.com/example/project/pull/270",
        issue=107,
    )
    assert cli._run_respond_to_pr_ci(url_args) == 0
    assert json.loads(capsys.readouterr().out)["pull_request"] == 270


def test_response_command_rejects_pull_request_url_from_another_repository(
    monkeypatch: Any, capsys: Any
) -> None:
    args = Namespace(
        repository="example/project",
        pr=None,
        pr_url="https://github.com/other/repository/pull/270",
        issue=107,
    )

    assert cli._run_respond_to_pr_ci(args) == 2
    assert "must identify a PR in the requested repository" in capsys.readouterr().err


def test_response_command_reproduces_matching_failure_at_exact_pr_head(
    monkeypatch: Any, capsys: Any
) -> None:
    head_sha = "6c85b1e5c334a113b3db274b2c1b7a8f350b9707"
    message = "AssertionError: expected 3, got 2"
    comment_bodies: list[str] = []
    executed: list[tuple[Any, ...]] = []

    class FakeClient:
        def __init__(self, _repository: str) -> None:
            pass

        def observe_pull_request(self, _number: int) -> GitHubActionsSnapshot:
            return GitHubActionsSnapshot(
                repository="example/project",
                pull_request={
                    "html_url": "https://github.com/example/project/pull/270",
                    "head": {"sha": head_sha},
                },
                runs=({"id": 9, "run_attempt": 1, "head_sha": head_sha},),
                jobs_by_run={
                    "9": (
                        {
                            "id": 10,
                            "check_run_url": "https://api.github.test/check-runs/11",
                            "status": "completed",
                            "conclusion": "failure",
                            "runner_id": 12,
                            "steps": [],
                        },
                    )
                },
                annotations_by_check_run={
                    "11": (
                        {
                            "message": message,
                            "annotation_level": "failure",
                        },
                    )
                },
            )

        def get_issue(self, _number: int) -> dict[str, str]:
            return {"body": "Tracking https://github.com/example/project/pull/270"}

        def list_issue_comments(self, _number: int) -> tuple[dict[str, str], ...]:
            return tuple({"body": body} for body in comment_bodies)

        def add_issue_comment(self, _number: int, body: str) -> None:
            comment_bodies.append(body)

    def run_pinned(root: Any, commit: str, command: tuple[str, ...]) -> Any:
        executed.append((root, commit, command))
        return SimpleNamespace(
            stdout="",
            stderr=f"E {message}\n",
            exit_code=1,
            stdout_sha256=sha256(b"").hexdigest(),
            stderr_sha256=sha256(f"E {message}\n".encode()).hexdigest(),
        )

    monkeypatch.setattr(cli, "GitHubActionsClient", FakeClient)
    monkeypatch.setattr(
        cli,
        "_prepare_ci_observation_worktree",
        lambda *_args, **_kwargs: (cli.Path.cwd(), cli.Path.cwd()),
    )
    monkeypatch.setattr(cli, "execute_pinned_local_command", run_pinned)
    args = Namespace(
        repository="example/project",
        pr=270,
        pr_url=None,
        issue=107,
        verification_command="pytest tests/test_behavior.py -q",
    )

    assert cli._run_respond_to_pr_ci(args) == 0
    result = json.loads(capsys.readouterr().out)
    (failure,) = result["failures"]
    assert failure["local_reproduction"]["status"] == "reproduced"
    assert failure["local_reproduction"]["reason"].startswith(
        "The normalized CI failure signature appears"
    )
    assert len(executed) == 1
    assert executed[0][1:] == (
        head_sha,
        ("pytest", "tests/test_behavior.py", "-q"),
    )
    assert "local reproduction `reproduced`" in comment_bodies[0]
    assert message.lower() in comment_bodies[0]
