"""Typed action lifecycle shared by workflow adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ActionLifecyclePhase(StrEnum):
    PROPOSED = "proposed"
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ActionLifecycleEvent:
    sequence: int
    phase: ActionLifecyclePhase
    action: Any
    error: Any = None


class ActionKernel:
    """Record one ordered lifecycle for an action.

    Adapters may present events differently, but cannot skip the typed
    proposal/start/terminal transitions or turn correction failures into
    indistinguishable provider errors.
    """

    def __init__(self) -> None:
        self._events: list[ActionLifecycleEvent] = []

    @property
    def events(self) -> tuple[ActionLifecycleEvent, ...]:
        return tuple(self._events)

    def propose(self, action: Any) -> ActionLifecycleEvent:
        return self._record(ActionLifecyclePhase.PROPOSED, action)

    def start(self, action: Any) -> ActionLifecycleEvent:
        return self._record(ActionLifecyclePhase.STARTED, action)

    def complete(self, action: Any) -> ActionLifecycleEvent:
        return self._record(ActionLifecyclePhase.COMPLETED, action)

    def fail(self, action: Any, error: Any) -> ActionLifecycleEvent:
        return self._record(ActionLifecyclePhase.FAILED, action, error)

    def _record(
        self,
        phase: ActionLifecyclePhase,
        action: Any,
        error: Any = None,
    ) -> ActionLifecycleEvent:
        event = ActionLifecycleEvent(len(self._events), phase, action, error)
        self._events.append(event)
        return event
