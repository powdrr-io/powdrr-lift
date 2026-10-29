"""Durable coordinator for one bounded question in a planning session.

Planning order and deterministic assembly belong to a supplied state machine.
This module owns the production boundary around that state machine: a tracker
issue exists before the first question, accepted answers are journaled, and a
resume reconstructs the accepted answer set before asking for more work.
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any, Protocol

from powdrr_lift.workrr.github_issue_queue import (
    GitHubIssueClient,
    GitHubIssueQueueError,
    QueueIssue,
)
from powdrr_lift.workrr.procedrr import WorkrrProcedrrClient
from powdrr_lift.workrr.procedrr_operations import WorkrrOperationExecutor
from powdrr_lift.workrr.protocol import WorkflowLLMClient
from powdrr_service.planning.checkpoints import (
    CheckpointStore,
    PlanningCheckpoint,
    PlanningJournalError,
)
from powdrr_service.planning.questions import (
    AcceptedQuestionAnswer,
    NeedsQuestionAnswer,
    QuestionRequest,
    run_question,
)

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
    question_ids: tuple[str, ...] = ()
    accepted_answers: tuple[Mapping[str, Any], ...] = ()

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
            "question_ids": list(self.question_ids),
            "accepted_answers": [dict(answer) for answer in self.accepted_answers],
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
    max_concurrent_questions: int = 4
    use_provider_schema: bool = True

    def __post_init__(self) -> None:
        if self.max_concurrent_questions < 1:
            raise ValueError("max_concurrent_questions must be positive")


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
    oversized_work_unit_groups: list[tuple[str, ...]] = []
    for event in journal.events:
        question = event.payload.get("question")
        if not isinstance(question, Mapping):
            continue
        question_id = question.get("question_id")
        if not isinstance(question_id, str):
            continue
        if event.event_type == "answer_accepted":
            answer = event.payload.get("accepted_answer")
            if isinstance(answer, Mapping):
                accepted[question_id] = dict(answer)
        elif event.event_type == "needs_answer":
            accepted.pop(question_id, None)
            validation_result = event.payload.get("validation_result")
            subject_ids = question.get("subject_ids")
            if (
                question.get("kind") == "propose_work_units"
                and isinstance(validation_result, Mapping)
                and isinstance(validation_result.get("message"), str)
                and validation_result["message"].startswith("serialized answer is ")
                and isinstance(subject_ids, list)
                and all(isinstance(item, str) for item in subject_ids)
            ):
                oversized_work_unit_groups.append(tuple(subject_ids))

    set_oversized_groups = getattr(
        state_machine, "set_oversized_work_unit_groups", None
    )
    if callable(set_oversized_groups):
        set_oversized_groups(oversized_work_unit_groups)

    ready_questions = getattr(state_machine, "ready_questions", None)
    if callable(ready_questions):
        requests = tuple(ready_questions(accepted))
    else:
        request = state_machine.next_question(accepted)
        requests = () if request is None else (request,)
    if not requests:
        return PlanningTransition("complete", session.session_id, session.issue.number)
    invalid_accepted_ids = getattr(
        state_machine, "invalid_accepted_question_ids", frozenset()
    )
    for question_id in invalid_accepted_ids:
        accepted.pop(question_id, None)
    # Bound each durable transition even when many independent questions are
    # ready. The next Procedrr iteration can checkpoint this window and
    # recompute the remaining frontier before asking more questions.
    requests = requests[: options.max_concurrent_questions]
    question_ids = [request.question.question_id for request in requests]
    if len(question_ids) != len(set(question_ids)):
        raise PlanningServiceError("planning frontier contains duplicate question IDs")
    for request in requests:
        request.validate()
        question = request.question
        if question.question_id not in accepted:
            continue
        existing = next(
            event
            for event in reversed(journal.events)
            if event.event_type == "answer_accepted"
            and isinstance(event.payload.get("question"), Mapping)
            and event.payload["question"].get("question_id") == question.question_id
        )
        if existing.payload.get("input_digest") != question.input_digest:
            raise PlanningServiceError(
                f"accepted question {question.question_id!r} changed its input digest"
            )
        raise PlanningServiceError(
            f"planning state machine did not advance past accepted question "
            f"{question.question_id!r}"
        )

    outcomes = _run_question_batch(
        requests,
        client=client,
        options=options,
        stderr=stderr,
    )
    parent = journal.head.digest if journal.head is not None else None
    sequence = 1 if journal.head is None else journal.head.sequence + 1
    accepted_results: list[AcceptedQuestionAnswer[Any]] = []
    needs_answers: list[NeedsQuestionAnswer] = []
    errors: list[Exception] = []
    for _request, result, error in outcomes:
        if error is not None:
            errors.append(error)
        elif isinstance(result, AcceptedQuestionAnswer):
            accepted_results.append(result)
        elif isinstance(result, NeedsQuestionAnswer):
            needs_answers.append(result)

    def append_checkpoint(checkpoint: PlanningCheckpoint) -> None:
        nonlocal parent, sequence
        state = checkpoint_store.append(checkpoint, expected_parent_digest=parent)
        parent = state.head.digest if state.head is not None else None
        sequence = 1 if state.head is None else state.head.sequence + 1

    # Results are committed in frontier order through one writer. The providers
    # run concurrently, but checkpoint heads never race.
    for result in accepted_results:
        append_checkpoint(
            PlanningCheckpoint.answer_accepted(
                session_id=session.session_id,
                event_id=f"answer-accepted-{sequence:06d}",
                sequence=sequence,
                parent_digest=parent,
                question=result.question,
                accepted_answer=result.raw_answer,
                validation_result={"accepted": True, "attempts": result.attempts},
                id_counters=journal.id_counters,
            )
        )
    if needs_answers:
        result = needs_answers[0]
        append_checkpoint(
            PlanningCheckpoint.needs_answer(
                session_id=session.session_id,
                event_id=f"needs-answer-{sequence:06d}",
                sequence=sequence,
                parent_digest=parent,
                question=result.question,
                error=result.error,
                previous_answer=result.previous_answer,
                id_counters=journal.id_counters,
            )
        )
    if errors:
        raise errors[0]

    accepted_ids = tuple(result.question_id for result in accepted_results)
    selected_needs_answer = needs_answers[0] if needs_answers else None
    selected_question_id = (
        selected_needs_answer.question_id
        if selected_needs_answer is not None
        else (accepted_ids[0] if accepted_ids else None)
    )
    return PlanningTransition(
        "needs_answer" if selected_needs_answer is not None else "question_accepted",
        session.session_id,
        session.issue.number,
        selected_question_id,
        (None if not accepted_results else accepted_results[0].raw_answer),
        None if selected_needs_answer is None else str(selected_needs_answer.error),
        question_ids=tuple(question_ids),
        accepted_answers=tuple(result.raw_answer for result in accepted_results),
    )


def _run_question_batch(
    requests: tuple[QuestionRequest[Any], ...],
    *,
    client: WorkflowLLMClient,
    options: PlanningOptions,
    stderr: Any,
) -> tuple[tuple[QuestionRequest[Any], Any, Exception | None], ...]:
    """Execute ready questions concurrently while keeping progress output ordered."""

    def run_one(
        request: QuestionRequest[Any],
    ) -> tuple[QuestionRequest[Any], Any, Exception | None, str]:
        output = StringIO()
        try:
            result = run_question(
                client,
                request,
                model=options.model,
                stderr=output,
                max_transport_retries=options.max_transport_retries,
                transport_backoff_seconds=options.transport_backoff_seconds,
                use_provider_schema=options.use_provider_schema,
            )
            return request, result, None, output.getvalue()
        except Exception as error:
            return request, None, error, output.getvalue()

    with ThreadPoolExecutor(
        max_workers=min(options.max_concurrent_questions, len(requests))
    ) as executor:
        futures = [executor.submit(run_one, request) for request in requests]
        completed = [future.result() for future in futures]

    output_stream = stderr if stderr is not None else sys.stdout
    for _request, _result, _error, output in completed:
        if output:
            output_stream.write(output)
    flush = getattr(output_stream, "flush", None)
    if callable(flush):
        flush()
    return tuple((request, result, error) for request, result, error, _ in completed)


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


def run_planning_procedrr(
    *,
    process_directory: str | Path,
    session: PlanningSession,
    state_machine: PlanningStateMachine,
    checkpoint_store: CheckpointStore,
    client: WorkflowLLMClient,
    options: PlanningOptions,
    stderr: Any,
) -> Any:
    """Execute the bundled plan process around the durable question service."""
    from procedrr import ProcessCatalog
    from procedrr_evaluator.runtime import ProcedrrExecutionService

    catalog = ProcessCatalog.load(Path(process_directory))
    process_client = WorkrrProcedrrClient(
        client, process_directory=Path(process_directory)
    )
    executor = planning_operation_executor(
        session=session,
        state_machine=state_machine,
        checkpoint_store=checkpoint_store,
        client=client,
        options=options,
        stderr=stderr,
    )
    return ProcedrrExecutionService(catalog, process_client, executor).execute(
        "plan-execution",
        {"session_id": session.session_id, "issue_number": session.issue.number},
    )


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
    "run_planning_procedrr",
]
