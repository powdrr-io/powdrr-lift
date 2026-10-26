"""Durable coordinator for one bounded question in a planning session.

Planning order and deterministic assembly belong to a supplied state machine.
This module owns the production boundary around that state machine: a tracker
issue exists before the first question, accepted answers are journaled, and a
resume reconstructs the accepted answer set before asking for more work.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from powdrr_lift.workrr.github_issue_queue import (
    GitHubIssueClient,
    GitHubIssueQueueError,
    QueueIssue,
)
from powdrr_lift.workrr.planning.checkpoints import (
    CheckpointStore,
    PlanningCheckpoint,
    PlanningJournalError,
)
from powdrr_lift.workrr.planning.questions import (
    AcceptedQuestionAnswer,
    NeedsQuestionAnswer,
    QuestionRequest,
    run_question,
)
from powdrr_lift.workrr.procedrr_operations import WorkrrOperationExecutor
from powdrr_lift.workrr.protocol import WorkflowLLMClient

PLANNING_LABEL = "powdrr:planning"
_SESSION_MARKER = "<!-- powdrr-planning-session:v1 -->"
_PLAN_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,99}$")


class PlanningServiceError(RuntimeError):
    """A planning session cannot safely start, advance, or resume."""


@dataclass(frozen=True, slots=True)
class PlanningSession:
    """Pinned identity and immutable inputs for an initial planning session."""

    session_id: str
    plan_id: str
    repository: str
    base_commit: str
    source_path: str
    source_blob_sha: str
    verification_command: str
    issue: QueueIssue


@dataclass(frozen=True, slots=True)
class PlanningTransition:
    """One service operation result for the Procedrr coordinator."""

    status: str
    session_id: str
    issue_number: int
    question_id: str | None = None
    accepted_answer: Mapping[str, Any] | None = None
    error: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "session_id": self.session_id,
            "issue_number": self.issue_number,
            "question_id": self.question_id,
            "accepted_answer": (
                None if self.accepted_answer is None else dict(self.accepted_answer)
            ),
            "error": self.error,
        }


class PlanningStateMachine(Protocol):
    """Deterministic question order and state reduction for a planning stage."""

    def next_question(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> QuestionRequest[Any] | None: ...


@dataclass(frozen=True, slots=True)
class PlanningOptions:
    """Provider and per-question retry settings owned by the caller."""

    model: str
    max_transport_retries: int = 5
    transport_backoff_seconds: float = 1.0


def create_planning_session(
    client: GitHubIssueClient,
    *,
    plan_id: str,
    base_commit: str,
    source_path: str,
    source_blob_sha: str,
    verification_command: str,
) -> PlanningSession:
    """Create the non-queue tracker before any planning provider invocation."""
    if not _PLAN_ID.fullmatch(plan_id):
        raise PlanningServiceError("plan_id is invalid")
    if not re.fullmatch(r"[0-9a-f]{40,64}", base_commit):
        raise PlanningServiceError("base_commit must be a full Git object ID")
    if not source_path or source_path.startswith("/") or ".." in source_path.split("/"):
        raise PlanningServiceError("source_path must be a repository-relative path")
    if not re.fullmatch(r"[0-9a-f]{40,64}", source_blob_sha):
        raise PlanningServiceError("source_blob_sha must be a Git blob object ID")
    if not verification_command.strip():
        raise PlanningServiceError("verification_command must not be empty")
    session_id = str(uuid.uuid4())
    metadata = {
        "schema": "planning-session-v1",
        "session_id": session_id,
        "plan_id": plan_id,
        "repository": client.repository,
        "base_commit": base_commit,
        "source_path": source_path,
        "source_blob_sha": source_blob_sha,
        "verification_command": verification_command,
    }
    body = "\n".join(
        (
            "Powdrr planning session. This issue records planning progress only; "
            "it is not an executable work item.",
            "",
            _SESSION_MARKER,
            "```json",
            json.dumps(metadata, sort_keys=True, indent=2),
            "```",
        )
    )
    try:
        client.ensure_planning_label()
        issue = client.create_issue(
            title=f"Planning: {plan_id}", body=body, labels=[PLANNING_LABEL]
        )
    except GitHubIssueQueueError:
        raise
    return PlanningSession(
        session_id,
        plan_id,
        client.repository,
        base_commit,
        source_path,
        source_blob_sha,
        verification_command,
        issue,
    )


def load_planning_session(
    client: GitHubIssueClient,
    issue_number: int,
    *,
    expected_repository: str | None = None,
) -> PlanningSession:
    """Read a session by explicit issue number; never search all repository issues."""
    issue = client.get_issue(issue_number)
    if PLANNING_LABEL not in issue.labels:
        raise PlanningServiceError(
            f"issue #{issue_number} is not labeled {PLANNING_LABEL}"
        )
    metadata = _session_metadata(issue.body)
    if metadata.get("repository") != client.repository:
        raise PlanningServiceError("planning issue belongs to a different repository")
    if expected_repository is not None and expected_repository != client.repository:
        raise PlanningServiceError("requested repository differs from GitHub client")
    session_id = metadata.get("session_id")
    plan_id = metadata.get("plan_id")
    base_commit = metadata.get("base_commit")
    source_path = metadata.get("source_path")
    source_blob_sha = metadata.get("source_blob_sha")
    verification_command = metadata.get("verification_command")
    if not all(
        isinstance(item, str) and item
        for item in (
            session_id,
            plan_id,
            base_commit,
            source_path,
            source_blob_sha,
            verification_command,
        )
    ):
        raise PlanningServiceError("planning issue metadata is incomplete")
    assert isinstance(session_id, str)
    assert isinstance(plan_id, str)
    assert isinstance(base_commit, str)
    assert isinstance(source_path, str)
    assert isinstance(source_blob_sha, str)
    assert isinstance(verification_command, str)
    if not re.fullmatch(r"[0-9a-f]{40,64}", base_commit):
        raise PlanningServiceError("planning issue base commit is invalid")
    if not re.fullmatch(r"[0-9a-f]{40,64}", source_blob_sha):
        raise PlanningServiceError("planning issue source blob digest is invalid")
    return PlanningSession(
        session_id,
        plan_id,
        client.repository,
        base_commit,
        source_path,
        source_blob_sha,
        verification_command,
        issue,
    )


def advance_planning_session(
    *,
    session: PlanningSession,
    state_machine: PlanningStateMachine,
    checkpoint_store: CheckpointStore,
    client: WorkflowLLMClient,
    options: PlanningOptions,
    stderr: Any,
) -> PlanningTransition:
    """Run and durably record at most one bounded question transition."""
    journal = checkpoint_store.load()
    if journal.status not in {"empty", "active"}:
        raise PlanningJournalError(
            f"planning journal is {journal.status}; select a valid checkpoint "
            "before continuing"
        )
    accepted: dict[str, Mapping[str, Any]] = {}
    for event in journal.events:
        if event.event_type != "answer_accepted":
            continue
        question = event.payload.get("question")
        answer = event.payload.get("accepted_answer")
        if isinstance(question, Mapping) and isinstance(answer, Mapping):
            question_id = question.get("question_id")
            if isinstance(question_id, str):
                accepted[question_id] = dict(answer)

    request = state_machine.next_question(accepted)
    if request is None:
        return PlanningTransition("complete", session.session_id, session.issue.number)
    request.validate()
    question = request.question
    if question.question_id in accepted:
        # The state machine must not ask a stable question again with changed inputs.
        existing = next(
            event
            for event in reversed(journal.events)
            if event.event_type == "answer_accepted"
            and isinstance(event.payload.get("question"), Mapping)
            and event.payload["question"].get("question_id")
            == question.question_id
        )
        if existing.payload.get("input_digest") != question.input_digest:
            raise PlanningServiceError(
                f"accepted question {question.question_id!r} changed its input digest"
            )
        return PlanningTransition(
            "question_accepted",
            session.session_id,
            session.issue.number,
            question.question_id,
            accepted[question.question_id],
        )

    parent = journal.head.digest if journal.head is not None else None
    sequence = 1 if journal.head is None else journal.head.sequence + 1
    started = PlanningCheckpoint.question_started(
        session_id=session.session_id,
        event_id=f"question-started-{sequence:06d}",
        sequence=sequence,
        parent_digest=parent,
        question=question,
        id_counters=journal.id_counters,
    )
    journal = checkpoint_store.append(started, expected_parent_digest=parent)
    result = run_question(
        client,
        request,
        model=options.model,
        stderr=stderr,
        max_transport_retries=options.max_transport_retries,
        transport_backoff_seconds=options.transport_backoff_seconds,
    )
    head = journal.head
    if head is None:
        raise PlanningJournalError("question start checkpoint was not persisted")
    next_sequence = head.sequence + 1
    if isinstance(result, NeedsQuestionAnswer):
        checkpoint = PlanningCheckpoint.needs_answer(
            session_id=session.session_id,
            event_id=f"needs-answer-{next_sequence:06d}",
            sequence=next_sequence,
            parent_digest=head.digest,
            question=result.question,
            error=result.error,
            previous_answer=result.previous_answer,
            id_counters=journal.id_counters,
        )
        checkpoint_store.append(checkpoint, expected_parent_digest=head.digest)
        return PlanningTransition(
            "needs_answer",
            session.session_id,
            session.issue.number,
            result.question_id,
            error=str(result.error),
        )

    assert isinstance(result, AcceptedQuestionAnswer)
    checkpoint = PlanningCheckpoint.answer_accepted(
        session_id=session.session_id,
        event_id=f"answer-accepted-{next_sequence:06d}",
        sequence=next_sequence,
        parent_digest=head.digest,
        question=result.question,
        accepted_answer=result.raw_answer,
        validation_result={"accepted": True, "attempts": result.attempts},
        id_counters=journal.id_counters,
    )
    checkpoint_store.append(checkpoint, expected_parent_digest=head.digest)
    return PlanningTransition(
        "question_accepted",
        session.session_id,
        session.issue.number,
        result.question_id,
        result.raw_answer,
    )


def planning_operation_executor(
    *,
    session: PlanningSession,
    state_machine: PlanningStateMachine,
    checkpoint_store: CheckpointStore,
    client: WorkflowLLMClient,
    options: PlanningOptions,
    stderr: Any,
) -> WorkrrOperationExecutor:
    """Register the two narrow planning commands used by plan-execution."""

    def dispatch(parameters: Mapping[str, Any]) -> Mapping[str, Any]:
        command = parameters.get("command")
        if not isinstance(command, list) or len(command) != 1:
            raise PlanningServiceError("planning operation requires one command")
        if (
            parameters.get("session_id") != session.session_id
            or parameters.get("issue_number") != session.issue.number
        ):
            raise PlanningServiceError(
                "Procedrr inputs do not match the authorized planning session"
            )
        if command[0] == "planning_initialize":
            journal = checkpoint_store.load()
            if journal.status not in {"empty", "active"}:
                raise PlanningJournalError(
                    f"planning journal is {journal.status}; select a valid checkpoint"
                )
            accepted: dict[str, Mapping[str, Any]] = {}
            for event in journal.events:
                question = event.payload.get("question")
                answer = event.payload.get("accepted_answer")
                if (
                    event.event_type == "answer_accepted"
                    and isinstance(question, Mapping)
                    and isinstance(answer, Mapping)
                ):
                    question_id = question.get("question_id")
                    if isinstance(question_id, str):
                        accepted[question_id] = dict(answer)
            status = (
                "complete"
                if state_machine.next_question(accepted) is None
                else "question_accepted"
            )
            return PlanningTransition(
                status, session.session_id, session.issue.number
            ).to_data()
        if command[0] == "planning_advance":
            return advance_planning_session(
                session=session,
                state_machine=state_machine,
                checkpoint_store=checkpoint_store,
                client=client,
                options=options,
                stderr=stderr,
            ).to_data()
        raise PlanningServiceError(f"unsupported planning operation {command[0]!r}")

    return WorkrrOperationExecutor({"internal": dispatch})


def _session_metadata(body: str) -> Mapping[str, Any]:
    if _SESSION_MARKER not in body:
        raise PlanningServiceError("issue does not contain planning session metadata")
    lines = body.splitlines()
    try:
        marker = lines.index(_SESSION_MARKER)
        start = lines.index("```json", marker) + 1
        end = lines.index("```", start)
        value = json.loads("\n".join(lines[start:end]))
    except (ValueError, json.JSONDecodeError) as error:
        raise PlanningServiceError("planning issue metadata is malformed") from error
    if not isinstance(value, Mapping) or value.get("schema") != "planning-session-v1":
        raise PlanningServiceError("unsupported planning session metadata")
    return value


__all__ = [
    "PLANNING_LABEL",
    "PlanningOptions",
    "PlanningServiceError",
    "PlanningSession",
    "PlanningStateMachine",
    "PlanningTransition",
    "advance_planning_session",
    "create_planning_session",
    "load_planning_session",
    "planning_operation_executor",
]
