from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from powdrr_local.file_lock import Timeout
from powdrr_service.planning.checkpoints import (
    PlanningCheckpoint,
    render_checkpoint_archive_comments,
)

import powdrr_lift.workrr.github_issue_queue as github_issue_queue
from powdrr_lift.workrr.github_issue_queue import (
    FULFILLED_WITHOUT_PR_LABEL,
    PLAN_REPAIR_PENDING_LABEL,
    QUEUE_LABELS,
    GitHubIssueClient,
    GitHubIssueQueueError,
    PlanIssueMatch,
    PlannedWorkItem,
    QueueIssue,
    ReviewedExecutionPlan,
    TaskCheckpoint,
    TaskCheckpointCallback,
    _ItemClaim,
    _parse_plan,
    _plan_label,
    _worker_store_root,
    continue_awaiting_ci_issue,
    continue_manual_prerequisite_issue,
    find_plan_issues,
    fulfill_issue_without_pr,
    latest_manual_prerequisite_checkpoint,
    parse_issue_pointer,
    publish_plan_issues,
    render_plan_repair_metadata,
    render_task_checkpoint_comment,
    run_worker_once,
    source_design_items_requiring_replan_at_commit,
)

PLAN_COMMIT = "a" * 40


def test_item_claim_is_exclusive_and_recovery_increments_generation(
    tmp_path: Path,
) -> None:
    claim_path = _worker_store_root(tmp_path) / "claims" / "issue-17.json"
    first = _ItemClaim(tmp_path, 17, "worker-a", ("src/a.py",), 2)
    with first:
        assert first.generation == 1
        with pytest.raises(Timeout):
            with _ItemClaim(tmp_path, 17, "worker-b", ("src/a.py",), 2):
                pytest.fail("a second worker acquired an active item claim")
        claim_record = json.loads(claim_path.read_text(encoding="utf-8"))
        claim_record["generation"] += 1
        claim_path.write_text(json.dumps(claim_record), encoding="utf-8")
        with pytest.raises(GitHubIssueQueueError, match="lost claim generation"):
            first.assert_current()
    recovered = _ItemClaim(tmp_path, 17, "worker-b", ("src/a.py",), 2)
    with recovered:
        assert recovered.generation == 2
        record = json.loads(claim_path.read_text(encoding="utf-8"))
        assert record["owner"] == "worker-b"
        assert record["attempt_id"] == recovered.attempt_id
        assert record["recovery_state"] == "fresh"


def test_item_claims_allow_disjoint_scopes_and_serialize_overlaps(
    tmp_path: Path,
) -> None:
    first = _ItemClaim(tmp_path, 18, "worker-a", ("src/a.py",), 2)
    second = _ItemClaim(tmp_path, 19, "worker-b", ("src/b.py",), 2)
    with first, second:
        assert first.generation == second.generation == 1
        with pytest.raises(Timeout):
            with _ItemClaim(tmp_path, 20, "worker-c", ("src/a.py",), 2):
                pytest.fail("overlapping path scopes were acquired concurrently")


def test_item_claims_enforce_configured_concurrency(tmp_path: Path) -> None:
    first = _ItemClaim(tmp_path, 21, "worker-a", ("src/a.py",), 1)
    with first:
        with pytest.raises(Timeout):
            with _ItemClaim(tmp_path, 22, "worker-b", ("tests/b.py",), 1):
                pytest.fail("claim exceeded the configured concurrency")


def test_item_claim_can_resume_only_after_reconciliation(tmp_path: Path) -> None:
    claim_path = _worker_store_root(tmp_path) / "claims" / "issue-23.json"
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    github_issue_queue._atomic_write_json(
        claim_path,
        {"generation": 3, "state": "reconciliation_required"},
    )
    github_issue_queue._atomic_write_json(
        claim_path.parent / "scope-claims.json",
        {
            "claims": [
                {
                    "issue_number": 23,
                    "attempt_id": "abandoned-attempt",
                    "scopes": ["src/a.py"],
                }
            ]
        },
    )
    with _ItemClaim(tmp_path, 23, "worker-reconciled", ("src/a.py",), 1) as claim:
        assert claim.generation == 4
        record = json.loads(claim_path.read_text(encoding="utf-8"))
        assert record["recovery_state"] == "reconciled"


def test_worker_rejects_multi_host_without_a_shared_atomic_store(
    tmp_path: Path,
) -> None:
    with pytest.raises(GitHubIssueQueueError, match="multi-host.*unsupported"):
        run_worker_once(
            client=object(),  # type: ignore[arg-type]
            repo_root=tmp_path,
            worker_id="worker-a",
            run_task=lambda *_args: 0,
            multi_host=True,
        )


def test_worker_fences_abandoned_claim_until_reconciliation(tmp_path: Path) -> None:
    client = FakeIssueClient()
    publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )
    issue = client.issues[0]
    client.update_issue(
        issue,
        labels=github_issue_queue._replace_queue_status(issue.labels, "claimed"),
    )
    claims_dir = github_issue_queue._worker_store_root(tmp_path) / "claims"
    claims_dir.mkdir(parents=True, exist_ok=True)
    github_issue_queue._atomic_write_json(
        claims_dir / f"issue-{issue.number}.json",
        {
            "owner": "worker-before-crash",
            "attempt_id": "attempt-before-crash",
            "generation": 1,
            "state": "running",
            "recovery_state": "heartbeat",
        },
    )

    report = run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=tmp_path,
        worker_id="worker-recovery",
        run_task=lambda *_args: pytest.fail("abandoned work must not rerun blindly"),
        plan_loader=lambda *_args, **_kwargs: plan(),
    )

    updated = client.get_issue(issue.number)
    claim = json.loads(
        (claims_dir / f"issue-{issue.number}.json").read_text(encoding="utf-8")
    )
    assert report.outcome == "no_eligible_work"
    assert QUEUE_LABELS["blocked"] in updated.labels
    assert claim["state"] == "reconciliation_required"
    assert claim["recovery_state"] == "owner_exited"
    assert any(
        "Reconcile its durable checkpoint" in body
        for number, body in client.comments
        if number == issue.number
    )


def test_worker_executes_independent_items_concurrently(tmp_path: Path) -> None:
    first_item = PlannedWorkItem(
        "first",
        "First",
        "First task.",
        (),
        ("Works.",),
        (),
        "docs/workflows/first",
        "first",
        allowed_paths=("src/first.py",),
    )
    second_item = PlannedWorkItem(
        "second",
        "Second",
        "Second task.",
        (),
        ("Works.",),
        (),
        "docs/workflows/second",
        "second",
        allowed_paths=("src/second.py",),
    )
    independent_plan = ReviewedExecutionPlan(
        "independent-v1", (first_item, second_item)
    )
    for item in independent_plan.items:
        assert item.workflow_directory is not None
        (tmp_path / item.workflow_directory).mkdir(parents=True)
    client = FakeIssueClient()
    publish_plan_issues(
        plan=independent_plan,
        plan_path="docs/plans/independent.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )
    first_started = threading.Event()
    second_started = threading.Event()

    def run_task(
        _workflow_path: Path,
        workflow_id: str,
        *_args: Any,
    ) -> int:
        if workflow_id == "first":
            first_started.set()
            assert second_started.wait(timeout=5), (
                "independent queue item did not start before the first finished"
            )
        else:
            assert first_started.is_set()
            second_started.set()
        return 1

    def run(worker_id: str) -> Any:
        return run_worker_once(
            client=client,  # type: ignore[arg-type]
            repo_root=tmp_path,
            worker_id=worker_id,
            run_task=run_task,
            plan_loader=lambda *_args, **_kwargs: independent_plan,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(run, "worker-a")
        assert first_started.wait(timeout=5)
        second_future = pool.submit(run, "worker-b")
        first_report = first_future.result(timeout=10)
        second_report = second_future.result(timeout=10)

    assert first_report.outcome == second_report.outcome == "blocked", (
        first_report,
        second_report,
    )
    assert {first_report.item_id, second_report.item_id} == {"first", "second"}


def test_worker_discovers_inventory_before_execution_when_committed_inventory_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for item in plan().items:
        assert item.workflow_directory is not None
        (tmp_path / item.workflow_directory).mkdir(parents=True)
    workflow_dir = tmp_path / ".github/workflows"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "ci.yml").write_text("name: CI\njobs: {}\n", encoding="utf-8")
    _write_test_validation_inventory(tmp_path)
    discovered_inventory = github_issue_queue._read_repository_validation_inventory(
        tmp_path
    )
    assert discovered_inventory is not None
    baseline = tmp_path / "docs/structrr/current/baseline-bootstrap.yaml"
    baseline.write_text("validation_inventory: []\n", encoding="utf-8")
    discoveries: list[Path] = []

    def discover(root: str | Path) -> dict[str, Any]:
        discoveries.append(Path(root))
        return discovered_inventory

    monkeypatch.setattr(
        github_issue_queue, "_discover_repository_validation_inventory", discover
    )
    client = FakeIssueClient()
    client.required_checks = [{"name": "test", "state": "pass", "workflow": "CI"}]
    publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )
    executed: list[str] = []

    def run_task(_path: Path, workflow_id: str, *_args: Any) -> int:
        executed.append(workflow_id)
        return 0

    run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=tmp_path,
        worker_id="worker-a",
        run_task=run_task,
        plan_loader=lambda *_args, **_kwargs: plan(),
    )

    assert discoveries == [tmp_path.resolve()]
    assert not any(
        "validation inventory is missing or stale" in body.lower()
        for _, body in client.comments
    )
    assert executed == ["base"]


def test_worker_blocks_before_execution_when_no_required_checks_are_configured(
    tmp_path: Path,
) -> None:
    for item in plan().items:
        assert item.workflow_directory is not None
        (tmp_path / item.workflow_directory).mkdir(parents=True)
    _write_test_validation_inventory(tmp_path)
    client = FakeIssueClient()
    client.default_branch_checks = ()
    publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )
    executed: list[str] = []

    def run_task(_path: Path, workflow_id: str, *_args: Any) -> int:
        executed.append(workflow_id)
        return 0

    report = run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=tmp_path,
        worker_id="worker-a",
        run_task=run_task,
        plan_loader=lambda *_args, **_kwargs: plan(),
    )

    issue = client.get_issue(report.issue_number or -1)
    assert report.outcome == "blocked"
    assert report.detail == "repository default branch has no required status checks"
    assert QUEUE_LABELS["blocked"] in issue.labels
    assert QUEUE_LABELS["ready"] not in issue.labels
    assert executed == []
    assert any(
        "Configure at least one required status check" in body
        for _, body in client.comments
    )


def _write_test_validation_inventory(root: Path) -> None:
    workflow_path = root / ".github/workflows/ci.yml"
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    workflow_path.write_text("name: CI\njobs: {}\n", encoding="utf-8")
    checks = []
    for job in ("test", "unit", "integration", "docs"):
        checks.append(
            {
                "id": f"validation:{job}",
                "schema_version": "verification-provider-inventory-v2",
                "provider": "pytest",
                "profile": job,
                "command": ["pytest", "tests"],
                "source": ".github/workflows/ci.yml",
                "execution": {
                    "kind": "argv",
                    "cwd": ".",
                    "shell": None,
                    "script": None,
                },
                "applicability": {"local": True, "evaluation": "applicable"},
                "settings": {},
                "requiredness": {"status": "unknown", "evidence": []},
                "provenance": {"declaration": "declared", "evidence": []},
                "confirmation": {"level": "static", "observations": []},
                "baseline": {"status": "not_run", "observation": None},
                "ci_origins": [
                    {
                        "workflow": ".github/workflows/ci.yml",
                        "workflow_name": "CI",
                        "job_id": job,
                        "job_name": job,
                    }
                ],
            }
        )
    document = {
        "validation_inventory": checks,
        "validation_context": {
            "input_fingerprints": [
                {
                    "path": ".github/workflows/ci.yml",
                    "digest": github_issue_queue._sha256_file(workflow_path),
                }
            ]
        },
    }
    path = root / "docs/structrr/current/baseline-bootstrap.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(document, sort_keys=True).encode("utf-8")
    path.write_bytes(raw)
    report_path = path.with_suffix(".md")
    report_path.write_text("# Validation baseline\n", encoding="utf-8")
    metadata = {
        "schema_version": "structrr-baseline-freshness-v1",
        "baseline_digest": github_issue_queue._sha256_file(path),
        "report_digest": github_issue_queue._sha256_file(report_path),
        "validation_input_fingerprint": github_issue_queue.content_fingerprint(
            {
                "validation_inventory": checks,
                "validation_context": document["validation_context"],
            }
        ),
    }
    path.with_suffix(".freshness.json").write_text(
        json.dumps(metadata, sort_keys=True), encoding="utf-8"
    )


def _test_validation_checks(root: Path) -> tuple[Mapping[str, Any], ...]:
    inventory = github_issue_queue._read_repository_validation_inventory(root)
    assert inventory is not None
    return inventory["checks"]


@pytest.fixture(autouse=True)
def _validation_inventory_baseline(tmp_path: Path) -> None:
    _write_test_validation_inventory(tmp_path)


def _checkpoint_completed_pr(
    checkpoint: TaskCheckpointCallback,
    item: PlannedWorkItem,
    *,
    pull_request_number: int | None = None,
) -> None:
    number = pull_request_number or (item.github_issue_number or 100)
    checkpoint(
        "implementation-task",
        "execute-proposed-pr-workflow-result-state",
        {"pull_request_url": f"https://github.com/example/project/pull/{number}"},
        "b" * 40,
    )


def _legacy_checkpoint_comment(checkpoint: TaskCheckpoint) -> str:
    payload = {
        "schema": "https://powdrr.io/schemas/github-issue-task-checkpoint-v1",
        "plan_id": checkpoint.plan_id,
        "item_id": checkpoint.item_id,
        "workflow_id": checkpoint.workflow_id,
        "task_id": checkpoint.task_id,
        "status": (
            "awaiting_human"
            if checkpoint.output_state_type == "manual-prerequisite-checkpoint-v1"
            else "completed"
        ),
        "output_state_type": checkpoint.output_state_type,
        "output_state": checkpoint.output_state,
        "commit_sha": checkpoint.commit_sha,
    }
    return (
        "### Powdrr task checkpoint\n\n"
        f"{github_issue_queue._TASK_CHECKPOINT_START}\n"
        "```json\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)}\n"
        "```"
    )


def _write_process_workflow(
    path: Path,
    *,
    plan_id: str,
    item_id: str,
    workflow_id: str,
    dependencies: list[str] | None = None,
) -> None:
    objective = f"Implement {item_id}."
    path.write_text(
        json.dumps(
            {
                "schema": "https://powdrr.io/schemas/plan-workflow-instance-v2",
                "workflow_id": workflow_id,
                "process_id": "execute-proposed-pr-workflow",
                "dependencies": dependencies or [],
                "inputs": {
                    "work_item_name": plan_id,
                    "proposed_pr_id": workflow_id,
                    "feature_description": objective,
                    "proposed_pr_context": {
                        "plan_id": plan_id,
                        "item_id": item_id,
                        "title": f"Implement {item_id}",
                        "objective": objective,
                        "scope": [f"src/{item_id}.py"],
                        "acceptance_criteria": [f"{item_id} works."],
                        "dependencies": dependencies or [],
                        "verification_command": "pytest -q",
                        "structrr_proposal_file": (
                            f"docs/plans/{plan_id}-structrr-proposal.json"
                        ),
                        "structrr_item_id": item_id,
                        "require_structrr_proposal": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )


class FakeIssueClient:
    def __init__(self, issues: tuple[QueueIssue, ...] = ()) -> None:
        self.repository = "example/project"
        self.issues = list(issues)
        self.issue_list_reads: dict[tuple[tuple[str, ...], str], int] = {}
        self.comments: list[tuple[int, str]] = []
        self.comment_reads: dict[int, int] = {}
        self.pull_requests: dict[int, dict[str, object]] = {}
        self.pull_request_reads: dict[int, int] = {}
        self.labels_ensured = False
        self.fulfillment_label_ensured = False
        self.merged_commit: str | None = None
        self.default_branch_checks: tuple[str, ...] = ("test",)
        self.required_checks = [{"name": "test", "state": "pass", "workflow": "ci"}]
        self.pull_request_files: list[dict[str, str]] = []
        self.pr_head = "c" * 40
        self._next_number = 100

    def ensure_queue_labels(self) -> None:
        self.labels_ensured = True

    def ensure_fulfilled_without_pr_label(self) -> None:
        self.fulfillment_label_ensured = True

    def get_issue(self, issue_number: int) -> QueueIssue:
        return next(issue for issue in self.issues if issue.number == issue_number)

    def ensure_plan_label(self, plan_id: str) -> str:
        return _plan_label(plan_id)

    def ensure_plan_commit_merged(self, plan_commit: str) -> None:
        self.merged_commit = plan_commit

    def default_branch_required_checks(self) -> tuple[str, ...]:
        return self.default_branch_checks

    def list_issues(
        self, *, labels: tuple[str, ...] = (), state: str = "all"
    ) -> tuple[QueueIssue, ...]:
        key = (labels, state)
        self.issue_list_reads[key] = self.issue_list_reads.get(key, 0) + 1
        return tuple(
            issue
            for issue in self.issues
            if all(label in issue.labels for label in labels)
            and (state == "all" or issue.state == state)
        )

    def create_issue(
        self, *, body: str, labels: tuple[str, ...], title: str
    ) -> QueueIssue:
        issue = QueueIssue(
            self._next_number,
            title,
            body,
            "open",
            labels,
            f"https://github.com/example/project/issues/{self._next_number}",
        )
        self._next_number += 1
        self.issues.append(issue)
        return issue

    def update_issue(
        self,
        issue: QueueIssue,
        *,
        body: str | None = None,
        labels: tuple[str, ...] | None = None,
        state: str | None = None,
        state_reason: str | None = None,
    ) -> None:
        assert state_reason in (None, "completed")
        index = next(
            index
            for index, value in enumerate(self.issues)
            if value.number == issue.number
        )
        self.issues[index] = QueueIssue(
            issue.number,
            issue.title,
            body if body is not None else issue.body,
            state if state is not None else issue.state,
            labels if labels is not None else issue.labels,
            issue.url,
        )

    def add_comment(self, issue: QueueIssue, body: str) -> None:
        self.comments.append((issue.number, body))

    def list_comments(self, issue_number: int) -> tuple[dict[str, str], ...]:
        self.comment_reads[issue_number] = self.comment_reads.get(issue_number, 0) + 1
        return tuple(
            {
                "body": body,
                "author_association": (
                    "OWNER" if "<!-- powdrr-plan-adjustment:v1 -->" in body else "NONE"
                ),
            }
            for number, body in self.comments
            if number == issue_number
        )

    def pull_request(self, number: int) -> dict[str, object]:
        self.pull_request_reads[number] = self.pull_request_reads.get(number, 0) + 1
        return self.pull_requests.get(number, {})

    def _invoke(
        self, arguments: list[str] | tuple[str, ...], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        del check
        if arguments[:2] == ["pr", "view"]:
            return subprocess.CompletedProcess(
                ["gh", *arguments], 0, json.dumps({"headRefOid": self.pr_head}), ""
            )
        if arguments[:2] == ["pr", "checks"]:
            return subprocess.CompletedProcess(
                ["gh", *arguments], 0, json.dumps(self.required_checks), ""
            )
        if arguments[:1] == ["api"] and any(
            "/pulls/" in argument and "/files?" in argument for argument in arguments
        ):
            return subprocess.CompletedProcess(
                ["gh", *arguments], 0, json.dumps(self.pull_request_files), ""
            )
        return subprocess.CompletedProcess(["gh", *arguments], 1, "", "unexpected")


def _closed_issue_worktree(tmp_path: Path, *, dirty: bool = False) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Test"], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "config",
            "user.email",
            "test@example.com",
        ],
        check=True,
    )
    (repository / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "initial"], check=True
    )
    worktree = repository / ".worktrees" / "powdrr" / "issue-workflow-attempt"
    worktree.parent.mkdir(parents=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "worktree",
            "add",
            "-b",
            "powdrr/issue-workflow-attempt",
            str(worktree),
        ],
        check=True,
    )
    state_directory = worktree / "docs" / "workflows"
    state_directory.mkdir(parents=True)
    (state_directory / "issue-workflow-workflow.yaml").write_text(
        "proposed_pr_id: issue-workflow\n"
        "base_branch: main\n"
        "integration_branch: powdrr/issue-workflow-attempt\n"
        "workflow_relative_directory: docs/workflows\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(worktree), "add", "docs/workflows"], check=True)
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-m", "workflow state"], check=True
    )
    if dirty:
        (worktree / "uncommitted.txt").write_text("keep\n", encoding="utf-8")
    github_issue_queue._write_local_task_checkpoint(
        repo_root=repository,
        issue_number=17,
        checkpoint=TaskCheckpoint("plan", "item", "issue-workflow", "task", "done", {}),
    )
    return repository, worktree


def test_closed_issue_cleanup_removes_matching_clean_worktree(tmp_path: Path) -> None:
    repository, worktree = _closed_issue_worktree(tmp_path)
    client = FakeIssueClient(
        (QueueIssue(17, "Closed", "", "closed", (), "https://example/17"),)
    )

    github_issue_queue._cleanup_closed_issue_worktrees(
        cast(GitHubIssueClient, client), repository
    )

    assert not worktree.exists()


def test_closed_issue_cleanup_keeps_dirty_worktree(tmp_path: Path) -> None:
    repository, worktree = _closed_issue_worktree(tmp_path, dirty=True)
    client = FakeIssueClient(
        (QueueIssue(17, "Closed", "", "closed", (), "https://example/17"),)
    )

    github_issue_queue._cleanup_closed_issue_worktrees(
        cast(GitHubIssueClient, client), repository
    )

    assert worktree.is_dir()
    assert (worktree / "uncommitted.txt").read_text(encoding="utf-8") == "keep\n"


def plan() -> ReviewedExecutionPlan:
    return ReviewedExecutionPlan(
        "feature-v1",
        (
            PlannedWorkItem(
                "base",
                "Base work",
                "Add the shared base.",
                ("src/base.py",),
                ("Base behavior is covered.",),
                (),
                "docs/workflows/base",
                "base",
            ),
            PlannedWorkItem(
                "followup",
                "Follow-up work",
                "Build on the shared base.",
                ("src/followup.py",),
                ("Follow-up behavior is covered.",),
                ("base",),
                "docs/workflows/followup",
                "followup",
            ),
        ),
    )


def test_dependency_evidence_cache_reuses_comment_and_pull_request_reads() -> None:
    client = FakeIssueClient()
    cache = github_issue_queue._DependencyEvidenceCache()

    first_comments = cache.comments(client, 12)  # type: ignore[arg-type]
    second_comments = cache.comments(client, 12)  # type: ignore[arg-type]
    first_pull_request = cache.pull_request(client, 34)  # type: ignore[arg-type]
    second_pull_request = cache.pull_request(client, 34)  # type: ignore[arg-type]

    assert first_comments == second_comments == ()
    assert first_pull_request == second_pull_request == {}
    assert client.comment_reads == {12: 1}
    assert client.pull_request_reads == {34: 1}


def test_dependency_result_is_checked_once_per_plan_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeIssueClient()
    dependency = QueueIssue(
        101,
        "Base work",
        "",
        "closed",
        (),
        "https://github.com/example/project/issues/101",
    )
    evidence_cache = github_issue_queue._DependencyEvidenceCache()
    checks: list[int] = []

    def verified_noop(*_args: Any, **_kwargs: Any) -> bool:
        checks.append(1)
        return True

    monkeypatch.setattr(
        github_issue_queue, "_dependency_has_verified_noop_receipt", verified_noop
    )
    for _ in range(2):
        assert github_issue_queue._dependency_is_fulfilled(
            dependency,
            {dependency.number: dependency},
            client=client,  # type: ignore[arg-type]
            plan=plan(),
            dependency_id="base",
            evidence_cache=evidence_cache,
        )

    assert len(checks) == 1


def test_plan_validation_rejects_unknown_dependency_and_cycle() -> None:
    text = """
schema: https://powdrr.io/schemas/github-issue-execution-plan-v1
plan_id: feature-v1
items:
  - id: first
    title: First
    objective: First objective
    scope: [src]
    acceptance_criteria: [Works]
    dependencies: [second]
    workflow_directory: docs/workflows/first
    workflow_id: first
  - id: second
    title: Second
    objective: Second objective
    scope: [src]
    acceptance_criteria: [Works]
    dependencies: [first]
    workflow_directory: docs/workflows/second
    workflow_id: second
"""
    with pytest.raises(GitHubIssueQueueError, match="acyclic"):
        _parse_plan(text)


def test_plan_validation_preserves_source_document_reference() -> None:
    text = """
schema: https://powdrr.io/schemas/github-issue-execution-plan-v1
plan_id: feature-v1
source_document: docs/plans/proposal.md
items:
  - id: first
    title: First
    objective: Do the first thing.
    scope: [src/first.py]
    allowed_paths: [src/first.py, tests]
    acceptance_criteria: [The first thing works.]
    dependencies: []
    workflow_file: docs/workflows/feature/first.yaml
    workflow_id: feature-first
"""

    plan = _parse_plan(text)
    assert plan.source_document == "docs/plans/proposal.md"
    assert plan.items[0].allowed_paths == ("src/first.py", "tests")


def test_plan_validation_accepts_more_than_twenty_allowed_paths() -> None:
    paths = ", ".join(f"src/module-{index}.py" for index in range(25))
    text = f"""
schema: https://powdrr.io/schemas/github-issue-execution-plan-v1
plan_id: many-paths-v1
items:
  - id: first
    title: First
    objective: Implement the feature across modules.
    scope: [Implement the feature.]
    allowed_paths: [{paths}]
    implementation_steps: [Implement the feature.]
    acceptance_criteria: [The feature works.]
    dependencies: []
    workflow_file: docs/workflows/feature/first.yaml
    workflow_id: feature-first
"""

    plan = _parse_plan(text)

    assert len(plan.items[0].allowed_paths) == 25


def test_plan_validation_preserves_timed_manual_prerequisite() -> None:
    text = """
schema: https://powdrr.io/schemas/github-issue-execution-plan-v1
plan_id: feature-v1
items:
  - id: first
    title: First
    objective: Use the configured credential.
    scope: [src/client.py]
    allowed_paths: [src/client.py]
    implementation_steps: [Use the credential in the client.]
    acceptance_criteria: [The client authenticates successfully.]
    dependencies: []
    workflow_file: docs/workflows/feature/first.yaml
    workflow_id: feature-first
    manual_prerequisite:
      title: Install the CI credential
      instructions: [Add the repository secret.]
      verification: Confirm the secret is configured.
      trigger: after_implementation_before_validation
      unblocks: Running the verifier in pull request CI.
"""

    plan = _parse_plan(text)

    assert plan.items[0].manual_prerequisite == {
        "title": "Install the CI credential",
        "instructions": ["Add the repository secret."],
        "verification": "Confirm the secret is configured.",
        "trigger": "after_implementation_before_validation",
        "unblocks": "Running the verifier in pull request CI.",
    }
    with pytest.raises(GitHubIssueQueueError, match="trigger must be"):
        _parse_plan(
            text.replace(
                "after_implementation_before_validation", "before_implementation"
            )
        )


def test_plan_validation_rejects_traversing_allowed_paths() -> None:
    text = """
schema: https://powdrr.io/schemas/github-issue-execution-plan-v1
plan_id: feature-v1
items:
  - id: first
    title: First
    objective: Do the first thing.
    scope: [Implement the first behavior.]
    allowed_paths: [../outside]
    acceptance_criteria: [The first thing works.]
    dependencies: []
    workflow_file: docs/workflows/feature/first.yaml
    workflow_id: feature-first
"""

    with pytest.raises(GitHubIssueQueueError, match="repository-relative"):
        _parse_plan(text)


def test_plan_validation_enforces_bounds_on_new_implementation_steps() -> None:
    text = """
schema: https://powdrr.io/schemas/github-issue-execution-plan-v1
plan_id: feature-v1
items:
  - id: first
    title: First
    objective: Do the first thing.
    scope: [one, two, three, four, five, six]
    implementation_steps: [Implement the behavior.]
    acceptance_criteria: [The first thing works.]
    dependencies: []
    workflow_file: docs/workflows/feature/first.yaml
    workflow_id: feature-first
"""

    with pytest.raises(GitHubIssueQueueError, match="bounded scope"):
        _parse_plan(text)


def test_plan_validation_rejects_source_document_traversal() -> None:
    text = """
schema: https://powdrr.io/schemas/github-issue-execution-plan-v1
plan_id: feature-v1
source_document: ../proposal.md
items:
  - id: first
    title: First
    objective: Do the first thing.
    scope: [src/first.py]
    acceptance_criteria: [The first thing works.]
    dependencies: []
    workflow_file: docs/workflows/feature/first.yaml
    workflow_id: feature-first
"""

    with pytest.raises(GitHubIssueQueueError, match="source_document"):
        _parse_plan(text)


def test_github_issue_client_paginates_and_ignores_pull_requests() -> None:
    def run(
        arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert "--paginate" in arguments
        assert "--slurp" not in arguments
        assert arguments[arguments.index("-H") + 1] == "Cache-Control: no-cache"
        return subprocess.CompletedProcess(
            arguments,
            0,
            "\n".join(
                (
                    json.dumps(
                        [
                            {
                                "number": 1,
                                "title": "Powdrr work item",
                                "body": "pointer",
                                "state": "open",
                                "labels": [{"name": "powdrr:ready"}],
                                "url": "https://github.com/example/project/issues/1",
                            }
                        ]
                    ),
                    json.dumps(
                        [
                            {
                                "number": 2,
                                "title": "An issue",
                                "body": "pointer",
                                "state": "closed",
                                "labels": [],
                                "url": "https://github.com/example/project/issues/2",
                            },
                            {"number": 3, "pull_request": {"url": "ignored"}},
                        ]
                    ),
                )
            ),
            "",
        )

    issues = GitHubIssueClient("example/project", run=run).list_issues()

    assert [issue.number for issue in issues] == [1, 2]


def test_github_issue_client_filters_issue_state_locally_for_labeled_queries() -> None:
    commands: list[list[str]] = []

    def run(
        arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        return subprocess.CompletedProcess(
            arguments,
            0,
            json.dumps(
                [
                    {
                        "number": 1,
                        "title": "Ready item",
                        "body": "",
                        "state": "open",
                        "labels": [{"name": QUEUE_LABELS["ready"]}],
                        "url": "https://github.com/example/project/issues/1",
                    },
                    {
                        "number": 2,
                        "title": "Closed item",
                        "body": "",
                        "state": "closed",
                        "labels": [{"name": QUEUE_LABELS["ready"]}],
                        "url": "https://github.com/example/project/issues/2",
                    },
                ]
            ),
            "",
        )

    client = GitHubIssueClient("example/project", run=run)
    issues = client.list_issues(labels=(QUEUE_LABELS["ready"],), state="open")
    assert [issue.number for issue in issues] == [1]
    assert commands[0][-1] == (
        "repos/example/project/issues?state=all&per_page=100&labels=powdrr%3Aready"
    )


def test_github_issue_comment_retries_github_rate_limits_with_backoff() -> None:
    commands: list[list[str]] = []
    delays: list[float] = []
    responses = iter(
        (
            subprocess.CompletedProcess(
                [],
                1,
                "",
                "gh: You have exceeded a secondary rate limit (HTTP 403)",
            ),
            subprocess.CompletedProcess(
                [], 1, "", "gh: API rate limited (HTTP 429) Retry-After: 3"
            ),
            subprocess.CompletedProcess([], 0, "{}", ""),
        )
    )

    def run(
        arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        response = next(responses)
        return subprocess.CompletedProcess(
            arguments,
            response.returncode,
            response.stdout,
            response.stderr,
        )

    client = GitHubIssueClient("example/project", run=run, sleep_fn=delays.append)
    issue = QueueIssue(
        42,
        "Planning session",
        "",
        "open",
        (),
        "https://github.com/example/project/issues/42",
    )

    client.add_comment(issue, "checkpoint")

    assert len(commands) == 3
    assert delays == [60.0, 3.0]


def test_github_issue_comment_does_not_retry_unrelated_forbidden_response() -> None:
    delays: list[float] = []

    def run(
        arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 1, "", "HTTP 403 Forbidden")

    client = GitHubIssueClient("example/project", run=run, sleep_fn=delays.append)
    issue = QueueIssue(42, "Planning session", "", "open", (), "")

    with pytest.raises(GitHubIssueQueueError, match="HTTP 403 Forbidden"):
        client.add_comment(issue, "checkpoint")

    assert delays == []


def test_github_issue_comment_retries_transient_server_errors() -> None:
    commands: list[list[str]] = []
    delays: list[float] = []

    def run(
        arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        if len(commands) == 1:
            return subprocess.CompletedProcess(
                arguments, 1, "", "gh: Server Error (HTTP 502)"
            )
        return subprocess.CompletedProcess(arguments, 0, "{}", "")

    client = GitHubIssueClient("example/project", run=run, sleep_fn=delays.append)
    issue = QueueIssue(42, "Planning session", "", "open", (), "")

    client.add_comment(issue, "checkpoint")

    assert len(commands) == 2
    assert commands[0] == commands[1]
    assert delays == [1.0]


def test_github_issue_comment_retries_connection_reset() -> None:
    commands: list[list[str]] = []
    delays: list[float] = []

    def run(
        arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        if len(commands) == 1:
            return subprocess.CompletedProcess(
                arguments,
                1,
                "",
                "Post failed: read tcp: read: connection reset by peer",
            )
        return subprocess.CompletedProcess(arguments, 0, "{}", "")

    client = GitHubIssueClient("example/project", run=run, sleep_fn=delays.append)
    issue = QueueIssue(42, "Planning session", "", "open", (), "")

    client.add_comment(issue, "checkpoint")

    assert len(commands) == 2
    assert commands[0] == commands[1]
    assert delays == [1.0]


def test_github_issue_comment_listing_retries_transient_timeout() -> None:
    commands: list[list[str]] = []
    delays: list[float] = []

    def run(
        arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        if len(commands) == 1:
            return subprocess.CompletedProcess(
                arguments,
                1,
                "",
                "Get https://api.github.com: read tcp: operation timed out",
            )
        return subprocess.CompletedProcess(
            arguments, 0, json.dumps([[{"id": 1, "body": "checkpoint"}]]), ""
        )

    client = GitHubIssueClient("example/project", run=run, sleep_fn=delays.append)

    comments = client.list_comments(42)

    assert comments == ({"id": 1, "body": "checkpoint"},)
    assert len(commands) == 2
    assert commands[0] == commands[1]
    assert delays == [1.0]


def test_checkpoint_comment_listing_stops_at_a_complete_archive() -> None:
    root = PlanningCheckpoint.create(
        session_id="planning-session-1",
        event_id="event-root",
        sequence=1,
        parent_digest=None,
        event_type="question_started",
        payload={"question": {"question_id": "q-1"}},
        id_counters={},
    )
    archive_comments = render_checkpoint_archive_comments(
        (root,), session_id="planning-session-1"
    )
    commands: list[list[str]] = []

    def run(
        arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        if arguments[-1] == "repos/example/project/issues/42":
            return subprocess.CompletedProcess(
                arguments, 0, json.dumps({"comments": 150}), ""
            )
        page = int(arguments[-1].rsplit("page=", 1)[1])
        if page == 1:
            return subprocess.CompletedProcess(
                arguments,
                0,
                json.dumps(
                    [{"id": index, "body": "old checkpoint"} for index in range(1, 101)]
                ),
                "",
            )
        return subprocess.CompletedProcess(
            arguments,
            0,
            json.dumps(
                [
                    {"id": index, "body": body}
                    for index, body in enumerate(archive_comments, start=1)
                ]
            ),
            "",
        )

    comments = GitHubIssueClient("example/project", run=run).list_checkpoint_comments(
        42, session_id="planning-session-1"
    )

    assert len(comments) == len(archive_comments)
    assert len(commands) == 2
    assert commands[0][-1] == "repos/example/project/issues/42"
    assert "page=2" in commands[1][-1]


def test_checkpoint_comment_listing_reads_legacy_pages_without_an_archive() -> None:
    commands: list[list[str]] = []
    pages = [
        [{"id": index, "body": "legacy checkpoint"} for index in range(100)],
        [{"id": 100, "body": "legacy checkpoint"}],
    ]

    def run(
        arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        if arguments[-1] == "repos/example/project/issues/42":
            return subprocess.CompletedProcess(
                arguments, 0, json.dumps({"comments": 101}), ""
            )
        page_number = int(arguments[-1].rsplit("page=", 1)[1])
        return subprocess.CompletedProcess(
            arguments, 0, json.dumps(pages[page_number - 1]), ""
        )

    comments = GitHubIssueClient("example/project", run=run).list_checkpoint_comments(
        42, session_id="planning-session-1"
    )

    assert len(comments) == 101
    assert [comment["id"] for comment in comments] == list(range(101))
    assert len(commands) == 3
    assert "page=2" in commands[1][-1]
    assert "page=1" in commands[2][-1]


def test_github_issue_client_closes_fulfilled_issue_with_completed_reason() -> None:
    commands: list[list[str]] = []

    def run(
        arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        if arguments[1] == "api" and arguments[2].startswith("repos/"):
            return subprocess.CompletedProcess(
                arguments,
                0,
                json.dumps(
                    {
                        "number": 63,
                        "title": "Powdrr work item",
                        "body": "pointer",
                        "state": "open",
                        "labels": [],
                        "html_url": "https://github.com/example/project/issues/63",
                    }
                )
                if "GET" in arguments
                else "{}",
                "",
            )
        return subprocess.CompletedProcess(arguments, 0, "{}", "")

    client = GitHubIssueClient("example/project", run=run)
    issue = client.get_issue(63)
    client.update_issue(
        issue,
        labels=(FULFILLED_WITHOUT_PR_LABEL,),
        state="closed",
        state_reason="completed",
    )

    patch_arguments = commands[1]
    assert patch_arguments[3] == "--method"
    assert "PATCH" in patch_arguments
    assert f"labels[]={FULFILLED_WITHOUT_PR_LABEL}" in patch_arguments
    assert "state=closed" in patch_arguments
    assert "state_reason=completed" in patch_arguments


def test_github_issue_client_reads_required_checks_from_active_rulesets() -> None:
    commands: list[list[str]] = []

    def run(
        arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        endpoint = arguments[2]
        if endpoint == "repos/example/project":
            return subprocess.CompletedProcess(
                arguments, 0, json.dumps({"default_branch": "main"}), ""
            )
        if endpoint == "repos/example/project/rules/branches/main":
            return subprocess.CompletedProcess(
                arguments,
                0,
                json.dumps(
                    [
                        {
                            "type": "required_status_checks",
                            "parameters": {
                                "required_status_checks": [
                                    {"context": "verify", "integration_id": None}
                                ]
                            },
                        }
                    ]
                ),
                "",
            )
        raise AssertionError(f"unexpected GitHub API request: {arguments!r}")

    checks = GitHubIssueClient(
        "example/project", run=run
    ).default_branch_required_checks()

    assert checks == ("verify",)
    assert len(commands) == 2


def test_github_issue_client_falls_back_to_classic_branch_protection() -> None:
    commands: list[list[str]] = []

    def run(
        arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        endpoint = arguments[2]
        if endpoint == "repos/example/project":
            return subprocess.CompletedProcess(
                arguments, 0, json.dumps({"default_branch": "main"}), ""
            )
        if endpoint == "repos/example/project/rules/branches/main":
            return subprocess.CompletedProcess(arguments, 0, "[]", "")
        if endpoint == (
            "repos/example/project/branches/main/protection/required_status_checks"
        ):
            return subprocess.CompletedProcess(
                arguments, 0, json.dumps({"contexts": ["unit"], "checks": []}), ""
            )
        raise AssertionError(f"unexpected GitHub API request: {arguments!r}")

    checks = GitHubIssueClient(
        "example/project", run=run
    ).default_branch_required_checks()

    assert checks == ("unit",)
    assert len(commands) == 3


def test_github_issue_client_falls_back_when_rulesets_are_not_available() -> None:
    commands: list[list[str]] = []

    def run(
        arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        endpoint = arguments[2]
        if endpoint == "repos/example/project":
            return subprocess.CompletedProcess(
                arguments, 0, json.dumps({"default_branch": "main"}), ""
            )
        if endpoint == "repos/example/project/rules/branches/main":
            return subprocess.CompletedProcess(
                arguments, 1, "", "HTTP 403: Upgrade to GitHub Pro"
            )
        if endpoint == (
            "repos/example/project/branches/main/protection/required_status_checks"
        ):
            return subprocess.CompletedProcess(
                arguments, 0, json.dumps({"contexts": ["unit"], "checks": []}), ""
            )
        raise AssertionError(f"unexpected GitHub API request: {arguments!r}")

    checks = GitHubIssueClient(
        "example/project", run=run
    ).default_branch_required_checks()

    assert checks == ("unit",)
    assert len(commands) == 3


def test_github_client_skips_labels_that_already_exist() -> None:
    commands: list[list[str]] = []

    def run(
        arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "{}", "")

    client = GitHubIssueClient("example/project", run=run)
    client.ensure_queue_labels()
    assert client.ensure_plan_label("feature-v1") == _plan_label("feature-v1")

    assert len(commands) == len(QUEUE_LABELS) + 2
    assert all("--method" not in command for command in commands)


def test_github_client_creates_missing_labels() -> None:
    commands: list[list[str]] = []

    def run(
        arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        if "--method" not in arguments:
            return subprocess.CompletedProcess(arguments, 1, "", "HTTP 404")
        return subprocess.CompletedProcess(arguments, 0, "{}", "")

    GitHubIssueClient("example/project", run=run).ensure_queue_labels()

    assert len(commands) == (len(QUEUE_LABELS) + 1) * 2
    assert all("--method" in command for command in commands[1::2])


def test_github_client_requires_plan_commit_on_default_branch() -> None:
    commands: list[list[str]] = []

    def run(
        arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        output = (
            '{"default_branch":"release/stable"}'
            if "repos/example/project" == arguments[2]
            else '{"behind_by":0}'
        )
        return subprocess.CompletedProcess(arguments, 0, output, "")

    client = GitHubIssueClient("example/project", run=run)
    client.ensure_plan_commit_merged(PLAN_COMMIT)

    assert "release%2Fstable" in commands[1][2]


def test_publisher_records_verified_merged_commit() -> None:
    client = FakeIssueClient()

    publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )

    assert client.merged_commit == PLAN_COMMIT


def test_plan_lookup_returns_item_to_issue_status_and_missing_items() -> None:
    existing_issue = QueueIssue(
        42,
        "Powdrr work item: feature-v1/base",
        "Reviewed plan pointer",
        "open",
        (QUEUE_LABELS["claimed"], _plan_label("feature-v1")),
        "https://github.com/example/project/issues/42",
    )
    client = FakeIssueClient((existing_issue,))
    client.issues[0] = QueueIssue(
        existing_issue.number,
        existing_issue.title,
        "Reviewed plan: link\n\n"
        "<!-- powdrr-queue-item:v1 "
        '{"item_id":"base","plan_commit":"'
        + PLAN_COMMIT
        + '","plan_id":"feature-v1","plan_path":"docs/plans/feature.yaml"} -->\n',
        existing_issue.state,
        existing_issue.labels,
        existing_issue.url,
    )

    matches = find_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )

    assert matches == (
        PlanIssueMatch(
            "base",
            42,
            "https://github.com/example/project/issues/42",
            "open",
            (QUEUE_LABELS["claimed"], _plan_label("feature-v1")),
        ),
        PlanIssueMatch("followup", None, None, None, ()),
    )
    assert not client.labels_ensured


def test_publisher_creates_minimal_pointer_issues_and_is_idempotent() -> None:
    client = FakeIssueClient()

    first = publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )
    second = publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )

    assert first.created_issue_numbers == (100, 101)
    assert second.created_issue_numbers == ()
    assert second.existing_issue_numbers == (100, 101)
    assert client.labels_ensured
    assert QUEUE_LABELS["ready"] in client.issues[0].labels
    assert QUEUE_LABELS["blocked_on_dependency"] in client.issues[1].labels
    assert QUEUE_LABELS["ready"] not in client.issues[1].labels
    pointer = parse_issue_pointer(client.issues[0].body)
    assert pointer == {
        "plan_id": "feature-v1",
        "item_id": "base",
        "plan_path": "docs/plans/feature.yaml",
        "plan_commit": PLAN_COMMIT,
    }
    assert "Add the shared base" not in client.issues[0].body
    assert "https://github.com/example/project/blob/" in client.issues[0].body


def test_publisher_blocks_items_with_replan_required_source_designs(
    tmp_path: Path,
) -> None:
    reviewed = plan()
    source_path = "docs/plans/feature-v1-source-design/base.yaml"
    reviewed = replace(
        reviewed,
        items=(replace(reviewed.items[0], source_reference_design_file=source_path),)
        + reviewed.items[1:],
    )
    source_design = {
        "schema_version": "structrr-source-reference-task-v1",
        "plan_id": reviewed.plan_id,
        "item_id": "base",
        "design": {"compilation_status": "requires_replan"},
        "status": "requires_replan",
        "amendment_requirement": "An accepted behavior scenario is missing.",
    }

    def run(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert arguments[-1] == f"{PLAN_COMMIT}:{source_path}"
        return subprocess.CompletedProcess(
            arguments, 0, yaml.safe_dump(source_design), ""
        )

    blocked = source_design_items_requiring_replan_at_commit(
        reviewed, PLAN_COMMIT, repo_root=tmp_path, run=run
    )
    client = FakeIssueClient()
    publish_plan_issues(
        plan=reviewed,
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
        blocked_item_ids=blocked,
    )

    assert blocked == frozenset({"base"})
    assert QUEUE_LABELS["blocked"] in client.issues[0].labels
    assert QUEUE_LABELS["ready"] not in client.issues[0].labels
    assert QUEUE_LABELS["blocked_on_dependency"] in client.issues[1].labels
    assert QUEUE_LABELS["ready"] not in client.issues[1].labels


def test_worker_skips_unmet_dependency_then_executes_ready_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path
    (root / "docs/workflows/base").mkdir(parents=True)
    (root / "docs/workflows/followup").mkdir(parents=True)
    client = FakeIssueClient()
    publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )
    executed: list[str] = []
    waits: list[float] = []
    monkeypatch.setattr(github_issue_queue, "sleep", waits.append)

    def load_plan(
        _path: str, _commit: str, *, repo_root: str | Path
    ) -> ReviewedExecutionPlan:
        assert Path(repo_root) == root
        return plan()

    def run_task(
        _directory: Path,
        workflow_id: str,
        _worker_id: str,
        item: PlannedWorkItem,
        _plan: ReviewedExecutionPlan,
        _fulfilled_without_pr_dependencies: tuple[str, ...],
        _checkpoint: TaskCheckpointCallback,
    ) -> int:
        executed.append(workflow_id)
        _checkpoint_completed_pr(_checkpoint, item)
        return 0

    first = run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=root,
        worker_id="worker-a",
        run_task=run_task,
        plan_loader=load_plan,
    )
    assert first.outcome == "awaiting_review"
    assert first.item_id == "base"
    assert executed == ["base"]
    assert waits == []
    assert QUEUE_LABELS["awaiting_review"] in client.issues[0].labels
    assert QUEUE_LABELS["blocked_on_dependency"] in client.issues[1].labels
    assert QUEUE_LABELS["ready"] not in client.issues[1].labels

    waiting = run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=root,
        worker_id="worker-a",
        run_task=run_task,
        plan_loader=load_plan,
    )
    assert waiting.outcome == "no_eligible_work"
    assert waiting.readiness is not None
    assert waiting.readiness["counts"]["waiting_dependencies"] == 1
    assert waiting.readiness["blocking_reasons"][0]["reason"] == (
        "dependency_not_fulfilled"
    )
    assert waiting.detail is not None
    assert "verified fulfillment" in waiting.detail
    assert executed == ["base"]
    assert waits == []

    client.issues[0] = QueueIssue(
        client.issues[0].number,
        client.issues[0].title,
        client.issues[0].body,
        "closed",
        client.issues[0].labels,
        client.issues[0].url,
    )
    manual_close_wait = run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=root,
        worker_id="worker-a",
        run_task=run_task,
        plan_loader=load_plan,
    )
    assert manual_close_wait.outcome == "no_eligible_work"

    client.pull_requests[100] = {
        "state": "open",
        "merged": False,
        "draft": True,
        "body": "Closes #100",
        "merge_commit_sha": None,
    }
    draft_wait = run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=root,
        worker_id="worker-a",
        run_task=run_task,
        plan_loader=load_plan,
    )
    assert draft_wait.outcome == "no_eligible_work"
    assert draft_wait.readiness is not None
    assert draft_wait.readiness["counts"]["waiting_dependencies"] == 1

    client.pull_requests[100] = {
        "state": "closed",
        "merged": True,
        "body": "Closes #999",
        "merge_commit_sha": "d" * 40,
    }
    unrelated_wait = run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=root,
        worker_id="worker-a",
        run_task=run_task,
        plan_loader=load_plan,
    )
    assert unrelated_wait.outcome == "no_eligible_work"

    client.pull_requests[100] = {
        "state": "closed",
        "merged": True,
        "body": "Closes #100",
        "merge_commit_sha": "d" * 40,
    }
    second = run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=root,
        worker_id="worker-a",
        run_task=run_task,
        plan_loader=load_plan,
    )

    assert second.outcome == "awaiting_review"
    assert second.item_id == "followup"
    assert executed == ["base", "followup"]
    assert QUEUE_LABELS["awaiting_review"] in client.issues[1].labels
    assert waits == [1.0]


def test_worker_skips_issue_relabelled_before_fresh_claim_check(
    tmp_path: Path,
) -> None:
    class RelabelOnReadyListClient(FakeIssueClient):
        relabeled = False

        def list_issues(
            self, *, labels: tuple[str, ...] = (), state: str = "all"
        ) -> tuple[QueueIssue, ...]:
            result = super().list_issues(labels=labels, state=state)
            if (
                labels == (QUEUE_LABELS["ready"],)
                and state == "open"
                and not self.relabeled
            ):
                self.relabeled = True
                candidate = next(issue for issue in result if issue.number == 100)
                self.update_issue(
                    candidate,
                    labels=github_issue_queue._replace_queue_status(
                        candidate.labels, "claimed"
                    ),
                )
            return result

    root = tmp_path
    (root / "docs/workflows/base").mkdir(parents=True)
    (root / "docs/workflows/followup").mkdir(parents=True)
    client = RelabelOnReadyListClient()
    publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )
    client.issue_list_reads.clear()
    executed: list[str] = []

    def load_plan(
        _path: str, _commit: str, *, repo_root: str | Path
    ) -> ReviewedExecutionPlan:
        assert Path(repo_root) == root
        return plan()

    def run_task(
        _directory: Path,
        workflow_id: str,
        _worker_id: str,
        _item: PlannedWorkItem,
        _plan: ReviewedExecutionPlan,
        _fulfilled_without_pr_dependencies: tuple[str, ...],
        _checkpoint: TaskCheckpointCallback,
    ) -> int:
        executed.append(workflow_id)
        return 0

    report = run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=root,
        worker_id="worker-a",
        run_task=run_task,
        plan_loader=load_plan,
    )

    assert report.outcome == "no_eligible_work"
    assert QUEUE_LABELS["claimed"] in client.issues[0].labels
    assert executed == []
    # Promotion and no-work readiness each need an index; candidate analysis
    # reuses the promotion index instead of fetching the plan a third time.
    assert client.issue_list_reads[((_plan_label("feature-v1"),), "all")] == 2


def test_worker_passes_explicit_no_pr_dependency_to_workflow(
    tmp_path: Path,
) -> None:
    (tmp_path / "docs/workflows/base").mkdir(parents=True)
    (tmp_path / "docs/workflows/followup").mkdir(parents=True)
    client = FakeIssueClient()
    publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )
    fulfill_issue_without_pr(
        issue_number=client.issues[0].number,
        reason="The requested capability is already present.",
        client=client,  # type: ignore[arg-type]
    )
    received: list[tuple[str, ...]] = []

    def load_plan(
        _path: str, _commit: str, *, repo_root: str | Path
    ) -> ReviewedExecutionPlan:
        assert Path(repo_root) == tmp_path
        return plan()

    def run_task(
        _directory: Path,
        _workflow_id: str,
        _worker_id: str,
        _item: PlannedWorkItem,
        _plan: ReviewedExecutionPlan,
        fulfilled_without_pr_dependencies: tuple[str, ...],
        _checkpoint: TaskCheckpointCallback,
    ) -> int:
        received.append(fulfilled_without_pr_dependencies)
        return 1

    report = run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=tmp_path,
        worker_id="worker-after-no-pr-completion",
        run_task=run_task,
        plan_loader=load_plan,
    )

    assert report.outcome == "blocked"
    assert report.item_id == "followup"
    assert received == [("base",)]


def test_worker_applies_inline_issue_adjustment_to_effective_item(
    tmp_path: Path,
) -> None:
    workflow_file = tmp_path / "docs/workflows/base.yaml"
    workflow_file.parent.mkdir(parents=True)
    _write_process_workflow(
        workflow_file,
        plan_id="feature-v1",
        item_id="base",
        workflow_id="base",
    )
    plan_with_paths = ReviewedExecutionPlan(
        "feature-v1",
        (
            PlannedWorkItem(
                "base",
                "Base work",
                "Add the shared base.",
                ("src/base.py",),
                ("Base behavior is covered.",),
                (),
                None,
                "base",
                workflow_file="docs/workflows/base.yaml",
                allowed_paths=("src", "tests"),
            ),
        ),
    )
    client = FakeIssueClient()
    publish_plan_issues(
        plan=plan_with_paths,
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )
    adjusted_paths = [f"src/module-{index}.py" for index in range(25)]
    adjustment = {
        "schema": "https://powdrr.io/schemas/github-issue-plan-adjustment-v1",
        "plan_id": "feature-v1",
        "item_id": "base",
        "reason": "The implementation steps update many related modules.",
        "changes": {"allowed_paths": adjusted_paths},
    }
    client.comments.append(
        (
            client.issues[0].number,
            "<!-- powdrr-plan-adjustment:v1 -->\n```json\n"
            + json.dumps(adjustment)
            + "\n```",
        )
    )
    received: list[PlannedWorkItem] = []

    def load_plan(
        _path: str, _commit: str, *, repo_root: str | Path
    ) -> ReviewedExecutionPlan:
        assert Path(repo_root) == tmp_path
        return plan_with_paths

    def run_task(
        _directory: Path,
        _workflow_id: str,
        _worker_id: str,
        item: PlannedWorkItem,
        _plan: ReviewedExecutionPlan,
        _fulfilled_without_pr_dependencies: tuple[str, ...],
        checkpoint: TaskCheckpointCallback,
    ) -> int:
        received.append(item)
        _checkpoint_completed_pr(checkpoint, item)
        return 0

    report = run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=tmp_path,
        worker_id="worker-inline-adjustment",
        run_task=run_task,
        plan_loader=load_plan,
    )

    assert report.outcome == "awaiting_review"
    assert received[0].allowed_paths == tuple(adjusted_paths)


def test_fulfill_issue_without_pr_records_explicit_reason_and_closes_issue() -> None:
    client = FakeIssueClient()
    publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )

    result = fulfill_issue_without_pr(
        issue_number=100,
        reason="The verifier and fixtures are already present on main.",
        client=client,  # type: ignore[arg-type]
    )

    assert client.fulfillment_label_ensured
    assert result.state == "closed"
    assert FULFILLED_WITHOUT_PR_LABEL in result.labels
    assert not (set(QUEUE_LABELS.values()) & set(result.labels))
    assert len(client.comments) == 1
    issue_number, comment = client.comments[0]
    assert issue_number == 100
    assert "powdrr-issue-fulfillment:v1" in comment
    assert '"fulfillment": "without_implementation_pr"' in comment
    assert "already present on main" in comment


def test_stale_no_pr_receipt_does_not_unblock_dependency(tmp_path: Path) -> None:
    client = FakeIssueClient()
    publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )
    fulfill_issue_without_pr(
        issue_number=client.issues[0].number,
        reason="The requested capability is already present.",
        client=client,  # type: ignore[arg-type]
    )
    stale = client.issues[0]
    client.update_issue(
        stale,
        body=github_issue_queue._issue_pointer_body(
            "feature-v1",
            "base",
            "docs/plans/feature.yaml",
            "e" * 40,
            repository=client.repository,
        ),
    )

    report = run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=tmp_path,
        worker_id="worker-stale-receipt",
        run_task=lambda *_args: pytest.fail("stale fulfillment must not unblock"),
        plan_loader=lambda *_args, **_kwargs: plan(),
    )

    assert report.outcome == "no_eligible_work"
    assert report.readiness is not None
    assert report.readiness["blocking_reasons"][0]["reason"] == (
        "dependency_not_fulfilled"
    )


def test_publisher_reconciles_old_ready_issues_with_plan_dependencies() -> None:
    client = FakeIssueClient()
    publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )
    client.issues[1] = QueueIssue(
        client.issues[1].number,
        client.issues[1].title,
        client.issues[1].body,
        client.issues[1].state,
        (QUEUE_LABELS["ready"], _plan_label("feature-v1")),
        client.issues[1].url,
    )

    publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )

    assert QUEUE_LABELS["blocked_on_dependency"] in client.issues[1].labels
    assert QUEUE_LABELS["ready"] not in client.issues[1].labels


def test_worker_accepts_one_file_workflow_definition(tmp_path: Path) -> None:
    workflow_file = tmp_path / "docs/workflows/feature-v1/base.yaml"
    workflow_file.parent.mkdir(parents=True)
    _write_process_workflow(
        workflow_file,
        plan_id="feature-v1",
        item_id="base",
        workflow_id="base",
    )
    reviewed_plan = ReviewedExecutionPlan(
        "feature-v1",
        (
            PlannedWorkItem(
                "base",
                "Base work",
                "Add the shared base.",
                ("src/base.py",),
                ("Base behavior is covered.",),
                (),
                None,
                "base",
                "docs/workflows/feature-v1/base.yaml",
            ),
        ),
    )
    client = FakeIssueClient()
    publish_plan_issues(
        plan=reviewed_plan,
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )
    observed: list[Path] = []

    def load_plan(
        _path: str, _commit: str, *, repo_root: str | Path
    ) -> ReviewedExecutionPlan:
        assert Path(repo_root) == tmp_path
        return reviewed_plan

    def run_task(
        path: Path,
        _workflow_id: str,
        _worker_id: str,
        item: PlannedWorkItem,
        _plan: ReviewedExecutionPlan,
        _fulfilled_without_pr_dependencies: tuple[str, ...],
        checkpoint: TaskCheckpointCallback,
    ) -> int:
        observed.append(path)
        _checkpoint_completed_pr(checkpoint, item)
        return 0

    report = run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=tmp_path,
        worker_id="worker-a",
        run_task=run_task,
        plan_loader=load_plan,
    )

    assert report.outcome == "awaiting_review"
    assert observed == [workflow_file]


def test_worker_blocks_when_workflow_dependencies_disagree_with_plan(
    tmp_path: Path,
) -> None:
    workflow_root = tmp_path / "docs/workflows/feature-v1"
    workflow_root.mkdir(parents=True)
    base_workflow = workflow_root / "base.yaml"
    followup_workflow = workflow_root / "followup.yaml"
    _write_process_workflow(
        base_workflow,
        plan_id="feature-v1",
        item_id="base",
        workflow_id="feature-v1-base",
    )
    _write_process_workflow(
        followup_workflow,
        plan_id="feature-v1",
        item_id="followup",
        workflow_id="feature-v1-followup",
    )
    reviewed_plan = ReviewedExecutionPlan(
        "feature-v1",
        (
            PlannedWorkItem(
                "base",
                "Base work",
                "Add the shared base.",
                ("src/base.py",),
                ("Base behavior is covered.",),
                (),
                None,
                "feature-v1-base",
                "docs/workflows/feature-v1/base.yaml",
            ),
            PlannedWorkItem(
                "followup",
                "Follow-up work",
                "Build on the shared base.",
                ("src/followup.py",),
                ("Follow-up behavior is covered.",),
                ("base",),
                None,
                "feature-v1-followup",
                "docs/workflows/feature-v1/followup.yaml",
            ),
        ),
    )
    client = FakeIssueClient()
    publish_plan_issues(
        plan=reviewed_plan,
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )
    fulfill_issue_without_pr(
        issue_number=client.issues[0].number,
        reason="The requested capability is already present.",
        client=client,  # type: ignore[arg-type]
    )

    report = run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=tmp_path,
        worker_id="worker-a",
        run_task=lambda *_args: pytest.fail("mismatched workflow must not run"),
        plan_loader=lambda *_args, **_kwargs: reviewed_plan,
    )

    assert report.outcome == "blocked"
    assert report.item_id == "followup"
    assert "workflow dependencies do not match" in (report.detail or "")
    assert QUEUE_LABELS["blocked"] in client.issues[1].labels


def test_worker_marks_failed_execution_blocked(tmp_path: Path) -> None:
    (tmp_path / "docs/workflows/base").mkdir(parents=True)
    client = FakeIssueClient()
    publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )

    def load_plan(
        _path: str, _commit: str, *, repo_root: str | Path
    ) -> ReviewedExecutionPlan:
        assert Path(repo_root) == tmp_path
        return plan()

    def fail_task(
        _directory: Path,
        _workflow_id: str,
        _worker_id: str,
        _item: PlannedWorkItem,
        _plan: ReviewedExecutionPlan,
        _fulfilled_without_pr_dependencies: tuple[str, ...],
        _checkpoint: TaskCheckpointCallback,
    ) -> int:
        return 1

    report = run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=tmp_path,
        worker_id="worker-a",
        run_task=fail_task,
        plan_loader=load_plan,
    )
    assert report.outcome == "blocked"
    assert QUEUE_LABELS["blocked"] in client.issues[0].labels
    result_path = tmp_path / ".powdrr/github-issue-worker/results/issue-100.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["schema"] == "https://powdrr.io/schemas/task-execution-result-v1"
    assert result["stage"] == "implementation"
    assert result["runner_exit_code"] == 1
    assert result["plan_id"] == "feature-v1"
    assert report.failure_record == str(result_path)
    assert "Durable diagnostic:" in client.comments[-1][1]


def test_worker_updates_issue_after_task_retries_are_exhausted(tmp_path: Path) -> None:
    (tmp_path / "docs/workflows/base").mkdir(parents=True)
    client = FakeIssueClient()
    publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )
    attempts: list[int] = []
    statuses_during_attempts: list[str] = []

    def run_task(*_args: Any) -> int:
        for attempt in range(1, 4):
            attempts.append(attempt)
            statuses_during_attempts.append(
                next(
                    label
                    for label in client.issues[0].labels
                    if label in QUEUE_LABELS.values()
                )
            )
            if attempt < 3:
                continue
            raise RuntimeError("final retry failed")
        return 0

    report = run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=tmp_path,
        worker_id="worker-retrying",
        run_task=run_task,
        plan_loader=lambda *_args, **_kwargs: plan(),
    )

    assert attempts == [1, 2, 3]
    assert statuses_during_attempts == [QUEUE_LABELS["claimed"]] * 3
    assert report.outcome == "blocked"
    assert report.detail == "RuntimeError: final retry failed"
    assert QUEUE_LABELS["blocked"] in client.issues[0].labels
    final_status_comments = [
        body
        for issue_number, body in client.comments
        if issue_number == 100 and "execution failed during implementation" in body
    ]
    assert len(final_status_comments) == 1
    assert "RuntimeError: final retry failed" in final_status_comments[0]
    result_path = tmp_path / ".powdrr/github-issue-worker/results/issue-100.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["detail"] == "RuntimeError: final retry failed"
    assert result["runner_exit_code"] == 1
    assert not any(
        "powdrr-task-checkpoint" in body
        for issue_number, body in client.comments
        if issue_number == 100
    )


def test_worker_persists_task_checkpoint_locally_and_on_issue(
    tmp_path: Path,
) -> None:
    (tmp_path / "docs/workflows/base").mkdir(parents=True)
    client = FakeIssueClient()
    publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )

    def load_plan(
        _path: str, _commit: str, *, repo_root: str | Path
    ) -> ReviewedExecutionPlan:
        assert Path(repo_root) == tmp_path
        return plan()

    def run_task(
        _directory: Path,
        _workflow_id: str,
        _worker_id: str,
        _item: PlannedWorkItem,
        _plan: ReviewedExecutionPlan,
        _fulfilled_without_pr_dependencies: tuple[str, ...],
        checkpoint: TaskCheckpointCallback,
    ) -> int:
        checkpoint(
            "base-task-02",
            "implementation-state",
            {
                "files": ["a.py"],
                "pull_request_url": "https://github.com/example/project/pull/100",
            },
            "b" * 40,
        )
        return 0

    report = run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=tmp_path,
        worker_id="worker-a",
        run_task=run_task,
        plan_loader=load_plan,
    )

    assert report.outcome == "awaiting_review"
    record = github_issue_queue._read_local_task_checkpoint(tmp_path, 100)
    assert record is not None
    assert record["task_id"] == "base-task-02"
    assert record["output_state"] == {
        "files": ["a.py"],
        "pull_request_url": "https://github.com/example/project/pull/100",
    }
    assert record["commit_sha"] == "b" * 40
    assert (
        github_issue_queue._issue_checkpoint_path(tmp_path, 100).stat().st_mode & 0o777
        == 0o600
    )
    issue_comments = [
        body for issue_number, body in client.comments if issue_number == 100
    ]
    assert any("powdrr-task-checkpoint" in body for body in issue_comments)
    assert any("pull/100" in body for body in issue_comments)


def test_worker_marks_manual_prerequisite_as_awaiting_human(tmp_path: Path) -> None:
    (tmp_path / "docs/workflows/base").mkdir(parents=True)
    client = FakeIssueClient()
    publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )

    def run_task(
        _directory: Path,
        _workflow_id: str,
        _worker_id: str,
        _item: PlannedWorkItem,
        _plan: ReviewedExecutionPlan,
        _fulfilled_without_pr_dependencies: tuple[str, ...],
        checkpoint: TaskCheckpointCallback,
    ) -> int:
        checkpoint(
            "base-task-03",
            "manual-prerequisite-checkpoint-v1",
            {
                "schema": "https://powdrr.io/schemas/manual-prerequisite-checkpoint-v1",
                "workflow_id": "base",
                "human_task_id": "base-task-04",
                "follow_up_task_id": "base-task-05",
                "workflow_directory": "docs/workflows/runtime/base",
                "integration_branch": "powdrr/base-attempt-abc",
                "pull_request_url": "https://github.com/example/project/pull/123",
                "commit_sha": "b" * 40,
                "prerequisite": {"title": "Install key", "instructions": ["Do it"]},
            },
            "b" * 40,
        )
        return 0

    report = run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=tmp_path,
        worker_id="worker-a",
        run_task=run_task,
        plan_loader=lambda *_args, **_kwargs: plan(),
    )

    assert report.outcome == "awaiting_human"
    assert QUEUE_LABELS["awaiting_human"] in client.issues[0].labels
    assert QUEUE_LABELS["awaiting_review"] not in client.issues[0].labels
    local_checkpoint = github_issue_queue._local_manual_prerequisite_checkpoint(
        tmp_path, 100
    )
    assert local_checkpoint is not None
    assert local_checkpoint["human_task_id"] == "base-task-04"
    assert any(
        "powdrr-task-checkpoint" in body
        for number, body in client.comments
        if number == 100
    )


def test_continue_manual_prerequisite_retries_ci_then_resumes(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    class FakeCIClient(FakeIssueClient):
        def _invoke(
            self, arguments: list[str] | tuple[str, ...], *, check: bool = True
        ) -> subprocess.CompletedProcess[str]:
            _ = check
            commands.append(list(arguments))
            if arguments[:2] == ["run", "list"]:
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    json.dumps(
                        [
                            {
                                "databaseId": 77,
                                "headSha": "b" * 40,
                                "status": "completed",
                                "conclusion": "failure",
                            }
                        ]
                    ),
                    "",
                )
            if arguments[:2] == ["pr", "view"]:
                return subprocess.CompletedProcess(
                    arguments, 0, json.dumps({"headRefOid": "c" * 40}), ""
                )
            if arguments[:2] == ["pr", "checks"]:
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    json.dumps([{"name": "test", "state": "pass", "workflow": "ci"}]),
                    "",
                )
            return subprocess.CompletedProcess(arguments, 0, "", "")

    client = FakeCIClient()
    publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )
    issue = client.issues[0]
    client.update_issue(
        issue,
        labels=github_issue_queue._replace_queue_status(issue.labels, "awaiting_human"),
    )
    checkpoint = {
        "schema": "https://powdrr.io/schemas/manual-prerequisite-checkpoint-v1",
        "workflow_id": "base",
        "human_task_id": "base-task-04",
        "follow_up_task_id": "base-task-05",
        "workflow_directory": "docs/workflows/runtime/base",
        "integration_branch": "powdrr/base-attempt-abc",
        "pull_request_url": "https://github.com/example/project/pull/123",
        "commit_sha": "b" * 40,
        "prerequisite": {"title": "Install key", "instructions": ["Do it"]},
    }
    github_issue_queue._write_local_task_checkpoint(
        repo_root=tmp_path,
        issue_number=issue.number,
        checkpoint=TaskCheckpoint(
            plan_id="feature-v1",
            item_id="base",
            workflow_id="base",
            task_id="base-task-03",
            output_state_type="manual-prerequisite-checkpoint-v1",
            output_state=checkpoint,
            commit_sha="b" * 40,
        ),
    )
    resumed: list[Mapping[str, object]] = []

    def resume(value: Mapping[str, object], callback: TaskCheckpointCallback) -> int:
        resumed.append(value)
        callback(
            "base-task-04",
            "manual-prerequisite-completed-v1",
            {"human_task_id": "base-task-04", "completed": True},
            "c" * 40,
        )
        return 0

    report = continue_manual_prerequisite_issue(
        client=client,  # type: ignore[arg-type]
        issue_number=100,
        repo_root=tmp_path,
        resume_workflow=resume,
    )

    assert report.outcome == "awaiting_review"
    assert resumed == [checkpoint]
    assert [command[1:4] for command in commands[:4]] == [
        ["list", "--branch", "powdrr/base-attempt-abc"],
        ["rerun", "77", "--failed"],
        ["watch", "77", "--exit-status"],
        ["checks", "https://github.com/example/project/pull/123", "--watch"],
    ]
    assert [command[1:3] for command in commands[4:]] == [
        ["view", "123"],
        ["checks", "123"],
        ["--paginate", "repos/example/project/pulls/123/files?per_page=100"],
        ["view", "123"],
    ]
    assert QUEUE_LABELS["awaiting_review"] in client.get_issue(100).labels
    assert latest_manual_prerequisite_checkpoint(client.list_comments(100)) is None
    assert any(
        "powdrr-task-checkpoint" in body
        for number, body in client.comments
        if number == 100
    )


def test_worker_does_not_use_plan_amendment_title_as_metadata(tmp_path: Path) -> None:
    client = FakeIssueClient()
    publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )
    issue = client.issues[0]
    client.issues[0] = QueueIssue(
        issue.number,
        issue.title,
        issue.body,
        issue.state,
        (QUEUE_LABELS["awaiting_review"], _plan_label("feature-v1")),
        issue.url,
    )
    client.comments.append(
        (
            issue.number,
            _legacy_checkpoint_comment(
                TaskCheckpoint(
                    plan_id="feature-v1",
                    item_id="base",
                    workflow_id="base",
                    task_id="amend-plan-task-01",
                    output_state_type="execute-proposed-pr-workflow-result-state",
                    output_state={
                        "pull_request_url": "https://github.com/example/project/pull/77"
                    },
                )
            ),
        )
    )
    client.pull_requests[77] = {
        "title": "Correct plan: Reuse the existing verifier",
        "merged": True,
        "merge_commit_sha": "c" * 40,
    }

    report = run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=tmp_path,
        worker_id="worker-after-amendment",
        run_task=lambda *_args: pytest.fail("no repair manifest means no requeue"),
        plan_loader=lambda *_args, **_kwargs: plan(),
    )

    assert report.outcome == "no_eligible_work"
    assert parse_issue_pointer(client.issues[0].body) == {
        "plan_id": "feature-v1",
        "item_id": "base",
        "plan_path": "docs/plans/feature.yaml",
        "plan_commit": PLAN_COMMIT,
    }


def test_worker_does_not_requeue_unmerged_plan_amendment(tmp_path: Path) -> None:
    client = FakeIssueClient()
    publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )
    issue = client.issues[0]
    client.issues[0] = QueueIssue(
        issue.number,
        issue.title,
        issue.body,
        issue.state,
        (QUEUE_LABELS["awaiting_review"], _plan_label("feature-v1")),
        issue.url,
    )
    client.comments.append(
        (
            issue.number,
            _legacy_checkpoint_comment(
                TaskCheckpoint(
                    plan_id="feature-v1",
                    item_id="base",
                    workflow_id="base",
                    task_id="amend-plan-task-01",
                    output_state_type="execute-proposed-pr-workflow-result-state",
                    output_state={
                        "pull_request_url": "https://github.com/example/project/pull/77"
                    },
                )
            ),
        )
    )
    client.pull_requests[77] = {
        "title": "Correct plan: Reuse the existing verifier",
        "merged": False,
        "merge_commit_sha": None,
    }

    report = run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=tmp_path,
        worker_id="worker-before-amendment-merge",
        run_task=lambda *_args: pytest.fail("issue must remain awaiting review"),
        plan_loader=lambda *_args, **_kwargs: plan(),
    )

    assert report.outcome == "no_eligible_work"
    pointer = parse_issue_pointer(client.issues[0].body)
    assert pointer is not None
    assert pointer["plan_commit"] == PLAN_COMMIT
    assert QUEUE_LABELS["awaiting_review"] in client.issues[0].labels


def test_legacy_issue_checkpoint_is_readable_for_rolling_upgrade() -> None:
    body = _legacy_checkpoint_comment(
        TaskCheckpoint(
            plan_id="plan-v1",
            item_id="PR-02",
            workflow_id="plan-pr-02",
            task_id="plan-pr-02-task-03",
            output_state_type="validation-state",
            output_state={"tests": "passed"},
            commit_sha="c" * 40,
        )
    )
    assert body.startswith("### Powdrr task checkpoint\n")
    assert "<!-- powdrr-task-checkpoint:v1 -->" in body
    assert '"task_id": "plan-pr-02-task-03"' in body


def test_plan_repair_reconciles_all_affected_issues_idempotently(
    tmp_path: Path,
) -> None:
    from powdrr_service.planning.repair import (
        item_content_digest,
        item_input_contract_digest,
    )

    client = FakeIssueClient()
    publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )
    base, followup = client.issues
    client.issues[0] = QueueIssue(
        base.number,
        base.title,
        base.body,
        "open",
        (QUEUE_LABELS["awaiting_review"], _plan_label("feature-v1")),
        base.url,
    )
    client.issues[1] = QueueIssue(
        followup.number,
        followup.title,
        followup.body,
        "closed",
        (FULFILLED_WITHOUT_PR_LABEL, _plan_label("feature-v1")),
        followup.url,
    )
    client.comments.append(
        (
            base.number,
            render_task_checkpoint_comment(
                TaskCheckpoint(
                    plan_id="feature-v1",
                    item_id="base",
                    workflow_id="base",
                    task_id="base-task-01",
                    output_state_type="implementation-state",
                    output_state={"files": ["src/base.py"]},
                    input_contract_digest="sha256:stale",
                )
            ),
        )
    )

    amended_base = PlannedWorkItem(
        "base",
        "Updated base work",
        "Add the revised shared base.",
        ("src/base.py",),
        ("Revised base behavior is covered.",),
        (),
        "docs/workflows/base",
        "base",
    )
    new_item = PlannedWorkItem(
        "followup-v2",
        "Replacement work",
        "Implement the newly allocated follow-up.",
        ("src/followup.py",),
        ("Replacement behavior works.",),
        ("base",),
        "docs/workflows/followup-v2",
        "followup-v2",
    )
    repaired_plan = ReviewedExecutionPlan(
        "feature-v1",
        (amended_base, new_item),
        planning={
            "latest_repair": {
                "repair_id": "repair-abc",
                "discovery_ids": ["discovery-1"],
                "old_semantic_revision_digest": "old-semantic",
                "new_semantic_revision_digest": "new-semantic",
                "old_plan_digest": "old-plan",
                "new_plan_digest": "new-plan",
                "affected_item_ids": ["base", "followup", "followup-v2"],
                "old_item_digests": {"base": "old-base"},
                "new_item_digests": {
                    "base": item_content_digest(amended_base),
                    "followup-v2": item_content_digest(new_item),
                },
                "old_input_contract_digests": {"base": "old-contract"},
                "new_input_contract_digests": {
                    "base": item_input_contract_digest(amended_base, None),
                    "followup-v2": item_input_contract_digest(new_item, None),
                },
                "retired_items": [
                    {
                        "item_id": "followup",
                        "disposition": "superseded",
                        "reason": (
                            "The remaining work was split into a new deliverable."
                        ),
                        "successor_item_ids": ["followup-v2"],
                        "requirement_ids": [],
                    }
                ],
            }
        },
    )
    assert repaired_plan.planning is not None
    repair = repaired_plan.planning["latest_repair"]
    assert isinstance(repair, Mapping)
    metadata = {
        "plan_id": "feature-v1",
        "plan_path": "docs/plans/feature.yaml",
        "repair": repair,
    }
    pull_request_body = render_plan_repair_metadata(
        plan_id="feature-v1",
        plan_path="docs/plans/feature.yaml",
        repair=repair,
    )
    client.pull_requests[77] = {
        "title": "A title that does not follow a naming convention",
        "body": pull_request_body,
        "state": "open",
        "merged": False,
        "merge_commit_sha": None,
    }
    github_issue_queue._mark_plan_repair_pending(
        client=client,  # type: ignore[arg-type]
        metadata=metadata,
        pull_request_number=77,
    )
    assert QUEUE_LABELS["blocked"] in client.issues[0].labels
    assert PLAN_REPAIR_PENDING_LABEL in client.issues[0].labels
    assert client.issues[1].state == "closed"
    assert PLAN_REPAIR_PENDING_LABEL not in client.issues[1].labels

    merge_commit = "c" * 40
    client.pull_requests[77].update(
        state="closed", merged=True, merge_commit_sha=merge_commit
    )
    load_calls: list[str] = []

    def load_plan(
        path: str, commit: str, *, repo_root: str | Path
    ) -> ReviewedExecutionPlan:
        assert path == "docs/plans/feature.yaml"
        assert Path(repo_root) == tmp_path
        load_calls.append(commit)
        return repaired_plan

    for _ in range(2):
        github_issue_queue._requeue_merged_plan_amendments(
            client=client,  # type: ignore[arg-type]
            repo_root=tmp_path,
            plan_loader=load_plan,
        )

    base_after, followup_after, created = client.issues
    base_pointer = parse_issue_pointer(base_after.body)
    assert base_pointer is not None
    assert base_pointer["plan_commit"] == merge_commit
    assert QUEUE_LABELS["ready"] in base_after.labels
    assert PLAN_REPAIR_PENDING_LABEL not in base_after.labels
    assert followup_after.state == "closed"
    assert FULFILLED_WITHOUT_PR_LABEL in followup_after.labels
    assert not any(
        "was superseded" in body
        for number, body in client.comments
        if number == followup.number
    )
    assert created.title == "Powdrr work item: feature-v1/followup-v2"
    assert QUEUE_LABELS["blocked_on_dependency"] in created.labels
    created_pointer = parse_issue_pointer(created.body)
    assert created_pointer is not None
    assert created_pointer["plan_commit"] == merge_commit
    assert any(
        "Task checkpoints invalidated" in body
        for number, body in client.comments
        if number == base.number
    )
    assert load_calls == [merge_commit]
    assert len(client.issues) == 3


def test_closed_unmerged_plan_repair_restores_prior_queue_state() -> None:
    client = FakeIssueClient()
    publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )
    issue = client.issues[0]
    metadata = {
        "plan_id": "feature-v1",
        "plan_path": "docs/plans/feature.yaml",
        "repair": {
            "repair_id": "repair-closed",
            "affected_item_ids": ["base"],
            "old_item_digests": {},
            "new_item_digests": {},
            "old_input_contract_digests": {},
            "new_input_contract_digests": {},
            "retired_items": [],
        },
    }
    client.pull_requests[88] = {"state": "closed", "merged": False}
    github_issue_queue._mark_plan_repair_pending(
        client=client,  # type: ignore[arg-type]
        metadata=metadata,
        pull_request_number=88,
    )
    assert QUEUE_LABELS["blocked"] in client.issues[0].labels
    github_issue_queue._restore_closed_plan_repair(
        client=client,  # type: ignore[arg-type]
        metadata=metadata,
        pull_request_number=88,
    )
    assert QUEUE_LABELS["ready"] in client.issues[0].labels
    assert PLAN_REPAIR_PENDING_LABEL not in client.issues[0].labels
    assert any(
        "closed without merging" in body
        for number, body in client.comments
        if number == issue.number
    )


def test_worker_checkpoint_pauses_when_an_affected_repair_is_pending() -> None:
    client = FakeIssueClient()
    publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )
    issue = client.issues[0]
    metadata = {
        "plan_id": "feature-v1",
        "plan_path": "docs/plans/feature.yaml",
        "repair": {
            "repair_id": "repair-pending",
            "affected_item_ids": ["base"],
            "old_item_digests": {},
            "new_item_digests": {},
            "old_input_contract_digests": {},
            "new_input_contract_digests": {},
            "retired_items": [],
        },
    }
    github_issue_queue._mark_plan_repair_pending(
        client=client,  # type: ignore[arg-type]
        metadata=metadata,
        pull_request_number=89,
    )
    callback = github_issue_queue._task_checkpoint_callback(
        client=client,  # type: ignore[arg-type]
        issue=client.issues[0],
        plan=plan(),
        item=plan().items[0],
    )
    with pytest.raises(GitHubIssueQueueError, match="paused at a durable checkpoint"):
        callback("base-task-02", "implementation-state", {"files": ["a.py"]}, None)
    assert not any(
        "base-task-02" in body
        for number, body in client.comments
        if number == issue.number
    )


def test_repair_pr_checkpoint_blocks_impact_before_checkpoint_is_written(
    tmp_path: Path,
) -> None:
    client = FakeIssueClient()
    publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )
    repair = {
        "repair_id": "repair-checkpoint",
        "affected_item_ids": ["base"],
        "old_item_digests": {},
        "new_item_digests": {},
        "old_input_contract_digests": {},
        "new_input_contract_digests": {},
        "retired_items": [],
    }
    client.pull_requests[90] = {
        "state": "open",
        "merged": False,
        "body": render_plan_repair_metadata(
            plan_id="feature-v1",
            plan_path="docs/plans/feature.yaml",
            repair=repair,
        ),
    }
    checkpoint = github_issue_queue._task_checkpoint_callback(
        client=client,  # type: ignore[arg-type]
        issue=client.issues[0],
        plan=plan(),
        item=plan().items[0],
        repo_root=tmp_path,
        plan_loader=lambda *_args, **_kwargs: plan(),
    )

    checkpoint(
        "repair-pr-task",
        "execute-proposed-pr-workflow-result-state",
        {"pull_request_url": "https://github.com/example/project/pull/90"},
        None,
    )

    issue = client.get_issue(100)
    assert PLAN_REPAIR_PENDING_LABEL in issue.labels
    assert QUEUE_LABELS["blocked"] in issue.labels
    checkpoint_record = github_issue_queue._read_local_task_checkpoint(tmp_path, 100)
    assert checkpoint_record is not None
    assert checkpoint_record["task_id"] == "repair-pr-task"
    assert any(
        "powdrr-task-checkpoint" in body
        for number, body in client.comments
        if number == issue.number
    )


def test_task_checkpoint_comment_is_readable_and_structured() -> None:
    body = render_task_checkpoint_comment(
        TaskCheckpoint(
            plan_id="plan-v1",
            item_id="PR-02",
            workflow_id="plan-pr-02",
            task_id="plan-pr-02-task-03",
            output_state_type="validation-state",
            output_state={"tests": "passed"},
            commit_sha="c" * 40,
        )
    )

    assert body.startswith("### Powdrr task checkpoint\n")
    assert "<!-- powdrr-task-checkpoint:v1 -->" in body
    assert '"task_id": "plan-pr-02-task-03"' in body


def test_ci_repair_artifact_round_trips_through_issue_comments() -> None:
    issue = QueueIssue(
        123,
        "CI repair",
        "",
        "open",
        (),
        "https://github.com/example/project/issues/123",
    )
    client = FakeIssueClient((issue,))
    contract_id = "repair-" + "a" * 20
    record = {
        "contract_id": contract_id,
        "observation_id": "b" * 64,
        "attempt_id": f"{contract_id}-dispatch",
        "phase": "completed",
        "candidate_bytes": "".join(
            hashlib.sha256(str(index).encode()).hexdigest() for index in range(3000)
        ),
    }

    github_issue_queue._persist_ci_repair_artifact(
        client=client,  # type: ignore[arg-type]
        issue=issue,
        contract_id=contract_id,
        record=record,
    )

    assert len(client.comments) > 1
    assert github_issue_queue._load_ci_repair_artifact(
        client=client,  # type: ignore[arg-type]
        issue_number=issue.number,
        contract_id=contract_id,
    ) == {"schema": "ci-repair-continuation-v1", **record}
    comment_number, body = client.comments[0]
    client.comments[0] = (
        comment_number,
        body.replace('"content_base64": "', '"content_base64": "A', 1),
    )
    assert (
        github_issue_queue._load_ci_repair_artifact(
            client=client,  # type: ignore[arg-type]
            issue_number=issue.number,
            contract_id=contract_id,
        )
        is None
    )


def test_worker_keeps_pending_required_checks_out_of_review_queue(
    tmp_path: Path,
) -> None:
    (tmp_path / "docs/workflows/base").mkdir(parents=True)
    client = FakeIssueClient()
    client.required_checks = [{"name": "test", "state": "pending", "workflow": "ci"}]
    publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )

    def run_task(*args: Any) -> int:
        _checkpoint_completed_pr(args[-1], args[3])
        return 0

    report = run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=tmp_path,
        worker_id="worker-pending-ci",
        run_task=run_task,
        plan_loader=lambda *_args, **_kwargs: plan(),
    )

    assert report.outcome == "awaiting_ci", report.detail
    assert report.ci_outcome == "awaiting_ci"
    assert QUEUE_LABELS["awaiting_ci"] in client.issues[0].labels
    assert QUEUE_LABELS["awaiting_review"] not in client.issues[0].labels
    assert any(
        "powdrr-ci-outcome:v1" in body
        for number, body in client.comments
        if number == 100
    )


def test_worker_persists_ci_response_result_but_uses_fresh_queue_observation(
    tmp_path: Path,
) -> None:
    (tmp_path / "docs/workflows/base").mkdir(parents=True)
    client = FakeIssueClient()
    client.required_checks = [{"name": "test", "state": "pending", "workflow": "ci"}]
    publish_plan_issues(
        plan=plan(),
        plan_path="docs/plans/feature.yaml",
        plan_commit=PLAN_COMMIT,
        client=client,  # type: ignore[arg-type]
    )

    def run_task(*args: Any) -> int:
        checkpoint: TaskCheckpointCallback = args[-1]
        checkpoint(
            "implementation-task",
            "execute-proposed-pr-workflow-result-state",
            {
                "pull_request_url": "https://github.com/example/project/pull/100",
                "ci_response_result": {
                    "observation_schema_version": 1,
                    "response_state": "awaiting_review",
                    "response_reason": "response observation initially looked green",
                    "head_sha": client.pr_head,
                    "observation_id": "e" * 64,
                    "required_checks_status": "available",
                    "waited_seconds": 20,
                    "wait_deadline_reached": False,
                    "repair_dispatch_status": "rejected",
                    "repair_reasons": ["candidate changed a protected workflow"],
                    "publication_status": "not_attempted",
                    "publication_reasons": [],
                },
            },
            "b" * 40,
        )
        return 0

    report = run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=tmp_path,
        worker_id="worker-response-result",
        run_task=run_task,
        plan_loader=lambda *_args, **_kwargs: plan(),
    )

    assert report.outcome == "awaiting_ci", report.detail
    assert report.ci_outcome == "awaiting_ci"
    ci_comment = next(
        body
        for number, body in client.comments
        if number == 100 and "powdrr-ci-outcome:v1" in body
    )
    payload_match = re.search(r"```json\s*(.*?)\s*```", ci_comment, re.DOTALL)
    assert payload_match is not None
    payload = json.loads(payload_match.group(1))
    assert payload["outcome"] == "awaiting_ci"
    assert payload["ci_response_result"]["response_state"] == "awaiting_review"
    assert payload["ci_response_result"]["matches_queue_head"] is True
    assert payload["ci_response_result"]["repair_dispatch_status"] == "rejected"
    assert payload["ci_response_result"]["repair_reasons"] == [
        "candidate changed a protected workflow"
    ]
    assert payload["ci_response_result"]["publication_status"] == "not_attempted"


def test_continue_awaiting_ci_rechecks_current_head_and_records_result(
    tmp_path: Path,
) -> None:
    client = FakeIssueClient()
    client.issues.append(
        QueueIssue(
            100,
            "Implement base",
            "",
            "open",
            (QUEUE_LABELS["awaiting_ci"],),
            "https://github.com/example/project/issues/100",
        )
    )
    client.comments.append(
        (
            100,
            _legacy_checkpoint_comment(
                TaskCheckpoint(
                    plan_id="feature-v1",
                    item_id="base",
                    workflow_id="base",
                    task_id="finished",
                    output_state_type="execute-proposed-pr-workflow-result-state",
                    output_state={
                        "pull_request_url": "https://github.com/example/project/pull/123"
                    },
                    commit_sha="b" * 40,
                )
            ),
        )
    )
    response_result = {
        "schema_version": 1,
        "head_sha": client.pr_head,
        "observation_id": "f" * 64,
        "response_state": "awaiting_ci",
        "response_reason": "wait deadline reached",
        "required_checks_status": "available",
        "waited_seconds": 900,
        "wait_deadline_reached": True,
    }
    client.comments.append(
        (
            100,
            github_issue_queue._render_ci_outcome_checkpoint(
                pull_request_url="https://github.com/example/project/pull/123",
                observation={
                    "outcome": "awaiting_ci",
                    "head_sha": client.pr_head,
                    "observed_at": "2026-11-15T00:00:00+00:00",
                    "checks": [],
                    "reason": "checks pending",
                },
                ci_response_result=response_result,
            ),
        )
    )

    report = continue_awaiting_ci_issue(
        client=client,  # type: ignore[arg-type]
        issue_number=100,
        repo_root=tmp_path,
    )

    assert report.outcome == "awaiting_review"
    assert QUEUE_LABELS["awaiting_review"] in client.get_issue(100).labels
    assert QUEUE_LABELS["awaiting_ci"] not in client.get_issue(100).labels
    latest = github_issue_queue._latest_ci_outcome_observation(
        client.list_comments(100), "https://github.com/example/project/pull/123"
    )
    assert latest is not None
    assert latest["ci_response_result"]["response_state"] == "awaiting_ci"
    assert latest["ci_response_result"]["matches_queue_head"] is True


def test_continue_ci_human_checkpoint_preserves_human_route_until_checks_clear(
    tmp_path: Path,
) -> None:
    client = FakeIssueClient()
    client.required_checks = [{"name": "test", "state": "fail", "workflow": "ci"}]
    client.issues.append(
        QueueIssue(
            100,
            "Implement base",
            "",
            "open",
            (QUEUE_LABELS["awaiting_human"],),
            "https://github.com/example/project/issues/100",
        )
    )
    client.comments.append(
        (
            100,
            _legacy_checkpoint_comment(
                TaskCheckpoint(
                    plan_id="feature-v1",
                    item_id="base",
                    workflow_id="base",
                    task_id="finished",
                    output_state_type="execute-proposed-pr-workflow-result-state",
                    output_state={
                        "pull_request_url": "https://github.com/example/project/pull/123"
                    },
                    commit_sha="b" * 40,
                )
            ),
        )
    )
    client.comments.append(
        (
            100,
            github_issue_queue._render_ci_outcome_checkpoint(
                pull_request_url="https://github.com/example/project/pull/123",
                observation={
                    "outcome": "blocked",
                    "head_sha": client.pr_head,
                    "observed_at": "2026-11-15T00:00:00+00:00",
                    "checks": [],
                    "reason": "required check failed",
                },
                ci_response_result={
                    "schema_version": 1,
                    "head_sha": client.pr_head,
                    "observation_id": "f" * 64,
                    "response_state": "awaiting_human",
                    "response_reason": "automatic repair scope was ambiguous",
                    "required_checks_status": "available",
                    "repair_dispatch_status": "human_required",
                    "repair_reasons": ["Review the failed workflow setup."],
                },
                human_prerequisite={
                    "schema_version": 1,
                    "reasons": ["Review the failed workflow setup."],
                },
            ),
        )
    )

    assert github_issue_queue.has_ci_human_checkpoint(
        client=cast(GitHubIssueClient, client), issue_number=100
    )
    report = continue_awaiting_ci_issue(
        client=client,  # type: ignore[arg-type]
        issue_number=100,
        repo_root=tmp_path,
    )

    assert report.outcome == "awaiting_human"
    assert QUEUE_LABELS["awaiting_human"] in client.get_issue(100).labels
    assert report.detail is not None
    assert "github-issue-worker-continue" in report.detail
    latest = github_issue_queue._latest_ci_outcome_observation(
        client.list_comments(100), "https://github.com/example/project/pull/123"
    )
    assert latest is not None
    assert latest["human_prerequisite"]["reasons"] == [
        "Review the failed workflow setup."
    ]

    client.required_checks = [{"name": "test", "state": "pass", "workflow": "ci"}]
    recovered = continue_awaiting_ci_issue(
        client=client,  # type: ignore[arg-type]
        issue_number=100,
        repo_root=tmp_path,
    )
    assert recovered.outcome == "awaiting_review"
    assert QUEUE_LABELS["awaiting_review"] in client.get_issue(100).labels
    assert QUEUE_LABELS["awaiting_human"] not in client.get_issue(100).labels


def test_required_ci_observation_rejects_head_change_during_check_read(
    tmp_path: Path,
) -> None:
    class ChangingHeadClient(FakeIssueClient):
        view_count = 0

        def _invoke(
            self, arguments: list[str] | tuple[str, ...], *, check: bool = True
        ) -> subprocess.CompletedProcess[str]:
            if arguments[:2] == ["pr", "view"]:
                self.view_count += 1
                head = "c" * 40 if self.view_count == 1 else "d" * 40
                return subprocess.CompletedProcess(
                    ["gh", *arguments],
                    0,
                    json.dumps({"headRefOid": head}),
                    "",
                )
            return super()._invoke(arguments, check=check)

    client = ChangingHeadClient()
    observation = github_issue_queue._observe_required_ci_outcome(
        client,  # type: ignore[arg-type]
        "https://github.com/example/project/pull/123",
        validation_inventory=_test_validation_checks(tmp_path),
        inventory_digest="test-digest",
    )

    assert observation["outcome"] == "blocked"
    assert "head changed" in observation["reason"]
    assert observation["refresh_succeeded"] is False
    assert observation["stale_remote_observations"] is True
    assert observation["checks"][0]["status"] == "unknown"
    assert observation["checks"][0]["last_known_status"] == "passed"


def test_required_ci_readiness_uses_timestamped_normalized_current_head_checks(
    tmp_path: Path,
) -> None:
    client = FakeIssueClient()
    client.required_checks = [
        {"name": "unit", "state": "success", "workflow": "ci"},
        {"name": "integration", "state": "pending", "workflow": "ci"},
        {"name": "docs", "state": "skipping", "workflow": "ci"},
    ]

    observation = github_issue_queue._observe_required_ci_outcome(
        client,  # type: ignore[arg-type]
        "https://github.com/example/project/pull/123",
        validation_inventory=_test_validation_checks(tmp_path),
        inventory_digest="test-digest",
    )

    assert observation["outcome"] == "blocked"
    assert "unrecognized state" in observation["reason"]
    assert observation["head_sha"] == client.pr_head
    assert observation["observed_at"].endswith("+00:00")
    assert [item["status"] for item in observation["checks"]] == [
        "passed",
        "pending",
        "unknown",
    ]
    assert [item["inventory_match"]["status"] for item in observation["checks"]] == [
        "matched",
        "matched",
        "matched",
    ]
    assert all(item["head_sha"] == client.pr_head for item in observation["checks"])
    assert all(
        item["observed_at"] == observation["observed_at"]
        for item in observation["checks"]
    )

    checkpoint = github_issue_queue._render_ci_outcome_checkpoint(
        pull_request_url="https://github.com/example/project/pull/123",
        observation=observation,
    )

    assert '"observed_at"' in checkpoint
    assert '"validation_inventory_digest": "test-digest"' in checkpoint
    assert '"status": "unknown"' in checkpoint


def test_failed_remote_refresh_preserves_prior_checks_as_dated_stale_evidence(
    tmp_path: Path,
) -> None:
    pull_request_url = "https://github.com/example/project/pull/123"
    old_observed_at = "2026-11-13T19:00:00+00:00"
    prior = github_issue_queue._ci_outcome_observation(
        "awaiting_review",
        "c" * 40,
        [
            {
                "name": "unit",
                "state": "success",
                "status": "passed",
                "workflow": "ci",
                "inventory_match": {"status": "matched"},
            }
        ],
        "all checks passed",
        old_observed_at,
        "inventory-v1",
    )
    checkpoint = github_issue_queue._render_ci_outcome_checkpoint(
        pull_request_url=pull_request_url, observation=prior
    )
    previous = github_issue_queue._latest_ci_outcome_observation(
        ({"body": checkpoint},), pull_request_url
    )
    assert previous is not None

    class OfflineIssueClient(FakeIssueClient):
        def _invoke(
            self, arguments: list[str] | tuple[str, ...], *, check: bool = True
        ) -> subprocess.CompletedProcess[str]:
            if arguments[:2] == ["pr", "view"]:
                return subprocess.CompletedProcess(
                    ["gh", *arguments], 1, "", "network unavailable"
                )
            return super()._invoke(arguments, check=check)

    observation = github_issue_queue._observe_required_ci_outcome(
        OfflineIssueClient(),  # type: ignore[arg-type]
        pull_request_url,
        validation_inventory=_test_validation_checks(tmp_path),
        inventory_digest="inventory-v2",
        previous_observation=previous,
    )

    assert observation["outcome"] == "blocked"
    assert observation["refresh_succeeded"] is False
    assert observation["stale_remote_observations"] is True
    assert observation["last_remote_observed_at"] == old_observed_at
    stale_check = observation["checks"][0]
    assert stale_check["status"] == "unknown"
    assert stale_check["last_known_status"] == "passed"
    assert stale_check["freshness"] == "stale"
    assert stale_check["observed_at"] == old_observed_at
    assert stale_check["head_sha"] == "c" * 40
    assert stale_check["stale_since"] != old_observed_at
    repeated_stale = github_issue_queue._stale_ci_outcome_observation(
        previous_observation=observation,
        current_checks=(),
        head_sha=None,
        reason="GitHub is still unavailable",
        observed_at="2026-11-13T20:00:00+00:00",
        inventory_digest="inventory-v2",
    )
    assert repeated_stale["last_remote_observed_at"] == old_observed_at
    assert repeated_stale["checks"][0]["last_known_status"] == "passed"
    assert repeated_stale["checks"][0]["stale_since"] == stale_check["stale_since"]
    stale_checkpoint = github_issue_queue._render_ci_outcome_checkpoint(
        pull_request_url=pull_request_url, observation=observation
    )
    assert '"stale_remote_observations": true' in stale_checkpoint
    assert '"last_known_status": "passed"' in stale_checkpoint


def test_missing_pull_request_url_preserves_latest_ci_observation_as_stale() -> None:
    pull_request_url = "https://github.com/example/project/pull/123"
    prior = github_issue_queue._ci_outcome_observation(
        "awaiting_review",
        "d" * 40,
        [{"name": "unit", "state": "success", "status": "passed"}],
        "all checks passed",
        "2026-11-13T19:00:00+00:00",
        "inventory-v1",
    )
    prior_checkpoint = github_issue_queue._render_ci_outcome_checkpoint(
        pull_request_url=pull_request_url, observation=prior
    )
    previous = github_issue_queue._latest_ci_outcome_observation(
        ({"body": prior_checkpoint},), None
    )
    assert previous is not None
    assert previous["pull_request_url"] == pull_request_url

    observation = github_issue_queue._observe_required_ci_outcome(
        FakeIssueClient(),  # type: ignore[arg-type]
        None,
        validation_inventory=None,
        inventory_digest=None,
        previous_observation=previous,
    )

    assert observation["outcome"] == "blocked"
    assert observation["refresh_succeeded"] is False
    assert observation["stale_remote_observations"] is True
    assert observation["head_sha"] == "d" * 40
    assert observation["checks"][0]["status"] == "unknown"
    assert observation["checks"][0]["last_known_status"] == "passed"
    stale_checkpoint = github_issue_queue._render_ci_outcome_checkpoint(
        pull_request_url=None, observation=observation
    )
    recovered = github_issue_queue._latest_ci_outcome_observation(
        ({"body": prior_checkpoint}, {"body": stale_checkpoint}), None
    )
    assert recovered is not None
    assert recovered["pull_request_url"] == pull_request_url
    assert recovered["checks"][0]["status"] == "unknown"


def test_required_ci_readiness_blocks_when_validation_inventory_is_missing() -> None:
    client = FakeIssueClient()
    observation = github_issue_queue._observe_required_ci_outcome(
        client,  # type: ignore[arg-type]
        "https://github.com/example/project/pull/123",
        validation_inventory=None,
        inventory_digest=None,
    )

    assert observation["outcome"] == "blocked"
    assert "inventory is missing" in observation["reason"]
    assert observation["checks"][0]["inventory_match"]["status"] == "unavailable"


def test_inventory_loader_rejects_tampered_report_and_stale_input(
    tmp_path: Path,
) -> None:
    _test_validation_checks(tmp_path)
    baseline = tmp_path / "docs/structrr/current/baseline-bootstrap.yaml"
    report = baseline.with_suffix(".md")
    workflow = tmp_path / ".github/workflows/ci.yml"

    report.write_text("# changed report\n", encoding="utf-8")
    assert github_issue_queue._read_repository_validation_inventory(tmp_path) is None

    _write_test_validation_inventory(tmp_path)
    workflow.write_text("name: CI\njobs:\n  extra: {}\n", encoding="utf-8")
    assert github_issue_queue._read_repository_validation_inventory(tmp_path) is None


def test_required_ci_readiness_blocks_unmatched_discovered_job(
    tmp_path: Path,
) -> None:
    client = FakeIssueClient()
    client.required_checks = [
        {"name": "security-scan", "state": "success", "workflow": "ci"}
    ]
    observation = github_issue_queue._observe_required_ci_outcome(
        client,  # type: ignore[arg-type]
        "https://github.com/example/project/pull/123",
        validation_inventory=_test_validation_checks(tmp_path),
        inventory_digest="test-digest",
    )

    assert observation["outcome"] == "blocked"
    assert "could not be matched" in observation["reason"]
    assert observation["checks"][0]["inventory_match"]["status"] == "unmatched"


@pytest.mark.parametrize(
    "changed_file",
    [
        {"filename": ".github/workflows/new-checks.yml", "status": "added"},
        {"filename": ".github/workflows/ci.yml", "status": "modified"},
        {"filename": ".github/workflows/ci.yml", "status": "removed"},
        {
            "filename": ".github/workflows/new-ci.yml",
            "previous_filename": ".github/workflows/ci.yml",
            "status": "renamed",
        },
    ],
)
def test_required_ci_readiness_blocks_changed_validation_inputs(
    tmp_path: Path, changed_file: dict[str, str]
) -> None:
    client = FakeIssueClient()
    client.pull_request_files = [changed_file]
    inventory = github_issue_queue._read_repository_validation_inventory(tmp_path)
    assert inventory is not None

    observation = github_issue_queue._observe_required_ci_outcome(
        client,  # type: ignore[arg-type]
        "https://github.com/example/project/pull/123",
        validation_inventory=inventory["checks"],
        inventory_digest=inventory["digest"],
        validation_input_paths=inventory["input_paths"],
    )

    assert observation["outcome"] == "blocked"
    assert "refresh the committed validation baseline" in observation["reason"]
    assert observation["validation_input_changes"]
    assert changed_file["filename"] in observation["validation_input_changes"]
    if previous_filename := changed_file.get("previous_filename"):
        assert previous_filename in observation["validation_input_changes"]
    checkpoint = github_issue_queue._render_ci_outcome_checkpoint(
        pull_request_url="https://github.com/example/project/pull/123",
        observation=observation,
    )
    assert '"validation_input_changes"' in checkpoint


def test_required_ci_matching_rejects_ambiguous_workflow_identity(
    tmp_path: Path,
) -> None:
    inventory = list(_test_validation_checks(tmp_path))
    duplicate = dict(inventory[1])
    duplicate["id"] = "validation:unit-other-workflow"
    duplicate["ci_origins"] = [
        {
            "workflow": ".github/workflows/other.yml",
            "workflow_name": "Other",
            "job_id": "unit",
            "job_name": "unit",
        }
    ]
    inventory.append(duplicate)

    match = github_issue_queue._match_required_check_to_inventory(
        {"name": "unit", "workflow": None}, inventory
    )

    assert match["status"] == "ambiguous"
    assert match["profile_ids"] == []
