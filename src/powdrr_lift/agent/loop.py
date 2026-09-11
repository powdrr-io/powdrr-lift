"""Contracts for observing and reporting one agent action round."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from powdrr_lift.agent.progress import ProgressDecision
from powdrr_lift.agent.protocol import WorkflowLLMClient


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
