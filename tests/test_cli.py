from __future__ import annotations

import argparse
import io
import json
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from powdrr_lift import parse_change_log, parse_validation_report
from powdrr_lift.cli import (
    _accepted_planning_answers,
    _bootstrap_planning_structrr_baseline,
    _run_harbor_feature,
    _stage_generated_file,
    build_parser,
    main,
)
from powdrr_lift.workrr.human_task import HumanTaskRunnerConfig
from powdrr_lift.workrr.task_agent import WorkflowTaskAgentConfig


def test_planning_answer_replay_discards_answers_invalidated_by_retry() -> None:
    def event(
        event_type: str, question_id: str, answer: dict[str, str] | None = None
    ) -> SimpleNamespace:
        payload = {"question": {"question_id": question_id}}
        if answer is not None:
            payload["accepted_answer"] = answer
        return SimpleNamespace(event_type=event_type, payload=payload)

    accepted = _accepted_planning_answers(
        (
            event("answer_accepted", "q-1", {"value": "stale"}),
            event("needs_answer", "q-1"),
            event("answer_accepted", "q-1", {"value": "revised"}),
            event("answer_accepted", "q-2", {"value": "stale"}),
            event("needs_answer", "q-2"),
        )
    )

    assert accepted == {"q-1": {"value": "revised"}}


def test_prompt_evaluation_cli_preserves_deepswe_and_adds_generic_case() -> None:
    parser = build_parser()
    generic = parser.parse_args(
        [
            "evaluate-prompt",
            "--case",
            "case.json",
            "--run-dir",
            "run",
            "--rubric",
            "rubric.yaml",
        ]
    )
    deepswe = parser.parse_args(
        ["evaluate-deepswe-prompt", "--task-dir", "task", "--run-dir", "run"]
    )

    assert generic.func.__name__ == "_run_generic_prompt_evaluation"
    assert generic.case == Path("case.json")
    assert deepswe.func.__name__ == "_run_deepswe_prompt_evaluation"


def test_deepswe_probe_evaluation_cli_dispatches() -> None:
    args = build_parser().parse_args(
        [
            "evaluate-deepswe-probes",
            "--task-dir",
            "task",
            "--run-dir",
            "run",
            "--probe-suite",
            "suite.json",
        ]
    )

    assert args.func.__name__ == "_run_deepswe_probe_evaluation"
    assert args.probe_suite == Path("suite.json")


def test_plan_resume_cli_requires_repository_and_session_issue() -> None:
    args = build_parser().parse_args(
        [
            "plan-resume",
            "--repository",
            "owner/repository",
            "--session-issue",
            "42",
        ]
    )

    assert args.func.__name__ == "_run_plan_resume"
    assert args.repository == "owner/repository"
    assert args.session_issue == 42


def test_planning_structrr_baseline_uses_temporary_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import powdrr_lift.cli as cli

    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setattr(cli.tempfile, "tempdir", str(tmp_path))
    captured: dict[str, Path] = {}

    def bootstrap(
        repo_root: Path, *, output_path: Path, taxonomy_path: Path
    ) -> SimpleNamespace:
        assert repo_root == repository
        captured["output"] = output_path
        captured["taxonomy"] = taxonomy_path
        output_path.write_text("baseline", encoding="utf-8")
        output_path.with_suffix(".md").write_text("report", encoding="utf-8")
        return SimpleNamespace(
            validation=SimpleNamespace(successful=True, issues=()),
            document={"entities": []},
        )

    monkeypatch.setattr(cli, "bootstrap_structrr", bootstrap)

    result = _bootstrap_planning_structrr_baseline(repository, "session-123")

    output_path = captured["output"]
    assert result == {"entities": []}
    assert output_path.name == "session-123.yaml"
    assert repository not in output_path.parents
    assert not output_path.exists()
    assert not output_path.with_suffix(".md").exists()
    assert not (repository / ".powdrr").exists()


def test_plan_repair_cli_requires_reviewed_plan_and_typed_request() -> None:
    args = build_parser().parse_args(
        ["plan-repair", "--plan", "docs/plan.yaml", "--request", "repair.json"]
    )

    assert args.func.__name__ == "_run_plan_repair"
    assert args.plan == Path("docs/plan.yaml")
    assert args.request == Path("repair.json")


def test_deepswe_probe_evaluation_skips_judge_for_empty_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import powdrr_lift.cli as cli

    run_dir = tmp_path / "run"
    suite_path = run_dir / "postrun-probes" / "probe-suite.json"
    suite_path.parent.mkdir(parents=True)
    suite_path.write_text('{"probes": []}', encoding="utf-8")
    captured: dict[str, object] = {}

    def evaluate(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "task_id": "sample-task",
            "summary": {
                "exact_case_recall": None,
                "any_case_recall": None,
                "unsupported_probe_count": 0,
            },
        }

    monkeypatch.setattr(cli, "evaluate_deepswe_probes", evaluate)
    monkeypatch.setattr(
        cli,
        "resolve_workflow_provider",
        lambda *_args: pytest.fail("judge provider should not be resolved"),
    )

    status = main(
        [
            "evaluate-deepswe-probes",
            "--task-dir",
            str(tmp_path / "task"),
            "--run-dir",
            str(run_dir),
        ]
    )

    assert status == 0
    assert captured["judge"] is None
    assert (run_dir / "probe-quality-evaluation.json").is_file()


def test_prompt_benchmark_comparison_cli_dispatches() -> None:
    args = build_parser().parse_args(
        [
            "compare-prompt-benchmark",
            "--prompt-report",
            "prompt.json",
            "--benchmark-run",
            "run",
        ]
    )

    assert args.func.__name__ == "_run_prompt_benchmark_comparison"


def test_harbor_feature_defaults_to_benchmark_mode_without_policy_option() -> None:
    stdout = io.StringIO()
    with redirect_stdout(stdout), pytest.raises(SystemExit) as exc_info:
        main(["harbor-feature", "--help"])

    assert exc_info.value.code == 0
    assert "benchmark mode" in stdout.getvalue()
    assert "--clarification-policy" not in stdout.getvalue()


def test_list_plan_issues_prints_item_to_issue_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("powdrr_lift.cli.resolve_repo_root", lambda _path: tmp_path)
    monkeypatch.setattr(
        "powdrr_lift.cli.load_reviewed_plan_at_commit",
        lambda *_args, **_kwargs: SimpleNamespace(plan_id="plan-v1"),
    )
    monkeypatch.setattr(
        "powdrr_lift.cli.find_plan_issues",
        lambda **_kwargs: (
            SimpleNamespace(
                item_id="base",
                issue_number=42,
                url="https://github.com/example/project/issues/42",
                state="open",
                labels=("powdrr:ready",),
            ),
            SimpleNamespace(
                item_id="followup",
                issue_number=None,
                url=None,
                state=None,
                labels=(),
            ),
        ),
    )
    stdout = io.StringIO()

    with redirect_stdout(stdout):
        exit_code = main(
            [
                "list-plan-issues",
                "--repository",
                "example/project",
                "--plan",
                "docs/plans/plan.yaml",
                "--plan-commit",
                "a" * 40,
            ]
        )

    assert exit_code == 0
    result = json.loads(stdout.getvalue())
    assert result["plan_id"] == "plan-v1"
    assert result["issues"][0]["issue_number"] == 42
    assert result["missing_item_ids"] == ["followup"]


def test_fulfill_plan_issue_without_pr_cli_dispatches() -> None:
    args = build_parser().parse_args(
        [
            "fulfill-plan-issue-without-pr",
            "--repository",
            "owner/repository",
            "--issue-number",
            "63",
            "--reason",
            "The implementation is already on main.",
        ]
    )

    assert args.func.__name__ == "_run_fulfill_plan_issue_without_pr"
    assert args.issue_number == 63


def test_harbor_feature_returns_success_for_completed_run_with_issues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mapping = SimpleNamespace(provider="test-provider", model="test-model")
    monkeypatch.setattr(
        "powdrr_lift.cli.resolve_workflow_provider", lambda _provider: "test-provider"
    )
    monkeypatch.setattr(
        "powdrr_lift.cli.default_llm_mappings",
        lambda _provider: {"standard_reasoning": mapping},
    )
    monkeypatch.setattr(
        "powdrr_lift.cli.resolve_provider_credentials",
        lambda *_args: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "powdrr_lift.cli.build_workflow_client", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        "powdrr_lift.cli.run_feature_in_place",
        lambda _config: SimpleNamespace(
            status="completed_with_issues",
            branch="main",
            worktree=tmp_path,
            review={"passed": False},
        ),
    )
    args = argparse.Namespace(
        repo_root=tmp_path,
        planning_provider="test-provider",
        planning_api_key=None,
        planning_base_url=None,
        planning_model=None,
        feature_description="Do the work.",
        work_item_name="benchmark-task",
        allowed_paths=["."],
        validation_command=None,
        opencode_executable="opencode",
        opencode_model="model",
        code_agent="minisweagent",
        minisweagent_executable="mini",
        minisweagent_model=None,
        code_agent_prompt_prefix="",
        code_agent_prompt_suffix="",
        output_root=None,
        task_id=None,
        design_only=False,
        capture_worker_prompts_only=False,
        json=False,
    )

    with redirect_stdout(io.StringIO()):
        exit_code = _run_harbor_feature(args)

    assert exit_code == 0


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


def test_cli_rebase_structrr_writes_machine_readable_report(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.yaml"
    current = tmp_path / "current.yaml"
    output = tmp_path / "rebase.json"
    snapshot = {
        "entities": [{"id": "api", "type": "Application"}],
        "entity_relationships": [],
        "source_subjects": [],
        "source_bindings": [],
    }
    baseline.write_text(yaml.safe_dump(snapshot), encoding="utf-8")
    current.write_text(
        yaml.safe_dump(
            {
                **snapshot,
                "entities": [
                    {"id": "api", "type": "Application"},
                    {"id": "billing", "type": "Service"},
                ],
            }
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "rebase-structrr",
                "--baseline",
                str(baseline),
                "--current",
                str(current),
                "--entity-id",
                "api",
                "--output",
                str(output),
            ]
        )
        == 0
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["classification"] == "clean"
    assert report["baseline_digest"].startswith("sha256:")


def test_cli_bootstrap_structrr_json_reports_validated_summary(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    taxonomy = Path(__file__).parents[1] / "software_development_entity_taxonomy.md"
    (repo_root / taxonomy.name).write_text(
        taxonomy.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (repo_root / "src").mkdir()
    (repo_root / "src" / "app.py").write_text(
        "def run() -> str:\n    return 'ok'\n", encoding="utf-8"
    )
    _git(repo_root, "init", "-q")
    _git(repo_root, "config", "user.email", "test@example.com")
    _git(repo_root, "config", "user.name", "Test")
    _git(repo_root, "add", ".")
    _git(repo_root, "commit", "-qm", "initial")
    output_path = tmp_path / "bootstrap.yaml"
    stdout = io.StringIO()

    with redirect_stdout(stdout):
        assert (
            main(
                [
                    "bootstrap-structrr",
                    "--repo-root",
                    str(repo_root),
                    "--output",
                    str(output_path),
                    "--json",
                ]
            )
            == 0
        )

    summary = json.loads(stdout.getvalue())
    assert summary == {
        "baseline_status": "not_run",
        "entity_count": 3,
        "relationship_count": 0,
        "source_anchor_count": 2,
        "source_binding_count": 0,
        "source_subject_count": 2,
        "output_path": str(output_path),
        "report_path": str(output_path.with_suffix(".md")),
    }
    assert output_path.is_file()
    assert output_path.with_suffix(".md").is_file()
    assert "## Code structure" in output_path.with_suffix(".md").read_text()

    human_stdout = io.StringIO()
    with redirect_stdout(human_stdout):
        assert (
            main(
                [
                    "bootstrap-structrr",
                    "--repo-root",
                    str(repo_root),
                    "--output",
                    str(output_path),
                ]
            )
            == 0
        )
    assert str(output_path) in human_stdout.getvalue()
    assert "Validated" in human_stdout.getvalue()


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
                f"{phase}: [read_document]"
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
    assert build_task["actions"] == ["read_document"]


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


def test_definition_interpreter_commands_are_removed() -> None:
    help_text = build_parser().format_help()
    removed_commands = {
        "workflow-replay",
        "analyze-workflow-errors",
        "review-workflow-ambiguity",
        "probe-workflow-step",
        "compare-workflow-definitions",
        "tune-workflow",
    }
    assert not any(command in help_text for command in removed_commands)


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
        feature_plan_path: docs/proposals/feature/feature-pr-specification.yaml
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
