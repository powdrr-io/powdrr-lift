"""Structured events emitted by a Procedrr-backed workflow chat."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock
from typing import Any


class WorkflowChatEventKind(StrEnum):
    """User-facing workflow event categories independent of terminal output."""

    ASSISTANT_MESSAGE = "assistant_message"
    QUESTION = "question"
    STAGE = "stage"
    ARTIFACT = "artifact"
    RETRY = "retry"
    OUTCOME = "outcome"


@dataclass(frozen=True, slots=True)
class WorkflowChatEvent:
    """One structured, ordered event for a single conversation execution."""

    sequence: int
    execution_id: str
    conversation_id: str | None
    kind: WorkflowChatEventKind
    process_id: str | None = None
    path: str | None = None
    text: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    occurred_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "execution_id": self.execution_id,
            "conversation_id": self.conversation_id,
            "kind": self.kind.value,
            "process_id": self.process_id,
            "path": self.path,
            "text": self.text,
            "data": dict(self.data),
            "occurred_at": self.occurred_at,
        }


WorkflowChatEventCallback = Callable[[WorkflowChatEvent], None]
"""Synchronous sink so an application adapter can persist before returning."""


class WorkflowChatEventEmitter:
    """Assign ordered event identities and deliver them to an optional sink."""

    def __init__(
        self,
        execution_id: str,
        conversation_id: str | None,
        callback: WorkflowChatEventCallback | None,
        *,
        first_sequence: int = 0,
    ) -> None:
        self.execution_id = execution_id
        self.conversation_id = conversation_id
        self.callback = callback
        self._next_sequence = first_sequence
        self._lock = Lock()

    @property
    def next_sequence(self) -> int:
        with self._lock:
            return self._next_sequence

    def emit(
        self,
        kind: WorkflowChatEventKind,
        *,
        process_id: str | None = None,
        path: str | None = None,
        text: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> WorkflowChatEvent:
        with self._lock:
            event = WorkflowChatEvent(
                sequence=self._next_sequence,
                execution_id=self.execution_id,
                conversation_id=self.conversation_id,
                kind=kind,
                process_id=process_id,
                path=path,
                text=text,
                data=dict(data or {}),
            )
            self._next_sequence += 1
            if self.callback is not None:
                self.callback(event)
            return event


__all__ = [
    "WorkflowChatEvent",
    "WorkflowChatEventCallback",
    "WorkflowChatEventKind",
    "WorkflowChatEventEmitter",
]
