"""Typed action lifecycle shared by workflow adapters."""

from __future__ import annotations

from collections.abc import Mapping
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
    OBLIGATION_OPENED = "obligation_opened"
    OBLIGATION_SATISFIED = "obligation_satisfied"


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
        semantic_action = self._semantic_action(action, semantic_action)
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
        self._close_matching_obligations(self._semantic_action(action, None))
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

    def _close_matching_obligations(self, semantic_action: str) -> None:
        for obligation_id, obligation in tuple(self._obligations.items()):
            if obligation.required_action == semantic_action:
                del self._obligations[obligation_id]
                self._record(
                    ActionLifecyclePhase.OBLIGATION_SATISFIED,
                    {"action": semantic_action},
                    obligations=(obligation,),
                )
