from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

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
    _parse_plan,
    _plan_label,
    continue_manual_prerequisite_issue,
    find_plan_issues,
    fulfill_issue_without_pr,
    latest_manual_prerequisite_checkpoint,
    parse_issue_pointer,
    publish_plan_issues,
    render_plan_repair_metadata,
    render_task_checkpoint_comment,
    run_worker_once,
)

PLAN_COMMIT = "a" * 40


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
        self.comments: list[tuple[int, str]] = []
        self.pull_requests: dict[int, dict[str, object]] = {}
        self.labels_ensured = False
        self.fulfillment_label_ensured = False
        self.merged_commit: str | None = None
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
        return self.pull_requests[number]


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
        _item: PlannedWorkItem,
        _plan: ReviewedExecutionPlan,
        _fulfilled_without_pr_dependencies: tuple[str, ...],
        _checkpoint: TaskCheckpointCallback,
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
    prerequisite = client.issues[0]
    client.issues[0] = QueueIssue(
        prerequisite.number,
        prerequisite.title,
        prerequisite.body,
        "closed",
        (FULFILLED_WITHOUT_PR_LABEL, _plan_label("feature-v1")),
        prerequisite.url,
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
    client.comments.append(
        (
            client.issues[0].number,
            """<!-- powdrr-plan-adjustment:v1 -->
```json
{
  "schema": "https://powdrr.io/schemas/github-issue-plan-adjustment-v1",
  "plan_id": "feature-v1",
  "item_id": "base",
  "reason": "The implementation steps update the CI workflow.",
  "changes": {
    "allowed_paths": ["src", "tests", ".github/workflows/ci.yml"]
  }
}
```""",
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
        _checkpoint: TaskCheckpointCallback,
    ) -> int:
        received.append(item)
        return 0

    report = run_worker_once(
        client=client,  # type: ignore[arg-type]
        repo_root=tmp_path,
        worker_id="worker-inline-adjustment",
        run_task=run_task,
        plan_loader=load_plan,
    )

    assert report.outcome == "awaiting_review"
    assert received[0].allowed_paths == (
        "src",
        "tests",
        ".github/workflows/ci.yml",
    )


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
        _item: PlannedWorkItem,
        _plan: ReviewedExecutionPlan,
        _fulfilled_without_pr_dependencies: tuple[str, ...],
        _checkpoint: TaskCheckpointCallback,
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
    client.issues[0] = QueueIssue(
        client.issues[0].number,
        client.issues[0].title,
        client.issues[0].body,
        "closed",
        client.issues[0].labels,
        client.issues[0].url,
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


def test_worker_persists_task_checkpoint_as_issue_comment(tmp_path: Path) -> None:
    from powdrr_lift.workrr.planning.repair import item_input_contract_digest

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
            {"files": ["a.py"]},
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
    issue_comments = [
        body for issue_number, body in client.comments if issue_number == 100
    ]
    checkpoint_body = next(
        body for body in issue_comments if "powdrr-task-checkpoint" in body
    )
    payload = json.loads(checkpoint_body.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert payload == {
        "commit_sha": "b" * 40,
        "item_id": "base",
        "output_state": {"files": ["a.py"]},
        "output_state_type": "implementation-state",
        "plan_id": "feature-v1",
        "schema": "https://powdrr.io/schemas/github-issue-task-checkpoint-v1",
        "status": "completed",
        "task_id": "base-task-02",
        "workflow_id": "base",
        "input_contract_digest": (
            item_input_contract_digest(plan().items[0], plan().planning)
        ),
    }


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
    assert latest_manual_prerequisite_checkpoint(client.list_comments(100)) is not None
    checkpoint_body = next(
        body
        for number, body in client.comments
        if number == 100 and "manual-prerequisite-checkpoint-v1" in body
    )
    checkpoint_payload = json.loads(
        checkpoint_body.split("```json\n", 1)[1].split("\n```", 1)[0]
    )
    assert checkpoint_payload["status"] == "awaiting_human"


def test_continue_manual_prerequisite_retries_ci_then_resumes(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    class FakeCIClient(FakeIssueClient):
        def _invoke(
            self, arguments: list[str], *, check: bool = True
        ) -> subprocess.CompletedProcess[str]:
            _ = check
            commands.append(arguments)
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
    client.add_comment(
        issue,
        render_task_checkpoint_comment(
            TaskCheckpoint(
                plan_id="feature-v1",
                item_id="base",
                workflow_id="base",
                task_id="base-task-03",
                output_state_type="manual-prerequisite-checkpoint-v1",
                output_state=checkpoint,
                commit_sha="b" * 40,
            )
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
    assert [command[1:4] for command in commands] == [
        ["list", "--branch", "powdrr/base-attempt-abc"],
        ["rerun", "77", "--failed"],
        ["watch", "77", "--exit-status"],
        ["checks", "https://github.com/example/project/pull/123", "--watch"],
    ]
    assert QUEUE_LABELS["awaiting_review"] in client.get_issue(100).labels
    assert latest_manual_prerequisite_checkpoint(client.list_comments(100)) is None


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
            render_task_checkpoint_comment(
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
            render_task_checkpoint_comment(
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


def test_plan_repair_reconciles_all_affected_issues_idempotently(
    tmp_path: Path,
) -> None:
    from powdrr_lift.workrr.planning.repair import (
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
    assert any(
        "repair-pr-task" in body
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
