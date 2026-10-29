from __future__ import annotations

from powdrr_local.planning.sessions import GitHubPlanningIssueStore
from powdrr_service.planning.service import (
    create_planning_session,
    load_planning_session,
)

from powdrr_lift.workrr.github_issue_queue import QueueIssue


class _GitHubIssueClient:
    repository = "owner/repo"

    def __init__(self) -> None:
        self.label_checks = 0
        self.created: tuple[str, tuple[str, ...], str] | None = None
        self.issue = QueueIssue(12, "Planning", "body", "open", (), "issue-url")
        self.requested_issue_numbers: list[int] = []

    def ensure_planning_label(self) -> None:
        self.label_checks += 1

    def create_issue(
        self, *, body: str, labels: tuple[str, ...], title: str
    ) -> QueueIssue:
        self.created = (body, labels, title)
        self.issue = QueueIssue(12, title, body, "open", labels, "issue-url")
        return self.issue

    def get_issue(self, issue_number: int) -> QueueIssue:
        self.requested_issue_numbers.append(issue_number)
        return self.issue


def test_github_planning_issue_store_delegates_local_issue_effects() -> None:
    client = _GitHubIssueClient()
    store = GitHubPlanningIssueStore(client)  # type: ignore[arg-type]

    session = create_planning_session(
        store,
        plan_id="plan-12",
        base_commit="a" * 40,
        source_path="docs/plan.md",
        source_blob_sha="b" * 40,
        verification_command="make test",
    )
    resumed = load_planning_session(store, 12)

    assert store.repository == "owner/repo"
    assert client.label_checks == 1
    assert client.created is not None
    body, labels, title = client.created
    assert labels == ("powdrr:planning",)
    assert title == "Planning: plan-12"
    assert "planning-session-v1" in body
    assert session == resumed
    assert client.requested_issue_numbers == [12]
