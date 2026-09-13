"""Provider-independent repair policy and state types."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RepairStage(StrEnum):
    """Semantic recovery stages shared by agent adapters."""

    TARGETED = "targeted"
    CLEAN_ROOM = "clean_room"
    SELECT_ACTION = "select_action"
    FILL_ACTION = "fill_action"
    DETERMINISTIC = "deterministic"
    MODEL_FALLBACK = "model_fallback"
    HUMAN_HANDOFF = "human_handoff"
    EXHAUSTED = "exhausted"


class RepairFailureClass(StrEnum):
    """Stable categories used to select semantic recovery."""

    RESPONSE = "response"
    PROPOSAL = "proposal"
    EXECUTION = "execution"
    NO_PROGRESS = "no_progress"
    TRANSPORT_TRANSIENT = "transport_transient"
    TRANSPORT_TERMINAL = "transport_terminal"
    RESPONSE_EMPTY = "response_empty"
    RESPONSE_SYNTAX = "response_syntax"
    RESPONSE_SCHEMA = "response_schema"
    ACTION_CONTRACT = "action_contract"
    ACTION_PRECONDITION = "action_precondition"
    ACTION_EXECUTION = "action_execution"
    NO_MATERIAL_PROGRESS = "no_material_progress"
    VALIDATION_REGRESSION = "validation_regression"
    COMPLETION_BLOCKED = "completion_blocked"


def classify_repair_failure(
    error_code: str,
    *,
    default: RepairFailureClass,
) -> RepairFailureClass:
    """Map stable error codes to repair classes without parsing messages."""

    code = error_code.casefold()
    if code in {"timeout", "rate_limit", "provider_overloaded"}:
        return RepairFailureClass.TRANSPORT_TRANSIENT
    if code in {"authentication", "unsupported_model", "provider_unavailable"}:
        return RepairFailureClass.TRANSPORT_TERMINAL
    if code in {"empty_response", "incomplete_response"}:
        return RepairFailureClass.RESPONSE_EMPTY
    if code in {"invalid_json", "invalid_response_json", "non_object_response"}:
        return RepairFailureClass.RESPONSE_SYNTAX
    if code in {"response_schema", "invalid_action_shape", "missing_action"}:
        return RepairFailureClass.RESPONSE_SCHEMA
    if code in {"workflow_action_not_allowed", "action_not_allowed"}:
        return RepairFailureClass.ACTION_CONTRACT
    if code in {
        "precondition_failed",
        "file_not_found",
        "relationship_obligation_open",
    }:
        return RepairFailureClass.ACTION_PRECONDITION
    if code in {"no_progress", "stalled_action"}:
        return RepairFailureClass.NO_MATERIAL_PROGRESS
    if code in {"validation_regression", "issue_unchanged"}:
        return RepairFailureClass.VALIDATION_REGRESSION
    if code in {"completion_blocked", "output_state_missing"}:
        return RepairFailureClass.COMPLETION_BLOCKED
    return default


@dataclass(frozen=True, slots=True)
class RepairPolicy:
    """Bound semantic recovery independently from transport retries."""

    targeted_attempts: int = 1
    clean_room_attempts: int = 1
    action_selection_attempts: int = 1
    parameter_attempts: int = 1
    deterministic_recovery: bool = True
    model_fallback_attempts: int = 1
    allow_human_handoff: bool = True


@dataclass(frozen=True, slots=True)
class RepairFailure:
    """Failure facts supplied to the shared recovery coordinator."""

    classification: RepairFailureClass
    error_code: str
    message: str
    action_signature: str | None = None
    target_signature: str | None = None
    action_payload: Mapping[str, Any] | None = None
    remediation: str | None = None


@dataclass(frozen=True, slots=True)
class RepairContext:
    """Typed, adapter-neutral state supplied to semantic repair decisions."""

    execution_id: str = ""
    boundary_id: str = ""
    objective: str = ""
    deterministic_state: Mapping[str, Any] = field(default_factory=dict)
    allowed_actions: tuple[str, ...] = ()
    action_schemas: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    required_outputs: tuple[str, ...] = ()
    open_obligations: tuple[Mapping[str, Any], ...] = ()
    rejected_strategies: tuple[str, ...] = ()
    last_material_progress: Mapping[str, Any] | None = None
    material_state_fingerprint: str = ""

    def to_data(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "boundary_id": self.boundary_id,
            "objective": self.objective,
            "deterministic_state": _json_safe(self.deterministic_state),
            "allowed_actions": list(self.allowed_actions),
            "action_schemas": _json_safe(self.action_schemas),
            "required_outputs": list(self.required_outputs),
            "open_obligations": _json_safe(self.open_obligations),
            "rejected_strategies": list(self.rejected_strategies),
            "last_material_progress": (
                _json_safe(self.last_material_progress)
                if self.last_material_progress is not None
                else None
            ),
            "material_state_fingerprint": self.material_state_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class RepairDirective:
    """Adapter-independent result of recording one semantic failure."""

    stage: RepairStage
    attempt: int
    reason: str
    allowed_actions: tuple[str, ...] = ()
    prompt_profile: str = "normal_full_context"
    model_policy: str = "current_model"
    failure_class: RepairFailureClass = RepairFailureClass.EXECUTION
    error_code: str = "unknown"
    target_signature: str | None = None
    material_state_fingerprint: str = ""
    rejected_strategy_signatures: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RepairExhaustionReport:
    """Structured terminal record for an exhausted recovery boundary."""

    boundary_id: str
    objective: str
    final_state: Mapping[str, Any]
    failures: tuple[Mapping[str, Any], ...] = ()
    prompt_manifests: tuple[Mapping[str, Any], ...] = ()
    rejected_strategies: tuple[Mapping[str, Any], ...] = ()
    allowed_actions: tuple[str, ...] = ()
    reason: str = ""

    def to_data(self) -> dict[str, Any]:
        return {
            "boundary_id": self.boundary_id,
            "objective": self.objective,
            "final_state": dict(self.final_state),
            "failures": [dict(item) for item in self.failures],
            "prompt_manifests": [dict(item) for item in self.prompt_manifests],
            "rejected_strategies": [dict(item) for item in self.rejected_strategies],
            "allowed_actions": list(self.allowed_actions),
            "reason": self.reason,
        }


def _json_safe(value: Any) -> Any:
    """Normalize repair state for durable JSON events and prompt payloads."""

    return json.loads(json.dumps(value, ensure_ascii=False, default=str))
