"""GitHub issue adapter for planning session metadata and tracker creation."""

from __future__ import annotations

from powdrr_service.planning.service import PlanningIssueStore

from powdrr_lift.workrr.github_issue_queue import GitHubIssueClient, QueueIssue


class GitHubPlanningIssueStore(PlanningIssueStore):
    """Keep planning tracker API effects in the local package."""

    def __init__(self, client: GitHubIssueClient) -> None:
        self._client = client

    @property
    def repository(self) -> str:
        return self._client.repository

    def ensure_planning_label(self) -> None:
        self._client.ensure_planning_label()

    def create_issue(
        self, *, body: str, labels: tuple[str, ...], title: str
    ) -> QueueIssue:
        return self._client.create_issue(body=body, labels=labels, title=title)

    def get_issue(self, issue_number: int) -> QueueIssue:
        return self._client.get_issue(issue_number)


__all__ = ["GitHubPlanningIssueStore"]
