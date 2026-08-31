"""Typed action lifecycle shared by workflow adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from powdrr_lift.core.action_relationship import BUILTIN_ACTION_RELATIONSHIPS
from powdrr_lift.core.execution_state import ExecutionObligation
from powdrr_lift.execution.relationships import expand_execution_obligations


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
    obligations: tuple[ExecutionObligation, ...] = ()


class ActionKernel:
    """Record one ordered lifecycle for an action.

    Adapters may present events differently, but cannot skip the typed
    proposal/start/terminal transitions or turn correction failures into
    indistinguishable provider errors.
    """

    def __init__(self) -> None:
        self._events: list[ActionLifecycleEvent] = []
        self._obligations: dict[str, ExecutionObligation] = {}

    @property
    def events(self) -> tuple[ActionLifecycleEvent, ...]:
        return tuple(self._events)

    @property
    def open_obligations(self) -> tuple[ExecutionObligation, ...]:
        return tuple(self._obligations.values())

    def propose(
        self,
        action: Any,
        *,
        semantic_action: str | None = None,
        attributes: frozenset[str] = frozenset(),
    ) -> ActionLifecycleEvent:
        semantic_action = semantic_action or str(getattr(action, "semantic_action", ""))
        obligations: tuple[ExecutionObligation, ...] = ()
        if semantic_action:
            expansion = expand_execution_obligations(
                None,
                action_instance_id=f"action-{len(self._events)}",
                action=semantic_action,
                attributes=attributes,
                relationships=BUILTIN_ACTION_RELATIONSHIPS,
            )
            obligations = expansion.obligations
            self._obligations.update({item.obligation_id: item for item in obligations})
        return self._record(
            ActionLifecyclePhase.PROPOSED, action, obligations=obligations
        )

    def satisfy(self, action_instance_id: str, semantic_action: str) -> bool:
        """Close only obligations belonging to the exact proposed action."""
        matches = tuple(
            item
            for item in self._obligations.values()
            if item.source_action_instance_id == action_instance_id
            and item.required_action == semantic_action
        )
        for item in matches:
            del self._obligations[item.obligation_id]
        return bool(matches)

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
        obligations: tuple[ExecutionObligation, ...] = (),
    ) -> ActionLifecycleEvent:
        event = ActionLifecycleEvent(
            len(self._events), phase, action, error, obligations
        )
        self._events.append(event)
        return event
