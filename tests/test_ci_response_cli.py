from __future__ import annotations

import json
from argparse import Namespace
from hashlib import sha256
from types import SimpleNamespace
from typing import Any

from powdrr_agent.execution.github_actions import GitHubActionsSnapshot
from powdrr_contracts.ci import CIFailureUnit, CIJobLogEvidence, CIJobObservation
from powdrr_service.ci_response import build_failure_units

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
                    "body": "Closes #107",
                    "draft": True,
                    "head": {
                        "sha": head_sha,
                        "ref": "feature/example",
                        "repo": {"full_name": "example/project"},
                    },
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
                logs_by_job={
                    "10": CIJobLogEvidence(
                        status="available",
                        excerpt="failure: token=<redacted>",
                        raw_sha256=sha256(b"failure: token=secret").hexdigest(),
                    )
                },
                changed_paths=(
                    ".github/workflows/ci.yml",
                    "src/behavior.py",
                    "tests/test_behavior.py",
                ),
            )

        def get_issue(self, _number: int) -> dict[str, str]:
            return {"body": "Tracking implementation request"}

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
    assert first["pull_request_metadata"]["draft"] is True
    assert first["pull_request_metadata"]["head_repository"] == "example/project"
    assert first["observation_schema_version"] == 1
    assert first["tested_sha"] == head_sha
    assert first["required_checks_status"] == "unavailable"
    assert first["response_state"] == "blocked"
    assert "PR is a draft" in comment_bodies[0]
    assert first["failures"][0]["phase"] == "runner_provisioning"
    failure = CIFailureUnit.from_dict(first["failures"][0])
    assert failure.observation_id == first["observation_id"]
    assert failure.head_sha == head_sha
    assert failure.workflow_path == ".github/workflows/ci.yml"
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
    assert first["job_log_evidence"]["10"]["excerpt"] == "failure: token=<redacted>"
    assert "job_log:10:sha256:" in first["failures"][0]["evidence"][-1]
    assert "secret" not in comment_bodies[0]

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


def test_local_reproduction_targets_the_test_node_from_ci_logs(
    monkeypatch: Any,
) -> None:
    head_sha = "6c85b1e5c334a113b3db274b2c1b7a8f350b9707"
    log_line = "FAILED tests/test_calc.py::test_sum - AssertionError: expected 2, got 3"
    (failure,) = build_failure_units(
        (
            CIJobObservation(
                job_id=21,
                run_id=22,
                run_attempt=1,
                head_sha=head_sha,
                status="completed",
                conclusion="failure",
                check_context="CI / unit",
                steps=({"name": "pytest", "conclusion": "failure"},),
                log_evidence=CIJobLogEvidence(
                    status="available",
                    excerpt=log_line,
                    raw_sha256=sha256(log_line.encode()).hexdigest(),
                ),
            ),
        )
    )
    commands: list[tuple[str, ...]] = []

    def run_pinned(
        _root: Any, _commit: str, command: tuple[str, ...]
    ) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(
            stdout=log_line,
            stderr="",
            exit_code=1,
            stdout_sha256=sha256(log_line.encode()).hexdigest(),
            stderr_sha256=sha256(b"").hexdigest(),
        )

    monkeypatch.setattr(cli, "execute_pinned_local_command", run_pinned)

    reproductions = cli._reproduce_ci_failures(
        (failure,),
        repository_root=cli.Path.cwd(),
        head_sha=head_sha,
        verification_command="pytest -q",
    )

    assert commands == [
        ("pytest", "-vv", "tests/test_calc.py::test_sum"),
    ]
    assert reproductions[failure.failure_id].status == "reproduced"


def test_response_command_retries_issue_link_lookup_for_eventual_consistency(
    monkeypatch: Any, capsys: Any
) -> None:
    head_sha = "6c85b1e5c334a113b3db274b2c1b7a8f350b9707"
    comment_reads = 0
    checkpoints: list[str] = []

    class DelayedIssueLinkClient:
        def __init__(self, _repository: str) -> None:
            pass

        def get_issue(self, _number: int) -> dict[str, str]:
            return {"body": "Tracking implementation request"}

        def list_issue_comments(self, _number: int) -> tuple[dict[str, str], ...]:
            nonlocal comment_reads
            comment_reads += 1
            if comment_reads == 1:
                return ()
            return ({"body": "Powdrr PR: #270"},)

        def observe_pull_request(self, _number: int) -> GitHubActionsSnapshot:
            return GitHubActionsSnapshot(
                repository="example/project",
                pull_request={
                    "html_url": "https://github.com/example/project/pull/270",
                    "head": {"sha": head_sha},
                },
                runs=(),
                jobs_by_run={},
                annotations_by_check_run={},
            )

        def add_issue_comment(self, _number: int, body: str) -> None:
            checkpoints.append(body)

    monkeypatch.setattr(cli, "GitHubActionsClient", DelayedIssueLinkClient)
    monkeypatch.setattr(cli.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(
        cli,
        "_prepare_ci_observation_worktree",
        lambda *_args, **_kwargs: (cli.Path.cwd(), cli.Path.cwd()),
    )
    args = Namespace(repository="example/project", pr=270, pr_url=None, issue=107)

    assert cli._run_respond_to_pr_ci(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["pull_request"] == 270
    assert comment_reads == 2
    assert len(checkpoints) == 1


def test_response_command_checkpoints_before_wait_and_records_current_head_success(
    monkeypatch: Any, capsys: Any
) -> None:
    head_sha = "6c85b1e5c334a113b3db274b2c1b7a8f350b9707"
    comments: list[str] = []
    refreshes = 0

    class WaitingClient:
        def __init__(self, _repository: str) -> None:
            pass

        def get_issue(self, _number: int) -> dict[str, str]:
            return {"body": "Tracking implementation request"}

        def list_issue_comments(self, _number: int) -> tuple[dict[str, str], ...]:
            return tuple({"body": body} for body in comments)

        def observe_pull_request(self, _number: int) -> GitHubActionsSnapshot:
            return GitHubActionsSnapshot(
                repository="example/project",
                pull_request={
                    "html_url": "https://github.com/example/project/pull/270",
                    "body": "Closes #107",
                    "draft": False,
                    "head": {"sha": head_sha},
                },
                runs=(),
                jobs_by_run={},
                annotations_by_check_run={},
                required_checks=(
                    {"name": "unit", "state": "PENDING", "workflow": "ci.yml"},
                ),
            )

        def refresh_required_checks(
            self, _number: int, expected_head: str
        ) -> tuple[tuple[dict[str, str], ...], None]:
            nonlocal refreshes
            assert expected_head == head_sha
            refreshes += 1
            return (
                ({"name": "unit", "state": "SUCCESS", "workflow": "ci.yml"},),
                None,
            )

        def add_issue_comment(self, _number: int, body: str) -> None:
            comments.append(body)

    monkeypatch.setattr(cli, "GitHubActionsClient", WaitingClient)
    monkeypatch.setattr(
        cli,
        "_prepare_ci_observation_worktree",
        lambda *_args, **_kwargs: (cli.Path.cwd(), cli.Path.cwd()),
    )
    monkeypatch.setattr(cli.time, "sleep", lambda _delay: None)
    args = Namespace(
        repository="example/project",
        pr=270,
        pr_url=None,
        issue=107,
        wait_deadline_seconds=5,
        poll_interval_seconds=1,
    )

    assert cli._run_respond_to_pr_ci(args) == 0
    result = json.loads(capsys.readouterr().out)

    assert refreshes == 1
    assert result["response_state"] == "awaiting_review"
    assert result["required_checks"][0]["state"] == "SUCCESS"
    assert result["checkpoint_written"] is True
    assert len(comments) == 2
    assert "**awaiting_ci**" in comments[0]
    assert "**awaiting_review**" in comments[1]
    assert "Required check `unit`: **SUCCESS**" in comments[1]


def test_response_command_records_wait_deadline_when_checks_remain_pending(
    monkeypatch: Any, capsys: Any
) -> None:
    head_sha = "6c85b1e5c334a113b3db274b2c1b7a8f350b9707"
    comments: list[str] = []
    refreshes = 0
    ticks = iter((0.0, 0.0, 1.0, 1.0, 1.0, 1.0))

    class PendingClient:
        def __init__(self, _repository: str) -> None:
            pass

        def get_issue(self, _number: int) -> dict[str, str]:
            return {"body": "Tracking implementation request"}

        def list_issue_comments(self, _number: int) -> tuple[dict[str, str], ...]:
            return tuple({"body": body} for body in comments)

        def observe_pull_request(self, _number: int) -> GitHubActionsSnapshot:
            return GitHubActionsSnapshot(
                repository="example/project",
                pull_request={
                    "html_url": "https://github.com/example/project/pull/270",
                    "body": "Closes #107",
                    "draft": False,
                    "head": {"sha": head_sha},
                },
                runs=(),
                jobs_by_run={},
                annotations_by_check_run={},
                required_checks=(
                    {"name": "unit", "state": "PENDING", "workflow": "ci.yml"},
                ),
            )

        def refresh_required_checks(
            self, _number: int, expected_head: str
        ) -> tuple[tuple[dict[str, str], ...], None]:
            nonlocal refreshes
            assert expected_head == head_sha
            refreshes += 1
            return (
                ({"name": "unit", "state": "PENDING", "workflow": "ci.yml"},),
                None,
            )

        def add_issue_comment(self, _number: int, body: str) -> None:
            comments.append(body)

    monkeypatch.setattr(cli, "GitHubActionsClient", PendingClient)
    monkeypatch.setattr(
        cli,
        "_prepare_ci_observation_worktree",
        lambda *_args, **_kwargs: (cli.Path.cwd(), cli.Path.cwd()),
    )
    monkeypatch.setattr(cli.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(ticks))
    args = Namespace(
        repository="example/project",
        pr=270,
        pr_url=None,
        issue=107,
        wait_deadline_seconds=1,
        poll_interval_seconds=1,
    )

    assert cli._run_respond_to_pr_ci(args) == 0
    result = json.loads(capsys.readouterr().out)

    assert refreshes == 1
    assert result["response_state"] == "awaiting_ci"
    assert result["wait_deadline_reached"] is True
    assert result["waited_seconds"] == 1
    assert len(comments) == 2
    assert "Check wait **waiting**" in comments[0]
    assert "Check wait **deadline_reached**" in comments[1]


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


def test_response_command_marks_last_checkpoint_stale_when_refresh_fails(
    monkeypatch: Any, capsys: Any
) -> None:
    head_sha = "6c85b1e5c334a113b3db274b2c1b7a8f350b9707"
    observed_at = "2026-11-13T12:00:00+00:00"
    comments: list[dict[str, str]] = []
    observe_calls = 0

    class IntermittentClient:
        def __init__(self, _repository: str) -> None:
            pass

        def get_issue(self, _number: int) -> dict[str, str]:
            return {"body": "Tracking GitHub PR #270"}

        def list_issue_comments(self, _number: int) -> tuple[dict[str, str], ...]:
            return tuple(comments)

        def observe_pull_request(self, _number: int) -> GitHubActionsSnapshot:
            nonlocal observe_calls
            observe_calls += 1
            if observe_calls > 1:
                raise cli.GitHubActionsError("network unavailable")
            return GitHubActionsSnapshot(
                repository="example/project",
                pull_request={
                    "html_url": "https://github.com/example/project/pull/270",
                    "head": {"sha": head_sha},
                },
                runs=(),
                jobs_by_run={},
                annotations_by_check_run={},
                observed_at=observed_at,
            )

        def add_issue_comment(self, _number: int, body: str) -> None:
            comments.append({"body": body})

    monkeypatch.setattr(cli, "GitHubActionsClient", IntermittentClient)
    monkeypatch.setattr(
        cli,
        "_prepare_ci_observation_worktree",
        lambda *_args, **_kwargs: (cli.Path.cwd(), cli.Path.cwd()),
    )
    args = Namespace(repository="example/project", pr=270, pr_url=None, issue=107)

    assert cli._run_respond_to_pr_ci(args) == 0
    capsys.readouterr()
    assert (
        "Observed at `2026-11-13T12:00:00+00:00`; freshness **current**."
        in comments[0]["body"]
    )

    assert cli._run_respond_to_pr_ci(args) == 2
    assert "network unavailable" in capsys.readouterr().err
    assert len(comments) == 2
    stale_body = comments[1]["body"]
    assert "<!-- powdrr-ci-response-stale:v1 -->" in stale_body
    assert "**stale; current status unknown**" in stale_body
    assert f"head `{head_sha}` at `{observed_at}`" in stale_body
    assert '"status": "unknown"' in stale_body
    assert '"last_remote_observed_at": "2026-11-13T12:00:00+00:00"' in stale_body

    assert cli._run_respond_to_pr_ci(args) == 2
    capsys.readouterr()
    assert len(comments) == 2


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
