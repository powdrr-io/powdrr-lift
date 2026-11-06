from __future__ import annotations

import json
from argparse import Namespace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from powdrr_agent.execution.github_actions import GitHubActionsSnapshot

from powdrr_lift import cli
from powdrr_lift.workrr import github_issue_queue, task_agent


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
        "_prepare_ci_response_worktree",
        lambda *_args, **_kwargs: (cli.Path.cwd(), cli.Path.cwd()),
    )
    args = Namespace(
        repository="example/project",
        pr=270,
        pr_url=None,
        issue=107,
        observe_only=True,
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

    url_args = Namespace(
        repository="example/project",
        pr=None,
        pr_url="https://github.com/example/project/pull/270",
        issue=107,
        observe_only=True,
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
        "_prepare_ci_response_worktree",
        lambda *_args, **_kwargs: (cli.Path.cwd(), cli.Path.cwd()),
    )
    monkeypatch.setattr(cli, "execute_pinned_local_command", run_pinned)
    args = Namespace(
        repository="example/project",
        pr=270,
        pr_url=None,
        issue=107,
        verification_command="pytest tests/test_behavior.py -q",
        observe_only=True,
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


def test_ci_repair_cli_builds_and_runs_issue_backed_procedrr_task(
    monkeypatch: Any, tmp_path: Path
) -> None:
    plan_item = SimpleNamespace(
        item_id="ci-config",
        title="Correct CI runner configuration",
        objective="Use a supported runner for the repository workflow.",
        scope=("Update the affected GitHub Actions workflow.",),
        allowed_paths=(".github/workflows/ci.yml",),
        implementation_steps=("Replace the unsupported runner label.",),
        acceptance_criteria=("The workflow uses a supported runner.",),
        dependencies=(),
    )
    plan = SimpleNamespace(plan_id="plan-ci", items=(plan_item,))
    monkeypatch.setattr(
        github_issue_queue, "load_reviewed_plan_at_commit", lambda *a, **k: plan
    )
    task = SimpleNamespace(task_id="ci-response-task-001")
    generated: dict[str, Any] = {}

    def instantiate(**kwargs: Any) -> tuple[Path, tuple[Any, ...]]:
        generated.update(kwargs)
        return tmp_path / "workflow", (task,)

    monkeypatch.setattr(cli, "instantiate_procedrr_process_workflow", instantiate)
    observed: dict[str, Any] = {}

    def run_workflow(config: Any, **_kwargs: Any) -> int:
        observed["config"] = config
        manifest = config.repo_root / ".powdrr/procedrr-runs/ci-response-task-001.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{"status":"succeeded"}\n', encoding="utf-8")
        config.task_checkpoint_callback(
            "ci-response-task-001",
            "repair-result",
            {"pull_request_url": "https://github.com/example/repo/pull/270"},
            None,
        )
        return 0

    monkeypatch.setattr(task_agent, "run_workflow_task", run_workflow)
    comments: list[tuple[int, str]] = []
    client = SimpleNamespace(
        add_issue_comment=lambda issue_number, body: comments.append(
            (issue_number, body)
        )
    )
    issue_body = (
        "<!-- powdrr-queue-item:v1 "
        '{"plan_id":"plan-ci","item_id":"ci-config",'
        '"plan_path":"docs/plans/plan-ci.yaml",'
        '"plan_commit":"0123456789abcdef0123456789abcdef01234567"} -->'
    )
    args = Namespace(
        repository="powdrr-io/powdrr-gaterr",
        issue=107,
        verification_command="pytest -q",
        provider="deepinfra-cheap",
        api_key=None,
        base_url=None,
        max_roundtrips=128,
        context_compaction_threshold=0.75,
        verbose=False,
        minisweagent_executable="mini",
        minisweagent_model="deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731",
        coding_agent_timeout_seconds=1800.0,
    )
    observation = {
        "repository": args.repository,
        "pull_request": 270,
        "issue": args.issue,
        "pull_request_metadata": {
            "head_ref": "powdrr/change",
            "base_ref": "main",
            "title": "Change CI",
            "body": "Original PR body",
        },
        "changed_paths": ["src/code.py"],
    }

    result = cli._launch_ci_repair_workflow(
        args=args,
        client=client,
        issue={"body": issue_body},
        prior_comments=(),
        observation=observation,
        head_sha="a" * 40,
        clone_root=tmp_path / "repository",
        worker_root=tmp_path / "worktree",
    )

    assert result["exit_code"] == 0
    assert generated["process_id"] == "run-ci-repair-from-observation"
    assert generated["input_state"]["pull_request_url"].endswith("/pull/270")
    assert generated["input_state"]["implementation_context"]["item_id"] == "ci-config"
    config = observed["config"]
    assert config.repo_root == tmp_path / "worktree"
    assert config.allow_unmanaged_git is True
    assert config.publish_workflow_progress is False
    assert config.provider == "deepinfra-cheap"
    assert result["status"] == "succeeded"
    assert len(comments) == 2
    assert "ci-response-checkpoint" in comments[1][1]
