from __future__ import annotations

import io
import json
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from powdrr_lift import parse_change_log, parse_validation_report
from powdrr_lift.cli import main
from powdrr_lift.workflow_human_task import HumanTaskRunnerConfig
from powdrr_lift.workflow_task_agent import WorkflowTaskAgentConfig


def test_cli_init_writes_template(tmp_path: Path) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)
    output_path = repo_root / "change-log.template.yaml"

    with redirect_stdout(io.StringIO()) as stdout:
        exit_code = main(
            [
                "init",
                "feature/change-log",
                "--repo-root",
                str(repo_root),
                "--output",
                str(output_path),
            ]
        )

    assert exit_code == 0
    assert output_path.exists()
    assert str(output_path) in stdout.getvalue()
    assert (
        _git_output(repo_root, "diff", "--cached", "--name-only")
        == "change-log.template.yaml"
    )
    change_log = parse_change_log(output_path.read_text(encoding="utf-8"))
    assert [change.path for change in change_log.file_changes] == [
        "src/app.py",
        "tests/test_app.py",
    ]


def test_cli_llm_diff_shows_json_changes(tmp_path: Path) -> None:
    first_path = tmp_path / "llm-first.json"
    second_path = tmp_path / "llm-second.json"
    first_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-14T00:00:00Z",
                "input": [{"role": "user", "content": "old prompt"}],
                "output": {"kind": "complete", "text": "old answer"},
            }
        ),
        encoding="utf-8",
    )
    second_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-14T00:01:00Z",
                "input": [{"role": "user", "content": "new prompt"}],
                "output": {"kind": "complete", "text": "new answer"},
            }
        ),
        encoding="utf-8",
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        assert main(["llm-diff", str(first_path), str(second_path)]) == 0

    diff = stdout.getvalue()
    assert f"--- {first_path}" in diff
    assert f"+++ {second_path}" in diff
    assert '-      "content": "old prompt"' in diff
    assert '+      "content": "new prompt"' in diff
    assert '-    "text": "old answer"' in diff
    assert '+    "text": "new answer"' in diff


def test_cli_llm_diff_reports_invalid_json(tmp_path: Path) -> None:
    invalid_path = tmp_path / "llm-invalid.json"
    valid_path = tmp_path / "llm-valid.json"
    invalid_path.write_text("not json", encoding="utf-8")
    valid_path.write_text("{}", encoding="utf-8")
    stderr = io.StringIO()

    with redirect_stderr(stderr):
        assert main(["llm-diff", str(invalid_path), str(valid_path)]) == 2

    assert "invalid JSON" in stderr.getvalue()


def test_cli_repository_state_reports_staged_unstaged_and_untracked_files(
    tmp_path: Path,
) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)
    (repo_root / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
    (repo_root / "new.txt").write_text("new\n", encoding="utf-8")
    _git(repo_root, "add", "src/app.py")

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        assert main(["repository-state", "--repo-root", str(repo_root)]) == 0

    state = json.loads(stdout.getvalue())
    assert state["branch"] == "feature/change-log"
    assert state["clean"] is False
    files = {item["path"]: item for item in state["files"]}
    assert files["src/app.py"]["staged"] is True
    assert files["new.txt"]["untracked"] is True


def test_cli_pull_request_description_generates_instructed_feature_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "powdrr_lift.pull_request_description.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 1, stdout="", stderr="no pull request"
        ),
    )
    stdout = io.StringIO()

    with redirect_stdout(stdout):
        assert main(["pull-request-description", "--kind", "feature"]) == 0

    template = stdout.getvalue()
    for heading in (
        "## Summary",
        "## Problem",
        "## Behavior",
        "## Scope",
        "## Implementation",
        "## Validation",
        "## Risks and Mitigations",
        "## Reviewer Guide",
        "## Dependencies and Follow-up",
        "## References",
        "## Feature Plan",
    ):
        assert heading in template
    assert "Do not leave placeholders" in template


def test_cli_pull_request_description_preserves_existing_pr_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    existing_body = "## Historical Validation\n\nPassed on the previous update."
    monkeypatch.setattr(
        "powdrr_lift.pull_request_description.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(
                {
                    "url": "https://github.com/example/repo/pull/42",
                    "body": existing_body,
                }
            ),
            stderr="",
        ),
    )
    stdout = io.StringIO()

    with redirect_stdout(stdout):
        assert (
            main(
                [
                    "pull-request-description",
                    "--kind",
                    "ci-fix",
                    "--repo-root",
                    str(tmp_path),
                ]
            )
            == 0
        )

    template = stdout.getvalue()
    assert "## Existing PR Body (preserve and reconcile)" in template
    assert "https://github.com/example/repo/pull/42" in template
    assert existing_body in template
    assert "Carry forward every informative section" in template
    assert "## CI Failure" in template


def test_cli_process_workflow_task_wires_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_process_workflow_task(
        config: WorkflowTaskAgentConfig,
        *,
        stdout: object,
        stderr: object,
    ) -> int:
        captured["config"] = config
        return 0

    monkeypatch.setattr(
        "powdrr_lift.cli.run_workflow_task", _fake_process_workflow_task
    )
    workflow_dir = tmp_path / "workflow"

    assert (
        main(
            [
                "process-workflow-task",
                "--workflow-dir",
                str(workflow_dir),
                "--repo-root",
                str(tmp_path),
                "--task-id",
                "task-1",
                "--max-roundtrips",
                "4",
                "--context-compaction-threshold",
                "0.6",
            ]
        )
        == 0
    )
    config = captured["config"]
    assert isinstance(config, WorkflowTaskAgentConfig)
    assert config.workflow_dir == workflow_dir
    assert config.repo_root == tmp_path
    assert config.task_id == "task-1"
    assert config.max_roundtrips == 4
    assert config.context_compaction_threshold == 0.6


def test_cli_process_human_task_wires_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_process_human_task(
        config: HumanTaskRunnerConfig,
        *,
        stdout: object,
        stderr: object,
    ) -> int:
        captured["config"] = config
        return 0

    monkeypatch.setattr("powdrr_lift.cli.run_human_task", _fake_process_human_task)
    workflow_dir = tmp_path / "workflow"
    answer_file = tmp_path / "answer.txt"

    assert (
        main(
            [
                "process-human-task",
                "--workflow-dir",
                str(workflow_dir),
                "--repo-root",
                str(tmp_path),
                "--task-id",
                "human-task-1",
                "--role",
                "reviewer",
                "--answer-file",
                str(answer_file),
            ]
        )
        == 0
    )
    config = captured["config"]
    assert isinstance(config, HumanTaskRunnerConfig)
    assert config.workflow_dir == workflow_dir
    assert config.repo_root == tmp_path
    assert config.task_id == "human-task-1"
    assert config.assignee_role is not None
    assert config.assignee_role.value == "reviewer"
    assert config.answer_file == answer_file


def test_cli_init_uses_pr_changelog_path(tmp_path: Path) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "init",
                "feature/change-log",
                "--repo-root",
                str(repo_root),
                "--pr-number",
                "123",
            ]
        )

    output_path = repo_root / "docs" / "changelogs" / "PR-123-changelog.yaml"
    assert exit_code == 0
    assert output_path.exists()
    assert "Next: fill out the template" in stdout.getvalue()
    assert "docs/changelogs/PR-123-changelog.yaml" in stdout.getvalue()


def test_cli_init_from_plan_diff_writes_template(tmp_path: Path) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)
    plan_diff_path = repo_root / "docs" / "plan-diffs" / "feature" / "plan-diff.yaml"
    plan_diff_path.parent.mkdir(parents=True, exist_ok=True)
    plan_diff_path.write_text(
        """
        schema: https://powdrr.io/schema/plan-diff-v1
        feature_plan_path: docs/specs/feature/feature-pr-specification.yaml
        changelog_paths:
          - docs/changelogs/PR-1-changelog.yaml
        differences: []
        """,
        encoding="utf-8",
    )
    output_path = repo_root / "change-log.template.yaml"

    with redirect_stdout(io.StringIO()) as stdout:
        exit_code = main(
            [
                "init-from-plan-diff",
                "feature/change-log",
                "--repo-root",
                str(repo_root),
                "--plan-diff",
                str(plan_diff_path),
                "--output",
                str(output_path),
            ]
        )

    assert exit_code == 0
    assert output_path.exists()
    assert str(output_path) in stdout.getvalue()


def test_cli_evaluate_reports_validation_failure(tmp_path: Path) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)
    proposed_yaml = tmp_path / "proposed-change-log.yaml"
    proposed_yaml.write_text(
        """
        version: 1
        change_id: 7
        title: Add application files

        changes:
          - file: src/app.py
            span:
              start_line: 1
              end_line: 1
            summary: Add app code
        """,
        encoding="utf-8",
    )

    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(
            [
                "evaluate-pr-against-changelog",
                "feature/change-log",
                "--repo-root",
                str(repo_root),
                "--input",
                str(proposed_yaml),
            ]
        )

    assert exit_code == 1
    report = parse_validation_report(stdout.getvalue())
    assert report.validation_successful is False
    assert report.issues[0].code == "missing_change"
    assert "Corrective action:" in report.issues[0].message
    assert "rerun the same evaluate command" in report.issues[0].message


def test_cli_evaluate_uses_pr_changelog_path(tmp_path: Path) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)
    changelog_path = repo_root / "docs" / "changelogs" / "PR-123-changelog.yaml"
    changelog_path.parent.mkdir(parents=True, exist_ok=True)
    changelog_path.write_text(
        """
        version: 1
        change_id: 123
        title: Add application files

        changes:
          - file: src/app.py
            span:
              start_line: 1
              end_line: 1
            summary: Add app code
          - file: tests/test_app.py
            span:
              start_line: 1
              end_line: 2
            summary: Add app test
        """,
        encoding="utf-8",
    )

    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(
            [
                "evaluate-pr-against-changelog",
                "feature/change-log",
                "--repo-root",
                str(repo_root),
                "--pr-number",
                "123",
            ]
        )

    assert exit_code == 0
    report = parse_validation_report(stdout.getvalue())
    assert report.validation_successful is True
    assert "Next: include docs/changelogs/PR-123-changelog.yaml in the PR." in (
        stderr.getvalue()
    )


def test_cli_blame_ui_invokes_local_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)
    captured: dict[str, object] = {}

    def _fake_serve_blame_ui(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("powdrr_lift.cli.serve_blame_ui", _fake_serve_blame_ui)

    exit_code = main(
        [
            "blame-ui",
            "feature/change-log",
            "--repo-root",
            str(repo_root),
            "--parent-branch",
            "main",
            "--file",
            "src/app.py",
            "--host",
            "0.0.0.0",
            "--port",
            "8123",
        ]
    )

    assert exit_code == 0
    assert captured == {
        "repo_root": repo_root,
        "branch_name": "feature/change-log",
        "parent_branch": "main",
        "selected_file": "src/app.py",
        "host": "0.0.0.0",
        "port": 8123,
    }


def _create_repo_with_feature_branch(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")

    (repo_root / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo_root, "add", "README.md")
    _git(repo_root, "commit", "-m", "Initial commit")

    _git(repo_root, "checkout", "-b", "feature/change-log")
    (repo_root / "src").mkdir()
    (repo_root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (repo_root / "tests").mkdir()
    (repo_root / "tests" / "test_app.py").write_text(
        "def test_app():\n    assert True\n",
        encoding="utf-8",
    )
    _git(repo_root, "add", "src/app.py", "tests/test_app.py")
    _git(repo_root, "commit", "-m", "Add application files")

    return repo_root


def _git(repo_root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _git_output(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()
