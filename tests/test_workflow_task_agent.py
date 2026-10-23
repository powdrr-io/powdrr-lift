from __future__ import annotations

import io
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

from powdrr_lift.core.spec_context import (
    gather_specification_context,
    render_gather_context_report,
)
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.process.task_step import (
    TaskPreStep,
)
from powdrr_lift.process.tasks import (
    AgentRole,
    AssigneeType,
    HumanRole,
    TaskComplexity,
    TaskStatus,
    WorkflowInstance,
    WorkflowTask,
)
from powdrr_lift.structrr.proposal import ProposalOperation, ProposalRevision
from powdrr_lift.structrr.source_manifest import SourceManifest
from powdrr_lift.workrr import task_agent
from powdrr_lift.workrr.actions import WorkflowEdit
from powdrr_lift.workrr.git import (
    WorkflowGitInconsistency,
    WorkflowGitState,
    save_workflow_git_state,
)
from powdrr_lift.workrr.llm import (
    WorkflowAction,
    WorkflowLLMHTTPError,
    complete_json_with_timeout_retry,
)
from powdrr_lift.workrr.multipr_structrr import (
    compile_multi_pr_proposal,
    write_multi_pr_proposal,
)
from powdrr_lift.workrr.task_agent import (
    WorkflowTaskAgentConfig,
    _apply_task_edits,
    _build_workflow_client,
    _durable_task_action_output_state,
    _execute_pr_test_author_prompt,
    _execute_pr_test_author_write_denied_paths,
    _handle_exhausted_timeout,
    _implementation_paths_against_base,
    _issue_worker_implementation_request,
    _issue_worker_plan_amendment_paths,
    _issue_worker_structrr_context,
    _list_worktree_files,
    _normalize_execute_pr_coding_attempt,
    _open_final_workflow_pull_request,
    _publish_workflow_progress,
    _read_task_document,
    _review_execute_pr_test_quality,
    _select_ready_workflow_git_state,
    _TaskProcedrrOperations,
    _validate_issue_worker_plan_amendment,
    _validate_workflow_task_state,
    _workflow_file_command_error,
    run_workflow_task,
)
from powdrr_lift.workrr.test_obligations import acceptance_criteria_contract


def _commit_test_structrr_baseline(
    repo_root: Path,
    snapshot: Mapping[str, Any],
) -> tuple[Mapping[str, Any], SourceManifest]:
    from powdrr_lift.structrr.source_manifest import compile_source_manifest

    source_revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    tracked_paths = subprocess.run(
        ["git", "ls-files"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    manifest = compile_source_manifest(
        repo_root,
        tracked_paths,
        submission_base=source_revision,
        source_revision=source_revision,
        taxonomy_fingerprint="test-taxonomy",
    )
    document = {**snapshot, "source_manifest": manifest.to_data()}
    baseline_path = repo_root / "docs/structrr/current/baseline-bootstrap.yaml"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    subprocess.run(
        ["git", "add", "docs/structrr/current/baseline-bootstrap.yaml"],
        cwd=repo_root,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "Commit Structrr baseline bootstrap",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return document, manifest


def test_execute_pr_test_findings_retry_provider_429_with_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class RateLimitedThenOpinionAndSuggestion:
        calls = 0

        def complete_json(self, _messages: list[dict[str, str]]) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                raise WorkflowLLMHTTPError(
                    "OpenAI",
                    429,
                    '{"error":{"code":"engine_overloaded"}}',
                )
            if self.calls == 2:
                return {
                    "shortcoming_present": True,
                    "explanation": "The concern is present.",
                }
            return {
                "test_name": "test_workflow_runs_command_with_trigger",
                "test_code": "def test_workflow_runs_command_with_trigger():\n    ...",
                "rationale": "Observe the command on the pull request path.",
            }

    for name in ("TYPESAFEAI_API_KEY", "TYPESAFE_API_KEY", "SYSTEM_ONE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    delays: list[float] = []
    monkeypatch.setattr("powdrr_lift.workrr.llm.time.sleep", delays.append)
    client = RateLimitedThenOpinionAndSuggestion()
    config = WorkflowTaskAgentConfig(
        workflow_dir=tmp_path,
        max_timeout_retries=2,
        timeout_backoff_seconds=1.5,
    )

    class FakeJevClient:
        calls = 0

        def __init__(self, _fallback: Any, *, fail_closed: bool) -> None:
            assert fail_closed

        def complete_json(
            self,
            _messages: list[dict[str, str]],
            *,
            response_schema: dict[str, Any],
        ) -> dict[str, Any]:
            self.calls += 1
            assert response_schema["required"] == ["shortcoming_present"]
            return {"shortcoming_present": self.calls == 1}

    monkeypatch.setattr(task_agent, "JevSemanticClassifierClient", FakeJevClient)
    review = _review_execute_pr_test_quality(
        client,
        {
            "test_obligations": [
                {
                    "obligation_id": "AC-01",
                    "source_text": "The CI workflow runs verification.",
                }
            ],
            "test_sources": {
                "tests/test_ci.py": (
                    "# Covers: AC-01 test_workflow_runs_command\n"
                    "def test_workflow_runs_command():\n    assert True\n"
                )
            },
        },
        config=config,
    )

    assert review["outcome"] == "advisory"
    assert client.calls == 3
    assert delays == [1.5]
    assert "retrying in 1.5 seconds (retry 1/2)" in capsys.readouterr().err


def test_execute_pr_test_author_prompt_pins_authoring_worktree(
    tmp_path: Path,
) -> None:
    authoring_worktree = tmp_path / "isolated" / "authoring-worktree"
    prompt = _execute_pr_test_author_prompt(
        authoring_worktree=authoring_worktree,
        test_path="attestation/tests/test_powdrr_execute_pr_abcdef123456.py",
        plan_context={"acceptance_criteria": ["The workflow runs on pull requests."]},
        obligations=acceptance_criteria_contract(
            ["The workflow runs on pull requests."]
        ),
    )

    assert f"exact, only workspace for this attempt is:\n{authoring_worktree}" in prompt
    assert "run `pwd` and `git rev-parse --show-toplevel`" in prompt
    assert "stop without making or staging any changes" in prompt
    assert (
        "Do not use the parent checkout or any path outside this workspace." in prompt
    )
    assert "implementation is intentionally absent" in prompt
    assert "fail through a behavioral assertion" in prompt
    assert "Deterministic test obligations" in prompt
    assert "# Covers: AC-01 test_function_name" in prompt
    assert "attestation/tests/test_powdrr_execute_pr_abcdef123456.py" in prompt
    assert "do not create other files" in prompt
    assert '"obligation_id": "AC-01"' in prompt
    assert "partitions" not in prompt


def test_execute_pr_test_author_write_denials_cover_primary_checkout(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    subprocess.run(["git", "init", str(primary)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(primary),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
    )
    worker = tmp_path / "worker"
    subprocess.run(
        ["git", "-C", str(primary), "worktree", "add", "--detach", str(worker)],
        check=True,
        capture_output=True,
    )

    denied_paths = _execute_pr_test_author_write_denied_paths(worker)

    assert worker.resolve() in denied_paths
    assert primary.resolve() in denied_paths
    assert (primary / ".git").resolve() in denied_paths


def test_preflight_proposed_file_additions_ignores_files_outside_base_commit(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "README.md").write_text("baseline\n", encoding="utf-8")
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
            "baseline",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    candidate = tmp_path / ".github" / "workflows" / "ci.yml"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("name: CI\n", encoding="utf-8")
    subprocess.run(["git", "add", str(candidate)], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "persist candidate checkpoint",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    proposal = ProposalRevision(
        proposal_id="feature-pr-01",
        structrr_baseline_fingerprint="sha256:baseline",
        operations=(
            ProposalOperation(
                operation_id="add-ci-workflow",
                section="files",
                subject_id="ci-workflow",
                action="add",
                content={"path": ".github/workflows/ci.yml", "content": "name: CI\n"},
            ),
        ),
        acceptance_criteria=(),
        must_preserve=(),
        non_goals=(),
        allowed_paths=(".github/workflows/ci.yml",),
        source_refs=(),
    )

    findings = task_agent._preflight_proposed_file_additions(
        tmp_path, proposal, base_commit=base_commit
    )

    assert findings == ()


def test_implementation_paths_include_committed_work_and_exclude_runtime_state(
    tmp_path: Path,
) -> None:
    def git(*arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Test User")
    git("config", "user.email", "test@example.invalid")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-m", "base")
    git("switch", "-c", "implementation")

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "implementation.py").write_text(
        "VALUE = True\n", encoding="utf-8"
    )
    runtime_state = (
        tmp_path / "docs" / "workflows" / "example" / "runtime" / "state.json"
    )
    runtime_state.parent.mkdir(parents=True)
    runtime_state.write_text("{}\n", encoding="utf-8")
    git("add", "src/implementation.py", "docs/workflows/example/runtime/state.json")
    git("commit", "-m", "checkpoint implementation")

    (tmp_path / "src" / "untracked.py").write_text("OTHER = True\n", encoding="utf-8")
    (tmp_path / ".powdrr").mkdir()
    (tmp_path / ".powdrr" / "run.json").write_text("{}\n", encoding="utf-8")

    assert _implementation_paths_against_base(tmp_path) == (
        "src/implementation.py",
        "src/untracked.py",
    )


def test_workflow_base_commit_uses_target_branch_not_integration_checkpoint(
    tmp_path: Path,
) -> None:
    def git(*arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Test User")
    git("config", "user.email", "test@example.invalid")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-m", "base")
    base_commit = git("rev-parse", "HEAD")
    git("switch", "-c", "powdrr/pr-01")

    workflow_file = tmp_path / ".github/workflows/ci.yml"
    workflow_file.parent.mkdir(parents=True)
    workflow_file.write_text("name: CI\n", encoding="utf-8")
    git("add", ".github/workflows/ci.yml")
    git("commit", "-m", "checkpoint candidate workflow")

    workflow_directory = tmp_path / "docs/workflows/example"
    workflow_directory.mkdir(parents=True)
    save_workflow_git_state(
        workflow_directory,
        WorkflowGitState(
            proposed_pr_id="pr-01",
            base_branch="main",
            integration_branch="powdrr/pr-01",
            workflow_relative_directory="docs/workflows/example",
        ),
    )

    resolved_base = task_agent._workflow_base_commit(
        tmp_path,
        workflow_directory,
        workflow_id="pr-01",
    )
    assert resolved_base == base_commit
    assert (
        task_agent._preflight_reviewed_file_creation_conflicts(
            tmp_path,
            {
                "allowed_paths": [".github/workflows/ci.yml"],
                "implementation_steps": ["Add .github/workflows/ci.yml."],
            },
            base_commit=resolved_base,
        )
        == ()
    )


def test_reviewed_plan_creation_conflict_detects_existing_verifier(
    tmp_path: Path,
) -> None:
    def git(*arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Test User")
    git("config", "user.email", "test@example.invalid")
    verifier = tmp_path / "attestation/scripts/verify-all.sh"
    verifier.parent.mkdir(parents=True)
    verifier.write_text("#!/usr/bin/env bash\nset -euo pipefail\necho real checks\n")
    git("add", "attestation/scripts/verify-all.sh")
    git("commit", "-m", "add existing verifier")
    base_commit = git("rev-parse", "HEAD")

    evidence = task_agent._preflight_reviewed_file_creation_conflicts(
        tmp_path,
        {
            "allowed_paths": [
                ".github/workflows/ci.yml",
                "attestation/scripts/verify-all.sh",
            ],
            "implementation_steps": [
                "Create attestation/scripts/verify-all.sh with an initial no-op check.",
                "Add .github/workflows/ci.yml to run the verifier.",
            ],
        },
        base_commit=base_commit,
    )

    assert len(evidence) == 1
    assert "attestation/scripts/verify-all.sh" in evidence[0]
    assert "already exists in base commit" in evidence[0]
    assert "initial no-op check" in evidence[0]


def test_reviewed_plan_reference_to_existing_allowed_file_is_not_creation(
    tmp_path: Path,
) -> None:
    def git(*arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Test User")
    git("config", "user.email", "test@example.invalid")
    verifier = tmp_path / "attestation/scripts/verify-all.sh"
    verifier.parent.mkdir(parents=True)
    verifier.write_text("#!/usr/bin/env bash\nset -euo pipefail\necho real checks\n")
    git("add", "attestation/scripts/verify-all.sh")
    git("commit", "-m", "add existing verifier")
    base_commit = git("rev-parse", "HEAD")

    evidence = task_agent._preflight_reviewed_file_creation_conflicts(
        tmp_path,
        {
            "allowed_paths": [
                ".github/workflows/ci.yml",
                "attestation/scripts/verify-all.sh",
            ],
            "implementation_steps": [
                "Add .github/workflows/ci.yml that triggers on pull_request and push "
                "to the default branch and runs bash "
                "attestation/scripts/verify-all.sh."
            ],
        },
        base_commit=base_commit,
    )

    assert evidence == ()


def test_normalize_execute_pr_coding_attempt_marks_unshaped_error_failed() -> None:
    assert _normalize_execute_pr_coding_attempt(
        {"error": "coverage contract failed", "retryable": True}
    ) == {
        "passed": False,
        "changed_paths": [],
        "error": "coverage contract failed",
        "retryable": True,
    }


def test_issue_worker_compiles_the_benchmark_implementation_request() -> None:
    request, validation = _issue_worker_implementation_request(
        {
            "plan_id": "feature-v1",
            "item_id": "feature-v1-pr-01",
            "objective": "Implement the feature.",
            "scope": ["Implement the parser and report stable rejection codes."],
            "implementation_steps": ["Add the parser."],
            "acceptance_criteria": ["Valid input is parsed."],
            "verification_command": f"{sys.executable} verify.py",
        },
        workflow_id="feature-v1-pr-01",
        base_commit="a" * 40,
    )

    assert request.allowed_paths == (".",)
    assert request.validation_profiles == ("verify",)
    assert request.allowed_commands == (f"{sys.executable} verify.py",)
    assert request.implementation_packet is not None
    assert request.implementation_packet.repository.allowed_paths == (".",)
    assert "Add the parser." in request.prompt
    assert "Valid input is parsed." in request.prompt
    assert "Implement the parser and report stable rejection codes." in request.prompt
    assert "Do not create branches, commits, pull requests" in request.prompt
    assert request.allow_existing_changes is True
    assert validation.command == (sys.executable, "verify.py")


def test_issue_worker_uses_explicit_repository_relative_allowed_paths() -> None:
    request, _validation = _issue_worker_implementation_request(
        {
            "objective": "Implement the feature.",
            "scope": ["Implement the parser."],
            "allowed_paths": ["src/feature.py", "tests/"],
            "implementation_steps": ["Add the parser."],
            "acceptance_criteria": ["Valid input is parsed."],
            "verification_command": f"{sys.executable} verify.py",
        },
        workflow_id="feature-v1-pr-01",
        base_commit="a" * 40,
    )

    assert request.allowed_paths == ("src/feature.py", "tests/")


def test_issue_worker_rejects_traversing_allowed_paths() -> None:
    with pytest.raises(PowdrrExecutionError, match="repository-relative paths"):
        _issue_worker_implementation_request(
            {
                "objective": "Implement the feature.",
                "scope": ["Implement the parser."],
                "allowed_paths": ["../outside.py"],
                "implementation_steps": ["Add the parser."],
                "acceptance_criteria": ["Valid input is parsed."],
                "verification_command": f"{sys.executable} verify.py",
            },
            workflow_id="feature-v1-pr-01",
            base_commit="a" * 40,
        )


def test_plan_amendment_artifacts_validate_without_touching_other_items(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "docs/plans/feature-v1-execution-plan.yaml"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(
        """\
schema: https://powdrr.io/schemas/github-issue-execution-plan-v1
plan_id: feature-v1
source_document: docs/plans/feature.md
items:
  - id: first
    title: First implementation
    objective: Add the first feature.
    scope: [Add the first feature.]
    allowed_paths: [src/]
    implementation_steps: [Implement the first feature.]
    acceptance_criteria: [The first feature works.]
    dependencies: []
    workflow_file: docs/workflows/feature-v1/feature-v1-first.yaml
    workflow_id: feature-v1-first
  - id: second
    title: Second implementation
    objective: Add the second feature.
    scope: [Add the second feature.]
    allowed_paths: [src/]
    implementation_steps: [Implement the second feature.]
    acceptance_criteria: [The second feature works.]
    dependencies: [first]
    workflow_file: docs/workflows/feature-v1/feature-v1-second.yaml
    workflow_id: feature-v1-second
""",
        encoding="utf-8",
    )
    proposal_path = tmp_path / "docs/plans/feature-v1-structrr-proposal.json"
    proposal = compile_multi_pr_proposal(
        plan_id="feature-v1",
        baseline_snapshot={
            "entities": [],
            "entity_relationships": [],
            "source_subjects": [],
        },
        items=[
            {"id": "first", "allowed_paths": ["src/"]},
            {"id": "second", "allowed_paths": ["src/"]},
        ],
        proposed_operations=[],
        source_ref="docs/plans/feature.md",
    )
    write_multi_pr_proposal(proposal_path, proposal)
    workflows = tmp_path / "docs/workflows/feature-v1"
    workflows.mkdir(parents=True)
    first_workflow = workflows / "feature-v1-first.yaml"
    first_workflow.write_text(
        """\
schema: https://powdrr.io/schemas/plan-workflow-instance-v2
workflow_id: feature-v1-first
process_id: execute-proposed-pr-workflow
dependencies: []
inputs:
  work_item_name: feature-v1
  proposed_pr_id: feature-v1-first
  feature_description: Add the first feature.
  proposed_pr_context:
    plan_id: feature-v1
    item_id: first
    title: First implementation
    objective: Add the first feature.
    scope: [Add the first feature.]
    allowed_paths: [src/]
    implementation_steps: [Implement the first feature.]
    acceptance_criteria: [The first feature works.]
    dependencies: []
    verification_command: bash verify.sh
    structrr_proposal_file: docs/plans/feature-v1-structrr-proposal.json
    structrr_item_id: first
    require_structrr_proposal: true
""",
        encoding="utf-8",
    )
    (workflows / "feature-v1-second.yaml").write_text(
        "second workflow\n", encoding="utf-8"
    )
    proposed = {
        "plan_id": "feature-v1",
        "item_id": "first",
        "structrr_proposal_file": "docs/plans/feature-v1-structrr-proposal.json",
    }
    workflow_id = "feature-v1-first"

    paths = _issue_worker_plan_amendment_paths(
        tmp_path, proposed, workflow_id=workflow_id
    )
    assert paths == (
        "docs/plans/feature-v1-execution-plan.yaml",
        "docs/plans/feature-v1-structrr-proposal.json",
        "docs/workflows/feature-v1/feature-v1-first.yaml",
    )
    baselines = {path: (tmp_path / path).read_text(encoding="utf-8") for path in paths}
    assert (
        _validate_issue_worker_plan_amendment(
            tmp_path, proposed, workflow_id=workflow_id, baselines=baselines
        )
        == ()
    )

    changed_plan = baselines[paths[0]].replace(
        "title: Second implementation", "title: Changed unrelated item"
    )
    (tmp_path / paths[0]).write_text(changed_plan, encoding="utf-8")
    findings = _validate_issue_worker_plan_amendment(
        tmp_path, proposed, workflow_id=workflow_id, baselines=baselines
    )
    assert "plan amendment changed unrelated plan items" in findings


def test_issue_worker_structrr_baseline_uses_target_commit_not_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def git(*arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Test User")
    git("config", "user.email", "test@example.com")
    (repo_root / "baseline.txt").write_text("target branch\n", encoding="utf-8")
    git("add", "baseline.txt")
    git("commit", "-qm", "Target branch baseline")
    baseline = {
        "entities": [{"id": "existing", "type": "Module", "description": "old"}],
        "entity_relationships": [],
        "source_subjects": [],
        "guidance": [
            {
                "id": "bootstrap-review-before-edit",
                "description": "Review inferred entities before using them.",
            }
        ],
    }
    latest = {
        **baseline,
        "entities": [
            *baseline["entities"],
            {"id": "other-feature", "type": "Module", "description": "landed"},
        ],
    }
    committed_bootstrap, manifest = _commit_test_structrr_baseline(repo_root, latest)
    base_commit = git("rev-parse", "HEAD")

    checkpoint_file = repo_root / ".github/workflows/ci.yml"
    checkpoint_file.parent.mkdir(parents=True)
    checkpoint_file.write_text("name: checkpoint\n", encoding="utf-8")
    git("add", ".github/workflows/ci.yml")
    git("commit", "-qm", "Persist earlier workflow checkpoint")
    checkpoint_commit = git("rev-parse", "HEAD")

    proposal = compile_multi_pr_proposal(
        plan_id="feature-v1",
        baseline_snapshot=baseline,
        items=[{"id": "feature", "allowed_paths": ["src/"]}],
        proposed_operations=[
            {
                "operation_id": "update-module",
                "item_id": "feature",
                "section": "entities",
                "subject_id": "existing",
                "action": "changed",
                "content": {
                    "id": "existing",
                    "type": "Module",
                    "description": "new",
                },
            }
        ],
        source_ref="docs/plans/feature.md",
    )
    proposal_path = repo_root / "docs/plans/feature-v1-structrr-proposal.json"
    proposal_path.parent.mkdir(parents=True)
    write_multi_pr_proposal(proposal_path, proposal)
    fresh_baseline = {
        **latest,
        "entities": [
            *latest["entities"],
            {"id": "present-on-main", "type": "Module", "description": "fresh"},
        ],
        "guidance": [],
    }
    bootstrap_calls: list[tuple[Path, dict[str, Any]]] = []

    def bootstrap_base(root: Path, **kwargs: Any) -> SimpleNamespace:
        assert root != repo_root
        assert git("-C", str(root), "rev-parse", "HEAD") == base_commit
        assert not (root / ".github/workflows/ci.yml").exists()
        bootstrap_calls.append((root, kwargs))
        return SimpleNamespace(
            document=fresh_baseline,
            source_manifest=manifest,
            validation=SimpleNamespace(successful=True, issues=()),
        )

    monkeypatch.setattr(task_agent, "bootstrap_structrr", bootstrap_base)

    result = _issue_worker_structrr_context(
        repo_root,
        {
            "plan_id": "feature-v1",
            "item_id": "feature",
            "structrr_item_id": "feature",
            "structrr_proposal_file": "docs/plans/feature-v1-structrr-proposal.json",
        },
        base_commit=base_commit,
    )

    assert result is not None
    current, rebased_proposal, current_manifest, report = result
    assert current["entities"] == fresh_baseline["entities"]
    assert current["guidance"] == committed_bootstrap["guidance"]
    assert rebased_proposal.structrr_baseline_fingerprint == task_agent.snapshot_digest(
        current
    )
    assert current_manifest == manifest
    assert len(bootstrap_calls) == 1
    bootstrap_root, bootstrap_options = bootstrap_calls[0]
    assert not bootstrap_root.exists()
    assert bootstrap_options["include_untracked"] is False
    assert bootstrap_options["repository_name"] == "repo"
    assert bootstrap_options["submission_base"] == base_commit
    assert (
        "docs/plans/feature-v1-execution-plan.yaml"
        in bootstrap_options["artifact_exclusions"]
    )
    candidate_exclusions = task_agent._issue_worker_structrr_artifact_exclusions(
        {
            "plan_id": "feature-v1",
            "final_review_report_path": "docs/reports/final.json",
        },
        additional_paths=("tests/test_generated_feature.py",),
    )
    assert "tests/test_generated_feature.py" in candidate_exclusions
    assert "docs/reports/final.json" in candidate_exclusions
    assert report["classification"] == "clean"
    assert checkpoint_commit != base_commit
    assert git("rev-parse", "HEAD") == checkpoint_commit
    assert (repo_root / ".github/workflows/ci.yml").is_file()


def test_issue_worker_blocks_file_addition_overlap_before_structrr_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def git(*arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Test User")
    git("config", "user.email", "test@example.com")
    existing_file = repo_root / "attestation/scripts/verify-all.sh"
    existing_file.parent.mkdir(parents=True)
    existing_file.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\npytest attestation/tests\n",
        encoding="utf-8",
    )
    git("add", "attestation/scripts/verify-all.sh")
    git("commit", "-qm", "Add existing verifier")
    base_commit = git("rev-parse", "HEAD")

    proposal = compile_multi_pr_proposal(
        plan_id="feature-v1",
        baseline_snapshot={
            "entities": [],
            "entity_relationships": [],
            "source_subjects": [],
        },
        items=[
            {
                "id": "feature",
                "allowed_paths": ["attestation/scripts/verify-all.sh"],
            }
        ],
        proposed_operations=[
            {
                "operation_id": "add-placeholder-verifier",
                "item_id": "feature",
                "section": "files",
                "subject_id": "file:attestation/scripts/verify-all.sh",
                "action": "added",
                "content": {
                    "path": "attestation/scripts/verify-all.sh",
                    "content": "#!/usr/bin/env bash\necho placeholder\n",
                },
            }
        ],
        source_ref="docs/plans/feature.md",
    )
    write_multi_pr_proposal(
        repo_root / "docs/plans/feature-v1-structrr-proposal.json", proposal
    )
    monkeypatch.setattr(
        task_agent,
        "bootstrap_structrr",
        lambda *_args, **_kwargs: pytest.fail(
            "proposal overlap must be found before Structrr bootstrap"
        ),
    )

    with pytest.raises(PowdrrExecutionError) as error:
        _issue_worker_structrr_context(
            repo_root,
            {
                "plan_id": "feature-v1",
                "item_id": "feature",
                "structrr_item_id": "feature",
                "structrr_proposal_file": (
                    "docs/plans/feature-v1-structrr-proposal.json"
                ),
                "require_structrr_proposal": True,
            },
            base_commit=base_commit,
        )

    message = str(error.value)
    assert "before generating a test plan" in message
    assert "verify-all.sh already exists with different content" in message


def test_issue_worker_blocks_structrr_subject_changed_since_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def git(*arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Test User")
    git("config", "user.email", "test@example.com")
    (repo_root / "baseline.txt").write_text("baseline\n", encoding="utf-8")
    git("add", "baseline.txt")
    git("commit", "-qm", "Target branch baseline")
    base_commit = git("rev-parse", "HEAD")

    baseline = {
        "entities": [{"id": "existing", "type": "Module", "description": "old"}],
        "entity_relationships": [],
        "source_subjects": [],
    }
    proposal = compile_multi_pr_proposal(
        plan_id="feature-v1",
        baseline_snapshot=baseline,
        items=[{"id": "feature", "allowed_paths": ["src/"]}],
        proposed_operations=[
            {
                "operation_id": "update-module",
                "item_id": "feature",
                "section": "entities",
                "subject_id": "existing",
                "action": "changed",
                "content": {
                    "id": "existing",
                    "type": "Module",
                    "description": "new",
                },
            }
        ],
        source_ref="docs/plans/feature.md",
    )
    proposal_path = repo_root / "docs/plans/feature-v1-structrr-proposal.json"
    proposal_path.parent.mkdir(parents=True)
    write_multi_pr_proposal(proposal_path, proposal)
    latest = {
        **baseline,
        "entities": [
            {"id": "existing", "type": "Module", "description": "changed elsewhere"}
        ],
    }
    _committed_snapshot, manifest = _commit_test_structrr_baseline(repo_root, latest)
    base_commit = git("rev-parse", "HEAD")
    monkeypatch.setattr(
        task_agent,
        "bootstrap_structrr",
        lambda *_args, **_kwargs: SimpleNamespace(
            document=latest,
            source_manifest=manifest,
            validation=SimpleNamespace(successful=True, issues=()),
        ),
    )

    with pytest.raises(PowdrrExecutionError, match="intervening repository work"):
        _issue_worker_structrr_context(
            repo_root,
            {
                "plan_id": "feature-v1",
                "item_id": "feature",
                "structrr_item_id": "feature",
                "structrr_proposal_file": (
                    "docs/plans/feature-v1-structrr-proposal.json"
                ),
            },
            base_commit=base_commit,
        )


def test_issue_worker_continues_after_nontrivial_validation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "README.md").write_text("initial\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\npythonpath = ['src']\n", encoding="utf-8"
    )
    (tmp_path / "verify.py").write_text(
        "from pathlib import Path\n"
        "assert 'value = 42' in Path('src/feature.py').read_text()\n",
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "feature.py").write_text("value = 1\n", encoding="utf-8")
    baseline = {
        "entities": [],
        "entity_relationships": [],
        "source_subjects": [],
        "source_bindings": [],
        "active_intent": [
            {
                "clause_id": "api-stable",
                "intent_id": "api-stable",
                "kind": "invariant",
                "statement": "Keep the public API stable.",
                "source_ref": "docs/decisions.md",
                "active": True,
            }
        ],
        "human-decisions": [],
        "invariants": [],
    }
    proposal = compile_multi_pr_proposal(
        plan_id="feature-v1",
        baseline_snapshot=baseline,
        items=[{"id": "feature-v1-pr-01", "allowed_paths": ["src"]}],
        proposed_operations=[],
        source_ref="docs/plans/feature-v1.md",
    )
    proposal_path = tmp_path / "docs/plans/feature-v1-structrr-proposal.json"
    write_multi_pr_proposal(proposal_path, proposal)
    workflow = _workflow(tmp_path)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
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
    _committed_baseline, baseline_manifest = _commit_test_structrr_baseline(
        tmp_path, baseline
    )
    task = replace(
        workflow.tasks[0],
        input_state={
            "proposed_pr_id": "feature-v1-pr-01",
            "proposed_pr_context": {
                "plan_id": "feature-v1",
                "item_id": "feature-v1-pr-01",
                "structrr_item_id": "feature-v1-pr-01",
                "structrr_proposal_file": (
                    "docs/plans/feature-v1-structrr-proposal.json"
                ),
                "require_structrr_proposal": True,
                "objective": "Implement the feature value.",
                "scope": ["src"],
                "implementation_steps": ["Set the feature value to 42."],
                "acceptance_criteria": ["The feature value equals 42."],
                "verification_command": "pytest",
            },
        },
    )
    provider_requests: list[Any] = []
    provider_timeouts: list[float] = []

    class NontrivialProvider:
        provider_name = "test-provider"

        def run(
            self,
            request: Any,
            *,
            worktree_root: Path,
            attempt_id: str,
        ) -> subprocess.CompletedProcess[str]:
            del attempt_id
            assert (worktree_root / "tests/integration/test_feature_e2e.py").is_file()
            provider_requests.append(request)
            source = worktree_root / "src" / "feature.py"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(
                "value = 1\n" if len(provider_requests) == 1 else "value = 42\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(["fake-agent"], 0, "done\n", "")

    def build_provider(*_args: Any, **kwargs: Any) -> NontrivialProvider:
        provider_timeouts.append(kwargs["timeout_seconds"])
        return NontrivialProvider()

    monkeypatch.setattr(task_agent, "build_coding_agent_provider", build_provider)
    monkeypatch.setattr(
        task_agent.tempfile,
        "gettempdir",
        lambda: str(tmp_path.parent / f"{tmp_path.name}-artifacts"),
    )
    generated_test_source = (
        "from feature import value\n\n"
        "# Covers: AC-01 test_public_feature_value_is_implemented\n"
        "def test_public_feature_value_is_implemented():\n"
        "    assert value == 42\n"
    )
    monkeypatch.setattr(
        task_agent,
        "_author_execute_pr_tests",
        lambda *_args, **_kwargs: (
            {"tests/integration/test_feature_e2e.py": generated_test_source},
            {
                "generation_method": "miniswe_pre_coding",
                "test_sha256": {"tests/integration/test_feature_e2e.py": "a" * 64},
                "authoring_attempts": [{"status": "completed"}],
                "test_obligations": [
                    {
                        "obligation_id": "AC-01",
                        "source_text": "The feature value equals 42.",
                    }
                ],
                "coverage_references": {
                    "AC-01": ["test_public_feature_value_is_implemented"]
                },
                "quality_review": {"outcome": "advisory", "notes": []},
                "baseline_execution_evidence": {"executed": 1},
                "baseline_result": {
                    "command": ["pytest", "tests/integration/test_feature_e2e.py"],
                    "returncode": 1,
                    "stdout": (
                        "FAILED tests/integration/test_feature_e2e.py::"
                        "test_public_feature_value_is_implemented"
                    ),
                    "stderr": "AssertionError: expected value 42, got 1",
                },
            },
        ),
    )
    candidate_manifest = replace(
        baseline_manifest,
        source_revision="b" * 40,
    )
    bootstrap_exclusions: list[tuple[str, ...]] = []

    def fake_bootstrap(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        bootstrap_exclusions.append(tuple(kwargs.get("artifact_exclusions", ())))
        return SimpleNamespace(
            document=baseline,
            validation=SimpleNamespace(successful=True, issues=[]),
            source_manifest=candidate_manifest,
        )

    monkeypatch.setattr(task_agent, "bootstrap_structrr", fake_bootstrap)
    reconciled_allowed_paths: list[tuple[str, ...]] = []
    reconcile = task_agent.reconcile_item_candidate

    def reconcile_candidate(**kwargs: Any) -> Any:
        reconciled_allowed_paths.append(kwargs["proposal"].allowed_paths)
        return reconcile(**kwargs)

    monkeypatch.setattr(task_agent, "reconcile_item_candidate", reconcile_candidate)
    review_phases: list[str] = []
    implementation_reviews = 0

    def review_decisions(
        _client: Any,
        *,
        phase: str,
        decisions: Sequence[Mapping[str, Any]],
        evidence: Mapping[str, Any],
        **_kwargs: Any,
    ) -> tuple[dict[str, str], ...]:
        nonlocal implementation_reviews
        del evidence
        review_phases.append(phase)
        if phase == "implementation":
            implementation_reviews += 1
        outcome = (
            "fail"
            if phase == "implementation" and implementation_reviews == 1
            else "pass"
        )
        return tuple(
            {
                "decision_id": str(item["decision_id"]),
                "outcome": outcome,
                "explanation": "The implementation did not yet prove API stability."
                if outcome == "fail"
                else "The candidate preserves the API invariant.",
            }
            for item in decisions
        )

    monkeypatch.setattr(task_agent, "review_inherited_decisions", review_decisions)
    config = WorkflowTaskAgentConfig(
        workflow_dir=workflow.directory,
        repo_root=tmp_path,
        code_agent="minisweagent",
        coding_agent_timeout_seconds=1800.0,
        coding_agent_validation_timeout_seconds=1800.0,
    )
    strategy = task_agent._TaskWorkflowExecutionStrategy(
        config=config,
        workflow=workflow,
        task=task,
        repo_root=tmp_path,
        error_log_root=tmp_path,
        client=cast(Any, None),
        compaction_client=cast(Any, None),
        model="test-model",
        mapping_provider="test-provider",
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        action_engine=cast(Any, None),
        events=[],
        repair_fallback_client=None,
        repair_fallback_model=None,
    )

    result = strategy._run_coding_agent()
    assert result["passed"] is True
    assert result["continuations"] == 1
    assert len(result["attempts"]) == 2
    assert result["attempts"][0]["validation"]["status"] == "failed"
    assert result["attempts"][1]["validation"]["status"] == "passed"
    assert result["structrr"]["passed"] is True
    assert result["integration_tests"]["passed"] is True
    assert result["integration_tests"]["generation_method"] == "miniswe_pre_coding"
    assert result["integration_tests"]["test_sha256"] == {
        "tests/integration/test_feature_e2e.py": "a" * 64
    }
    assert result["integration_tests"]["authoring_attempts"] == [
        {"status": "completed"}
    ]
    assert result["integration_tests"]["paths"] == [
        "tests/integration/test_feature_e2e.py"
    ]
    assert result["attempts"][0]["structrr"]["passed"] is False
    assert reconciled_allowed_paths == [("src",), ("src",)]
    assert (
        bootstrap_exclusions.count(
            (
                "docs/plans/feature-v1-execution-plan.yaml",
                "docs/plans/feature-v1-structrr-proposal.json",
                "docs/workflows/feature-v1/",
                "tests/integration/test_feature_e2e.py",
            )
        )
        == 2
    )
    assert review_phases == ["planning", "implementation", "implementation"]
    assert "Observed issue:" in provider_requests[1].prompt
    assert "active_intent:api-stable" in provider_requests[1].prompt
    assert "inherited decision" in provider_requests[1].prompt
    assert "validation" in provider_requests[1].prompt
    assert "Set the feature value to 42." in provider_requests[1].prompt
    assert "Advisory test-review notes for the pull request" in (
        provider_requests[0].prompt
    )
    assert "No unresolved test-quality concerns were reported." in (
        provider_requests[0].prompt
    )
    assert provider_timeouts == [1800.0]


@pytest.mark.parametrize("plan_amendment", [False, True])
def test_clean_pr_publish_result_is_recorded_like_pr_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plan_amendment: bool,
) -> None:
    workflow = _workflow(tmp_path)
    save_workflow_git_state(
        workflow.directory,
        WorkflowGitState(
            proposed_pr_id="feature-v1-pr-01",
            base_branch="main",
            integration_branch="powdrr/feature-v1-pr-01",
            workflow_relative_directory=workflow.directory.name,
            publication_id="stable-run-id",
        ),
    )
    task = replace(
        workflow.tasks[0],
        input_state={"proposed_pr_id": "feature-v1-pr-01"},
    )
    url = "https://github.com/example/repo/pull/42"
    recorded: list[object] = []
    published_parameters: list[Mapping[str, Any]] = []

    class RuntimeStub:
        def require_publish_readiness(self) -> None:
            pass

    def publish(_tool: str, parameters: Mapping[str, Any], **_kwargs: Any) -> str:
        published_parameters.append(dict(parameters))
        return url

    monkeypatch.setattr(task_agent, "invoke_intrinsic_capability", publish)
    monkeypatch.setattr(
        task_agent,
        "_record_task_pull_request",
        lambda *args, **_kwargs: recorded.append(args[5]),
    )
    strategy = task_agent._TaskWorkflowExecutionStrategy(
        config=cast(Any, SimpleNamespace(verbose=False)),
        workflow=workflow,
        task=task,
        repo_root=tmp_path,
        error_log_root=tmp_path,
        client=cast(Any, None),
        compaction_client=cast(Any, None),
        model="test-model",
        mapping_provider="test-provider",
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        action_engine=cast(Any, None),
        events=[{"kind": "plan_amendment_agent"}] if plan_amendment else [],
        repair_fallback_client=None,
        repair_fallback_model=None,
        runtime=cast(Any, RuntimeStub()),
    )

    strategy._execute_tool(
        WorkflowAction(
            kind="invoke_tool",
            tool="gh",
            parameters={
                "operation": "pr_publish_clean",
                "files_to_publish": ["src/example.py"],
                "publication_branch": "powdrr/feature-v1-pr-01",
            },
        )
    )

    assert len(recorded) == 1
    assert isinstance(recorded[0], Mapping)
    assert recorded[0]["stdout"] == url
    assert recorded[0]["command"] == ["gh", "pr", "create"]
    assert strategy.events[-1]["result"] == url
    assert published_parameters[0]["publication_branch"] == (
        "powdrr/feature-v1-pr-01-plan-amendment-stable-run-id"
        if plan_amendment
        else "powdrr/feature-v1-pr-01-implementation-stable-run-id"
    )


def test_implementation_pr_after_amendment_uses_a_distinct_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _workflow(tmp_path)
    save_workflow_git_state(
        workflow.directory,
        WorkflowGitState(
            proposed_pr_id="feature-v1-pr-01",
            base_branch="main",
            integration_branch="powdrr/feature-v1-pr-01",
            workflow_relative_directory=workflow.directory.name,
            publication_id="stable-run-id",
        ),
    )
    task = replace(
        workflow.tasks[0],
        input_state={"proposed_pr_id": "feature-v1-pr-01"},
    )
    published_parameters: list[Mapping[str, Any]] = []

    def publish(_tool: str, parameters: Mapping[str, Any], **_kwargs: Any) -> str:
        published_parameters.append(dict(parameters))
        return "https://github.com/example/repo/pull/42"

    class RuntimeStub:
        def require_publish_readiness(self) -> None:
            pass

    monkeypatch.setattr(task_agent, "invoke_intrinsic_capability", publish)
    monkeypatch.setattr(
        task_agent, "_record_task_pull_request", lambda *args, **_kwargs: None
    )
    strategy = task_agent._TaskWorkflowExecutionStrategy(
        config=cast(Any, SimpleNamespace(verbose=False)),
        workflow=workflow,
        task=task,
        repo_root=tmp_path,
        error_log_root=tmp_path,
        client=cast(Any, None),
        compaction_client=cast(Any, None),
        model="test-model",
        mapping_provider="test-provider",
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        action_engine=cast(Any, None),
        events=[{"kind": "plan_amendment_agent"}],
        repair_fallback_client=None,
        repair_fallback_model=None,
        runtime=cast(Any, RuntimeStub()),
    )

    for kind in ("plan-amendment", "implementation"):
        strategy._execute_tool(
            WorkflowAction(
                kind="invoke_tool",
                tool="gh",
                parameters={
                    "operation": "pr_publish_clean",
                    "files_to_publish": ["src/example.py"],
                    "publication_branch": f"powdrr/feature-v1-pr-01-{kind}",
                },
            )
        )

    assert [item["publication_branch"] for item in published_parameters] == [
        "powdrr/feature-v1-pr-01-plan-amendment-stable-run-id",
        "powdrr/feature-v1-pr-01-implementation-stable-run-id",
    ]


def test_plan_amendment_recovers_reviewed_context_for_nested_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def git(*arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Test User")
    git("config", "user.email", "test@example.invalid")

    workflow_id = "feature-v1-pr-01"
    plan_path = Path("docs/plans/feature-v1-execution-plan.yaml")
    proposal_path = Path("docs/plans/feature-v1-structrr-proposal.json")
    workflow_path = Path("docs/workflows/feature-v1/feature-v1-pr-01.yaml")
    artifacts = {
        plan_path: yaml.safe_dump(
            {
                "items": [
                    {
                        "id": "PR-01",
                        "workflow_id": workflow_id,
                        "workflow_file": workflow_path.as_posix(),
                    }
                ]
            }
        ),
        proposal_path: '{"items": []}\n',
        workflow_path: "schema: plan-workflow-instance-v1\n",
    }
    for path, content in artifacts.items():
        artifact = tmp_path / path
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(content, encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "Add generated plan artifacts")

    proposed_pr_context = {
        "plan_id": "feature-v1",
        "item_id": "PR-01",
        "proposed_pr_id": workflow_id,
        "title": "Bootstrap CI",
        "objective": "Add the CI workflow while preserving the existing verifier.",
        "scope": ["Add the CI workflow."],
        "implementation_steps": ["Edit the CI workflow."],
        "acceptance_criteria": ["CI runs the existing verifier."],
        "verification_command": "bash attestation/scripts/verify-all.sh",
        "structrr_proposal_file": proposal_path.as_posix(),
        "execution_plan_file": plan_path.as_posix(),
    }
    task_context = {
        "proposed_pr_id": workflow_id,
        "proposed_pr_context": proposed_pr_context,
    }
    workflow = _workflow(tmp_path)
    task = replace(workflow.tasks[0], input_state=task_context)
    workflow.update_task(task)
    strategy = task_agent._TaskWorkflowExecutionStrategy(
        config=cast(
            Any,
            SimpleNamespace(
                code_agent="minisweagent",
                opencode_executable=None,
                opencode_model=None,
                minisweagent_executable=None,
                minisweagent_model=None,
                coding_agent_timeout_seconds=1800.0,
                coding_agent_validation_timeout_seconds=1800.0,
            ),
        ),
        workflow=workflow,
        task=task,
        repo_root=tmp_path,
        error_log_root=tmp_path,
        client=cast(Any, None),
        compaction_client=cast(Any, None),
        model="test-model",
        mapping_provider="test-provider",
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        action_engine=cast(Any, None),
        events=[],
        repair_fallback_client=None,
        repair_fallback_model=None,
    )

    captured_requests: list[Any] = []
    expected_paths = [
        plan_path.as_posix(),
        proposal_path.as_posix(),
        workflow_path.as_posix(),
    ]
    monkeypatch.setattr(
        task_agent, "build_coding_agent_provider", lambda *_a, **_k: object()
    )
    monkeypatch.setattr(task_agent, "CodingAgentRunner", lambda *_a, **_k: object())

    def execute_attempt(
        _runner: object,
        request: Any,
        *,
        attempt_id: str,
        **_kwargs: Any,
    ) -> Any:
        captured_requests.append(request)
        for path in expected_paths:
            artifact = tmp_path / path
            artifact.write_text(
                artifact.read_text(encoding="utf-8") + "# amended\n",
                encoding="utf-8",
            )
        attempt = SimpleNamespace(
            status=SimpleNamespace(value="completed"),
            to_data=lambda: {"attempt_id": attempt_id, "status": "completed"},
        )
        validation = SimpleNamespace(
            status=task_agent.ValidationReportStatus.PASSED,
            to_data=lambda: {"status": "passed"},
        )
        return SimpleNamespace(
            attempt=attempt,
            validation=validation,
            diff_check=SimpleNamespace(returncode=0),
        )

    monkeypatch.setattr(task_agent, "execute_coding_agent_attempt", execute_attempt)
    monkeypatch.setattr(
        task_agent,
        "_implementation_paths_against_base",
        lambda _root: tuple(expected_paths),
    )
    monkeypatch.setattr(
        task_agent,
        "_validate_issue_worker_plan_amendment",
        lambda *_args, **_kwargs: (),
    )

    def one_amendment_attempt(run_attempt: Any, _evaluate_attempt: Any) -> Any:
        return SimpleNamespace(attempts=[run_attempt(1, None)])

    monkeypatch.setattr(task_agent, "run_coding_agent_recovery", one_amendment_attempt)
    failure_review = {
        "classification": "plan_defect",
        "evidence": [
            "verify-all.sh already exists with different content "
            "than the proposed placeholder."
        ],
        "explanation": "The accepted plan contradicts the repository baseline.",
    }
    implementation_result = {
        "passed": False,
        "changed_paths": [],
        "error": "pre-coding feasibility check rejected the file addition",
    }

    result = strategy._run_coding_agent(
        mode="plan-amendment",
        mode_context={
            # Simulate the nested Procedrr call that carried identity metadata
            # but dropped proposed_pr_context.
            "task_context": {"proposed_pr_id": workflow_id},
            "failure_review": failure_review,
            "implementation_result": implementation_result,
        },
    )

    assert result["passed"] is True
    assert result["changed_paths"] == expected_paths
    assert len(captured_requests) == 1
    request = captured_requests[0]
    assert request.objective == (
        "Correct plan item PR-01 from feature-v1 using the recorded failure evidence."
    )
    assert request.allowed_paths == tuple(expected_paths)
    assert "verify-all.sh already exists with different content" in request.prompt
    assert "pre-coding feasibility check rejected the file addition" in request.prompt


def test_edit_can_create_a_missing_file_from_line_one(tmp_path: Path) -> None:
    action = WorkflowAction(
        kind="edit",
        file_path="attestation/scripts/verify-all.sh",
        edits=(
            WorkflowEdit(
                "replace_lines",
                start_line=1,
                end_line=1,
                text="#!/usr/bin/env bash\necho verified",
            ),
        ),
    )

    _apply_task_edits(action, tmp_path)

    created = tmp_path / "attestation" / "scripts" / "verify-all.sh"
    assert created.read_text(encoding="utf-8") == (
        "#!/usr/bin/env bash\necho verified\n"
    )


class _FakeClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.response_index = 0
        self.messages: list[list[dict[str, str]]] = []

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, object]:
        self.messages.append(messages)
        if self.response_index >= len(self.responses):
            # Legacy action-shaped fixtures are adapted to the procedural
            # fragment protocol. Once the scripted actions are consumed, the
            # fragment is complete and the builder appends its terminal step.
            return {"step_json": json.dumps({"terminal": "succeeded"})}
        response = self.responses[self.response_index]
        self.response_index += 1
        if "action" in response:
            step = _legacy_action_to_process_step(response)
            if (
                response.get("action") in {"complete", "next_step"}
                and "output_state" in response
            ):
                self.responses.insert(
                    self.response_index,
                    {"step_json": json.dumps({"terminal": "succeeded"})},
                )
            return {"step_json": json.dumps(step)}
        return response


def _legacy_action_to_process_step(response: Mapping[str, Any]) -> dict[str, Any]:
    action = response.get("action")
    if action in {"complete", "next_step"}:
        if "output_state" not in response:
            return {
                "operation": {
                    "tool": "respond",
                    "parameters": {"text": response.get("text", "")},
                }
            }
        return {
            "operation": {
                "tool": "task_complete",
                "parameters": {
                    "output_state": response["output_state"],
                    "terminate_workflow": False,
                    "text": response.get("text", ""),
                },
            }
        }
    if action in {"get-human-input", "prompt_user"}:
        return {
            "operation": {
                "tool": "task_handoff",
                "parameters": {
                    "human_input": response.get("human_input"),
                    "question": response.get(
                        "text", "Please provide the required input."
                    ),
                    "continuation_instructions": "Use the human response to continue.",
                },
                "bind": "handoff_result",
            }
        }
    operations = {
        "edit": "edit",
        "yaml_edit": "yaml_edit",
        "file_management": "file_management",
        "read_document": "read_document",
        "list_files": "list_files",
        "gather_context": "gather_context",
        "invoke_tool": "invoke_tool",
    }
    tool = operations.get(str(action))
    if action == "task_run_process":
        return {
            "operation": {
                "tool": "task_run_process",
                "parameters": {
                    "process_id": response.get("process_id"),
                    "bindings": response.get("bindings", {}),
                },
                "bind": "nested_process_result",
            }
        }
    if tool is None:
        return {"terminal": "succeeded"}
    if tool == "invoke_tool":
        parameters = {
            "tool": response.get("tool"),
            "parameters": response.get("parameters", {}),
        }
    elif tool == "edit":
        parameters = {
            "file_path": response.get("file_path"),
            "edits": response.get("edits", []),
        }
    else:
        parameters = {key: value for key, value in response.items() if key != "action"}
    operation: dict[str, Any] = {"tool": tool, "parameters": parameters}
    if response.get("capture_as"):
        operation["bind"] = response["capture_as"]
    return {"operation": operation}


class _ProcedrrFragmentClient:
    def __init__(self, steps: list[dict[str, Any]]) -> None:
        self.steps = iter(steps)
        self.messages: list[list[dict[str, str]]] = []

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, object]:
        self.messages.append(messages)
        step = next(self.steps)
        return {"step_json": json.dumps(step)}


def test_repeated_stalled_task_action_requires_material_change() -> None:
    action = WorkflowAction(
        kind="edit",
        file_path="example.txt",
        decisions_and_context="first explanation",
    )
    stalled_actions: dict[str, object] = {
        task_agent.workflow_action_failure_signature(
            action, signature=task_agent.workflow_action_signature
        ): "unchanged",
    }

    with pytest.raises(task_agent.PowdrrExecutionError, match="materially"):
        task_agent._reject_repeated_stalled_action(
            replace(action, decisions_and_context="different explanation"),
            stalled_actions,
            "unchanged",
        )

    task_agent._reject_repeated_stalled_action(
        replace(action, decisions_and_context="different explanation"),
        stalled_actions,
        "changed",
    )
    assert not stalled_actions


def _workflow(tmp_path: Path) -> WorkflowInstance:
    return WorkflowInstance.create(
        tmp_path / "workflow",
        (
            WorkflowTask(
                task_id="agent-task",
                status=TaskStatus.OPEN,
                upstream_task_ids=(),
                dependent_state=(),
                complexity=TaskComplexity.MEDIUM,
                input_state={"request": "Choose an API version."},
                description="Choose an API version.",
                assignee_type=AssigneeType.AGENT,
                assignee_role=AgentRole.ARCHITECT,
                llm_type="simple_task",
            ),
        ),
    )


def _write_procedrr_process(
    tmp_path: Path,
    name: str,
    steps: list[dict[str, Any]],
    *,
    inputs: list[dict[str, Any]] | None = None,
    outputs: dict[str, Any] | None = None,
) -> Path:
    directory = tmp_path / "docs" / "procedrr" / "processes"
    directory.mkdir(parents=True, exist_ok=True)
    process: dict[str, Any] = {"version": 1, "name": name, "steps": steps}
    if inputs is not None:
        process["inputs"] = inputs
    if outputs is not None:
        process["outputs"] = outputs
    path = directory / f"{name}.yaml"
    path.write_text(yaml.safe_dump(process, sort_keys=False), encoding="utf-8")
    return path


def test_next_step_requires_top_level_output_state() -> None:
    action = WorkflowAction(kind="next_step", outputs={"result": "claimed"})

    with pytest.raises(Exception, match="top-level.*output_state"):
        _durable_task_action_output_state(action)


def test_select_ready_workflow_skips_workflow_with_incomplete_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_directory = tmp_path / "workflow"
    base_task = _workflow(tmp_path).tasks[0]
    WorkflowInstance.create(
        workflow_directory,
        (
            replace(base_task, task_id="tool-workflow-task-001"),
            replace(base_task, task_id="writer-workflow-task-001"),
        ),
    )
    save_workflow_git_state(
        workflow_directory,
        WorkflowGitState(
            proposed_pr_id="tool",
            base_branch="main",
            integration_branch="powdrr/tool",
            workflow_relative_directory="workflow",
            depends_on_workflows=("writer",),
        ),
    )
    save_workflow_git_state(
        workflow_directory,
        WorkflowGitState(
            proposed_pr_id="writer",
            base_branch="main",
            integration_branch="powdrr/writer",
            workflow_relative_directory="workflow",
        ),
    )

    monkeypatch.setattr(
        "powdrr_lift.workrr.task_agent.workflow_dependencies_completion",
        lambda _repo_root, state: (not state.depends_on_workflows, ()),
    )

    state, workflow_id = _select_ready_workflow_git_state(
        workflow_directory,
        tmp_path,
    )

    assert state is not None
    assert state.proposed_pr_id == "writer"
    assert workflow_id == "writer"


@pytest.mark.parametrize("verbose", [False, True])
def test_process_workflow_task_completes_claimed_agent_task(
    tmp_path: Path,
    verbose: bool,
) -> None:
    workflow = _workflow(tmp_path)
    client = _ProcedrrFragmentClient(
        [
            {
                "operation": {
                    "tool": "task_complete",
                    "parameters": {"output_state": {"version": "v2"}},
                }
            },
            {"terminal": "succeeded"},
        ]
    )
    stderr = io.StringIO()
    stdout = io.StringIO()

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
            verbose=verbose,
        ),
        client=client,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0, stderr.getvalue()
    completed = WorkflowInstance.from_directory(workflow.directory).tasks[0]
    assert completed.status is TaskStatus.COMPLETED
    assert completed.output_state == {"version": "v2"}
    prompt = "\n".join(
        message["content"] for messages in client.messages for message in messages
    )
    assert "Complete the claimed durable task" in prompt
    assert "task_complete" in prompt
    assert "Choose an API version." in prompt
    assert str(tmp_path) not in prompt
    displayed = stderr.getvalue()
    assert ("Workflow task LLM input:" in displayed) is verbose
    assert ("Workflow task LLM output:" in displayed) is verbose
    assert ('"step_json"' in displayed) is verbose
    assert "received streamed LLM data" not in displayed
    assert "Completed workflow task: agent-task" in stdout.getvalue()


def test_named_procedrr_process_outputs_complete_durable_task(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)
    task = replace(
        workflow.tasks[0],
        process_id="result-producing-process",
        output_state_type="implementation-result",
    )
    workflow.update_task(task)
    _write_procedrr_process(
        tmp_path,
        "result-producing-process",
        [
            {
                "operation": {
                    "tool": "respond",
                    "parameters": {"text": "Published pull request."},
                    "bind": "summary",
                }
            },
            {"terminal": "succeeded"},
        ],
        inputs=[{"name": "task_context", "type": "object", "required": True}],
        outputs={"summary": {"type": "object"}},
    )

    client = _ProcedrrFragmentClient(
        [
            {
                "operation": {
                    "tool": "respond",
                    "parameters": {"text": "Published pull request."},
                    "bind": "summary",
                }
            },
            {"terminal": "succeeded"},
        ]
    )
    stderr = io.StringIO()
    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 0, stderr.getvalue()
    completed = WorkflowInstance.from_directory(workflow.directory).tasks[0]
    assert completed.status is TaskStatus.COMPLETED
    assert completed.output_state == {"summary": {"text": "Published pull request."}}


def test_procedrr_task_handoff_persists_and_resumes_checkpoint(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)
    client = _ProcedrrFragmentClient(
        [
            {
                "operation": {
                    "tool": "task_handoff",
                    "parameters": {
                        "question": "Choose v1 or v2.",
                        "continuation_instructions": "Implement the selected version.",
                    },
                    "bind": "handoff_result",
                }
            },
            {
                "operation": {
                    "tool": "task_complete",
                    "parameters": {"output_state": {"version": "v2"}},
                }
            },
            {"terminal": "succeeded"},
        ]
    )
    config = WorkflowTaskAgentConfig(
        workflow_dir=workflow.directory,
        repo_root=tmp_path,
    )

    first_exit = run_workflow_task(
        config,
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    paused = WorkflowInstance.from_directory(workflow.directory)
    original = next(task for task in paused.tasks if task.task_id == "agent-task")
    human = next(
        task for task in paused.tasks if task.assignee_type is AssigneeType.HUMAN
    )
    follow_up = next(
        task
        for task in paused.tasks
        if task.assignee_type is AssigneeType.AGENT and task.task_id != "agent-task"
    )

    assert first_exit == 0
    assert original.status is TaskStatus.LOCKED
    assert follow_up.process_id == "durable-task"
    assert follow_up.process_checkpoint is not None
    assert (
        follow_up.input_state["human_response"]
        == f"{human.task_id}.{human.output_state_type}"
    )

    paused.complete_task(human.task_id, {"decision": "v2"})
    resumed_client = _ProcedrrFragmentClient([])
    second_exit = run_workflow_task(
        config,
        client=resumed_client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    completed = WorkflowInstance.from_directory(workflow.directory)

    assert second_exit == 0
    assert not resumed_client.messages
    assert all(
        task.status is TaskStatus.COMPLETED
        for task in completed.tasks
        if task.assignee_type is not AssigneeType.HUMAN or task.task_id == human.task_id
    )
    assert next(
        task for task in completed.tasks if task.task_id == "agent-task"
    ).output_state == {"version": "v2"}


def test_procedrr_task_dispatches_file_edits_through_task_authorization(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)
    (tmp_path / "notes.txt").write_text("draft\n", encoding="utf-8")
    client = _ProcedrrFragmentClient(
        [
            {
                "operation": {
                    "tool": "edit",
                    "parameters": {
                        "operation": "replace_lines",
                        "file_path": "notes.txt",
                        "start_line": 1,
                        "end_line": 1,
                        "replacement": "approved\n",
                    },
                    "bind": "edit_result",
                }
            },
            {
                "operation": {
                    "tool": "task_complete",
                    "parameters": {"output_state": {"updated": True}},
                }
            },
            {"terminal": "succeeded"},
        ]
    )

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "approved\n"
    assert WorkflowInstance.from_directory(workflow.directory).tasks[
        0
    ].output_state == {"updated": True}


def test_procedrr_task_cannot_use_an_operation_outside_declared_actions(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)
    task = replace(workflow.tasks[0], actions=("read_document",))
    workflow.update_task(task)
    (tmp_path / "notes.txt").write_text("draft\n", encoding="utf-8")
    client = _ProcedrrFragmentClient(
        [
            {
                "operation": {
                    "tool": "edit",
                    "parameters": {
                        "operation": "replace_lines",
                        "file_path": "notes.txt",
                        "start_line": 1,
                        "end_line": 1,
                        "replacement": "unauthorized\n",
                    },
                }
            },
            {"terminal": "succeeded"},
        ]
    )
    stderr = io.StringIO()

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 1
    assert "not allowed by the active step contract" in stderr.getvalue()
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "draft\n"


def test_deterministic_gather_context_must_be_persisted_exactly(
    tmp_path: Path,
) -> None:
    specs = tmp_path / "docs" / "proposals" / "feature"
    specs.mkdir(parents=True)
    (specs / "proposed-pr-specification.yaml").write_text(
        "schema: https://powdrr.io/schemas/proposed-pr-specification-v1\n"
        "id: feature\n"
        "feature_ids: [feature-id]\n"
        "proposed_prs:\n"
        "- id: feature-pr\n"
        "  intent: Capture the feature context.\n"
        "  justification: Required for the workflow.\n",
        encoding="utf-8",
    )
    workflow = WorkflowInstance.create(
        tmp_path / "workflow",
        (
            WorkflowTask(
                task_id="feature-task",
                status=TaskStatus.OPEN,
                upstream_task_ids=(),
                dependent_state=(),
                complexity=TaskComplexity.MEDIUM,
                input_state={"feature_id": "feature"},
                description="Gather feature context.",
                assignee_type=AssigneeType.AGENT,
                assignee_role=AgentRole.ARCHITECT,
                output_state_type="context-state",
                step_type="invoke_tool",
                pre_step=TaskPreStep(
                    action="gather_context",
                    template={
                        "feature_id": "<feature_id>",
                        "types": ["proposed_prs"],
                    },
                ),
            ),
        ),
    )
    expected_context = json.loads(
        render_gather_context_report(
            gather_specification_context(
                tmp_path,
                types=["proposed_prs"],
                feature_id="feature",
            )
        )
    )
    client = _FakeClient(
        [
            {"action": "complete", "output_state": {"context-state": "summary"}},
            {
                "action": "complete",
                "output_state": {"context-state": expected_context},
            },
        ]
    )

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(workflow_dir=workflow.directory, repo_root=tmp_path),
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    persisted = WorkflowInstance.from_directory(workflow.directory)
    assert persisted.tasks[0].output_state == {"context-state": expected_context}


def test_process_workflow_task_resolves_input_placeholders_before_llm(
    tmp_path: Path,
) -> None:
    workflow = WorkflowInstance.create(
        tmp_path / "workflow",
        (
            WorkflowTask(
                task_id="agent-task",
                status=TaskStatus.OPEN,
                upstream_task_ids=(),
                dependent_state=(),
                complexity=TaskComplexity.MEDIUM,
                input_state={
                    "feature_id": "interaction-file-logging",
                    "proposed_pr": "pr-interaction-capture-17",
                },
                description="Resolve the proposed PR.",
                details=(
                    "Use docs/proposals/<work-item-name>/ for "
                    "input_state.feature_id and input_state.proposed_pr."
                ),
                assignee_type=AssigneeType.AGENT,
                assignee_role=AgentRole.ARCHITECT,
                llm_type="simple_task",
            ),
        ),
    )
    client = _FakeClient([{"action": "complete", "output_state": {"ok": True}}])
    stderr = io.StringIO()

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 0, stderr.getvalue()
    prompt = "\n".join(
        message["content"] for messages in client.messages for message in messages
    )
    assert "docs/proposals/interaction-file-logging/" in prompt
    assert "pr-interaction-capture-17" in prompt
    assert "<work-item-name>" not in prompt
    assert "input_state.feature_id" not in prompt
    assert "input_state.proposed_pr" not in prompt


def test_process_workflow_task_runs_nested_process_in_same_worktree(
    tmp_path: Path,
) -> None:
    (tmp_path / "nested.txt").write_text("before\n", encoding="utf-8")
    _write_procedrr_process(
        tmp_path,
        "nested-process",
        [
            {
                "operation": {
                    "tool": "edit",
                    "parameters": {
                        "operation": "replace_lines",
                        "file_path": "nested.txt",
                        "start_line": 1,
                        "end_line": 1,
                        "replacement": "after\n",
                    },
                }
            }
        ],
    )
    workflow = _workflow(tmp_path)
    workflow.update_task(replace(workflow.tasks[0], uses_processes=("nested-process",)))
    client = _FakeClient(
        [
            {"action": "task_run_process", "process_id": "nested-process"},
            {"action": "complete", "output_state": {"ok": True}},
        ]
    )

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    assert WorkflowInstance.from_directory(workflow.directory).tasks[
        0
    ].output_state == {"ok": True}
    assert (tmp_path / "nested.txt").read_text(encoding="utf-8") == "after\n"
    assert len(client.messages) == 3


def test_nested_procedrr_operation_returns_action_result_binding() -> None:
    result = {"returncode": 0, "stdout": "verified", "stderr": ""}
    strategy: Any = SimpleNamespace(
        workflow=SimpleNamespace(),
        task=SimpleNamespace(task_id="task-001", process_checkpoint=None),
        stdout=io.StringIO(),
        events=[],
    )

    def execute_action(action: Any) -> SimpleNamespace:
        strategy.events.append(
            {
                "kind": action.kind,
                "tool": action.tool,
                "result": result,
            }
        )
        return SimpleNamespace(continue_running=True)

    strategy.execute_action = execute_action
    process_catalog: Any = SimpleNamespace()
    process_client: Any = SimpleNamespace()
    operations = _TaskProcedrrOperations(
        strategy,
        process_id="outer-process",
        process_catalog=process_catalog,
        process_client=process_client,
    )

    output = operations.execute(
        "internal",
        {"command": ["echo", "verified"]},
    )

    assert output == result


def test_coding_agent_error_result_is_normalized_at_operation_boundary() -> None:
    strategy: Any = SimpleNamespace(
        workflow=SimpleNamespace(),
        task=SimpleNamespace(task_id="task-001", process_checkpoint=None),
        stdout=io.StringIO(),
        events=[],
    )

    def execute_action(_action: Any) -> SimpleNamespace:
        raise PowdrrExecutionError("invalid test coverage contract")

    strategy.execute_action = execute_action
    operations = _TaskProcedrrOperations(
        strategy,
        process_id="execute-proposed-pr-workflow",
        process_catalog=cast(Any, SimpleNamespace()),
        process_client=cast(Any, SimpleNamespace()),
    )

    output = operations.execute(
        "invoke_tool",
        {"tool": "coding-agent", "parameters": {}},
    )

    assert output == {
        "passed": False,
        "changed_paths": [],
        "error": "invalid test coverage contract",
        "retryable": True,
    }


def test_coding_agent_exception_binds_and_reaches_parent_workflow_review(
    tmp_path: Path,
) -> None:
    """Carry the adapter's raised-error path through the real parent process."""
    from procedrr import parse_and_validate
    from procedrr_evaluator import Evaluator

    (tmp_path / "execute-proposed-pr.yaml").write_text(
        """\
version: 1
name: execute-proposed-pr
inputs: [{name: task_context, type: object, required: true}]
outputs:
  coding_attempt:
    type: object
    required: [passed, changed_paths, error, retryable]
    properties:
      passed: {type: boolean}
      changed_paths: {type: array, items: {type: string}}
      error: {type: [string, "null"]}
      retryable: {type: boolean}
steps:
  - operation:
      tool: invoke_tool
      parameters: {tool: coding-agent, parameters: {}}
      bind: coding_attempt
  - terminal: succeeded
""",
        encoding="utf-8",
    )
    repository_processes = (
        Path(__file__).resolve().parents[1] / "docs/procedrr/processes"
    )
    for name in ("finish-implementation-pr", "amend-plan-and-open-pr"):
        (tmp_path / f"{name}.yaml").write_text(
            (repository_processes / f"{name}.yaml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    classified_contexts: list[str] = []

    class WorkflowLLM:
        def complete_json(
            self, messages: list[dict[str, str]], **_: Any
        ) -> dict[str, Any]:
            question = messages[1]["content"]
            assert "Does the recorded failure demonstrate" in question
            classified_contexts.append(question)
            return {
                "classification": "unclear",
                "evidence": ["The coding agent raised a retryable execution error."],
                "explanation": "The failure does not establish a plan defect.",
            }

    strategy: Any = SimpleNamespace(
        workflow=SimpleNamespace(),
        task=SimpleNamespace(task_id="task-001", process_checkpoint=None),
        stdout=io.StringIO(),
        events=[],
    )

    def raise_coding_agent_error(_action: Any) -> Any:
        raise PowdrrExecutionError("invalid test coverage contract")

    strategy.execute_action = raise_coding_agent_error
    operations = _TaskProcedrrOperations(
        strategy,
        process_id="execute-proposed-pr-workflow",
        process_catalog=cast(Any, SimpleNamespace()),
        process_client=cast(Any, SimpleNamespace()),
    )
    operation_calls: list[tuple[str, Mapping[str, Any]]] = []

    def execute(tool: str, parameters: Mapping[str, Any]) -> Any:
        operation_calls.append((tool, parameters))
        return operations.execute(tool, parameters)

    workflow = parse_and_validate(
        (repository_processes / "execute-proposed-pr-workflow.yaml").read_text(
            encoding="utf-8"
        )
    )
    result = Evaluator(WorkflowLLM(), execute, process_directory=tmp_path).evaluate(
        workflow, {"task_context": {"objective": "Implement a feature"}}
    )

    assert result.events[-1].data["status"] == "failed"
    assert len(operation_calls) == 1
    assert operation_calls[0][0] == "invoke_tool"
    assert "invalid test coverage contract" in classified_contexts[0]
    assert "unknown binding" not in classified_contexts[0]


def test_nested_atomic_tool_step_advances_without_llm_confirmation(
    tmp_path: Path,
) -> None:
    _write_procedrr_process(
        tmp_path,
        "nested-process",
        [
            {
                "operation": {
                    "tool": "invoke_tool",
                    "parameters": {
                        "tool": "shell",
                        "parameters": {"command": ["printf", "nested"]},
                    },
                }
            }
        ],
    )
    workflow = _workflow(tmp_path)
    workflow.update_task(replace(workflow.tasks[0], uses_processes=("nested-process",)))
    client = _FakeClient(
        [
            {"action": "task_run_process", "process_id": "nested-process"},
            {"action": "complete", "output_state": {"ok": True}},
        ]
    )

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    assert len(client.messages) == 3
    assert WorkflowInstance.from_directory(workflow.directory).tasks[
        0
    ].output_state == {"ok": True}


def test_process_workflow_task_propagates_verbose_to_nested_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_procedrr_process(
        tmp_path,
        "nested-process",
        [
            {
                "operation": {
                    "tool": "invoke_tool",
                    "parameters": {
                        "tool": "shell",
                        "parameters": {"command": ["printf", "nested"]},
                    },
                }
            }
        ],
    )
    workflow = _workflow(tmp_path)
    workflow.update_task(replace(workflow.tasks[0], uses_processes=("nested-process",)))
    client = _FakeClient(
        [
            {"action": "task_run_process", "process_id": "nested-process"},
            {"action": "complete", "output_state": {"ok": True}},
        ]
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    observed_verbose: list[bool] = []
    original_execute_shell_tool = task_agent._execute_shell_tool

    def capture_verbose(*args: Any, **kwargs: Any) -> dict[str, Any]:
        observed_verbose.append(kwargs["verbose"])
        return original_execute_shell_tool(*args, **kwargs)

    monkeypatch.setattr(
        task_agent,
        "_execute_shell_tool",
        capture_verbose,
    )

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
            verbose=True,
        ),
        client=client,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert observed_verbose == [True]


@pytest.mark.parametrize("with_checkpoint_callback", [False, True])
def test_process_workflow_task_uses_workflow_integration_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_checkpoint_callback: bool,
) -> None:
    primary_root = tmp_path / "repo"
    primary_root.mkdir()
    for command in (
        ("git", "init", "-b", "main"),
        ("git", "config", "user.email", "test@example.com"),
        ("git", "config", "user.name", "Test User"),
    ):
        subprocess.run(
            list(command),
            cwd=primary_root,
            check=True,
            capture_output=True,
            text=True,
        )
    (primary_root / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=primary_root,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=primary_root,
        check=True,
        capture_output=True,
        text=True,
    )
    source_root = primary_root / ".worktrees" / "source"
    subprocess.run(
        ["git", "worktree", "add", "-b", "source", str(source_root), "main"],
        cwd=primary_root,
        check=True,
        capture_output=True,
        text=True,
    )
    workflow_dir = source_root / "docs" / "workflows" / "feature"
    WorkflowInstance.create(
        workflow_dir,
        (
            WorkflowTask(
                task_id="feature-17-task-001",
                status=TaskStatus.OPEN,
                upstream_task_ids=(),
                dependent_state=(),
                complexity=TaskComplexity.MEDIUM,
                input_state={"request": "Choose an API version."},
                description="Choose an API version.",
                assignee_type=AssigneeType.AGENT,
                assignee_role=AgentRole.ARCHITECT,
                llm_type="simple_task",
            ),
        ),
    )
    state = WorkflowGitState(
        proposed_pr_id="feature-17",
        base_branch="main",
        integration_branch="powdrr/feature-17",
        workflow_relative_directory="docs/workflows/feature",
    )
    save_workflow_git_state(workflow_dir, state)
    subprocess.run(
        ["git", "add", "docs/workflows/feature"],
        cwd=source_root,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "initialize workflow"],
        cwd=source_root,
        check=True,
        capture_output=True,
        text=True,
    )
    integration_root = primary_root / ".worktrees" / "powdrr" / "powdrr-feature-17"
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "-b",
            "powdrr/feature-17",
            str(integration_root),
            "source",
        ],
        cwd=primary_root,
        check=True,
        capture_output=True,
        text=True,
    )
    published_roots: list[Path] = []
    final_pr_calls: list[str] = []
    checkpoints: list[tuple[str, str, object, str | None]] = []

    def _record_checkpoint(
        task_id: str,
        state_type: str,
        state: object,
        commit_sha: str | None,
    ) -> None:
        checkpoints.append((task_id, state_type, state, commit_sha))

    monkeypatch.setattr(
        "powdrr_lift.workrr.task_agent._open_final_workflow_pull_request",
        lambda *_args, **_kwargs: final_pr_calls.append("opened"),
    )

    def _record_publish(
        repo_root: Path,
        published_workflow: WorkflowInstance,
        *,
        workflow_id: str | None = None,
        reason: str,
        stdout: object,
        open_pull_request: bool = True,
        events: Sequence[Mapping[str, object]] = (),
        runtime: object = None,
    ) -> str:
        del workflow_id, reason, stdout, open_pull_request, events, runtime
        published_roots.append(repo_root)
        assert published_workflow.directory == (
            integration_root / "docs" / "workflows" / "feature"
        )
        return "d" * 40

    monkeypatch.setattr(
        "powdrr_lift.workrr.task_agent._publish_workflow_progress",
        _record_publish,
    )

    config = WorkflowTaskAgentConfig(
        workflow_dir=workflow_dir,
        repo_root=primary_root,
        task_checkpoint_callback=(
            _record_checkpoint if with_checkpoint_callback else None
        ),
    )
    exit_code = run_workflow_task(
        config,
        client=_FakeClient([{"action": "complete", "output_state": {"ok": True}}]),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    assert published_roots == [integration_root, integration_root]
    assert final_pr_calls == []
    assert checkpoints == (
        [("feature-17-task-001", "state", {"ok": True}, "d" * 40)]
        if with_checkpoint_callback
        else []
    )
    assert WorkflowInstance.from_directory(
        integration_root / "docs" / "workflows" / "feature"
    ).tasks[0].status is (TaskStatus.COMPLETED)
    assert (
        subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=integration_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "powdrr/feature-17"
    )
    assert not (integration_root / "tasks").exists()


def test_publish_workflow_progress_pushes_clean_worktree_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = WorkflowInstance.create(tmp_path / "workflow")
    save_workflow_git_state(
        workflow.directory,
        WorkflowGitState(
            proposed_pr_id="feature-17",
            base_branch="main",
            integration_branch="powdrr/feature-17",
            workflow_relative_directory="workflow",
        ),
    )
    git_calls: list[list[str]] = []

    monkeypatch.setattr(
        "powdrr_lift.workrr.task_agent._is_git_worktree",
        lambda _repo_root: True,
    )
    monkeypatch.setattr(
        "powdrr_lift.workrr.task_agent._git_output",
        lambda _repo_root, _arguments, **_kwargs: "powdrr/feature-17",
    )
    monkeypatch.setattr(
        "powdrr_lift.workrr.task_agent._git_result",
        lambda _repo_root, _arguments, **_kwargs: subprocess.CompletedProcess(
            [], 0, stdout="", stderr=""
        ),
    )

    def _record_git_call(
        _repo_root: Path, arguments: list[str], **_kwargs: object
    ) -> str:
        git_calls.append(arguments)
        return ""

    monkeypatch.setattr("powdrr_lift.workrr.task_agent._run_git", _record_git_call)

    _publish_workflow_progress(
        tmp_path,
        workflow,
        workflow_id="feature-17",
        reason="complete feature-17-task-001",
        stdout=io.StringIO(),
        open_pull_request=False,
    )

    assert ["add", "--all"] in git_calls
    assert ["push", "--set-upstream", "origin", "powdrr/feature-17"] in git_calls


def test_final_workflow_pull_request_leaves_branch_identity_to_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = WorkflowInstance.create(tmp_path / "workflow")
    state = WorkflowGitState(
        proposed_pr_id="feature-17",
        base_branch="main",
        integration_branch="powdrr/feature-17",
        workflow_relative_directory="workflow",
    )
    gh_calls: list[Mapping[str, Any]] = []

    def _record_gh(
        _repo_root: Path,
        parameters: Mapping[str, Any],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        gh_calls.append(parameters)
        if parameters["operation"] == "pr_view":
            return subprocess.CompletedProcess([], 1, stdout="", stderr="missing")
        return subprocess.CompletedProcess(
            [],
            0,
            stdout="https://github.com/example/repo/pull/17\n",
            stderr="",
        )

    monkeypatch.setattr("powdrr_lift.workrr.task_agent._gh_result", _record_gh)
    monkeypatch.setattr(
        "powdrr_lift.workrr.task_agent._write_workflow_record",
        lambda *_args, **_kwargs: None,
    )

    _open_final_workflow_pull_request(
        tmp_path,
        workflow,
        state,
        stdout=io.StringIO(),
    )

    create_request = gh_calls[-1]
    assert create_request["operation"] == "pr_create"
    assert "base" not in create_request
    assert "head" not in create_request


def test_process_workflow_task_persists_output_for_downstream_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _workflow(tmp_path)
    downstream = WorkflowTask(
        task_id="next-task",
        status=TaskStatus.OPEN,
        upstream_task_ids=("agent-task",),
        dependent_state=("next-input-ready",),
        complexity=TaskComplexity.MEDIUM,
        input_state={"plan": "agent-task.state"},
        description="Use the completed plan.",
        output_state_type="implementation-state",
    )
    workflow.add_task(downstream)
    published_reasons: list[str] = []
    checkpoints: list[tuple[str, str, object, str | None]] = []

    def _record_publish(
        repo_root: Path,
        published_workflow: WorkflowInstance,
        *,
        workflow_id: str | None = None,
        reason: str,
        stdout: object,
        open_pull_request: bool = True,
        events: Sequence[Mapping[str, object]] = (),
        runtime: object = None,
    ) -> str:
        del workflow_id, open_pull_request, events, runtime
        assert repo_root == tmp_path
        assert published_workflow.directory == workflow.directory
        published_reasons.append(reason)
        return "d" * 40

    monkeypatch.setattr(
        "powdrr_lift.workrr.task_agent._publish_workflow_progress",
        _record_publish,
    )
    client = _FakeClient(
        [
            {"action": "next_step", "output_state": {"plan": ["step"]}},
            {"action": "complete", "output_state": {"result": "done"}},
        ]
    )

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
            task_checkpoint_callback=lambda task_id, state_type, state, commit: (
                checkpoints.append((task_id, state_type, state, commit))
            ),
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    persisted = WorkflowInstance.from_directory(workflow.directory)
    completed_task = next(
        task for task in persisted.tasks if task.task_id == "agent-task"
    )
    next_task = next(task for task in persisted.tasks if task.task_id == "next-task")
    assert exit_code == 0
    assert completed_task.output_state == {"plan": ["step"]}
    assert next_task.input_state == {"plan": {"plan": ["step"]}}
    assert next_task.status is TaskStatus.COMPLETED
    assert next_task.output_state == {"result": "done"}
    assert published_reasons == [
        "claim agent-task",
        "next_step agent-task",
        "claim next-task",
        "next_step next-task",
    ]
    assert checkpoints == [
        ("agent-task", "state", {"plan": ["step"]}, "d" * 40),
        ("next-task", "implementation-state", {"result": "done"}, "d" * 40),
    ]


def test_locked_workflow_task_reports_recovery_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = replace(_workflow(tmp_path).tasks[0], status=TaskStatus.LOCKED)
    workflow = WorkflowInstance.create(tmp_path / "workflow", (task,))
    state = WorkflowGitState(
        proposed_pr_id="feature-17",
        base_branch="main",
        integration_branch="powdrr/feature-17",
        workflow_relative_directory="workflow",
    )
    monkeypatch.setattr(
        "powdrr_lift.workrr.task_agent.inspect_workflow_run",
        lambda _repo_root, _proposed_pr_id, **_kwargs: {
            "claim_refs": [
                "refs/agents/claims/feature-17/agent-task",
            ]
        },
    )

    with pytest.raises(WorkflowGitInconsistency) as raised:
        _validate_workflow_task_state(workflow, state, tmp_path)

    message = str(raised.value)
    assert "agent-task is locked" in message
    assert "refs/agents/claims/feature-17/agent-task" in message
    assert (
        "powdrr-lift workflow-recovery --proposed-pr-id feature-17 "
        "--integration-branch powdrr/feature-17 --cleanup" in message
    )


def test_process_workflow_task_stops_when_human_task_becomes_ready(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)
    workflow.add_task(
        WorkflowTask(
            task_id="human-task",
            status=TaskStatus.OPEN,
            upstream_task_ids=("agent-task",),
            dependent_state=(),
            complexity=TaskComplexity.LOW,
            input_state={"decision": "required"},
            description="Make the final decision.",
            assignee_type=AssigneeType.HUMAN,
            assignee_role=HumanRole.REVIEWER,
        )
    )
    stdout = io.StringIO()

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=_FakeClient(
            [{"action": "next_step", "output_state": {"result": "ready"}}]
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    persisted = WorkflowInstance.from_directory(workflow.directory)
    assert exit_code == 0
    assert persisted.tasks[0].status is TaskStatus.COMPLETED
    assert persisted.tasks[1].status is TaskStatus.OPEN
    assert "Workflow waiting on human task: human-task" in stdout.getvalue()


def test_process_workflow_task_repairs_invalid_json_response(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)

    class _InvalidThenCompleteClient(_FakeClient):
        def __init__(self) -> None:
            super().__init__([])
            self.calls = 0

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, object]:
            self.messages.append(messages)
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError(
                    "OpenAI response content was not valid JSON: Expecting value"
                )
            if self.calls == 2:
                return {
                    "step_json": json.dumps(
                        {
                            "operation": {
                                "tool": "task_complete",
                                "parameters": {"output_state": {"version": "v2"}},
                            }
                        }
                    )
                }
            return {"step_json": json.dumps({"terminal": "succeeded"})}

    client = _InvalidThenCompleteClient()
    stderr = io.StringIO()
    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 0
    assert client.calls == 3
    assert "The previous response was invalid" in client.messages[1][-1]["content"]
    assert "Expecting value" in client.messages[1][-1]["content"]
    assert WorkflowInstance.from_directory(workflow.directory).tasks[0].status is (
        TaskStatus.COMPLETED
    )


def test_process_workflow_task_retries_llm_timeouts_with_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _workflow(tmp_path)
    client = _FakeClient([])
    calls = 0
    sleeps: list[float] = []

    def _complete(messages: list[dict[str, str]]) -> dict[str, object]:
        nonlocal calls
        client.messages.append(messages)
        calls += 1
        if calls < 3:
            raise RuntimeError("OpenAI-compatible request timed out")
        if calls == 3:
            return {
                "step_json": json.dumps(
                    {
                        "operation": {
                            "tool": "task_complete",
                            "parameters": {"output_state": {"version": "v2"}},
                        }
                    }
                )
            }
        return {"step_json": json.dumps({"terminal": "succeeded"})}

    client.complete_json = _complete  # type: ignore[method-assign]
    monkeypatch.setattr("powdrr_lift.workrr.llm.time.sleep", sleeps.append)
    stderr = io.StringIO()

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
            max_timeout_retries=2,
            timeout_backoff_seconds=1.5,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 0
    assert calls == 4
    assert sleeps == [1.5, 3.0]
    assert len(client.messages) == 4


def test_process_workflow_task_retries_provider_overload_with_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _workflow(tmp_path)
    client = _FakeClient([])
    calls = 0
    sleeps: list[float] = []

    def _complete(messages: list[dict[str, str]]) -> dict[str, object]:
        nonlocal calls
        client.messages.append(messages)
        calls += 1
        if calls < 3:
            raise WorkflowLLMHTTPError(
                "OpenAI",
                429,
                '{"error":{"code":"engine_overloaded"}}',
            )
        if calls == 3:
            return {
                "step_json": json.dumps(
                    {
                        "operation": {
                            "tool": "task_complete",
                            "parameters": {"output_state": {"version": "v2"}},
                        }
                    }
                )
            }
        return {"step_json": json.dumps({"terminal": "succeeded"})}

    client.complete_json = _complete  # type: ignore[method-assign]
    monkeypatch.setattr("powdrr_lift.workrr.llm.time.sleep", sleeps.append)
    stderr = io.StringIO()

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
            max_timeout_retries=2,
            timeout_backoff_seconds=1.5,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 0
    assert calls == 4
    assert sleeps == [1.5, 3.0]
    assert len(client.messages) == 4


def test_workflow_retries_dropped_provider_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient([])
    calls = 0
    sleeps: list[float] = []

    def _complete(messages: list[dict[str, str]]) -> dict[str, object]:
        nonlocal calls
        client.messages.append(messages)
        calls += 1
        if calls < 3:
            raise RuntimeError(
                "OpenAI request connection dropped: "
                "Remote end closed connection without response"
            )
        return {"action": "next_step"}

    client.complete_json = _complete  # type: ignore[method-assign]
    monkeypatch.setattr("powdrr_lift.workrr.llm.time.sleep", sleeps.append)

    result = complete_json_with_timeout_retry(
        client,
        [{"role": "user", "content": "hello"}],
        model="test-model",
        stderr=io.StringIO(),
        max_timeout_retries=2,
        timeout_backoff_seconds=1.5,
    )

    assert result == {"action": "next_step"}
    assert calls == 3
    assert sleeps == [1.5, 3.0]


def test_exhausted_timeout_keeps_workflow_worktree_for_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted: list[Path] = []
    monkeypatch.setattr(
        "powdrr_lift.workrr.task_agent._delete_workflow_task_worktree",
        lambda path, *, stderr: deleted.append(path),
    )
    task = _workflow(tmp_path).tasks[0]
    dedicated_worktree = tmp_path / ".worktrees" / "timed-out-task"

    result = _handle_exhausted_timeout(
        dedicated_worktree,
        task,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        error=RuntimeError("timed out"),
    )

    assert result == 2
    assert deleted == []


def test_process_workflow_task_repairs_invalid_fragment_step(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)
    stderr = io.StringIO()
    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=_FakeClient(
            [
                {"step_json": "not-json"},
                {"action": "complete", "output_state": {"version": "v2"}},
            ]
        ),
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 0
    assert WorkflowInstance.from_directory(workflow.directory).tasks[0].status is (
        TaskStatus.COMPLETED
    )


def test_process_workflow_task_supports_fuzzy_match_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = _workflow(tmp_path)
    (tmp_path / "candidate-spec.yaml").write_text("name: candidate\n", encoding="utf-8")
    client = _FakeClient(
        [
            {
                "action": "invoke_tool",
                "tool": "fuzzy-match",
                "parameters": {
                    "command": [
                        "fuzzy-match",
                        ".",
                        "-name",
                        "candidate",
                        "-type",
                        "f",
                        "-print",
                    ]
                },
            },
            {"action": "complete", "output_state": {"found": True}},
        ]
    )
    commands: list[list[str]] = []
    original_fuzzy_match = task_agent.invoke_fuzzy_match_capability

    def capture_command(parameters: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        command = parameters.get("command")
        if isinstance(command, list):
            commands.append([str(part) for part in command])
        return original_fuzzy_match(parameters, **kwargs)

    monkeypatch.setattr(task_agent, "invoke_fuzzy_match_capability", capture_command)

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    assert commands == [
        ["fuzzy-match", ".", "-name", "candidate", "-type", "f", "-print"]
    ]


def test_process_workflow_task_can_continue_after_tool_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = _workflow(tmp_path)
    (tmp_path / "candidate-spec.yaml").write_text("name: candidate\n", encoding="utf-8")
    client = _FakeClient(
        [
            {
                "action": "invoke_tool",
                "tool": "fuzzy-match",
                "parameters": {"command": ["fuzzy-match", "."]},
            },
            {
                "action": "invoke_tool",
                "tool": "fuzzy-match",
                "parameters": {
                    "command": [
                        "fuzzy-match",
                        ".",
                        "-name",
                        "candidate",
                        "-type",
                        "f",
                    ]
                },
            },
            {"action": "complete", "output_state": {"found": True}},
        ]
    )
    commands: list[list[str]] = []
    original_fuzzy_match = task_agent.invoke_fuzzy_match_capability

    def capture_command(parameters: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        command = parameters.get("command")
        if isinstance(command, list):
            commands.append([str(part) for part in command])
        return original_fuzzy_match(parameters, **kwargs)

    monkeypatch.setattr(task_agent, "invoke_fuzzy_match_capability", capture_command)

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    assert len(client.messages) == 4
    assert commands == [
        ["fuzzy-match", "."],
        ["fuzzy-match", ".", "-name", "candidate", "-type", "f"],
    ]


def test_process_workflow_task_repairs_invalid_edit_line_number(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)
    client = _FakeClient(
        [
            {
                "action": "edit",
                "file_path": "candidate.py",
                "edits": [
                    {
                        "kind": "add",
                        "start_line": 0,
                        "text": "value = 1\n",
                    }
                ],
            },
            {"action": "complete", "output_state": {"fixed": True}},
        ]
    )

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    assert len(client.messages) == 3
    correction = "\n".join(
        message["content"]
        for message in client.messages[1]
        if message["role"] == "user"
    )
    assert "rejected_fingerprints" in correction


def test_process_workflow_task_supports_gather_context_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = _workflow(tmp_path)
    specs = tmp_path / "docs" / "specs" / "example"
    specs.mkdir(parents=True)
    (specs / "proposed-pr-specification.yaml").write_text(
        "proposed_prs:\n- id: example-pr\n  state: proposed\n",
        encoding="utf-8",
    )
    client = _FakeClient(
        [
            {
                "action": "gather_context",
                "types": ["proposed_prs"],
                "keywords": ["example-pr"],
            },
            {"action": "complete", "output_state": {"found": True}},
        ]
    )
    gathered: list[dict[str, Any]] = []
    original_gather_context = task_agent.gather_specification_context

    def capture_gather_context(root: Path, **kwargs: Any) -> Any:
        gathered.append(kwargs)
        return original_gather_context(root, **kwargs)

    monkeypatch.setattr(
        task_agent, "gather_specification_context", capture_gather_context
    )

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    assert WorkflowInstance.from_directory(workflow.directory).tasks[0].status is (
        TaskStatus.COMPLETED
    )
    assert gathered == [
        {
            "types": ["proposed_prs"],
            "keywords": ["example-pr"],
            "filters": {},
            "feature_id": None,
        }
    ]


def test_process_workflow_task_can_advance_after_empty_gather_context(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)
    client = _FakeClient(
        [
            {"action": "gather_context", "types": ["proposed_prs"]},
            {"action": "next_step"},
            {"action": "complete", "output_state": {"found": False}},
        ]
    )

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0


def test_process_workflow_task_repairs_read_document_range_error(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)
    (tmp_path / "specification.yaml").write_text("first\nsecond\n", encoding="utf-8")
    client = _FakeClient(
        [
            {
                "action": "read_document",
                "file_path": "specification.yaml",
                "start_line": 1,
                "end_line": 10,
            },
            {"action": "complete", "output_state": {"read": True}},
        ]
    )

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    assert len(client.messages) == 3


def test_read_task_document_clamps_end_line_to_document_length(
    tmp_path: Path,
) -> None:
    (tmp_path / "specification.yaml").write_text("first\nsecond\n", encoding="utf-8")

    result = _read_task_document(
        WorkflowAction(
            kind="read_document",
            file_path="specification.yaml",
            start_line=1,
            end_line=50,
        ),
        tmp_path,
    )

    assert result["end_line"] == 2
    assert result["document_line_count"] == 2
    assert result["document_complete"] is True
    assert result["next_start_line"] is None
    assert [line["text"] for line in result["lines"]] == ["first", "second"]


def test_read_task_document_clamps_overlapping_range_to_context_limit(
    tmp_path: Path,
) -> None:
    (tmp_path / "long.md").write_text(
        "\n".join(f"line-{index}" for index in range(1, 2501)),
        encoding="utf-8",
    )

    result = _read_task_document(
        WorkflowAction(
            kind="read_document",
            file_path="long.md",
            start_line=1,
            end_line=100_000,
        ),
        tmp_path,
    )

    assert result["start_line"] == 1
    assert result["end_line"] == 2000
    assert result["document_line_count"] == 2500
    assert result["document_complete"] is False
    assert result["next_start_line"] == 2001
    assert len(result["lines"]) == 2000


def test_read_task_document_rejects_non_overlapping_range(tmp_path: Path) -> None:
    (tmp_path / "short.md").write_text("one\ntwo\n", encoding="utf-8")

    with pytest.raises(PowdrrExecutionError, match="no overlap"):
        _read_task_document(
            WorkflowAction(
                kind="read_document",
                file_path="short.md",
                start_line=100,
                end_line=200,
            ),
            tmp_path,
        )


def test_read_task_document_accepts_zero_to_max_lines_range(
    tmp_path: Path,
) -> None:
    (tmp_path / "document.md").write_text(
        "\n".join(f"line-{index}" for index in range(1, 2001)),
        encoding="utf-8",
    )

    result = _read_task_document(
        WorkflowAction(
            kind="read_document",
            file_path="document.md",
            start_line=0,
            end_line=2000,
        ),
        tmp_path,
    )

    assert result["start_line"] == 1
    assert result["end_line"] == 2000
    assert len(result["lines"]) == 2000


def test_missing_read_document_is_recoverable_and_lists_exact_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _workflow(tmp_path)
    source_directory = tmp_path / "src" / "pkg"
    source_directory.mkdir(parents=True)
    (source_directory / "actual.py").write_text("value = 1\n", encoding="utf-8")
    client = _FakeClient(
        [
            {
                "action": "read_document",
                "file_path": "src/pkg/missing.py",
                "start_line": 1,
                "end_line": 10,
            },
            {
                "action": "list_files",
                "directory": "src/pkg",
                "pattern": "*.py",
            },
            {"action": "complete", "output_state": {"read": True}},
        ]
    )
    listed_files: list[list[str]] = []
    original_list_worktree_files = task_agent._list_worktree_files

    def capture_list_files(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original_list_worktree_files(*args, **kwargs)
        listed_files.append(list(result["files"]))
        return result

    monkeypatch.setattr(task_agent, "_list_worktree_files", capture_list_files)

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(workflow_dir=workflow.directory, repo_root=tmp_path),
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    assert len(client.messages) == 4
    assert listed_files == [["src/pkg/actual.py"]]


def test_list_worktree_files_supports_recursive_glob(tmp_path: Path) -> None:
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    (tmp_path / "src" / "top.py").write_text("", encoding="utf-8")
    (nested / "nested.py").write_text("", encoding="utf-8")
    (nested / "notes.txt").write_text("", encoding="utf-8")

    result = _list_worktree_files("src", "*.py", True, tmp_path)

    assert result["files"] == ["src/pkg/nested.py", "src/top.py"]


def test_process_workflow_task_repairs_guessed_workflow_filename_suffix(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)
    guessed_path = workflow.directory / "agent-task."
    client = _FakeClient(
        [
            {
                "action": "invoke_tool",
                "tool": "shell",
                "parameters": {
                    "command": f"cat {guessed_path}",
                },
            },
            {"action": "complete", "output_state": {"version": "v2"}},
        ]
    )
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert "Corrected malformed workflow filename suffix" in stderr.getvalue()
    assert "Rejected workflow shell command" not in stderr.getvalue()
    assert "agent-task.yaml" in stdout.getvalue()
    assert WorkflowInstance.from_directory(workflow.directory).tasks[0].status is (
        TaskStatus.COMPLETED
    )


def test_workflow_file_command_error_is_not_reported_for_exact_filename(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)

    assert (
        _workflow_file_command_error(
            {"command": f"cat {workflow.directory / 'agent-task.yaml'}"},
            workflow.directory,
        )
        is None
    )


def test_workflow_task_client_defaults_to_deepinfra_cheap_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _workflow(tmp_path)
    captured: dict[str, str] = {}
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("DEEPINFRA_API_TOKEN", "test-token")

    class _FakeOpenAIClient:
        def __init__(
            self,
            *,
            model: str,
            api_key: str,
            base_url: str,
            limits: object,
            progress_stream: object = None,
            reasoning_effort: str | None = None,
            timeout: float = 120.0,
        ) -> None:
            _ = timeout
            captured.update(
                {
                    "model": model,
                    "api_key": api_key,
                    "base_url": base_url,
                    "reasoning_effort": str(reasoning_effort),
                }
            )

    monkeypatch.setattr(
        "powdrr_lift.workrr.providers.OpenAIChatClient",
        _FakeOpenAIClient,
    )
    monkeypatch.setattr(
        "powdrr_lift.workrr.task_agent.resolve_provider_credentials",
        lambda provider, api_key, base_url: type(
            "Credentials",
            (),
            {"api_key": "test-token", "base_url": "https://example.test"},
        )(),
    )

    _build_workflow_client(
        WorkflowTaskAgentConfig(workflow_dir=workflow.directory),
        workflow.tasks[0],
    )

    assert captured == {
        "model": "deepseek-ai/DeepSeek-V4-Flash-0731",
        "api_key": "test-token",
        "base_url": "https://example.test",
        "reasoning_effort": "none",
    }


def test_process_workflow_task_blocks_with_human_handoff_and_follow_up(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)
    client = _FakeClient(
        [
            {
                "action": "get-human-input",
                "human_input": {
                    "human_task": {
                        "description": "Choose v1 or v2.",
                        "role": "decider",
                        "input_state": {"options": ["v1", "v2"]},
                        "output_state_type": "api-decision",
                    },
                    "incorporation_instructions": (
                        "Use the human decision in the implementation."
                    ),
                    "follow_up_task": {
                        "description": "Implement the selected API version.",
                        "role": "coder",
                        "input_state": {"source": "human-input-1"},
                        "output_state_type": "implementation-state",
                    },
                },
            }
        ]
    )

    exit_code = run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow.directory,
            repo_root=tmp_path,
        ),
        client=client,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    blocked_workflow = WorkflowInstance.from_directory(workflow.directory)
    assert exit_code == 0
    assert blocked_workflow.tasks[0].status is TaskStatus.LOCKED
    assert [task.task_id for task in blocked_workflow.ready_tasks()] == [
        "human-input-1"
    ]
    follow_up = next(
        task
        for task in blocked_workflow.tasks
        if task.task_id == "human-input-1-follow-up"
    )
    assert follow_up.upstream_task_ids == ("human-input-1",)
    assert follow_up.details == "Use the human decision in the implementation."

    blocked_workflow.complete_task(
        "human-input-1",
        output_state={"decision": "v2"},
    )
    assert [task.task_id for task in blocked_workflow.ready_tasks()] == [
        "human-input-1-follow-up"
    ]
