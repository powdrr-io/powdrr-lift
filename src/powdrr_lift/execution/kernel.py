"""Typed action lifecycle shared by workflow adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from powdrr_lift.core.action_relationship import BUILTIN_ACTION_RELATIONSHIPS
from powdrr_lift.core.execution_state import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionObligation,
    ExecutionState,
    ObligationStatus,
)
from powdrr_lift.execution.relationships import expand_execution_obligations


class ActionLifecyclePhase(StrEnum):
    PROPOSED = "proposed"
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    OBLIGATION_OPENED = "obligation_opened"
    OBLIGATION_SATISFIED = "obligation_satisfied"


@dataclass(frozen=True, slots=True)
class ActionLifecycleEvent:
    sequence: int
    phase: ActionLifecyclePhase
    action: Any
    error: Any = None
    obligations: tuple[ExecutionObligation, ...] = ()

    def to_data(self) -> dict[str, Any]:
        error = self.error
        if error is not None and not isinstance(error, (str, int, float, bool)):
            error = str(error)
        return {
            "sequence": self.sequence,
            "phase": self.phase.value,
            "action": self.action,
            "error": error,
            "obligations": [item.to_data() for item in self.obligations],
        }


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

    def snapshot(self) -> dict[str, Any]:
        """Return the durable, JSON-compatible relationship kernel state."""
        return {
            "events": [event.to_data() for event in self._events],
            "open_obligations": [
                obligation.to_data() for obligation in self._obligations.values()
            ],
        }

    def restore_obligations(self, obligations: tuple[ExecutionObligation, ...]) -> None:
        """Hydrate open obligations when a durable execution is resumed."""
        self._obligations = {
            obligation.obligation_id: obligation
            for obligation in obligations
            if obligation.status is ObligationStatus.OPEN
        }

    def to_execution_events(
        self,
        execution_id: str,
        *,
        phase_type: str,
        actor_id: str,
        starting_state: ExecutionState | None = None,
        events: tuple[ActionLifecycleEvent, ...] | None = None,
    ) -> tuple[ExecutionEvent, ...]:
        """Project kernel lifecycle into the durable execution event schema."""
        state_version = starting_state.state_version if starting_state else 0
        sequence = starting_state.event_sequence if starting_state else 0
        projected: list[ExecutionEvent] = []
        proposal_sequences: dict[int, str] = {}
        lifecycle_events = events if events is not None else self._events
        for event in lifecycle_events:
            event_type = {
                ActionLifecyclePhase.PROPOSED: ExecutionEventType.ACTION_PROPOSED,
                ActionLifecyclePhase.STARTED: ExecutionEventType.ACTION_STARTED,
                ActionLifecyclePhase.COMPLETED: ExecutionEventType.ACTION_COMPLETED,
                ActionLifecyclePhase.FAILED: ExecutionEventType.ACTION_FAILED,
                ActionLifecyclePhase.OBLIGATION_OPENED: (
                    ExecutionEventType.OBLIGATION_OPENED
                ),
                ActionLifecyclePhase.OBLIGATION_SATISFIED: (
                    ExecutionEventType.OBLIGATION_SATISFIED
                ),
            }[event.phase]
            action_id = self._action_instance_id(event.action, event.sequence)
            if event.phase is ActionLifecyclePhase.PROPOSED:
                proposal_sequences[event.sequence] = action_id
            payload: dict[str, Any]
            if event.phase in {
                ActionLifecyclePhase.OBLIGATION_OPENED,
                ActionLifecyclePhase.OBLIGATION_SATISFIED,
            }:
                payload = dict(event.obligations[0].to_data())
            else:
                payload = {
                    "action_instance_id": action_id,
                    "kind": self._semantic_action(event.action, None),
                    "actor_id": actor_id,
                    "phase_type": phase_type,
                }
                if event.error is not None:
                    payload["status"] = "correctable_error"
                    payload["error_code"] = getattr(
                        event.error, "error_code", type(event.error).__name__
                    )
            sequence += 1
            projected.append(
                ExecutionEvent(
                    execution_id,
                    sequence,
                    state_version,
                    event_type,
                    payload,
                    f"{execution_id}:{sequence}",
                )
            )
            state_version += 1
        return tuple(projected)

    def propose(
        self,
        action: Any,
        *,
        semantic_action: str | None = None,
        attributes: frozenset[str] = frozenset(),
    ) -> ActionLifecycleEvent:
        semantic_action = self._semantic_action(action, semantic_action)
        if not attributes:
            attributes = self._action_attributes(action)
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
        event = self._record(
            ActionLifecyclePhase.PROPOSED, action, obligations=obligations
        )
        for obligation in obligations:
            self._record(
                ActionLifecyclePhase.OBLIGATION_OPENED,
                action,
                obligations=(obligation,),
            )
        return event

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
            self._record(
                ActionLifecyclePhase.OBLIGATION_SATISFIED,
                {"action_instance_id": action_instance_id, "action": semantic_action},
                obligations=(item,),
            )
        return bool(matches)

    def start(self, action: Any) -> ActionLifecycleEvent:
        return self._record(ActionLifecyclePhase.STARTED, action)

    def complete(self, action: Any) -> ActionLifecycleEvent:
        self._close_matching_obligations(
            self._semantic_action(action, None),
            self._source_action_instance_id(action),
        )
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

    def validate_proposal(
        self, action: Any, *, semantic_action: str | None = None
    ) -> tuple[str, ...]:
        """Return relationship violations before an action can start."""
        semantic_action = self._semantic_action(action, semantic_action)
        if not self._obligations:
            return ()
        required = {
            item.required_action
            for item in self._obligations.values()
            if item.required_action is not None
        }
        if semantic_action == "resolve_review_thread" and "run_validation" in required:
            return (
                "Review-thread resolution is blocked until run_validation is complete.",
            )
        if semantic_action in required:
            return ()
        return (
            f"Action {semantic_action!r} is blocked by open relationship "
            f"obligations: {', '.join(sorted(required))}.",
        )

    @staticmethod
    def _semantic_action(action: Any, explicit: str | None) -> str:
        if isinstance(action, Mapping):
            return explicit or str(
                action.get("semantic_action") or action.get("kind") or ""
            )
        return explicit or str(
            getattr(action, "semantic_action", None) or getattr(action, "kind", "")
        )

    @staticmethod
    def _action_instance_id(action: Any, sequence: int) -> str:
        value = (
            action.get("action_instance_id")
            if isinstance(action, Mapping)
            else getattr(action, "action_instance_id", None)
        )
        return value if isinstance(value, str) and value else f"action-{sequence}"

    @staticmethod
    def _action_attributes(action: Any) -> frozenset[str]:
        if isinstance(action, Mapping):
            value = action.get("attributes", ())
        else:
            value = getattr(action, "attributes", ())
        if isinstance(value, (set, frozenset, list, tuple)) and all(
            isinstance(item, str) for item in value
        ):
            return frozenset(value)
        return frozenset()

    @staticmethod
    def _source_action_instance_id(action: Any) -> str | None:
        if isinstance(action, Mapping):
            value = action.get("source_action_instance_id")
        else:
            value = getattr(action, "source_action_instance_id", None)
        return value if isinstance(value, str) and value else None

    def _close_matching_obligations(
        self, semantic_action: str, source_action_instance_id: str | None
    ) -> None:
        matches = tuple(
            (obligation_id, obligation)
            for obligation_id, obligation in self._obligations.items()
            if obligation.required_action == semantic_action
            and (
                source_action_instance_id is None
                or obligation.source_action_instance_id == source_action_instance_id
            )
        )
        # A follow-up without explicit provenance closes only the oldest matching
        # obligation; it must not accidentally satisfy another source action.
        if source_action_instance_id is None:
            matches = matches[:1]
        for obligation_id, obligation in matches:
            del self._obligations[obligation_id]
            self._record(
                ActionLifecyclePhase.OBLIGATION_SATISFIED,
                {"action": semantic_action},
                obligations=(obligation,),
            )
