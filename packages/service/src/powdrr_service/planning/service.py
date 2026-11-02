"""Durable coordinator for one bounded question in a planning session.

Planning order and deterministic assembly belong to a supplied state machine.
This module owns the production boundary around that state machine: a tracker
issue exists before the first question, accepted answers are journaled, and a
resume reconstructs the accepted answer set before asking for more work.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any, Protocol

from powdrr_lift.workrr.procedrr import WorkrrProcedrrClient
from powdrr_lift.workrr.procedrr_operations import WorkrrOperationExecutor
from powdrr_lift.workrr.protocol import WorkflowLLMClient
from powdrr_service.planning.checkpoints import (
    CheckpointStore,
    PlanningCheckpoint,
    PlanningJournalError,
)
from powdrr_service.planning.contracts import PlanningContractError
from powdrr_service.planning.questions import (
    AcceptedQuestionAnswer,
    NeedsQuestionAnswer,
    QuestionRequest,
    build_question_messages,
    run_question,
)

PLANNING_LABEL = "powdrr:planning"
_SESSION_MARKER = "<!-- powdrr-planning-session:v1 -->"
_PLAN_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,99}$")


class PlanningServiceError(RuntimeError):
    """A planning session cannot safely start, advance, or resume."""


class PlanningIssue(Protocol):
    """Repository issue data needed by the planning coordinator."""

    @property
    def number(self) -> int: ...

    @property
    def title(self) -> str: ...

    @property
    def body(self) -> str: ...

    @property
    def state(self) -> str: ...

    @property
    def labels(self) -> tuple[str, ...]: ...

    @property
    def url(self) -> str: ...


@dataclass(frozen=True, slots=True)
class PlanningIssueRecord:
    """Public issue fields needed to resume a planning session."""

    number: int
    title: str
    body: str
    state: str
    labels: tuple[str, ...]
    url: str


class PlanningIssueStore(Protocol):
    """Issue operations supplied by a local persistence adapter."""

    @property
    def repository(self) -> str: ...

    def ensure_planning_label(self) -> None: ...

    def create_issue(
        self, *, body: str, labels: tuple[str, ...], title: str
    ) -> PlanningIssue: ...

    def get_issue(self, issue_number: int) -> PlanningIssue: ...


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
    issue: PlanningIssue

    def to_data(self) -> dict[str, Any]:
        return {
            "schema": "planning-session-record-v1",
            "session_id": self.session_id,
            "plan_id": self.plan_id,
            "repository": self.repository,
            "base_commit": self.base_commit,
            "source_path": self.source_path,
            "source_blob_sha": self.source_blob_sha,
            "verification_command": self.verification_command,
            "issue": {
                "number": self.issue.number,
                "title": self.issue.title,
                "body": self.issue.body,
                "state": self.issue.state,
                "labels": list(self.issue.labels),
                "url": self.issue.url,
            },
        }

    @classmethod
    def from_data(cls, value: object) -> PlanningSession:
        if (
            not isinstance(value, Mapping)
            or value.get("schema") != "planning-session-record-v1"
        ):
            raise PlanningServiceError(
                "planning session operation returned malformed data"
            )
        issue = value.get("issue")
        string_fields = (
            "session_id",
            "plan_id",
            "repository",
            "base_commit",
            "source_path",
            "source_blob_sha",
            "verification_command",
        )
        if any(not isinstance(value.get(field), str) for field in string_fields):
            raise PlanningServiceError(
                "planning session operation returned malformed data"
            )
        if (
            not isinstance(issue, Mapping)
            or not isinstance(issue.get("number"), int)
            or isinstance(issue.get("number"), bool)
            or not all(
                isinstance(issue.get(field), str)
                for field in ("title", "body", "state", "url")
            )
            or not isinstance(issue.get("labels"), list)
            or not all(isinstance(label, str) for label in issue["labels"])
        ):
            raise PlanningServiceError(
                "planning session operation returned malformed data"
            )
        record = PlanningIssueRecord(
            number=issue["number"],
            title=issue["title"],
            body=issue["body"],
            state=issue["state"],
            labels=tuple(issue["labels"]),
            url=issue["url"],
        )
        return cls(
            session_id=value["session_id"],
            plan_id=value["plan_id"],
            repository=value["repository"],
            base_commit=value["base_commit"],
            source_path=value["source_path"],
            source_blob_sha=value["source_blob_sha"],
            verification_command=value["verification_command"],
            issue=record,
        )


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


def _replay_planning_answers(
    events: Sequence[Any],
) -> tuple[dict[str, Mapping[str, Any]], dict[str, str], list[tuple[str, ...]]]:
    accepted: dict[str, Mapping[str, Any]] = {}
    input_digests: dict[str, str] = {}
    oversized_work_unit_groups: list[tuple[str, ...]] = []
    for event in events:
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
                input_digest = question.get("input_digest")
                if isinstance(input_digest, str):
                    input_digests[question_id] = input_digest
        elif event.event_type == "needs_answer":
            accepted.pop(question_id, None)
            input_digests.pop(question_id, None)
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
    return accepted, input_digests, oversized_work_unit_groups


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
    provider: str = "unknown"
    telemetry_sink: Callable[[Mapping[str, Any]], None] | None = None
    deterministic_telemetry_sink: Callable[[Mapping[str, Any]], None] | None = None

    def __post_init__(self) -> None:
        if self.max_concurrent_questions < 1:
            raise ValueError("max_concurrent_questions must be positive")


class _QuestionTelemetryClient:
    """Per-question client view that captures usage without shared-state races."""

    def __init__(self, client: WorkflowLLMClient) -> None:
        self._client = client
        self.prompt_chars = 0
        self.response_chars = 0
        self.api_calls = 0
        self.provider_failures = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.has_prompt_tokens = False
        self.has_completion_tokens = False

    def complete_json(self, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        self.prompt_chars += sum(
            len(message.get("content", "")) for message in messages
        )
        self.api_calls += 1
        try:
            parameters = inspect.signature(self._client.complete_json).parameters
            supports_schema = "response_schema" in parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
            answer = (
                self._client.complete_json(messages, **kwargs)
                if supports_schema and kwargs
                else self._client.complete_json(messages)
            )
        except Exception:
            self.provider_failures += 1
            raise
        self.response_chars += len(
            json.dumps(answer, ensure_ascii=False, separators=(",", ":"))
        )
        usage = getattr(self._client, "last_usage", None)
        if isinstance(usage, Mapping):
            prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
            completion = usage.get("completion_tokens", usage.get("output_tokens"))
            if isinstance(prompt, int) and not isinstance(prompt, bool):
                self.prompt_tokens += prompt
                self.has_prompt_tokens = True
            if isinstance(completion, int) and not isinstance(completion, bool):
                self.completion_tokens += completion
                self.has_completion_tokens = True
        return answer


def create_planning_session(
    client: PlanningIssueStore,
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
    client.ensure_planning_label()
    issue = client.create_issue(
        title=f"Planning: {plan_id}", body=body, labels=(PLANNING_LABEL,)
    )
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
    client: PlanningIssueStore,
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
    accepted, accepted_input_digests, oversized_work_unit_groups = (
        _replay_planning_answers(journal.events)
    )

    set_oversized_groups = getattr(
        state_machine, "set_oversized_work_unit_groups", None
    )
    if callable(set_oversized_groups):
        set_oversized_groups(oversized_work_unit_groups)

    set_accepted_input_digests = getattr(
        state_machine, "set_accepted_question_input_digests", None
    )
    if callable(set_accepted_input_digests):
        set_accepted_input_digests(accepted_input_digests)

    parent = journal.head.digest if journal.head is not None else None
    sequence = 1 if journal.head is None else journal.head.sequence + 1

    def append_checkpoint(checkpoint: PlanningCheckpoint) -> None:
        nonlocal parent, sequence
        state = checkpoint_store.append(checkpoint, expected_parent_digest=parent)
        parent = state.head.digest if state.head is not None else None
        sequence = 1 if state.head is None else state.head.sequence + 1

    derive_deterministic_answers = getattr(state_machine, "deterministic_answers", None)
    if callable(derive_deterministic_answers):
        for deterministic in derive_deterministic_answers(accepted):
            question = deterministic["question"]
            answer = deterministic["answer"]
            if question.question_id in accepted:
                continue
            if not isinstance(answer, Mapping):
                raise PlanningServiceError(
                    "deterministic planning answer must be an object"
                )
            append_checkpoint(
                PlanningCheckpoint.answer_accepted(
                    session_id=session.session_id,
                    event_id=f"deterministic-answer-{sequence:06d}",
                    sequence=sequence,
                    parent_digest=parent,
                    question=question,
                    accepted_answer=answer,
                    validation_result={
                        "accepted": True,
                        "origin": "deterministic",
                        "rule_id": deterministic["rule_id"],
                        "reason": deterministic["reason"],
                    },
                    id_counters=journal.id_counters,
                )
            )
            if options.deterministic_telemetry_sink is not None:
                try:
                    options.deterministic_telemetry_sink(
                        {
                            "question_id": question.question_id,
                            "question_kind": question.kind,
                            "phase": _question_phase(question.kind),
                            "input_digest": question.input_digest,
                            "rule_id": deterministic["rule_id"],
                        }
                    )
                except Exception as telemetry_error:
                    if stderr is not None:
                        stderr.write(
                            "Planning deterministic telemetry could not be "
                            f"recorded: {telemetry_error}\n"
                        )
            accepted[question.question_id] = dict(answer)
            accepted_input_digests[question.question_id] = question.input_digest
        if callable(set_accepted_input_digests):
            set_accepted_input_digests(accepted_input_digests)

    ready_questions = getattr(state_machine, "ready_questions", None)
    if callable(ready_questions):
        requests = tuple(ready_questions(accepted))
    else:
        request = state_machine.next_question(accepted)
        requests = () if request is None else (request,)
    if not requests:
        return PlanningTransition("complete", session.session_id, session.issue.number)
    invalid_accepted_ids: frozenset[str] = getattr(
        state_machine, "invalid_accepted_question_ids", frozenset()
    )
    for question_id in invalid_accepted_ids:
        accepted.pop(question_id, None)
    # Bound each durable transition even when many independent questions are
    # ready. The next Procedrr iteration can checkpoint this window and
    # recompute the remaining frontier before asking more questions.
    requests = requests[: options.max_concurrent_questions]
    question_ids = [
        member.question.question_id
        for request in requests
        for member in (request.batch_members or (request,))
    ]
    if len(question_ids) != len(set(question_ids)):
        raise PlanningServiceError("planning frontier contains duplicate question IDs")
    for request in requests:
        request.validate()
        for member in request.batch_members or (request,):
            question = member.question
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
                    f"accepted question {question.question_id!r} changed its input "
                    "digest"
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
        prior_events=journal.events,
    )
    accepted_results: list[AcceptedQuestionAnswer[Any]] = []
    needs_answers: list[NeedsQuestionAnswer] = []
    errors: list[Exception] = []
    for request, result, error in outcomes:
        if error is not None:
            errors.append(error)
        elif isinstance(result, AcceptedQuestionAnswer):
            if request.batch_members:
                raw_answers = result.raw_answer.get("answers")
                if not isinstance(raw_answers, Mapping):
                    errors.append(
                        PlanningServiceError("question batch returned no answer map")
                    )
                    continue
                for member in request.batch_members:
                    raw_answer = raw_answers.get(member.question.question_id)
                    if not isinstance(raw_answer, Mapping):
                        errors.append(
                            PlanningServiceError(
                                f"question batch omitted {member.question.question_id}"
                            )
                        )
                        continue
                    try:
                        answer = member.parse_answer(raw_answer)
                    except Exception as error:
                        needs_answers.append(
                            NeedsQuestionAnswer(
                                member.question,
                                result.attempts,
                                error
                                if isinstance(error, PlanningContractError)
                                else PlanningContractError("/", str(error)),
                                dict(raw_answer),
                            )
                        )
                    else:
                        accepted_results.append(
                            AcceptedQuestionAnswer(
                                member.question,
                                result.attempts,
                                answer,
                                dict(raw_answer),
                            )
                        )
            else:
                accepted_results.append(result)
        elif isinstance(result, NeedsQuestionAnswer):
            needs_answers.append(result)

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
    prior_events: Sequence[Any] = (),
) -> tuple[tuple[QuestionRequest[Any], Any, Exception | None], ...]:
    """Execute ready questions concurrently while keeping progress output ordered."""

    def run_one(
        request: QuestionRequest[Any],
    ) -> tuple[QuestionRequest[Any], Any, Exception | None, str, dict[str, Any]]:
        output = StringIO()
        started = time.monotonic()
        telemetry_client = _QuestionTelemetryClient(client)
        error: Exception | None = None
        request_chars = None
        try:
            initial_messages = build_question_messages(
                request, schema_in_response=options.use_provider_schema
            )
            request_chars = sum(
                len(message.get("content", "")) for message in initial_messages
            )
        except Exception:
            # The normal question runner owns validation and error reporting.
            # Telemetry must never change its result.
            pass
        try:
            result = run_question(
                telemetry_client,
                request,
                model=options.model,
                stderr=output,
                max_transport_retries=options.max_transport_retries,
                transport_backoff_seconds=options.transport_backoff_seconds,
                use_provider_schema=options.use_provider_schema,
            )
            error = None
        except Exception as caught:
            error = caught
            result = None
        raw_answer = (
            result.raw_answer
            if isinstance(result, AcceptedQuestionAnswer)
            else result.previous_answer
            if isinstance(result, NeedsQuestionAnswer)
            else None
        )
        question = request.question
        members = request.batch_members or (request,)
        raw_answers = (
            raw_answer.get("answers")
            if isinstance(raw_answer, Mapping)
            and isinstance(raw_answer.get("answers"), Mapping)
            else None
        )
        pagination = []
        for member in members:
            member_answer = (
                raw_answers.get(member.question.question_id)
                if raw_answers is not None
                else raw_answer
            )
            pagination_metric = _pagination_metric(member, member_answer, prior_events)
            if pagination_metric is not None:
                pagination.append(pagination_metric)
        metric = {
            "question_ids": [member.question.question_id for member in members],
            "question_kinds": sorted({member.question.kind for member in members}),
            "phase": _question_phase(members[0].question.kind),
            "input_digests": [member.question.input_digest for member in members],
            "batch_member_count": len(members),
            "request_chars": request_chars,
            "request_chars_all_attempts": telemetry_client.prompt_chars,
            "answer_chars": telemetry_client.response_chars
            or (
                None
                if raw_answer is None
                else len(
                    json.dumps(raw_answer, ensure_ascii=False, separators=(",", ":"))
                )
            ),
            "attempts": getattr(result, "attempts", None),
            "validation_failures": max(0, getattr(result, "attempts", 1) - 1),
            "api_calls": telemetry_client.api_calls,
            "provider_failures": telemetry_client.provider_failures,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "provider": options.provider,
            "model": options.model,
            "outcome": (
                "error"
                if error is not None
                else "accepted"
                if isinstance(result, AcceptedQuestionAnswer)
                else "needs_answer"
            ),
            "deterministic": False,
            "input_digest": question.input_digest if len(members) == 1 else None,
            "prompt_tokens": (
                telemetry_client.prompt_tokens
                if telemetry_client.has_prompt_tokens
                else None
            ),
            "completion_tokens": (
                telemetry_client.completion_tokens
                if telemetry_client.has_completion_tokens
                else None
            ),
            "pagination": pagination,
        }
        return request, result, error, output.getvalue(), metric

    with ThreadPoolExecutor(
        max_workers=min(options.max_concurrent_questions, len(requests))
    ) as executor:
        futures = [executor.submit(run_one, request) for request in requests]
        completed = [future.result() for future in futures]

    output_stream = stderr if stderr is not None else sys.stdout
    for _request, _result, _error, output, metric in completed:
        if output:
            output_stream.write(output)
        if options.telemetry_sink is not None:
            try:
                options.telemetry_sink(metric)
            except Exception as telemetry_error:
                output_stream.write(
                    f"Planning telemetry could not be recorded: {telemetry_error}\n"
                )
    flush = getattr(output_stream, "flush", None)
    if callable(flush):
        flush()
    return tuple(
        (request, result, error)
        for request, result, error, _output, _metric in completed
    )


def _question_phase(kind: str) -> str:
    """Map typed planning questions to the planner phase they measure."""
    if kind in {
        "extract_requirements",
        "classify_source_block",
        "check_requirement_fidelity",
        "compare_requirement_equivalence",
    }:
        return "source-ledger"
    if kind in {"assess_requirement_coverage", "assess_setup_state"}:
        return "repository-coverage"
    if kind in {
        "propose_work_units",
        "check_unit_cohesion",
        "describe_human_setup",
        "requires_human_setup",
    }:
        return "work-unit-design"
    if kind in {
        "describe_item",
        "resolve_prerequisite_output",
        "confirm_dependency",
        "review_dependency_graph",
    }:
        return "dependency-and-pr-slicing"
    if kind in {
        "enumerate_structural_subjects",
        "describe_structural_change",
        "check_validation_coverage",
    }:
        return "structural-review"
    return "other"


def _pagination_metric(
    request: QuestionRequest[Any], answer: Any, prior_events: Sequence[Any]
) -> Mapping[str, Any] | None:
    kind = request.question.kind
    if kind not in {"extract_requirements", "enumerate_structural_subjects"}:
        return None
    subject_id = request.question.subject_ids[0]
    if not isinstance(answer, Mapping):
        return {
            "question_id": request.question.question_id,
            "subject_id": subject_id,
            "page_ordinal": None,
            "has_more": None,
            "returned_records": None,
            "unique_new_records": None,
            "made_progress": None,
        }
    current_records = answer.get("records")
    if not isinstance(current_records, list):
        current_records = []
    prior_records: set[str] = set()
    prior_pages = 0
    for event in prior_events:
        question = event.payload.get("question")
        accepted_answer = event.payload.get("accepted_answer")
        if (
            event.event_type != "answer_accepted"
            or not isinstance(question, Mapping)
            or question.get("kind") != kind
            or not isinstance(question.get("subject_ids"), list)
            or not question["subject_ids"]
            or question["subject_ids"][0] != subject_id
            or not isinstance(accepted_answer, Mapping)
        ):
            continue
        prior_pages += 1
        previous_records = accepted_answer.get("records")
        if isinstance(previous_records, list):
            prior_records.update(
                _planning_record_digest(record) for record in previous_records
            )
    unique_new = len(
        {_planning_record_digest(record) for record in current_records} - prior_records
    )
    has_more = answer.get("has_more")
    return {
        "question_id": request.question.question_id,
        "subject_id": subject_id,
        "page_ordinal": prior_pages + 1,
        "has_more": has_more if isinstance(has_more, bool) else None,
        "returned_records": len(current_records),
        "unique_new_records": unique_new,
        "made_progress": (
            None if not isinstance(has_more, bool) else not has_more or unique_new > 0
        ),
    }


def _planning_record_digest(record: Any) -> str:
    encoded = json.dumps(
        record, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
            accepted, input_digests, oversized_groups = _replay_planning_answers(
                journal.events
            )
            set_oversized_groups = getattr(
                state_machine, "set_oversized_work_unit_groups", None
            )
            if callable(set_oversized_groups):
                set_oversized_groups(oversized_groups)
            set_input_digests = getattr(
                state_machine, "set_accepted_question_input_digests", None
            )
            if callable(set_input_digests):
                set_input_digests(input_digests)
            ready_questions = getattr(state_machine, "ready_questions", None)
            if callable(ready_questions):
                requests = tuple(ready_questions(accepted))
            else:
                request = state_machine.next_question(accepted)
                requests = () if request is None else (request,)
            status = "complete" if not requests else "question_accepted"
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
