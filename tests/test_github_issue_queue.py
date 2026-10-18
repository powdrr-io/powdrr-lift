from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from powdrr_lift.workrr.github_issue_queue import (
    QUEUE_LABELS,
    GitHubIssueClient,
    GitHubIssueQueueError,
    PlanIssueMatch,
    PlannedWorkItem,
    QueueIssue,
    ReviewedExecutionPlan,
    _parse_plan,
    _plan_label,
    find_plan_issues,
    parse_issue_pointer,
    publish_plan_issues,
    run_worker_once,
)

PLAN_COMMIT = "a" * 40


class FakeIssueClient:
    def __init__(self, issues: tuple[QueueIssue, ...] = ()) -> None:
        self.repository = "example/project"
        self.issues = list(issues)
        self.comments: list[tuple[int, str]] = []
        self.labels_ensured = False
        self.merged_commit: str | None = None
        self._next_number = 100

    def ensure_queue_labels(self) -> None:
        self.labels_ensured = True

    def ensure_plan_label(self, plan_id: str) -> str:
        return _plan_label(plan_id)

    def ensure_plan_commit_merged(self, plan_commit: str) -> None:
        self.merged_commit = plan_commit

    def list_issues(
        self, *, labels: tuple[str, ...] = (), state: str = "all"
    ) -> tuple[QueueIssue, ...]:
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
    ) -> None:
        index = next(
            index
            for index, value in enumerate(self.issues)
            if value.number == issue.number
        )
        self.issues[index] = QueueIssue(
            issue.number,
            issue.title,
            body if body is not None else issue.body,
            issue.state,
            labels if labels is not None else issue.labels,
            issue.url,
        )

    def add_comment(self, issue: QueueIssue, body: str) -> None:
        self.comments.append((issue.number, body))


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


def test_github_issue_client_paginates_and_ignores_pull_requests() -> None:
    def run(
        arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert "--paginate" in arguments
        assert "--slurp" in arguments
        return subprocess.CompletedProcess(
            arguments,
            0,
            json.dumps(
                [
                    [
                        {
                            "number": 1,
                            "title": "Powdrr work item",
                            "body": "pointer",
                            "state": "open",
                            "labels": [{"name": "powdrr:ready"}],
                            "html_url": "https://github.com/example/project/issues/1",
                        }
                    ],
                    [
                        {
                            "number": 2,
                            "title": "An issue",
                            "body": "pointer",
                            "state": "closed",
                            "labels": [],
                            "html_url": "https://github.com/example/project/issues/2",
                        },
                        {"number": 3, "pull_request": {"url": "ignored"}},
                    ],
                ]
            ),
            "",
        )

    issues = GitHubIssueClient("example/project", run=run).list_issues()

    assert [issue.number for issue in issues] == [1, 2]


def test_github_issue_client_filters_issues_server_side_by_label_and_state() -> None:
    commands: list[list[str]] = []

    def run(
        arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "[[]]", "")

    client = GitHubIssueClient("example/project", run=run)
    assert client.list_issues(labels=(QUEUE_LABELS["ready"],), state="open") == ()

    assert commands[0][4] == (
        "repos/example/project/issues?state=open&per_page=100&labels=powdrr%3Aready"
    )


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
    pointer = parse_issue_pointer(client.issues[0].body)
    assert pointer == {
        "plan_id": "feature-v1",
        "item_id": "base",
        "plan_path": "docs/plans/feature.yaml",
        "plan_commit": PLAN_COMMIT,
    }
    assert "Add the shared base" not in client.issues[0].body
    assert "https://github.com/example/project/blob/" in client.issues[0].body


def test_worker_skips_unmet_dependency_then_executes_ready_item(tmp_path: Path) -> None:
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
    ) -> int:
        executed.append(workflow_id)
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
    assert QUEUE_LABELS["awaiting_review"] in client.issues[0].labels

    waiting = run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=root,
        worker_id="worker-a",
        run_task=run_task,
        plan_loader=load_plan,
    )
    assert waiting.outcome == "no_eligible_work"
    assert executed == ["base"]

    client.issues[0] = QueueIssue(
        client.issues[0].number,
        client.issues[0].title,
        client.issues[0].body,
        "closed",
        client.issues[0].labels,
        client.issues[0].url,
    )
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


def test_worker_accepts_one_file_workflow_definition(tmp_path: Path) -> None:
    workflow_file = tmp_path / "docs/workflows/feature-v1/base.yaml"
    workflow_file.parent.mkdir(parents=True)
    workflow_file.write_text("template: execute-proposed-pr\n", encoding="utf-8")
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
        _item: PlannedWorkItem,
        _plan: ReviewedExecutionPlan,
    ) -> int:
        observed.append(path)
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

    report = run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=tmp_path,
        worker_id="worker-a",
        run_task=lambda _directory, _workflow_id, _worker_id, _item, _plan: 1,
        plan_loader=load_plan,
    )
    assert report.outcome == "blocked"
    assert QUEUE_LABELS["blocked"] in client.issues[0].labels
