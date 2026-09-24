from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from powdrr_lift.cli import main
from powdrr_lift.core.execution_plan import ExecutionPlan, ExecutionUnit
from powdrr_lift.workrr.coding_agent import (
    CodingAgentAttempt,
    CodingAgentAttemptStore,
    CodingAgentOutcome,
    CodingAgentRunner,
    CodingAgentStatus,
    ImplementationRequest,
    MiniSWEAgentProvider,
    OpenCodePermissionPolicy,
    OpenCodeProvider,
    classify_coding_agent_attempt,
    run_coding_agent,
)
from powdrr_lift.workrr.coding_agent_validation import (
    ValidationProfile,
    ValidationReportStatus,
    ValidationResultStatus,
    ValidationRunner,
    parse_validation_profile,
)


class FakeProvider:
    provider_name = "fake"

    def __init__(self, action: str) -> None:
        self.action = action

    def run(
        self,
        request: ImplementationRequest,
        *,
        worktree_root: Path,
        attempt_id: str,
    ) -> subprocess.CompletedProcess[str]:
        del request, attempt_id
        if self.action == "allowed":
            (worktree_root / "src").mkdir()
            (worktree_root / "src" / "change.py").write_text("value = 1\n")
            return subprocess.CompletedProcess(
                ["fake"], 0, '{"type":"session.completed"}\n', ""
            )
        if self.action == "out-of-scope":
            (worktree_root / "README.md").write_text("changed\n")
            return subprocess.CompletedProcess(["fake"], 0, "", "")
        if self.action == "ephemeral":
            (worktree_root / "src").mkdir()
            (worktree_root / "src" / "change.py").write_text("value = 1\n")
            (worktree_root / "test_helper.py").write_text("print('scratch')\n")
            return subprocess.CompletedProcess(["fake"], 0, "", "")
        if self.action == "runtime-artifacts":
            (worktree_root / "src").mkdir()
            (worktree_root / "src" / "change.py").write_text("value = 1\n")
            (worktree_root / "agent_error.txt").write_text("diagnostic\n")
            (worktree_root / "coverage.xml").write_text("<coverage/>\n")
            return subprocess.CompletedProcess(["fake"], 0, "", "")
        (worktree_root / "README.md").write_text("committed unexpectedly\n")
        subprocess.run(["git", "add", "README.md"], cwd=worktree_root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "unexpected",
            ],
            cwd=worktree_root,
            check=True,
        )
        return subprocess.CompletedProcess(["fake"], 0, "", "")


def _git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "README.md").write_text("initial\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "initial",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return tmp_path


def _head(worktree: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _request(base_commit: str = "base") -> ImplementationRequest:
    return ImplementationRequest(
        request_id="request-1",
        objective="Implement the change.",
        prompt="Implement the attached request.",
        base_commit=base_commit,
        plan_fingerprint="plan-fingerprint",
        allowed_paths=("src",),
        acceptance_criteria=("the test passes",),
        validation_profiles=("python",),
        allowed_commands=("python3 *",),
    )


def test_execution_unit_compiles_to_worker_request() -> None:
    request = ImplementationRequest.from_execution_unit(
        ExecutionUnit(
            unit_id="unit-1",
            objective="Add the adapter.",
            paths=("src/powdrr_lift/workrr",),
            validation_profiles=("unit-tests",),
            acceptance_criteria=("the adapter is bounded",),
            planned_additions=(
                {"section": "features", "id": "worker-adapter", "action": "added"},
            ),
            planned_deletions=(
                {"section": "features", "id": "old-adapter", "action": "removed"},
            ),
        ),
        request_id="request-1",
        base_commit="abc123",
        plan_fingerprint="plan-1",
        context_refs=("entity:worker-adapter",),
    )

    assert request.allowed_paths == ("src/powdrr_lift/workrr",)
    assert request.context_refs == ("entity:worker-adapter",)
    assert "unit-1" not in request.prompt
    assert "Product contract:" in request.prompt
    assert "Worker policy:" in request.prompt
    assert "Required product changes:" in request.prompt
    assert "Acceptance criteria:\n- the adapter is bounded" in request.prompt
    assert "Required operations" not in request.prompt
    assert '"id": "worker-adapter"' in request.prompt
    assert '"id": "old-adapter"' in request.prompt
    assert "Validation profiles Workrr will run: unit-tests" in request.prompt
    assert (
        json.loads(request.to_json())["schema_version"] == "implementation-request-v2"
    )


def test_empty_product_change_lists_are_omitted_from_worker_prompt() -> None:
    request = ImplementationRequest.from_execution_unit(
        ExecutionUnit(
            unit_id="unit-without-explicit-changes",
            objective="Implement the behavior.",
            paths=("src",),
            validation_profiles=("unit-tests",),
            acceptance_criteria=("the behavior works",),
        ),
        request_id="request-1",
        base_commit="abc123",
        plan_fingerprint="plan-1",
    )

    assert "Required product changes:" not in request.prompt
    assert "None declared" not in request.prompt


def test_opencode_provider_pins_requested_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = _git_repo(tmp_path)
    provider = OpenCodeProvider(
        executable="opencode",
        model="deepinfra/deepseek-flash",
    )
    captured: dict[str, object] = {}

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("powdrr_lift.workrr.coding_agent.run_opencode", fake_run)
    provider.run(_request(_head(worktree)), worktree_root=worktree, attempt_id="a")

    command = captured["command"]
    assert isinstance(command, list)
    assert command[command.index("--model") + 1] == "deepinfra/deepseek-flash"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert environment.get("VIRTUAL_ENV") is None


def test_minisweagent_provider_uses_targeted_prompt_and_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = _git_repo(tmp_path)
    diagnostics = tmp_path / "diagnostics"
    provider = MiniSWEAgentProvider(
        executable="mini",
        model="openai/gpt-5",
        diagnostics_root=diagnostics,
        prompt_prefix="Use the repository's local conventions.",
        prompt_suffix="Stop after implementing the requested change.",
    )
    captured: dict[str, object] = {}

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["kwargs"] = kwargs
        if "--output" in command:
            output = command[command.index("--output") + 1]
            Path(output).write_text(
                json.dumps({"info": {"exit_status": "Submitted"}}),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, 0, "done\n", "")

    monkeypatch.setattr("powdrr_lift.workrr.coding_agent.subprocess.run", fake_run)
    request = provider.prepare_request(_request(_head(worktree)))
    provider.run(request, worktree_root=worktree, attempt_id="attempt-1")

    command = captured["command"]
    assert isinstance(command, list)
    assert command[:2] == ["mini", "--task"]
    assert "Use the repository's local conventions." in command[2]
    assert "Stop after implementing the requested change." in command[2]
    assert "--yolo" in command
    assert "--exit-immediately" in command
    assert command[command.index("--cost-limit") + 1] == "0"
    assert command[command.index("--model") + 1] == "openai/gpt-5"
    assert command[command.index("--output") + 1].endswith("attempt-1.traj.json")
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert environment["MSWEA_CONFIGURED"] == "true"


def test_minisweagent_provider_rejects_clean_exit_without_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = _git_repo(tmp_path)
    diagnostics = tmp_path / "diagnostics"
    provider = MiniSWEAgentProvider(diagnostics_root=diagnostics)
    head = _head(worktree)

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        assert "--output" in command
        output = command[command.index("--output") + 1]
        Path(output).write_text(
            json.dumps({"info": {"exit_status": "LimitsExceeded"}}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("powdrr_lift.workrr.coding_agent.subprocess.run", fake_run)
    result = provider.run(_request(head), worktree_root=worktree, attempt_id="limited")

    assert result.returncode == 125
    assert "exit_status='LimitsExceeded'" in result.stderr


def test_execution_plan_compiles_selected_unit_to_worker_request() -> None:
    request = ImplementationRequest.from_execution_plan(
        ExecutionPlan(
            plan_id="plan-1",
            proposed_pr_fingerprint="plan-fingerprint",
            units=(
                ExecutionUnit(
                    unit_id="hello",
                    objective="Create the hello world program.",
                    paths=("hello_world.py",),
                    validation_profiles=("hello-world-output",),
                    acceptance_criteria=("the output is correct",),
                ),
            ),
            allowed_paths=("hello_world.py",),
        ),
        unit_id="hello",
        request_id="request-plan",
        base_commit="abc123",
        allowed_commands=("python3 *",),
    )

    assert request.plan_fingerprint == "plan-fingerprint"
    assert request.allowed_paths == ("hello_world.py",)
    assert request.validation_profiles == ("hello-world-output",)


def test_execution_plan_rejects_unknown_unit() -> None:
    plan = ExecutionPlan("plan-1", "fingerprint", (), ("src",))

    with pytest.raises(ValueError, match="no unit 'missing'"):
        ImplementationRequest.from_execution_plan(
            plan,
            unit_id="missing",
            request_id="request-plan",
            base_commit="abc123",
        )


def test_allowed_worker_result_captures_json_events_and_diff(tmp_path: Path) -> None:
    worktree = _git_repo(tmp_path)
    attempt = run_coding_agent(
        FakeProvider("allowed"),
        _request(_head(worktree)),
        worktree_root=worktree,
        attempt_id="attempt-1",
    )

    assert attempt.status is CodingAgentStatus.COMPLETED
    assert attempt.changed_paths == ("src/change.py",)
    assert attempt.out_of_scope_paths == ()
    assert attempt.diff_fingerprint
    assert attempt.events == ({"type": "session.completed"},)


def test_worker_removes_declared_ephemeral_artifacts_before_final_diff(
    tmp_path: Path,
) -> None:
    worktree = _git_repo(tmp_path)
    request = _request(_head(worktree))
    request = ImplementationRequest(
        **{
            **request.to_data(),
            "allowed_paths": list(request.allowed_paths),
            "acceptance_criteria": list(request.acceptance_criteria),
            "validation_profiles": list(request.validation_profiles),
            "context_refs": list(request.context_refs),
            "allowed_commands": list(request.allowed_commands),
            "ephemeral_paths": ["test_helper.py"],
        }
    )

    attempt = run_coding_agent(
        FakeProvider("ephemeral"),
        request,
        worktree_root=worktree,
        attempt_id="attempt-ephemeral",
    )

    assert attempt.status is CodingAgentStatus.COMPLETED
    assert attempt.changed_paths == ("src/change.py",)
    assert not (worktree / "test_helper.py").exists()


def test_worker_removes_new_untracked_files_outside_scope(tmp_path: Path) -> None:
    worktree = _git_repo(tmp_path)
    attempt = run_coding_agent(
        FakeProvider("runtime-artifacts"),
        _request(_head(worktree)),
        worktree_root=worktree,
        attempt_id="attempt-runtime-artifacts",
    )

    assert attempt.status is CodingAgentStatus.COMPLETED
    assert attempt.changed_paths == ("src/change.py",)
    assert not (worktree / "agent_error.txt").exists()
    assert not (worktree / "coverage.xml").exists()


def test_worker_out_of_scope_change_is_policy_denied(tmp_path: Path) -> None:
    worktree = _git_repo(tmp_path)
    attempt = run_coding_agent(
        FakeProvider("out-of-scope"),
        _request(_head(worktree)),
        worktree_root=worktree,
        attempt_id="attempt-2",
    )

    assert attempt.status is CodingAgentStatus.POLICY_DENIED
    assert attempt.out_of_scope_paths == ("README.md",)


def test_worker_commit_is_policy_denied(tmp_path: Path) -> None:
    worktree = _git_repo(tmp_path)
    attempt = run_coding_agent(
        FakeProvider("commit"),
        _request(_head(worktree)),
        worktree_root=worktree,
        attempt_id="attempt-3",
    )

    assert attempt.status is CodingAgentStatus.POLICY_DENIED
    assert attempt.error == "worker changed HEAD"


def test_worker_rejects_worktree_at_wrong_base_commit(tmp_path: Path) -> None:
    worktree = _git_repo(tmp_path)
    attempt = run_coding_agent(
        FakeProvider("allowed"),
        _request("different-base"),
        worktree_root=worktree,
        attempt_id="attempt-wrong-base",
    )

    assert attempt.status is CodingAgentStatus.POLICY_DENIED
    assert "base commit" in (attempt.error or "")
    assert not (worktree / "src" / "change.py").exists()


def test_opencode_policy_is_noninteractive_and_denies_publication() -> None:
    policy = OpenCodePermissionPolicy(("pytest *", "ruff *"))

    assert policy.to_data()["*"] == "deny"
    assert policy.to_data()["question"] == "deny"
    assert policy.to_data()["bash"]["git commit *"] == "deny"
    assert policy.to_data()["bash"]["git push *"] == "deny"
    assert policy.to_data()["bash"]["pytest *"] == "allow"


def test_opencode_provider_defaults_to_five_minutes_of_inactivity() -> None:
    assert OpenCodeProvider().timeout_seconds == 300.0
    assert OpenCodeProvider().absolute_timeout_seconds == 900.0


def test_coding_agent_attempt_classification_distinguishes_partial_timeout() -> None:
    attempt = CodingAgentAttempt(
        attempt_id="attempt-1",
        request_id="request-1",
        provider="opencode",
        status=CodingAgentStatus.TIMED_OUT,
        exit_code=124,
        changed_paths=("src/change.py",),
        diff_fingerprint="new-diff",
    )

    assert (
        classify_coding_agent_attempt(attempt, previous_diff_fingerprint="old-diff")
        is CodingAgentOutcome.TIMED_OUT_WITH_PARTIAL_PROGRESS
    )
    assert (
        classify_coding_agent_attempt(attempt, previous_diff_fingerprint="new-diff")
        is CodingAgentOutcome.TIMED_OUT_WITHOUT_PROGRESS
    )


def test_opencode_provider_uses_json_events_and_inline_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, "{}\n", "")

    monkeypatch.setattr("powdrr_lift.workrr.coding_agent.run_opencode", fake_run)
    provider = OpenCodeProvider(timeout_seconds=4.0)
    result = provider.run(_request(), worktree_root=tmp_path, attempt_id="attempt-1")

    assert result.returncode == 0
    assert captured["command"] == [
        "opencode",
        "run",
        "--format",
        "json",
        "--agent",
        "build",
        "Implement the attached request.",
    ]
    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert environment["PWD"] == str(tmp_path.resolve())
    assert environment["XDG_DATA_HOME"] == str(tmp_path / ".opencode-data")
    assert environment["XDG_STATE_HOME"] == str(tmp_path / ".opencode-data")
    assert environment["XDG_CACHE_HOME"] == str(tmp_path / ".opencode-cache")
    assert json.loads(str(environment["OPENCODE_PERMISSION"]))["question"] == "deny"


def test_opencode_provider_resumes_the_session_for_repair_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        stdout = '{"type":"session.created","properties":{"info":{"id":"session-1"}}}\n'
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr("powdrr_lift.workrr.coding_agent.run_opencode", fake_run)
    provider = OpenCodeProvider()
    provider.run(_request(), worktree_root=tmp_path, attempt_id="attempt-1")
    provider.run(_request(), worktree_root=tmp_path, attempt_id="attempt-2")

    assert "--session" not in commands[0]
    assert commands[1][commands[1].index("--session") + 1] == "session-1"


def test_repair_attempt_can_continue_with_existing_in_scope_changes(
    tmp_path: Path,
) -> None:
    worktree = _git_repo(tmp_path)
    request = ImplementationRequest(
        **{**_request(_head(worktree)).to_data(), "allow_existing_changes": True}
    )

    class ExistingChangeProvider:
        provider_name = "fake"

        def run(
            self,
            request: ImplementationRequest,
            *,
            worktree_root: Path,
            attempt_id: str,
        ) -> subprocess.CompletedProcess[str]:
            del request, attempt_id
            (worktree_root / "src").mkdir(exist_ok=True)
            (worktree_root / "src" / "change.py").write_text("value = 2\n")
            return subprocess.CompletedProcess(["fake"], 0, "", "")

    first = run_coding_agent(
        ExistingChangeProvider(),
        request,
        worktree_root=worktree,
        attempt_id="first",
    )
    second = run_coding_agent(
        ExistingChangeProvider(),
        request,
        worktree_root=worktree,
        attempt_id="second",
    )

    assert first.status is CodingAgentStatus.COMPLETED
    assert second.status is CodingAgentStatus.COMPLETED


def test_runner_persists_request_and_attempt_artifacts(tmp_path: Path) -> None:
    worktree = _git_repo(tmp_path / "repo")
    store = CodingAgentAttemptStore(tmp_path / "artifacts")
    runner = CodingAgentRunner(FakeProvider("allowed"), store)
    request = _request(_head(worktree))

    attempt = runner.run(
        request,
        worktree_root=worktree,
        attempt_id="attempt-persisted",
    )

    assert attempt.status is CodingAgentStatus.COMPLETED
    assert store.load_request("request-1") == _request(_head(worktree))
    assert store.load_attempt("attempt-persisted") == attempt
    assert (tmp_path / "artifacts" / "requests" / "request-1.json").exists()
    assert (tmp_path / "artifacts" / "attempts" / "attempt-persisted.json").exists()
    prompt_path = tmp_path / "artifacts" / "prompts" / "attempt-persisted.txt"
    assert prompt_path.read_text(encoding="utf-8") == request.prompt
    prompt_metadata = json.loads(
        (tmp_path / "artifacts" / "prompts" / "attempt-persisted.json").read_text(
            encoding="utf-8"
        )
    )
    assert prompt_metadata["provider"] == "fake"
    assert (
        json.loads(
            (tmp_path / "artifacts" / "prompts" / "index.json").read_text(
                encoding="utf-8"
            )
        )[0]["attempt_id"]
        == "attempt-persisted"
    )


def test_runner_persists_policy_denial_for_dirty_worktree(tmp_path: Path) -> None:
    worktree = _git_repo(tmp_path / "repo")
    (worktree / "README.md").write_text("already dirty\n")
    store = CodingAgentAttemptStore(tmp_path / "artifacts")

    attempt = CodingAgentRunner(FakeProvider("allowed"), store).run(
        _request(_head(worktree)),
        worktree_root=worktree,
        attempt_id="attempt-dirty",
    )

    assert attempt.status is CodingAgentStatus.POLICY_DENIED
    assert store.load_attempt("attempt-dirty").error == (
        "coding-agent worktree must be clean before an attempt"
    )


def test_validation_runner_executes_every_declared_profile_and_persists_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = _git_repo(tmp_path / "repo")
    monkeypatch.setenv("VIRTUAL_ENV", "/another-checkout/.venv")
    store = CodingAgentAttemptStore(tmp_path / "artifacts")
    request = _request(_head(worktree))
    attempt = CodingAgentRunner(FakeProvider("allowed"), store).run(
        request, worktree_root=worktree, attempt_id="attempt-validation"
    )
    report = ValidationRunner(
        {
            "python": ValidationProfile(
                "python",
                (
                    "python3",
                    "-c",
                    "import os; assert not os.environ.get('VIRTUAL_ENV')",
                ),
            )
        }
    ).run(request, attempt, worktree_root=worktree)

    assert report.status is ValidationReportStatus.PASSED
    assert report.results[0].status is ValidationResultStatus.PASSED
    store.save_validation_report(report)
    assert store.load_validation_report("attempt-validation") == report


def test_validation_runner_blocks_unregistered_or_unauthorized_profiles(
    tmp_path: Path,
) -> None:
    worktree = _git_repo(tmp_path / "repo")
    request = _request(_head(worktree))
    attempt = run_coding_agent(
        FakeProvider("allowed"), request, worktree_root=worktree, attempt_id="attempt-4"
    )
    report = ValidationRunner(
        {"python": ValidationProfile("python", ("ruff", "check", "."))}
    ).run(request, attempt, worktree_root=worktree)

    assert report.status is ValidationReportStatus.BLOCKED
    assert report.results[0].status is ValidationResultStatus.BLOCKED
    assert "not allowed" in (report.results[0].error or "")


def test_validation_runner_blocks_after_worker_failure(tmp_path: Path) -> None:
    worktree = _git_repo(tmp_path / "repo")
    request = _request(_head(worktree))
    attempt = run_coding_agent(
        FakeProvider("out-of-scope"),
        request,
        worktree_root=worktree,
        attempt_id="attempt-5",
    )
    report = ValidationRunner(
        {"python": ValidationProfile("python", ("python3", "-c", "pass"))}
    ).run(request, attempt, worktree_root=worktree)

    assert report.status is ValidationReportStatus.BLOCKED
    assert report.results[0].status is ValidationResultStatus.BLOCKED


def test_parse_validation_profile_uses_argv_not_shell() -> None:
    profile = parse_validation_profile("unit-tests=python -m pytest tests -q")

    assert profile.name == "unit-tests"
    assert profile.command == ("python", "-m", "pytest", "tests", "-q")


def test_run_coding_agent_cli_persists_and_reports_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    worktree = _git_repo(tmp_path / "repo")
    request_path = tmp_path / "request.json"
    request_path.write_text(_request(_head(worktree)).to_json(), encoding="utf-8")
    monkeypatch.setattr(
        "powdrr_lift.cli.build_coding_agent_provider",
        lambda *_args, **_kwargs: FakeProvider("allowed"),
    )

    result = main(
        [
            "run-coding-agent",
            "--request",
            str(request_path),
            "--worktree",
            str(worktree),
            "--output-dir",
            str(tmp_path / "artifacts"),
            "--attempt-id",
            "attempt-cli",
            "--validation-profile",
            "python=python3 -c \"print('ok')\"",
        ]
    )

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "completed"
    assert output["validation"]["status"] == "passed"


def test_compile_implementation_request_cli_writes_selected_unit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    worktree = _git_repo(tmp_path / "repo")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": "execution-plan-v1",
                "plan_id": "plan-cli",
                "proposed_pr_fingerprint": "fingerprint-cli",
                "units": [
                    {
                        "unit_id": "hello",
                        "objective": "Create hello world.",
                        "paths": ["hello_world.py"],
                        "validation_profiles": ["hello-world-output"],
                        "acceptance_criteria": ["output is exact"],
                    }
                ],
                "allowed_paths": ["hello_world.py"],
            }
        ),
        encoding="utf-8",
    )
    request_path = tmp_path / "request.json"

    result = main(
        [
            "compile-implementation-request",
            "--plan",
            str(plan_path),
            "--unit-id",
            "hello",
            "--request-id",
            "request-cli",
            "--base-commit",
            _head(worktree),
            "--output",
            str(request_path),
            "--allowed-command",
            "python3 *",
        ]
    )

    assert result == 0
    assert json.loads(request_path.read_text(encoding="utf-8"))["request_id"] == (
        "request-cli"
    )
    assert json.loads(capsys.readouterr().out)["plan_fingerprint"] == "fingerprint-cli"
