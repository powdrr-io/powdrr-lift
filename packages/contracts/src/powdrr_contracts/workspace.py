"""Transport-independent records for the local chat and job workspace."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class JobState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    INTERRUPTED = "interrupted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Conversation:
    conversation_id: str
    repository_id: str
    title: str
    specification_refs: tuple[str, ...]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Message:
    message_id: str
    conversation_id: str
    role: str
    content: str
    created_at: str
    sequence: int


@dataclass(frozen=True)
class Job:
    job_id: str
    repository_id: str
    kind: str
    title: str
    state: JobState
    conversation_id: str | None
    dependency_ids: tuple[str, ...]
    artifact_refs: tuple[str, ...]
    detail: str
    revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class WorkspaceEvent:
    sequence: int
    repository_id: str
    entity_type: str
    entity_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: str
