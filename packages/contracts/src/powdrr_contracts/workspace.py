"""Versioned, transport-independent records for the local workspace."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self


class WorkspaceContractError(ValueError):
    """A serialized workspace record is malformed or uses an unknown version."""


class JobKind(StrEnum):
    PLANNING = "planning"
    PR_EXECUTION = "pr_execution"


class JobState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    WAITING_FOR_EXTERNAL = "waiting_for_external"
    INTERRUPTED = "interrupted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class TurnState(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class QuestionState(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class EventType(StrEnum):
    REPOSITORY_REGISTERED = "repository.registered"
    CONVERSATION_CREATED = "conversation.created"
    CONVERSATION_UPDATED = "conversation.updated"
    MESSAGE_CREATED = "message.created"
    JOB_CREATED = "job.created"
    JOB_UPDATED = "job.updated"
    TURN_CREATED = "turn.created"
    TURN_UPDATED = "turn.updated"
    QUESTION_CREATED = "question.created"
    QUESTION_UPDATED = "question.updated"
    JOB_OPERATION_BOUND = "job.operation_bound"
    SUPERVISOR_LEASE_UPDATED = "supervisor_lease.updated"
    WORKER_OWNERSHIP_UPDATED = "worker_ownership.updated"


class DispatchIntent(StrEnum):
    START = "start"
    RESUME = "resume"
    RETRY = "retry"


class WorkerState(StrEnum):
    CLAIMED = "claimed"
    RUNNING = "running"
    EXITED = "exited"


_SCHEMA_VERSION = 1
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _validate_json(value: Any) -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise WorkspaceContractError("Value must contain finite JSON data") from error


def _validate_id(value: Any, name: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise WorkspaceContractError(f"{name} must be a valid opaque ID")
    return value


def _validate_text(value: Any, name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise WorkspaceContractError(f"{name} must be a non-empty string")
    return value


def _record(data: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise WorkspaceContractError(f"{name} must be an object")
    version = data.get("schema_version")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != _SCHEMA_VERSION
    ):
        raise WorkspaceContractError(f"Unsupported {name} schema version")
    expected = fields | {"schema_version"}
    if set(data) != expected:
        raise WorkspaceContractError(f"{name} has missing or unexpected fields")
    _validate_json(data)
    return data


def _tuple_of_ids(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise WorkspaceContractError(f"{name} must be a list")
    result = tuple(_validate_id(item, name) for item in value)
    if len(set(result)) != len(result):
        raise WorkspaceContractError(f"{name} must not contain duplicates")
    return result


def _tuple_of_text(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise WorkspaceContractError(f"{name} must be a list")
    return tuple(_validate_text(item, name) for item in value)


@dataclass(frozen=True)
class Conversation:
    conversation_id: str
    repository_id: str
    title: str
    specification_refs: tuple[str, ...]
    created_at: str
    updated_at: str
    revision: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "conversation_id": self.conversation_id,
            "repository_id": self.repository_id,
            "title": self.title,
            "specification_refs": list(self.specification_refs),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, value: Any) -> Self:
        data = _record(
            value,
            {
                "conversation_id",
                "repository_id",
                "title",
                "specification_refs",
                "created_at",
                "updated_at",
                "revision",
            },
            "conversation",
        )
        revision = data["revision"]
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise WorkspaceContractError("revision must be a positive integer")
        return cls(
            _validate_id(data["conversation_id"], "conversation_id"),
            _validate_id(data["repository_id"], "repository_id"),
            _validate_text(data["title"], "title"),
            _tuple_of_text(data["specification_refs"], "specification_refs"),
            _validate_text(data["created_at"], "created_at"),
            _validate_text(data["updated_at"], "updated_at"),
            revision,
        )


@dataclass(frozen=True)
class Message:
    message_id: str
    conversation_id: str
    role: MessageRole
    content: str
    created_at: str
    sequence: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "role": self.role.value,
            "content": self.content,
            "created_at": self.created_at,
            "sequence": self.sequence,
        }

    @classmethod
    def from_dict(cls, value: Any) -> Self:
        data = _record(
            value,
            {
                "message_id",
                "conversation_id",
                "role",
                "content",
                "created_at",
                "sequence",
            },
            "message",
        )
        try:
            role = MessageRole(data["role"])
        except (TypeError, ValueError) as error:
            raise WorkspaceContractError("Unsupported message role") from error
        sequence = data["sequence"]
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise WorkspaceContractError("sequence must be a positive integer")
        return cls(
            _validate_id(data["message_id"], "message_id"),
            _validate_id(data["conversation_id"], "conversation_id"),
            role,
            _validate_text(data["content"], "content", empty=True),
            _validate_text(data["created_at"], "created_at"),
            sequence,
        )


@dataclass(frozen=True)
class Job:
    job_id: str
    repository_id: str
    kind: JobKind
    title: str
    state: JobState
    conversation_id: str | None
    dependency_ids: tuple[str, ...]
    artifact_refs: tuple[str, ...]
    detail: str
    revision: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "job_id": self.job_id,
            "repository_id": self.repository_id,
            "kind": self.kind.value,
            "title": self.title,
            "state": self.state.value,
            "conversation_id": self.conversation_id,
            "dependency_ids": list(self.dependency_ids),
            "artifact_refs": list(self.artifact_refs),
            "detail": self.detail,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Any) -> Self:
        data = _record(
            value,
            {
                "job_id",
                "repository_id",
                "kind",
                "title",
                "state",
                "conversation_id",
                "dependency_ids",
                "artifact_refs",
                "detail",
                "revision",
                "created_at",
                "updated_at",
            },
            "job",
        )
        try:
            kind, state = JobKind(data["kind"]), JobState(data["state"])
        except (TypeError, ValueError) as error:
            raise WorkspaceContractError("Unsupported job kind or state") from error
        revision = data["revision"]
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise WorkspaceContractError("revision must be a positive integer")
        conversation_id = data["conversation_id"]
        return cls(
            _validate_id(data["job_id"], "job_id"),
            _validate_id(data["repository_id"], "repository_id"),
            kind,
            _validate_text(data["title"], "title"),
            state,
            None
            if conversation_id is None
            else _validate_id(conversation_id, "conversation_id"),
            _tuple_of_ids(data["dependency_ids"], "dependency_ids"),
            _tuple_of_text(data["artifact_refs"], "artifact_refs"),
            _validate_text(data["detail"], "detail", empty=True),
            revision,
            _validate_text(data["created_at"], "created_at"),
            _validate_text(data["updated_at"], "updated_at"),
        )


@dataclass(frozen=True)
class WorkspaceEvent:
    sequence: int
    repository_id: str
    entity_type: str
    entity_id: str
    event_type: EventType
    payload: dict[str, Any]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "sequence": self.sequence,
            "repository_id": self.repository_id,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "event_type": self.event_type.value,
            "payload": self.payload,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: Any) -> Self:
        data = _record(
            value,
            {
                "sequence",
                "repository_id",
                "entity_type",
                "entity_id",
                "event_type",
                "payload",
                "created_at",
            },
            "event",
        )
        sequence = data["sequence"]
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise WorkspaceContractError("sequence must be a positive integer")
        try:
            event_type = EventType(data["event_type"])
        except (TypeError, ValueError) as error:
            raise WorkspaceContractError("Unsupported event type") from error
        if not isinstance(data["payload"], dict):
            raise WorkspaceContractError("payload must be an object")
        return cls(
            sequence,
            _validate_id(data["repository_id"], "repository_id"),
            _validate_text(data["entity_type"], "entity_type"),
            _validate_id(data["entity_id"], "entity_id"),
            event_type,
            data["payload"],
            _validate_text(data["created_at"], "created_at"),
        )


@dataclass(frozen=True)
class WorkspaceRepository:
    repository_id: str
    display_name: str
    canonical_root: str
    created_at: str

    def to_public_dict(self) -> dict[str, Any]:
        """Return browser-safe fields without the local filesystem path."""
        return {
            "schema_version": _SCHEMA_VERSION,
            "repository_id": self.repository_id,
            "display_name": self.display_name,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "repository_id": self.repository_id,
            "display_name": self.display_name,
            "canonical_root": self.canonical_root,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: Any) -> Self:
        data = _record(
            value,
            {"repository_id", "display_name", "canonical_root", "created_at"},
            "workspace repository",
        )
        return cls(
            _validate_id(data["repository_id"], "repository_id"),
            _validate_text(data["display_name"], "display_name"),
            _validate_text(data["canonical_root"], "canonical_root"),
            _validate_text(data["created_at"], "created_at"),
        )


@dataclass(frozen=True)
class WorkspaceTurn:
    turn_id: str
    conversation_id: str
    user_message_id: str
    run_id: str | None
    attempt_id: str | None
    state: TurnState
    final_message_id: str | None
    error: str | None
    revision: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "turn_id": self.turn_id,
            "conversation_id": self.conversation_id,
            "user_message_id": self.user_message_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "state": self.state.value,
            "final_message_id": self.final_message_id,
            "error": self.error,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Any) -> Self:
        data = _record(
            value,
            {
                "turn_id",
                "conversation_id",
                "user_message_id",
                "run_id",
                "attempt_id",
                "state",
                "final_message_id",
                "error",
                "revision",
                "created_at",
                "updated_at",
            },
            "turn",
        )
        try:
            state = TurnState(data["state"])
        except (TypeError, ValueError) as error:
            raise WorkspaceContractError("Unsupported turn state") from error
        revision = data["revision"]
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise WorkspaceContractError("revision must be a positive integer")
        run_id = data["run_id"]
        attempt_id = data["attempt_id"]
        if (run_id is None) != (attempt_id is None):
            raise WorkspaceContractError("run_id and attempt_id must be set together")
        return cls(
            _validate_id(data["turn_id"], "turn_id"),
            _validate_id(data["conversation_id"], "conversation_id"),
            _validate_id(data["user_message_id"], "user_message_id"),
            None if run_id is None else _validate_id(run_id, "run_id"),
            None if attempt_id is None else _validate_id(attempt_id, "attempt_id"),
            state,
            None
            if data["final_message_id"] is None
            else _validate_id(data["final_message_id"], "final_message_id"),
            None if data["error"] is None else _validate_text(data["error"], "error"),
            revision,
            _validate_text(data["created_at"], "created_at"),
            _validate_text(data["updated_at"], "updated_at"),
        )


@dataclass(frozen=True)
class WorkspaceQuestion:
    question_id: str
    conversation_id: str
    turn_id: str
    prompt: str
    response_schema: dict[str, Any]
    run_id: str
    attempt_id: str
    state: QuestionState
    answer: Any | None
    delivery_receipt_id: str | None
    revision: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "question_id": self.question_id,
            "conversation_id": self.conversation_id,
            "turn_id": self.turn_id,
            "prompt": self.prompt,
            "response_schema": self.response_schema,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "state": self.state.value,
            "answer": self.answer,
            "delivery_receipt_id": self.delivery_receipt_id,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Any) -> Self:
        data = _record(
            value,
            {
                "question_id",
                "conversation_id",
                "turn_id",
                "prompt",
                "response_schema",
                "run_id",
                "attempt_id",
                "state",
                "answer",
                "delivery_receipt_id",
                "revision",
                "created_at",
                "updated_at",
            },
            "question",
        )
        try:
            state = QuestionState(data["state"])
        except (TypeError, ValueError) as error:
            raise WorkspaceContractError("Unsupported question state") from error
        revision = data["revision"]
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise WorkspaceContractError("revision must be a positive integer")
        if not isinstance(data["response_schema"], dict):
            raise WorkspaceContractError("response_schema must be an object")
        _validate_json(data["answer"])
        return cls(
            _validate_id(data["question_id"], "question_id"),
            _validate_id(data["conversation_id"], "conversation_id"),
            _validate_id(data["turn_id"], "turn_id"),
            _validate_text(data["prompt"], "prompt"),
            data["response_schema"],
            _validate_id(data["run_id"], "run_id"),
            _validate_id(data["attempt_id"], "attempt_id"),
            state,
            data["answer"],
            None
            if data["delivery_receipt_id"] is None
            else _validate_id(data["delivery_receipt_id"], "delivery_receipt_id"),
            revision,
            _validate_text(data["created_at"], "created_at"),
            _validate_text(data["updated_at"], "updated_at"),
        )


@dataclass(frozen=True)
class JobOperation:
    operation_id: str
    job_id: str
    run_id: str
    attempt_id: str
    workflow_directory: str
    checkpoint_ref: str
    source_spec_revision: str | None
    source_refs: tuple[str, ...]
    job_inputs: dict[str, Any]
    provider_config_refs: tuple[str, ...]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "operation_id": self.operation_id,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "workflow_directory": self.workflow_directory,
            "checkpoint_ref": self.checkpoint_ref,
            "source_spec_revision": self.source_spec_revision,
            "source_refs": list(self.source_refs),
            "job_inputs": self.job_inputs,
            "provider_config_refs": list(self.provider_config_refs),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: Any) -> Self:
        data = _record(
            value,
            {
                "operation_id",
                "job_id",
                "run_id",
                "attempt_id",
                "workflow_directory",
                "checkpoint_ref",
                "source_spec_revision",
                "source_refs",
                "job_inputs",
                "provider_config_refs",
                "created_at",
            },
            "job operation",
        )
        if not isinstance(data["job_inputs"], dict):
            raise WorkspaceContractError("job_inputs must be an object")
        revision = data["source_spec_revision"]
        return cls(
            _validate_id(data["operation_id"], "operation_id"),
            _validate_id(data["job_id"], "job_id"),
            _validate_id(data["run_id"], "run_id"),
            _validate_id(data["attempt_id"], "attempt_id"),
            _validate_text(data["workflow_directory"], "workflow_directory"),
            _validate_text(data["checkpoint_ref"], "checkpoint_ref"),
            None
            if revision is None
            else _validate_text(revision, "source_spec_revision"),
            _tuple_of_text(data["source_refs"], "source_refs"),
            data["job_inputs"],
            _tuple_of_text(data["provider_config_refs"], "provider_config_refs"),
            _validate_text(data["created_at"], "created_at"),
        )


@dataclass(frozen=True)
class SupervisorLease:
    supervisor_id: str
    generation: int
    process_id: int
    process_started_at: str
    lease_expires_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "supervisor_id": self.supervisor_id,
            "generation": self.generation,
            "process_id": self.process_id,
            "process_started_at": self.process_started_at,
            "lease_expires_at": self.lease_expires_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Any) -> Self:
        data = _record(
            value,
            {
                "supervisor_id",
                "generation",
                "process_id",
                "process_started_at",
                "lease_expires_at",
                "updated_at",
            },
            "supervisor lease",
        )
        generation, process_id = data["generation"], data["process_id"]
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
        ):
            raise WorkspaceContractError("generation must be a positive integer")
        if (
            not isinstance(process_id, int)
            or isinstance(process_id, bool)
            or process_id < 1
        ):
            raise WorkspaceContractError("process_id must be a positive integer")
        return cls(
            _validate_id(data["supervisor_id"], "supervisor_id"),
            generation,
            process_id,
            _validate_text(data["process_started_at"], "process_started_at"),
            _validate_text(data["lease_expires_at"], "lease_expires_at"),
            _validate_text(data["updated_at"], "updated_at"),
        )


@dataclass(frozen=True)
class WorkerOwnership:
    worker_id: str
    job_id: str
    run_id: str
    attempt_id: str
    supervisor_id: str
    supervisor_generation: int
    process_id: int
    process_started_at: str
    heartbeat_at: str
    lease_expires_at: str
    cancellation_requested: bool
    dispatch_intent: DispatchIntent
    state: WorkerState
    revision: int
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "worker_id": self.worker_id,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "supervisor_id": self.supervisor_id,
            "supervisor_generation": self.supervisor_generation,
            "process_id": self.process_id,
            "process_started_at": self.process_started_at,
            "heartbeat_at": self.heartbeat_at,
            "lease_expires_at": self.lease_expires_at,
            "cancellation_requested": self.cancellation_requested,
            "dispatch_intent": self.dispatch_intent.value,
            "state": self.state.value,
            "revision": self.revision,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Any) -> Self:
        data = _record(
            value,
            {
                "worker_id",
                "job_id",
                "run_id",
                "attempt_id",
                "supervisor_id",
                "supervisor_generation",
                "process_id",
                "process_started_at",
                "heartbeat_at",
                "lease_expires_at",
                "cancellation_requested",
                "dispatch_intent",
                "state",
                "revision",
                "updated_at",
            },
            "worker ownership",
        )
        for name in ("supervisor_generation", "process_id", "revision"):
            number = data[name]
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                raise WorkspaceContractError(f"{name} must be a positive integer")
        if not isinstance(data["cancellation_requested"], bool):
            raise WorkspaceContractError("cancellation_requested must be a boolean")
        try:
            intent = DispatchIntent(data["dispatch_intent"])
            state = WorkerState(data["state"])
        except (TypeError, ValueError) as error:
            raise WorkspaceContractError(
                "Unsupported worker intent or state"
            ) from error
        return cls(
            _validate_id(data["worker_id"], "worker_id"),
            _validate_id(data["job_id"], "job_id"),
            _validate_id(data["run_id"], "run_id"),
            _validate_id(data["attempt_id"], "attempt_id"),
            _validate_id(data["supervisor_id"], "supervisor_id"),
            data["supervisor_generation"],
            data["process_id"],
            _validate_text(data["process_started_at"], "process_started_at"),
            _validate_text(data["heartbeat_at"], "heartbeat_at"),
            _validate_text(data["lease_expires_at"], "lease_expires_at"),
            data["cancellation_requested"],
            intent,
            state,
            data["revision"],
            _validate_text(data["updated_at"], "updated_at"),
        )
