from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Sequence
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from powdrr_lift.agent_feature import run_agent_feature_task
from powdrr_lift.agent_runtime import AgentWorktree
from powdrr_lift.cli import main
from powdrr_lift.workrr.feature_endpoint import (
    FeatureEndpointConfig,
    FeatureEndpointResult,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _repository(root: Path) -> Path:
    root.mkdir()
    _git(root, "init", "-q", "--initial-branch=main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "initial")
    return root


def _config(root: Path) -> FeatureEndpointConfig:
    return FeatureEndpointConfig(
        feature_description="Set the app value to two.",
        work_item_name="Set app value",
        repo_root=root,
        allowed_paths=("src",),
    )


def _fake_feature(
    config: FeatureEndpointConfig, *, runner: Any = subprocess.run
) -> FeatureEndpointResult:
    del runner
    assert config.prepared_worktree is not None
    (config.prepared_worktree / "src" / "app.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    return FeatureEndpointResult(
        status="completed",
        branch=config.prepared_branch or "",
        worktree=config.prepared_worktree,
        baseline_path=config.prepared_worktree / "baseline.yaml",
        plan_path=config.prepared_worktree / "plan.yaml",
        request_path=None,
        attempt=None,
        validation=None,
        review={"passed": True},
        task_id=config.task_id,
    )


def test_agent_feature_runs_locally_in_an_isolated_worktree(
    tmp_path: Path, monkeypatch: Any
) -> None:
    root = _repository(tmp_path / "repo")
    original_head = _git(root, "rev-parse", "HEAD")
    monkeypatch.setattr("powdrr_lift.agent_feature.run_feature_endpoint", _fake_feature)

    result = run_agent_feature_task(
        _config(root), run_id="local-feature", output_root=tmp_path / "reports"
    )

    assert result.status == "completed_local"
    assert result.task is not None
    assert result.task.worktree.is_dir()
    assert result.task.branch.startswith("powdrr/set-app-value-")
    assert _git(root, "rev-parse", "HEAD") == original_head
    assert _git(result.task.worktree, "log", "-1", "--format=%s") == (
        "Implement Set app value"
    )
    assert result.report_json_path.is_file()
    assert result.report_markdown_path.is_file()


def test_agent_feature_opens_pr_only_when_requested(
    tmp_path: Path, monkeypatch: Any
) -> None:
    root = _repository(tmp_path / "repo")
    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "push", "--set-upstream", "origin", "main")
    _git(root, "remote", "set-head", "origin", "main")
    monkeypatch.setattr("powdrr_lift.agent_feature.run_feature_endpoint", _fake_feature)
    gh_calls: list[list[str]] = []

    def runner(
        command: Sequence[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        args = list(command)
        if args[0] == "gh":
            gh_calls.append(args)
            return subprocess.CompletedProcess(
                args, 0, "https://github.com/acme/app/pull/42\n", ""
            )
        return subprocess.run(args, **kwargs)

    result = run_agent_feature_task(
        _config(root),
        open_pr=True,
        run_id="published-feature",
        output_root=tmp_path / "reports",
        runner=runner,
    )

    assert result.status == "pr_opened"
    assert result.pull_request_url == "https://github.com/acme/app/pull/42"
    assert len(gh_calls) == 1
    assert "Uncertainty decisions" in gh_calls[0][gh_calls[0].index("--body") + 1]
    assert _git(root, "status", "--porcelain") == ""


def test_implement_cli_forces_headless_normative_defaults(
    tmp_path: Path, monkeypatch: Any
) -> None:
    root = _repository(tmp_path / "repo")
    observed: dict[str, Any] = {}

    def fake_preflight(args: Any) -> int:
        print(json.dumps({"passed": True, "checks": []}))
        return 0

    def fake_task(config: FeatureEndpointConfig, **kwargs: Any) -> Any:
        observed["config"] = config
        observed["kwargs"] = kwargs
        from powdrr_lift.agent_feature import AgentFeatureTaskResult

        return AgentFeatureTaskResult(
            "no_op",
            AgentWorktree(root, root, "powdrr/example-run", "abc123", None, None),
            None,
            tmp_path / "report.json",
            tmp_path / "report.md",
        )

    monkeypatch.setattr("powdrr_lift.cli._run_check_credentials", fake_preflight)
    monkeypatch.setattr(
        "powdrr_lift.cli.resolve_workflow_provider", lambda value: value
    )
    monkeypatch.setattr(
        "powdrr_lift.cli.configured_model_mapping",
        lambda provider, model: type(
            "Mapping", (), {"provider": provider, "model": model or "test-model"}
        )(),
    )
    monkeypatch.setattr(
        "powdrr_lift.cli.resolve_provider_credentials", lambda *args: object()
    )
    monkeypatch.setattr(
        "powdrr_lift.cli.build_workflow_client", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr("powdrr_lift.cli.run_agent_feature_task", fake_task)
    stdout = io.StringIO()

    with redirect_stdout(stdout):
        exit_code = main(
            [
                "implement",
                "--repo-root",
                str(root),
                "--feature-description",
                "Add a greeting.",
                "--work-item-name",
                "greeting",
                "--headless",
                "--json",
            ]
        )

    assert exit_code == 0
    assert json.loads(stdout.getvalue())["status"] == "no_op"
    assert observed["config"].uncertainty_policy == "normative_default"
    assert observed["config"].open_pr is False
    assert observed["config"].push_changes is False
    assert observed["config"].allowed_paths == (".",)
