from __future__ import annotations

import json

from powdrr_contracts.models import OperationRequest
from powdrr_local import JsonServiceBridge, LocalPowdrrService
from powdrr_local.planning.sessions import GitHubPlanningIssueStore
from powdrr_service.planning.service import (
    PlanningSession,
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


def test_plan_session_create_and_resume_cross_the_json_service_boundary() -> None:
    client = _GitHubIssueClient()
    store = GitHubPlanningIssueStore(client)  # type: ignore[arg-type]
    bridge = JsonServiceBridge(LocalPowdrrService(planning_issue_store=store))

    created = bridge.start(
        OperationRequest(
            request_id="plan-create",
            operation_kind="plan",
            inputs={
                "action": "create_session",
                "repository_id": "owner/repo",
                "plan_id": "plan-12",
                "base_commit": "a" * 40,
                "source_path": "docs/plan.md",
                "source_blob_sha": "b" * 40,
                "verification_command": "make test",
            },
        )
    )

    assert created.status == "succeeded"
    assert created.progress == ({"phase": "planning_session_created"},)
    created_session = PlanningSession.from_data(
        json.loads(created.output_artifacts[0].data)
    )
    assert created_session.plan_id == "plan-12"
    assert created_session.issue.number == 12

    resumed = bridge.start(
        OperationRequest(
            request_id="plan-resume",
            operation_kind="plan",
            inputs={
                "action": "load_session",
                "repository_id": "owner/repo",
                "issue_number": 12,
            },
        )
    )

    assert resumed.status == "succeeded"
    assert resumed.progress == ({"phase": "planning_session_loaded"},)
    resumed_session = PlanningSession.from_data(
        json.loads(resumed.output_artifacts[0].data)
    )
    assert resumed_session.session_id == created_session.session_id
    assert resumed_session.plan_id == created_session.plan_id
    assert resumed_session.repository == created_session.repository
    assert resumed_session.base_commit == created_session.base_commit
    assert resumed_session.source_path == created_session.source_path
    assert resumed_session.source_blob_sha == created_session.source_blob_sha
    assert resumed_session.verification_command == created_session.verification_command
    assert resumed_session.issue.number == created_session.issue.number
    assert client.label_checks == 1
    assert client.requested_issue_numbers == [12]


def test_plan_session_operation_rejects_mismatched_repository() -> None:
    store = GitHubPlanningIssueStore(_GitHubIssueClient())  # type: ignore[arg-type]
    update = JsonServiceBridge(LocalPowdrrService(planning_issue_store=store)).start(
        OperationRequest(
            request_id="plan-repo-mismatch",
            operation_kind="plan",
            inputs={
                "action": "load_session",
                "repository_id": "other/repo",
                "issue_number": 12,
            },
        )
    )

    assert update.status == "failed"
    assert update.failure is not None
    assert update.failure.category == "planning_session_failed"
    assert "adapter repository differs" in update.failure.message
