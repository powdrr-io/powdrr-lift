"""Shared control state for the interactive and durable workflow runners.

The runners intentionally own their presentation and human-handoff policies,
but action failure and no-progress accounting must be identical in both paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ProgressDecision(Enum):
    PROGRESS = "progress"
    CONTINUE = "continue"
    THRESHOLD = "threshold"


def no_progress_feedback(action_signature: str) -> str:
    """Return the canonical correction sent after a repeated action."""
    return (
        "The previous workflow action made no progress because it repeated the "
        "same action without changing the file, staging state, or workflow step. "
        "Do not invoke this action unchanged again. If its result satisfies the "
        "current task, choose `next_step` immediately; otherwise make a real edit "
        "or choose a different action. Repeated action: "
        f"{action_signature}"
    )


@dataclass(slots=True)
class WorkflowExecutionController:
    """Track repeated actions and failures for either workflow execution path."""

    max_stalled_roundtrips: int
    previous_action_signature: str | None = None
    failed_action_signature: str | None = None
    stalled_roundtrips: int = 0

    def observe(
        self,
        action_signature: str,
        *,
        made_progress: bool,
    ) -> ProgressDecision:
        """Record a successful action and detect repeated work without progress."""
        if made_progress or self.previous_action_signature != action_signature:
            self.stalled_roundtrips = 0
            self.failed_action_signature = None
            decision = ProgressDecision.PROGRESS
        else:
            self.stalled_roundtrips += 1
            decision = (
                ProgressDecision.THRESHOLD
                if self.stalled_roundtrips >= max(1, self.max_stalled_roundtrips)
                else ProgressDecision.CONTINUE
            )
        self.previous_action_signature = action_signature
        return decision

    def record_failure(self, action_signature: str) -> ProgressDecision:
        """Record a failed action and apply the same repeated-failure threshold."""
        if self.failed_action_signature == action_signature:
            self.stalled_roundtrips += 1
        else:
            self.failed_action_signature = action_signature
            self.stalled_roundtrips = 1
        self.previous_action_signature = None
        return (
            ProgressDecision.THRESHOLD
            if self.stalled_roundtrips >= max(1, self.max_stalled_roundtrips)
            else ProgressDecision.CONTINUE
        )

    def reset(self) -> None:
        self.previous_action_signature = None
        self.failed_action_signature = None
        self.stalled_roundtrips = 0
