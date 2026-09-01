from __future__ import annotations

import io
import json
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from powdrr_lift import parse_change_log, parse_validation_report
from powdrr_lift.cli import _stage_generated_file, main
from powdrr_lift.workflow_error_logging import record_workflow_llm_error
from powdrr_lift.workflow_human_task import HumanTaskRunnerConfig
from powdrr_lift.workflow_task_agent import WorkflowTaskAgentConfig


def test_cli_init_writes_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)
    output_path = repo_root / "change-log.template.yaml"
    monkeypatch.setenv("POWDRR_FILE_ADDED_EVENTS", "1")
    stderr = io.StringIO()

    with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(stderr):
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
    assert "[powdrr-file-added] change-log.template.yaml" in stderr.getvalue()
    assert (
        _git_output(repo_root, "diff", "--cached", "--name-only")
        == "change-log.template.yaml"
    )
    change_log = parse_change_log(output_path.read_text(encoding="utf-8"))
    assert [change.path for change in change_log.file_changes] == [
        "src/app.py",
        "tests/test_app.py",
    ]


def test_cli_remember_and_explain_intent(tmp_path: Path) -> None:
    workflow_dir = tmp_path / "workflow"
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        assert (
            main(
                [
                    "remember-intent",
                    "--workflow-dir",
                    str(workflow_dir),
                    "--intent-id",
                    "intent-review",
                    "--clause-id",
                    "clause-review",
                    "--text",
                    "Resolve the review thread after validation.",
                    "--source-ref",
                    "conversation:1/message:2",
                    "--kind",
                    "procedure",
                    "--selector",
                    "phase_type=resolve_findings",
                    "--require",
                    "run_validation",
                    "--require",
                    "resolve_review_thread",
                ]
            )
            == 0
        )
    acknowledged = json.loads(stdout.getvalue())
    assert acknowledged["created"] is True
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        assert (
            main(
                [
                    "explain-effective-contract",
                    "--workflow-dir",
                    str(workflow_dir),
                    "--context",
                    "phase_type=resolve_findings",
                ]
            )
            == 0
        )
    explanation = json.loads(stdout.getvalue())
    assert explanation["contract"]["clause_ids"] == ["clause-review"]
    assert explanation["contract"]["clauses"][0]["text"].startswith("Resolve")


def test_cli_compiles_plan_into_profiled_workflow(tmp_path: Path) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)
    plan_path = repo_root / "execution-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": "execution-plan-v1",
                "plan_id": "plan-cli",
                "proposed_pr_fingerprint": "fingerprint-1",
                "units": [
                    {
                        "unit_id": "core",
                        "objective": "Implement the core change",
                        "paths": ["src/app.py"],
                        "acceptance_criteria": ["tests pass"],
                        "validation_profiles": ["repository-validation"],
                    }
                ],
                "allowed_paths": ["src"],
            }
        ),
        encoding="utf-8",
    )
    profile_path = Path("delivery-profiles/default-software-delivery.yaml").resolve()
    actions_path = repo_root / "actions.yaml"
    actions_path.write_text(
        "\n".join(
            [
                f"{phase}: [read_document, next_step]"
                for phase in (
                    "intake",
                    "specify",
                    "review_specifications",
                    "decompose",
                    "review_proposed_prs",
                    "plan_pr",
                    "await_plan_decision",
                    "build",
                    "validate",
                    "review_pr",
                    "resolve_findings",
                    "confirm_readiness",
                    "publish_pr",
                    "complete_feature",
                )
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    workflow_dir = repo_root / "workflow"

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        assert (
            main(
                [
                    "compile-execution-plan",
                    "--plan",
                    str(plan_path),
                    "--profile",
                    str(profile_path),
                    "--actions",
                    str(actions_path),
                    "--workflow-dir",
                    str(workflow_dir),
                    "--repo-root",
                    str(repo_root),
                ]
            )
            == 0
        )

    result = json.loads(stdout.getvalue())
    assert result["task_count"] == 14
    assert (workflow_dir / "core-build.yaml").exists()
    import yaml

    build_task = yaml.safe_load(
        (workflow_dir / "core-build.yaml").read_text(encoding="utf-8")
    )
    assert build_task["phase_type"] == "build"
    assert build_task["persona_id"] == "engineer"
    assert build_task["actions"] == ["read_document", "next_step"]


def test_staging_generated_directory_emits_each_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)
    generated = repo_root / "generated"
    (generated / "nested").mkdir(parents=True)
    (generated / "second.yaml").write_text("second\n", encoding="utf-8")
    (generated / "nested" / "first.yaml").write_text("first\n", encoding="utf-8")
    monkeypatch.setenv("POWDRR_FILE_ADDED_EVENTS", "1")
    stderr = io.StringIO()

    with redirect_stderr(stderr):
        _stage_generated_file(repo_root, generated)

    assert stderr.getvalue().splitlines() == [
        "[powdrr-file-added] generated/nested/first.yaml",
        "[powdrr-file-added] generated/second.yaml",
    ]
    assert _git_output(repo_root, "diff", "--cached", "--name-only").splitlines() == [
        "generated/nested/first.yaml",
        "generated/second.yaml",
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


def test_cli_workflow_replay_exports_and_renders_error_record(tmp_path: Path) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)
    skill_path = repo_root / "skill-definitions" / "inspect.yaml"
    skill_path.parent.mkdir()
    skill_path.write_text(
        """\
name: inspect
when_to_use:
  - Inspect the repository.
steps:
  - id: inspect-files
    description: Inspect files.
    tool_invocations:
      - tool: shell
        command: [rg, --files]
""",
        encoding="utf-8",
    )
    error_log = record_workflow_llm_error(
        repo_root,
        execution_mode="execute_selected_skill",
        phase="action_validation_or_execution",
        error=RuntimeError("simulated failure"),
        context={
            "skill": {
                "name": "inspect",
                "path": str(skill_path),
                "step_index": 0,
                "step_id": "inspect-files",
                "description": "Inspect files.",
            },
            "replay_state": {},
        },
        attempted_action={
            "action": "invoke_tool",
            "tool": "shell",
            "parameters": {"command": ["rg", "--files"]},
        },
    )
    assert error_log is not None
    record_id = json.loads(error_log.read_text(encoding="utf-8"))["record_id"]
    bundle_path = "workflow-evals/replays/inspect.yaml"

    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        export_exit_code = main(
            [
                "workflow-replay",
                "--error-log",
                str(error_log),
                "--record-id",
                record_id,
                "--output",
                bundle_path,
                "--repo-root",
                str(repo_root),
            ]
        )

    stdout = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
        replay_exit_code = main(
            [
                "workflow-replay",
                "--bundle",
                bundle_path,
                "--repo-root",
                str(repo_root),
                "--json",
            ]
        )

    assert export_exit_code == 0
    assert replay_exit_code == 0
    assert (repo_root / bundle_path).is_file()
    assert json.loads(stdout.getvalue())["response_validation"] == {
        "valid": True,
        "action": "invoke_tool",
    }


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
