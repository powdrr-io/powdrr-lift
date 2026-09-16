from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from powdrr_lift.core.execution_plan import ExecutionUnit
from powdrr_lift.workrr.coding_agent import (
    CodingAgentStatus,
    ImplementationRequest,
    OpenCodePermissionPolicy,
    OpenCodeProvider,
    run_coding_agent,
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
        (worktree_root / "README.md").write_text("committed unexpectedly\n")
        subprocess.run(["git", "add", "README.md"], cwd=worktree_root, check=True)
        subprocess.run(
            ["git", "commit", "-m", "unexpected"], cwd=worktree_root, check=True
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


def _request() -> ImplementationRequest:
    return ImplementationRequest(
        request_id="request-1",
        objective="Implement the change.",
        prompt="Implement the attached request.",
        base_commit="base",
        plan_fingerprint="plan-fingerprint",
        allowed_paths=("src",),
        acceptance_criteria=("the test passes",),
        validation_profiles=("python",),
    )


def test_execution_unit_compiles_to_worker_request() -> None:
    request = ImplementationRequest.from_execution_unit(
        ExecutionUnit(
            unit_id="unit-1",
            objective="Add the adapter.",
            paths=("src/powdrr_lift/workrr",),
            validation_profiles=("unit-tests",),
            acceptance_criteria=("the adapter is bounded",),
        ),
        request_id="request-1",
        base_commit="abc123",
        plan_fingerprint="plan-1",
        context_refs=("entity:worker-adapter",),
    )

    assert request.allowed_paths == ("src/powdrr_lift/workrr",)
    assert request.context_refs == ("entity:worker-adapter",)
    assert "unit-1" in request.prompt
    assert (
        json.loads(request.to_json())["schema_version"] == "implementation-request-v1"
    )


def test_allowed_worker_result_captures_json_events_and_diff(tmp_path: Path) -> None:
    attempt = run_coding_agent(
        FakeProvider("allowed"),
        _request(),
        worktree_root=_git_repo(tmp_path),
        attempt_id="attempt-1",
    )

    assert attempt.status is CodingAgentStatus.COMPLETED
    assert attempt.changed_paths == ("src/change.py",)
    assert attempt.out_of_scope_paths == ()
    assert attempt.diff_fingerprint
    assert attempt.events == ({"type": "session.completed"},)


def test_worker_out_of_scope_change_is_policy_denied(tmp_path: Path) -> None:
    attempt = run_coding_agent(
        FakeProvider("out-of-scope"),
        _request(),
        worktree_root=_git_repo(tmp_path),
        attempt_id="attempt-2",
    )

    assert attempt.status is CodingAgentStatus.POLICY_DENIED
    assert attempt.out_of_scope_paths == ("README.md",)


def test_worker_commit_is_policy_denied(tmp_path: Path) -> None:
    attempt = run_coding_agent(
        FakeProvider("commit"),
        _request(),
        worktree_root=_git_repo(tmp_path),
        attempt_id="attempt-3",
    )

    assert attempt.status is CodingAgentStatus.POLICY_DENIED
    assert attempt.error == "worker changed HEAD"


def test_opencode_policy_is_noninteractive_and_denies_publication() -> None:
    policy = OpenCodePermissionPolicy(("pytest *", "ruff *"))

    assert policy.to_data()["*"] == "deny"
    assert policy.to_data()["question"] == "deny"
    assert policy.to_data()["bash"]["git commit *"] == "deny"
    assert policy.to_data()["bash"]["git push *"] == "deny"
    assert policy.to_data()["bash"]["pytest *"] == "allow"


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

    monkeypatch.setattr("powdrr_lift.workrr.coding_agent.subprocess.run", fake_run)
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
    assert json.loads(str(environment["OPENCODE_PERMISSION"]))["question"] == "deny"
