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
    CONVERSATION_CREATED = "conversation.created"
    CONVERSATION_UPDATED = "conversation.updated"
    MESSAGE_CREATED = "message.created"
    JOB_CREATED = "job.created"
    JOB_UPDATED = "job.updated"


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
