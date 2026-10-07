from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Sequence
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from powdrr_lift.agent_bootstrap import BootstrapTaskConfig, run_bootstrap_task
from powdrr_lift.cli import main


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
    (root / "src" / "app.py").write_text(
        "def run() -> str:\n    return 'ok'\n", encoding="utf-8"
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "initial")
    return root


def test_bootstrap_uses_packaged_taxonomy_and_retains_local_worktree(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo")

    result = run_bootstrap_task(
        BootstrapTaskConfig(repo_root=root, run_id="bootstrap-one")
    )

    assert result.status == "completed_unverified"
    assert result.validation_status == "no_checks"
    assert result.task is not None
    assert result.bootstrap is not None
    assert result.bootstrap.validation.successful
    assert result.task.worktree.is_dir()
    assert (result.task.worktree / "software_development_entity_taxonomy.md").is_file()
    assert result.pull_request_url is None
    assert result.report_json_path is not None and result.report_json_path.is_file()
    assert result.report_markdown_path is not None
    assert result.report_markdown_path.is_file()
    run_result = json.loads(
        (result.report_json_path.parent / "run-result.json").read_text(encoding="utf-8")
    )
    assert run_result["report_json_path"] == str(result.report_json_path)
    assert run_result["report_markdown_path"] == str(result.report_markdown_path)
    assert _git(root, "status", "--porcelain") == ""
    assert _git(root, "log", "-1", "--format=%s") == "initial"
    assert _git(result.task.worktree, "log", "-1", "--format=%s") == (
        "Bootstrap repository structure"
    )


def test_bootstrap_runs_discovered_validation_and_records_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path / "repo")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "sample"\nversion = "0.1.0"\n'
        "[tool.ruff]\nline-length = 88\n",
        encoding="utf-8",
    )
    workflow = root / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "name: CI\njobs:\n  lint:\n    steps:\n      - run: ruff check .\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "declare ruff validation")
    monkeypatch.setenv("TEST_API_KEY", "do-not-pass")
    commands: list[list[str]] = []

    def runner(
        command: Sequence[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        args = list(command)
        if args and args[0] in {"ruff", "uv"}:
            commands.append(args)
            environment = kwargs["env"]
            assert isinstance(environment, dict)
            assert "TEST_API_KEY" not in environment
            assert "VIRTUAL_ENV" not in environment
            assert "PYTHONPATH" not in environment
            if args[0] == "uv":
                assert environment["UV_PROJECT_ENVIRONMENT"] == str(
                    kwargs["cwd"] / ".venv"
                )
            return subprocess.CompletedProcess(args, 0, "All checks passed.\n", "")
        return subprocess.run(args, **kwargs)

    result = run_bootstrap_task(
        BootstrapTaskConfig(repo_root=root, run_id="bootstrap-validation-pass"),
        runner=runner,
    )

    assert result.status == "completed_local"
    assert result.validation_status == "passed"
    assert ["ruff", "check", "."] in commands
    assert any(command[:3] == ["uv", "run", "ruff"] for command in commands)
    assert result.bootstrap is not None
    check = next(
        item
        for item in result.bootstrap.document["validation_inventory"]
        if item["profile"] == "ruff-check"
    )
    assert check["baseline"]["status"] == "passed"
    assert check["confirmation"]["level"] == "execution"


def test_bootstrap_reports_validation_failure_and_keeps_snapshot(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "sample"\nversion = "0.1.0"\n'
        "[tool.ruff]\nline-length = 88\n",
        encoding="utf-8",
    )
    workflow = root / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "name: CI\njobs:\n  lint:\n    steps:\n      - run: ruff check .\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "declare ruff validation")

    def runner(
        command: Sequence[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        args = list(command)
        if args and args[0] == "ruff":
            return subprocess.CompletedProcess(args, 2, "", "lint failed\n")
        return subprocess.run(args, **kwargs)

    result = run_bootstrap_task(
        BootstrapTaskConfig(repo_root=root, run_id="bootstrap-validation-fail"),
        runner=runner,
    )

    assert result.status == "validation_failed"
    assert result.validation_status == "failed"
    assert result.bootstrap is not None
    assert result.bootstrap.output_path.is_file()
    check = next(
        item
        for item in result.bootstrap.document["validation_inventory"]
        if item["profile"] == "ruff-check"
    )
    assert check["baseline"]["status"] == "failed"
    assert check["baseline"]["observation"]["returncode"] == 2
    assert "stderr_sha256" in check["baseline"]["observation"]


def test_bootstrap_reports_no_op_when_bootstrapping_existing_context(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo")
    first = run_bootstrap_task(
        BootstrapTaskConfig(repo_root=root, run_id="bootstrap-one")
    )
    assert first.task is not None

    second = run_bootstrap_task(
        BootstrapTaskConfig(
            repo_root=root,
            base_ref=first.task.branch,
            run_id="bootstrap-two",
        )
    )

    assert second.status == "completed_unverified"
    assert second.task is not None
    assert _git(second.task.worktree, "log", "-1", "--format=%s") == (
        "Bootstrap repository structure"
    )


def test_bootstrap_preserves_a_project_taxonomy(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    taxonomy = Path(__file__).parents[1] / "software_development_entity_taxonomy.md"
    project_taxonomy = root / "software_development_entity_taxonomy.md"
    custom_taxonomy = (
        taxonomy.read_text(encoding="utf-8") + "\n<!-- project-owned -->\n"
    )
    project_taxonomy.write_text(custom_taxonomy, encoding="utf-8")
    _git(root, "add", project_taxonomy.name)
    _git(root, "commit", "-qm", "add project taxonomy")

    result = run_bootstrap_task(
        BootstrapTaskConfig(repo_root=root, run_id="bootstrap-project-taxonomy")
    )

    assert result.status == "completed_unverified"
    assert result.taxonomy_provenance == "project_taxonomy"
    assert result.task is not None
    assert (result.task.worktree / "software_development_entity_taxonomy.md").read_text(
        encoding="utf-8"
    ) == custom_taxonomy


def test_bootstrap_preflight_failure_preserves_input_and_writes_report(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo")
    dirty_file = root / "keep-me.txt"
    dirty_file.write_text("user work", encoding="utf-8")

    result = run_bootstrap_task(
        BootstrapTaskConfig(repo_root=root, run_id="bootstrap-dirty")
    )

    assert result.status == "failed"
    assert result.failure_stage == "initialization"
    assert "keep-me.txt" in (result.error or "")
    assert dirty_file.read_text(encoding="utf-8") == "user work"
    assert result.report_json_path is not None and result.report_json_path.is_file()
    assert result.report_markdown_path is not None
    assert result.report_markdown_path.is_file()


def test_bootstrap_cli_emits_one_json_result(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    stdout = io.StringIO()

    with redirect_stdout(stdout):
        exit_code = main(
            [
                "bootstrap",
                "--repo-root",
                str(root),
                "--work-item-name",
                "sample onboarding",
                "--json",
            ]
        )

    assert exit_code == 1
    result = json.loads(stdout.getvalue())
    assert result["status"] == "completed_unverified"
    assert result["branch"].startswith("powdrr/sample-onboarding-")
    assert result["worktree"]
    assert result["report_json_path"]


def test_bootstrap_open_pr_publishes_only_onboarding_changes(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "sample"\nversion = "0.1.0"\n'
        "[tool.ruff]\nline-length = 88\n",
        encoding="utf-8",
    )
    workflow = root / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "name: CI\njobs:\n  lint:\n    steps:\n      - run: ruff check .\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "declare ruff validation")
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
    gh_calls: list[list[str]] = []

    def runner(
        command: Sequence[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        args = list(command)
        if args[0] == "gh":
            gh_calls.append(args)
            return subprocess.CompletedProcess(
                args, 0, "https://github.com/acme/repo/pull/42\n", ""
            )
        if args[0] in {"ruff", "uv"}:
            return subprocess.CompletedProcess(args, 0, "checks passed\n", "")
        return subprocess.run(args, **kwargs)

    result = run_bootstrap_task(
        BootstrapTaskConfig(
            repo_root=root,
            open_pr=True,
            run_id="bootstrap-publish",
        ),
        runner=runner,
    )

    assert result.status == "pr_opened"
    assert result.task is not None
    assert result.pull_request_url == "https://github.com/acme/repo/pull/42"
    assert len(gh_calls) == 1
    body = gh_calls[0][gh_calls[0].index("--body") + 1]
    assert "Bootstrap summary" in body
    assert "Added the packaged default taxonomy." in body
    assert "Coverage limitations" in body
    assert "Validation/check discovery" in body
    assert "docs/structrr/current/baseline-bootstrap.yaml" in body
    changed_paths = _git(
        result.task.worktree,
        "diff",
        "--name-only",
        "origin/main...HEAD",
    ).splitlines()
    assert set(changed_paths) == set(result.changed_paths)
