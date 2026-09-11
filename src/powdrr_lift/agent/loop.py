"""Contracts for observing and reporting one agent action round."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from powdrr_lift.agent.progress import ProgressDecision
from powdrr_lift.agent.protocol import WorkflowLLMClient

ActionT = TypeVar("ActionT", contravariant=True)


@dataclass(frozen=True, slots=True)
class WorkflowActionObservation:
    """The common result of evaluating one proposed agent action."""

    signature: str
    made_progress: bool
    decision: ProgressDecision
    correction: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowActionOutcome:
    """The adapter's result after the shared runner executes an action."""

    continue_running: bool = True
    exit_code: int | None = None


@dataclass(frozen=True, slots=True)
class WorkflowActionRequest:
    """One fully specified request for the next workflow action."""

    client: WorkflowLLMClient
    messages: list[dict[str, str]]
    parser: Callable[[dict[str, Any]], Any]
    model: str
    stderr: Any
    max_timeout_retries: int
    timeout_backoff_seconds: float
    response_schema: Mapping[str, Any] | None = None
    request_action: Callable[[], Any] | None = None


class WorkflowActionProgressStrategy(Protocol[ActionT]):
    """Adapter hooks for state snapshots and runner-specific reporting."""

    def material_state(self, action: ActionT) -> object: ...

    def record_no_progress(
        self,
        action: ActionT,
        observation: WorkflowActionObservation,
    ) -> None: ...


class WorkflowExecutionStrategy(WorkflowActionProgressStrategy[Any], Protocol):
    """Boundary between the shared loop and its presentation mode."""

    def next_request(self) -> WorkflowActionRequest | None: ...

    def report_roundtrip(self, roundtrip: int, action: Any) -> None:
        """Present one parsed action; adapters may leave this as a no-op."""
        _ = roundtrip, action

    def execute_action(self, action: Any) -> WorkflowActionOutcome: ...

    def record_response_error(
        self,
        error: RuntimeError,
        payload: dict[str, Any] | None,
    ) -> None: ...

    def record_action_error(self, action: Any, error: Exception) -> None: ...

    def action_failure_exit_code(self, action: Any) -> int | None: ...

    def observe_outcome(
        self,
        action: Any,
        observation: WorkflowActionObservation,
        outcome: WorkflowActionOutcome,
    ) -> WorkflowActionOutcome: ...

    def exhausted_roundtrips_exit_code(self) -> int: ...


class WorkflowExecutionObserver(Protocol):
    """Optional, failure-isolated observer of shared execution boundaries."""

    def response_failed(self, error: Exception) -> Any: ...

    def action_failed(self, action: Any, error: Exception) -> Any: ...

    def action_proposed(self, action: Any) -> Any: ...

    def action_completed(
        self,
        action: Any,
        observation: WorkflowActionObservation,
    ) -> Any: ...
