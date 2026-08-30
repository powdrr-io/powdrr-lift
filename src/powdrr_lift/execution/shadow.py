"""Best-effort event recording for existing workflow runners.

Shadow recording is deliberately isolated from action execution.  A recorder
failure is reported to its caller and never changes the legacy runner's result.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from powdrr_lift.core.delivery_profile import PhaseType
from powdrr_lift.core.execution_state import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionState,
)
from powdrr_lift.execution.store import FileExecutionStateStore


class ShadowExecutionRecorder:
    """Record action lifecycle events for one existing workflow execution."""

    def __init__(
        self,
        store: FileExecutionStateStore,
        execution_id: str,
        *,
        profile_id: str = "legacy-workflow",
        actor_id: str = "workflow-agent",
        phase: PhaseType = PhaseType.BUILD,
    ) -> None:
        self.store = store
        self.execution_id = execution_id
        self.actor_id = actor_id
        self._state = self._load_or_create(profile_id=profile_id, phase=phase)
        self._action_ids: dict[str, str] = {}

    @property
    def state(self) -> ExecutionState:
        return self._state

    def record_action(
        self,
        event_type: ExecutionEventType,
        action: Any,
        *,
        error_code: str | None = None,
    ) -> None:
        event_type = ExecutionEventType(event_type)
        signature = _action_signature(action)
        action_id = self._action_ids.setdefault(
            signature,
            f"{self.execution_id}-action-{self._state.event_sequence + 1}",
        )
        payload: dict[str, Any] = {
            "action_instance_id": action_id,
            "kind": str(getattr(action, "kind", "unknown")),
            "actor_id": self.actor_id,
            "phase_type": self._state.current_phase.value,
            "arguments_fingerprint": hashlib.sha256(
                signature.encode("utf-8")
            ).hexdigest(),
        }
        if error_code is not None:
            payload["error_code"] = error_code
            payload["status"] = "correctable_error"
        event = ExecutionEvent(
            execution_id=self.execution_id,
            sequence=self._state.event_sequence + 1,
            expected_state_version=self._state.state_version,
            event_type=event_type,
            payload=payload,
            event_id=(f"{self.execution_id}-event-{self._state.event_sequence + 1}"),
        )
        self._state = self.store.append(
            self.execution_id,
            self._state.state_version,
            (event,),
        )

    def _load_or_create(self, *, profile_id: str, phase: PhaseType) -> ExecutionState:
        try:
            return self.store.load(self.execution_id)
        except FileNotFoundError:
            return self.store.create(
                self.execution_id, profile_id=profile_id, phase=phase
            )


def _action_signature(action: Any) -> str:
    if hasattr(action, "to_data") and callable(action.to_data):
        value = action.to_data()
    elif hasattr(action, "__dict__"):
        value = vars(action)
    else:
        value = str(action)
    return json.dumps(value, sort_keys=True, default=str)
