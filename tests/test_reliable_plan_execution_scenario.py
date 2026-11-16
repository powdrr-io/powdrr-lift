from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from powdrr_agent.execution.execute_pr_tests import run_generated_pytest
from test_github_issue_plan_generation import (
    FakePlanningClient,
    _empty_structrr_proposal,
    _item,
)
from test_github_issue_queue import (
    FakeIssueClient,
    _checkpoint_completed_pr,
    _ItemClaim,
    _write_test_validation_inventory,
)

from powdrr_lift.clean_pr_publisher import (
    build_publication_authorization,
    publish_clean_pull_request,
    render_standard_pr_description,
)
from powdrr_lift.process.tasks import (
    AgentRole,
    AssigneeType,
    TaskComplexity,
    TaskStatus,
    WorkflowInstance,
    WorkflowTask,
)
from powdrr_lift.workrr.candidate_checkpoint import checkpoint_candidate_changes
from powdrr_lift.workrr.git import create_workflow_worktree, integration_branch_name
from powdrr_lift.workrr.github_issue_plan_generation import (
    generate_execution_plan_yaml,
    write_execution_plan_artifacts,
)
from powdrr_lift.workrr.github_issue_queue import (
    QUEUE_LABELS,
    GitHubIssueQueueError,
    load_reviewed_plan_at_commit,
    publish_plan_issues,
    run_worker_once,
)
from powdrr_lift.workrr.scenario import run_workflow_task_scenario
from powdrr_lift.workrr.task_agent import PowdrrExecutionError


@dataclass(frozen=True)
class _PytestProfile:
    command: tuple[str, ...] = (sys.executable, "-m", "pytest")


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit_all(root: Path, message: str) -> str:
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Scenario",
        "-c",
        "user.email=scenario@example.invalid",
        "commit",
        "-m",
        message,
    )
    return _git(root, "rev-parse", "HEAD")


def _scenario_workflow(path: Path) -> None:
    WorkflowInstance.create(
        path,
        (
            WorkflowTask(
                task_id="scenario-task",
                status=TaskStatus.OPEN,
                complexity=TaskComplexity.LOW,
                input_state={},
                description="Record the scripted item outcome.",
                assignee_type=AssigneeType.AGENT,
                assignee_role=AgentRole.ARCHITECT,
                output_state_type="scenario-result",
            ),
        ),
    )


def _run_scripted_worker_task(workflow_source: Path, item_id: str) -> dict[str, Any]:
    output = {"item_id": item_id, "accepted": True}
    response = {
        "step_json": json.dumps(
            {
                "operation": {
                    "tool": "task_complete",
                    "parameters": {"output_state": {"scenario-result": output}},
                }
            }
        )
    }
    result = run_workflow_task_scenario(
        workflow_source=workflow_source,
        task_id="scenario-task",
        responses=[response],
        expected_output_state={"scenario-result": output},
    )
    assert result["exit_code"] == 0, result["stderr"]
    assert result["output_matches"] is True
    assert result["roundtrips"] == 2
    return result


def _three_item_plan() -> tuple[Any, tuple[str, Any]]:
    foundation = _item("foundation")
    foundation["allowed_paths"] = ["src/foundation.py"]
    foundation["implementation_steps"] = ["Implement the shared foundation."]
    extension = _item("extension", dependencies=["foundation"])
    extension["allowed_paths"] = ["src/extension.py"]
    extension["implementation_steps"] = ["Extend the shared foundation."]
    independent = _item("independent")
    independent["allowed_paths"] = ["src/independent.py"]
    independent["implementation_steps"] = ["Implement the independent behavior."]

    class CountingPlanningClient(FakePlanningClient):
        calls = 0

        def complete_json(self, *args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            return super().complete_json(*args, **kwargs)

    client = CountingPlanningClient({"items": [foundation, extension, independent]})
    return client, generate_execution_plan_yaml(
        source_plan=(
            "# Reliable execution scenario\n\n"
            "Implement a shared foundation, a follow-up that depends on it, "
            "and an independent capability. Retain existing behavior."
        ),
        plan_id="reliable-execution-scenario",
        source_document="docs/plans/source-intent.md",
        verification_command="python -m pytest",
        client=client,
    )


def test_reliable_plan_execution_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "scenario-repository"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Scenario")
    _git(repo, "config", "user.email", "scenario@example.invalid")
    (repo / ".gitignore").write_text(
        ".powdrr/\n.worktrees/\n__pycache__/\n*.pyc\n", encoding="utf-8"
    )
    (repo / "docs/plans").mkdir(parents=True)
    (repo / "docs/plans/source-intent.md").write_text(
        "# Intent\n\nRetain the existing public value while adding the "
        "requested behavior.\n",
        encoding="utf-8",
    )
    _write_test_validation_inventory(repo)
    base_sha = _commit_all(repo, "Scenario base")

    planning_client, (rendered_plan, proposed_plan) = _three_item_plan()
    assert planning_client.calls == 2
    assert [item.item_id for item in proposed_plan.items] == [
        "foundation",
        "extension",
        "independent",
    ]
    assert proposed_plan.items[1].dependencies == ("foundation",)
    assert proposed_plan.items[2].dependencies == ()
    output_path = repo / "docs/plans/reliable-execution-scenario.yaml"
    workflow_artifacts = write_execution_plan_artifacts(
        rendered_plan=rendered_plan,
        plan=proposed_plan,
        output_path=output_path,
        repo_root=repo,
        verification_command="python -m pytest",
        structrr_proposal=_empty_structrr_proposal(proposed_plan),
    )
    plan_commit = _commit_all(repo, "Compile the reviewed scenario plan")
    compiled_plan = load_reviewed_plan_at_commit(
        output_path.relative_to(repo).as_posix(), plan_commit, repo_root=repo
    )
    assert compiled_plan == proposed_plan
    assert workflow_artifacts

    github = FakeIssueClient()
    publish_plan_issues(
        plan=compiled_plan,
        plan_path=output_path.relative_to(repo).as_posix(),
        plan_commit=plan_commit,
        client=github,  # type: ignore[arg-type]
    )
    workflow_source = tmp_path / "scripted-workflow"
    _scenario_workflow(workflow_source)
    started: set[str] = set()
    first_entered = threading.Event()
    both_started = threading.Event()
    item_outcomes: dict[str, str] = {}
    base_at_execution: dict[str, str] = {}

    def execute_item(
        _workflow_path: Path,
        workflow_id: str,
        _worker_id: str,
        item: Any,
        _plan: Any,
        _no_pr_dependencies: tuple[str, ...],
        checkpoint: Any,
    ) -> int:
        if item.item_id in {"foundation", "independent"}:
            started.add(item.item_id)
            if item.item_id == "foundation":
                first_entered.set()
            if started == {"foundation", "independent"}:
                both_started.set()
            assert both_started.wait(timeout=10), (
                "independent ready items did not overlap"
            )
        worktree_name = f"scenario-{item.item_id}-{len(item_outcomes)}"
        branch = integration_branch_name(worktree_name)
        worktree, _ = create_workflow_worktree(
            repo,
            workflow_id,
            base_branch="main",
            branch_name=branch,
            worktree_name=worktree_name,
        )
        base_at_execution[item.item_id] = _git(worktree, "rev-parse", "HEAD")
        try:
            scenario_result = _run_scripted_worker_task(workflow_source, item.item_id)
            issue_number = item.github_issue_number
            assert issue_number is not None
            github.pull_requests[issue_number] = {
                "state": "open",
                "merged": False,
                "draft": False,
                "body": f"Closes #{issue_number}",
                "merge_commit_sha": None,
            }
            _checkpoint_completed_pr(checkpoint, item)
            item_outcomes[item.item_id] = "awaiting_review"
            assert scenario_result["roundtrips"] == 2
            return 0
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=repo,
                capture_output=True,
                check=False,
            )

    def run(worker_id: str) -> Any:
        return run_worker_once(
            client=github,  # type: ignore[arg-type]
            repo_root=repo,
            worker_id=worker_id,
            run_task=execute_item,
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(run, "scenario-worker-a")
        assert first_entered.wait(timeout=10)
        second = workers.submit(run, "scenario-worker-b")
        parallel_reports = (first.result(timeout=30), second.result(timeout=30))
    assert {report.item_id for report in parallel_reports} == {
        "foundation",
        "independent",
    }
    assert all(report.outcome == "awaiting_review" for report in parallel_reports)
    assert (
        base_at_execution["foundation"]
        == base_at_execution["independent"]
        == plan_commit
    )
    assert QUEUE_LABELS["blocked_on_dependency"] in github.issues[1].labels

    foundation_issue = github.issues[0]
    github.update_issue(
        foundation_issue,
        state="closed",
    )
    waiting = run_worker_once(
        client=github,  # type: ignore[arg-type]
        repo_root=repo,
        worker_id="scenario-wait-check",
        run_task=lambda *_args: pytest.fail("manual close must not unblock work"),
    )
    assert waiting.outcome == "no_eligible_work"
    assert waiting.readiness is not None
    assert waiting.readiness["blocking_reasons"][0]["reason"] == (
        "dependency_not_fulfilled"
    )

    foundation_pr = github.issues[0].number
    github.pull_requests[foundation_pr] = {
        "state": "closed",
        "merged": True,
        "draft": False,
        "body": f"Closes #{foundation_pr}",
        "merge_commit_sha": "e" * 40,
    }
    _git(repo, "switch", "main")
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src/foundation.py").write_text("VALUE = 42\n", encoding="utf-8")
    merged_base_sha = _commit_all(repo, "Simulate external foundation merge")
    resumed = run_worker_once(
        client=github,  # type: ignore[arg-type]
        repo_root=repo,
        worker_id="scenario-dependent-worker",
        run_task=execute_item,
    )
    assert resumed.item_id == "extension"
    assert resumed.outcome == "awaiting_review"
    assert base_at_execution["extension"] == merged_base_sha

    verification_root = tmp_path / "verification-repository"
    bare_origin = tmp_path / "verification-origin.git"
    verification_root.mkdir()
    subprocess.run(["git", "init", "--bare", str(bare_origin)], check=True)
    _git(verification_root, "init", "-b", "main")
    _git(verification_root, "config", "user.name", "Scenario")
    _git(verification_root, "config", "user.email", "scenario@example.invalid")
    (verification_root / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n", encoding="utf-8"
    )
    (verification_root / "src").mkdir()
    (verification_root / "tests").mkdir()
    (verification_root / "docs/changelog").mkdir(parents=True)
    (verification_root / "src/value.py").write_text("VALUE = 0\n", encoding="utf-8")
    test_source = (
        "from value import VALUE\n\n"
        "def test_accepted_behavior():\n"
        "    assert VALUE == 42\n"
    )
    (verification_root / "tests/test_value.py").write_text(
        test_source, encoding="utf-8"
    )
    baseline_sha = _commit_all(verification_root, "Verification baseline")
    _git(verification_root, "remote", "add", "origin", str(bare_origin))
    _git(verification_root, "push", "-u", "origin", "main")
    _git(verification_root, "switch", "-c", "scenario/candidate")
    verifier_path = "tests/test_value.py"
    product_path = "src/value.py"
    first_candidate = "VALUE = 1\n"
    (verification_root / product_path).write_text(first_candidate, encoding="utf-8")
    first_verification = run_generated_pytest(
        _PytestProfile(),
        root=verification_root,
        paths=(verifier_path,),
        timeout_seconds=30,
    )
    assert first_verification["passed"] is False
    exact_feedback = first_verification["stdout"] + first_verification["stderr"]
    assert (
        first_verification["returncode"] not in (0, None) or first_verification["error"]
    )
    assert "assert 1 == 42" in exact_feedback
    correction_candidate = "VALUE = 42\n"
    (verification_root / product_path).write_text(
        correction_candidate, encoding="utf-8"
    )
    second_verification = run_generated_pytest(
        _PytestProfile(),
        root=verification_root,
        paths=(verifier_path,),
        timeout_seconds=30,
    )
    assert second_verification["passed"] is True
    (verification_root / "docs/changelog/2026-11-15-scenario.yaml").write_text(
        "schema_version: 2\nchanges:\n  - type: behavior\n"
        "    description: Preserve the accepted value.\n",
        encoding="utf-8",
    )
    out_of_scope = verification_root / "notes.txt"
    out_of_scope.write_text("unauthorized edit\n", encoding="utf-8")
    with pytest.raises(ValueError, match="outside allowed product paths"):
        checkpoint_candidate_changes(
            verification_root,
            submission_base=baseline_sha,
            allowed_paths=(product_path, "docs/changelog/2026-11-15-scenario.yaml"),
            artifact_exclusions=(verifier_path,),
            message="Reject out of scope edit",
        )
    out_of_scope.unlink()
    checkpoint = checkpoint_candidate_changes(
        verification_root,
        submission_base=baseline_sha,
        allowed_paths=(product_path, "docs/changelog/2026-11-15-scenario.yaml"),
        artifact_exclusions=(verifier_path,),
        message="Accept corrected candidate",
    )
    accepted_candidate = _git(verification_root, "rev-parse", "HEAD")
    assert checkpoint.candidate_revision == accepted_candidate
    attempts_before_resume = [first_candidate, correction_candidate]
    persisted_candidate = json.loads(json.dumps(checkpoint.to_data()))
    assert persisted_candidate["candidate_revision"] == accepted_candidate
    resumed_checkpoint = checkpoint_candidate_changes(
        verification_root,
        submission_base=baseline_sha,
        allowed_paths=(product_path, "docs/changelog/2026-11-15-scenario.yaml"),
        artifact_exclusions=(verifier_path,),
        message="Resume accepted candidate",
    )
    assert resumed_checkpoint.candidate_revision == accepted_candidate
    assert attempts_before_resume == ["VALUE = 1\n", "VALUE = 42\n"]

    files_to_publish = [
        product_path,
        "docs/changelog/2026-11-15-scenario.yaml",
    ]
    authorization = build_publication_authorization(
        base="main",
        files=files_to_publish,
        accepted_contract_digest=hashlib.sha256(b"accepted behavior").hexdigest(),
        verification_receipts=[
            {"check": "generated-pytest", "passed": True, "returncode": 0}
        ],
        lineage={
            "plan_id": compiled_plan.plan_id,
            "item_id": "foundation",
            "candidate_revision": accepted_candidate,
            "previous_revision": checkpoint.previous_revision,
        },
        worktree_root=verification_root,
    )
    description_sections = {
        "objective": "Preserve the accepted public behavior.",
        "scope": "Update the value and its changelog record.",
        "implementation_details": "The candidate passes its frozen test.",
        "acceptance_criteria": "The accepted value is 42.",
        "validation": "Generated pytest passed on the accepted revision.",
        "advisory_test_quality_notes": "The scenario uses a deterministic test.",
    }
    body = render_standard_pr_description(description_sections)
    (verification_root / product_path).write_text("VALUE = 43\n", encoding="utf-8")
    with pytest.raises(PowdrrExecutionError, match="changed after verification"):
        publish_clean_pull_request(
            {
                "base": "main",
                "publication_branch": "powdrr/reliable-execution-scenario",
                "title": "Preserve the accepted value",
                "commit_message": "Publish accepted value",
                "description_sections": description_sections,
                "require_standard_pr_description": True,
                "require_publication_authorization": True,
                "publication_authorization": authorization,
                "files_to_publish": files_to_publish,
            },
            worktree_root=verification_root,
        )
    (verification_root / product_path).write_text(
        correction_candidate, encoding="utf-8"
    )
    gh_state: dict[str, Any] = {"created": False, "create_calls": 0, "body": None}
    original_run = subprocess.run

    def fake_gh_run(command: Any, *args: Any, **kwargs: Any) -> Any:
        values = list(command) if isinstance(command, (list, tuple)) else [str(command)]
        if values and values[0] == "gh":
            if values[:3] == ["gh", "pr", "view"]:
                if gh_state["created"]:
                    return subprocess.CompletedProcess(
                        values,
                        0,
                        json.dumps(
                            {
                                "url": "https://github.com/example/project/pull/900",
                                "state": "OPEN",
                            }
                        ),
                        "",
                    )
                return subprocess.CompletedProcess(values, 1, "", "not found")
            if values[:3] == ["gh", "pr", "create"]:
                gh_state["create_calls"] += 1
                gh_state["created"] = True
                gh_state["body"] = values[values.index("--body") + 1]
                # The fake remote creates the PR, then simulates a lost API response.
                return subprocess.CompletedProcess(values, 1, "", "response lost")
            if values[:3] == ["gh", "pr", "edit"]:
                gh_state["body"] = values[values.index("--body") + 1]
                return subprocess.CompletedProcess(values, 0, "", "")
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_gh_run)
    publish_parameters = {
        "base": "main",
        "publication_branch": "powdrr/reliable-execution-scenario",
        "title": "Preserve the accepted value",
        "commit_message": "Publish accepted value",
        "description_sections": description_sections,
        "require_standard_pr_description": True,
        "require_publication_authorization": True,
        "publication_authorization": authorization,
        "files_to_publish": files_to_publish,
    }
    with pytest.raises(PowdrrExecutionError, match="response lost"):
        publish_clean_pull_request(publish_parameters, worktree_root=verification_root)
    recovered_url = publish_clean_pull_request(
        publish_parameters, worktree_root=verification_root
    )
    assert recovered_url == "https://github.com/example/project/pull/900"
    assert gh_state["create_calls"] == 1
    assert gh_state["body"] == body
    assert authorization["base_commit"] == baseline_sha
    assert (
        authorization["candidate_content"][product_path]
        == hashlib.sha256(correction_candidate.encode()).hexdigest()
    )
    assert _git(
        verification_root,
        "diff",
        "--name-only",
        "main..refs/remotes/origin/powdrr/reliable-execution-scenario",
    ).splitlines() == sorted(files_to_publish)

    scenario_report = {
        "outcomes": item_outcomes,
        "readiness": waiting.readiness,
        "source_commit": base_sha,
        "post_merge_base": merged_base_sha,
        "candidate_revision": accepted_candidate,
        "first_verification": first_verification,
        "correction_verification": second_verification,
        "checkpoint": checkpoint.to_data(),
        "publication_url": recovered_url,
        "llm_calls_per_item": 2,
        "github_create_calls": gh_state["create_calls"],
        "obligation_receipts": authorization["verification_receipts"],
    }
    claim_root = tmp_path / "claim-repository"
    claim_root.mkdir()
    _git(claim_root, "init", "-b", "main")
    claim = _ItemClaim(claim_root, 991, "scenario-owner", ("src/a.py",), 1)
    with claim:
        record = json.loads(claim.claim_path.read_text(encoding="utf-8"))
        record["generation"] += 1
        claim.claim_path.write_text(json.dumps(record), encoding="utf-8")
        with pytest.raises(GitHubIssueQueueError, match="lost claim generation"):
            claim.assert_current()
    assert scenario_report["outcomes"] == {
        "foundation": "awaiting_review",
        "independent": "awaiting_review",
        "extension": "awaiting_review",
    }
    assert scenario_report["llm_calls_per_item"] == 2


def test_claim_is_released_when_user_cancels(tmp_path: Path) -> None:
    repo = tmp_path / "cancelled-claim-repository"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    claim = _ItemClaim(repo, 992, "cancelled-owner", ("src/a.py",), 1)
    with pytest.raises(KeyboardInterrupt):
        with claim:
            raise KeyboardInterrupt
    record = json.loads(claim.claim_path.read_text(encoding="utf-8"))
    assert record["state"] == "released"
