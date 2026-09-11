"""Contracts for observing and reporting one agent action round."""

from __future__ import annotations

from dataclasses import dataclass

from powdrr_lift.agent.progress import ProgressDecision


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
