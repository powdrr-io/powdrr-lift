"""Serializable execution state and event contracts.

This module contains no filesystem or LLM behavior.  Runtime code records
typed events and reduces them into :class:`ExecutionState`; the event stream is
the durable history and the state document is a rebuildable materialization.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, cast

from powdrr_lift.core.delivery_profile import PhaseType

EXECUTION_STATE_SCHEMA_VERSION = "execution-state-v1"


class ExecutionMode(StrEnum):
    OFF = "off"
    OBSERVE = "observe"
    ENFORCE = "enforce"


class ExecutionEventType(StrEnum):
    CREATED = "execution_created"
    PHASE_ENTERED = "phase_entered"
    PHASE_EXITED = "phase_exited"
    ACTION_PROPOSED = "action_proposed"
    ACTION_STARTED = "action_started"
    ACTION_COMPLETED = "action_completed"
    ACTION_FAILED = "action_failed"
    ARTIFACT_PRODUCED = "artifact_produced"
    ARTIFACT_ACCEPTED = "artifact_accepted"
    OBLIGATION_OPENED = "obligation_opened"
    OBLIGATION_SATISFIED = "obligation_satisfied"
    EVIDENCE_RECORDED = "evidence_recorded"
    EVIDENCE_INVALIDATED = "evidence_invalidated"
    FINDING_OPENED = "finding_opened"
    FINDING_DISPOSED = "finding_disposed"
    CHECKPOINT_CREATED = "checkpoint_created"
    CHECKPOINT_REVERTED = "checkpoint_reverted"
    CAPABILITY_DECISION = "capability_decision"


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    STARTED = "started"
    COMPLETED = "completed"
    CORRECTABLE_ERROR = "correctable_error"
    TERMINAL_ERROR = "terminal_error"
    REVERTED = "reverted"


class ObligationStatus(StrEnum):
    OPEN = "open"
    SATISFIED = "satisfied"
    WAIVED = "waived"
    OBSOLETE = "obsolete"


class FindingStatus(StrEnum):
    OPEN = "open"
    ACCEPTED = "accepted"
    FIXED = "fixed"
    NOT_APPLICABLE = "not_applicable"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class ExecutionArtifact:
    artifact_id: str
    artifact_type: str
    schema_version: str
    owner_persona_id: str
    content_ref: str
    accepted: bool = False

    def to_data(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "schema_version": self.schema_version,
            "owner_persona_id": self.owner_persona_id,
            "content_ref": self.content_ref,
            "accepted": self.accepted,
        }


@dataclass(frozen=True, slots=True)
class ActionRecord:
    action_instance_id: str
    kind: str
    status: ActionStatus
    actor_id: str
    phase_type: PhaseType
    arguments_fingerprint: str | None = None
    error_code: str | None = None

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "action_instance_id": self.action_instance_id,
            "kind": self.kind,
            "status": self.status.value,
            "actor_id": self.actor_id,
            "phase_type": self.phase_type.value,
        }
        if self.arguments_fingerprint is not None:
            data["arguments_fingerprint"] = self.arguments_fingerprint
        if self.error_code is not None:
            data["error_code"] = self.error_code
        return data


@dataclass(frozen=True, slots=True)
class ExecutionObligation:
    obligation_id: str
    description: str
    status: ObligationStatus = ObligationStatus.OPEN
    source_action_instance_id: str | None = None
    required_action: str | None = None
    relationship_id: str | None = None

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "obligation_id": self.obligation_id,
            "description": self.description,
            "status": self.status.value,
        }
        if self.source_action_instance_id is not None:
            data["source_action_instance_id"] = self.source_action_instance_id
        if self.required_action is not None:
            data["required_action"] = self.required_action
        if self.relationship_id is not None:
            data["relationship_id"] = self.relationship_id
        return data


@dataclass(frozen=True, slots=True)
class ExecutionEvidence:
    evidence_id: str
    producer_action_instance_id: str
    evidence_type: str
    input_fingerprint: str
    successful: bool
    fresh: bool = True

    def to_data(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "producer_action_instance_id": self.producer_action_instance_id,
            "evidence_type": self.evidence_type,
            "input_fingerprint": self.input_fingerprint,
            "successful": self.successful,
            "fresh": self.fresh,
        }


@dataclass(frozen=True, slots=True)
class ExecutionFinding:
    finding_id: str
    reviewer_run_id: str
    severity: str
    description: str
    status: FindingStatus = FindingStatus.OPEN
    blocking: bool = True

    def to_data(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "reviewer_run_id": self.reviewer_run_id,
            "severity": self.severity,
            "description": self.description,
            "status": self.status.value,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class ExecutionState:
    execution_id: str
    profile_id: str
    mode: ExecutionMode
    current_phase: PhaseType
    schema_version: str = EXECUTION_STATE_SCHEMA_VERSION
    phase_revision: int = 0
    state_version: int = 0
    event_sequence: int = 0
    current_persona_id: str | None = None
    active_unit_id: str | None = None
    artifacts: tuple[ExecutionArtifact, ...] = field(default_factory=tuple)
    actions: tuple[ActionRecord, ...] = field(default_factory=tuple)
    obligations: tuple[ExecutionObligation, ...] = field(default_factory=tuple)
    evidence: tuple[ExecutionEvidence, ...] = field(default_factory=tuple)
    findings: tuple[ExecutionFinding, ...] = field(default_factory=tuple)

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "execution_id": self.execution_id,
            "profile_id": self.profile_id,
            "mode": self.mode.value,
            "current_phase": self.current_phase.value,
            "phase_revision": self.phase_revision,
            "state_version": self.state_version,
            "event_sequence": self.event_sequence,
            "current_persona_id": self.current_persona_id,
            "active_unit_id": self.active_unit_id,
            "artifacts": [item.to_data() for item in self.artifacts],
            "actions": [item.to_data() for item in self.actions],
            "obligations": [item.to_data() for item in self.obligations],
            "evidence": [item.to_data() for item in self.evidence],
            "findings": [item.to_data() for item in self.findings],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_data(), indent=2, ensure_ascii=False) + "\n"

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ExecutionState:
        if data.get("schema_version") != EXECUTION_STATE_SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {EXECUTION_STATE_SCHEMA_VERSION!r}."
            )
        return cls(
            execution_id=_required_string(data, "execution_id"),
            profile_id=_required_string(data, "profile_id"),
            mode=ExecutionMode(_required_string(data, "mode")),
            current_phase=PhaseType(_required_string(data, "current_phase")),
            schema_version=EXECUTION_STATE_SCHEMA_VERSION,
            phase_revision=_required_nonnegative_int(data, "phase_revision"),
            state_version=_required_nonnegative_int(data, "state_version"),
            event_sequence=_required_nonnegative_int(data, "event_sequence"),
            current_persona_id=_optional_string(data.get("current_persona_id")),
            active_unit_id=_optional_string(data.get("active_unit_id")),
            artifacts=tuple(
                _parse_artifact(item) for item in _mapping_sequence(data, "artifacts")
            ),
            actions=tuple(
                _parse_action(item) for item in _mapping_sequence(data, "actions")
            ),
            obligations=tuple(
                _parse_obligation(item)
                for item in _mapping_sequence(data, "obligations")
            ),
            evidence=tuple(
                _parse_evidence(item) for item in _mapping_sequence(data, "evidence")
            ),
            findings=tuple(
                _parse_finding(item) for item in _mapping_sequence(data, "findings")
            ),
        )

    @classmethod
    def from_json(cls, content: str) -> ExecutionState:
        loaded = json.loads(content)
        if not isinstance(loaded, Mapping):
            raise ValueError("Execution state JSON must decode to an object.")
        return cls.from_data(cast("Mapping[str, Any]", loaded))


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    execution_id: str
    sequence: int
    expected_state_version: int
    event_type: ExecutionEventType
    payload: Mapping[str, Any]
    event_id: str

    def to_data(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "execution_id": self.execution_id,
            "sequence": self.sequence,
            "expected_state_version": self.expected_state_version,
            "event_type": self.event_type.value,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ExecutionEvent:
        payload = data.get("payload", {})
        if not isinstance(payload, Mapping):
            raise ValueError("Execution event payload must be an object.")
        return cls(
            event_id=_required_string(data, "event_id"),
            execution_id=_required_string(data, "execution_id"),
            sequence=_required_nonnegative_int(data, "sequence"),
            expected_state_version=_required_nonnegative_int(
                data, "expected_state_version"
            ),
            event_type=ExecutionEventType(_required_string(data, "event_type")),
            payload=dict(payload),
        )


def initial_execution_state(
    execution_id: str,
    *,
    profile_id: str,
    mode: ExecutionMode = ExecutionMode.OBSERVE,
    phase: PhaseType = PhaseType.INTAKE,
) -> ExecutionState:
    """Create the zero-event state for a new execution."""
    if not execution_id.strip():
        raise ValueError("execution_id must be a non-empty string.")
    if not profile_id.strip():
        raise ValueError("profile_id must be a non-empty string.")
    return ExecutionState(
        execution_id=execution_id,
        profile_id=profile_id,
        mode=mode,
        current_phase=phase,
    )


def reduce_execution_event(
    state: ExecutionState,
    event: ExecutionEvent,
) -> ExecutionState:
    """Apply one event after checking sequence and optimistic version guards."""
    if event.execution_id != state.execution_id:
        raise ValueError("Execution event belongs to a different execution.")
    if event.sequence != state.event_sequence + 1:
        raise ValueError(
            f"Execution event sequence must be {state.event_sequence + 1}."
        )
    if event.expected_state_version != state.state_version:
        raise ValueError(
            "Execution event expected_state_version does not match current state."
        )

    payload = event.payload
    next_state = state
    if event.event_type is ExecutionEventType.PHASE_ENTERED:
        next_state = replace(
            state,
            current_phase=PhaseType(_required_string(payload, "phase_type")),
            current_persona_id=_optional_string(payload.get("persona_id")),
            phase_revision=state.phase_revision + 1,
        )
    elif event.event_type is ExecutionEventType.PHASE_EXITED:
        next_state = state
    elif event.event_type in {
        ExecutionEventType.ACTION_PROPOSED,
        ExecutionEventType.ACTION_STARTED,
        ExecutionEventType.ACTION_COMPLETED,
        ExecutionEventType.ACTION_FAILED,
    }:
        next_state = _reduce_action_event(state, event.event_type, payload)
    elif event.event_type is ExecutionEventType.ARTIFACT_PRODUCED:
        artifact = _parse_artifact(payload)
        next_state = replace(
            state, artifacts=_upsert(state.artifacts, artifact, "artifact_id")
        )
    elif event.event_type is ExecutionEventType.ARTIFACT_ACCEPTED:
        artifact_id = _required_string(payload, "artifact_id")
        next_state = replace(
            state,
            artifacts=tuple(
                replace(item, accepted=True)
                if item.artifact_id == artifact_id
                else item
                for item in state.artifacts
            ),
        )
    elif event.event_type is ExecutionEventType.OBLIGATION_OPENED:
        obligation = _parse_obligation(payload)
        next_state = replace(
            state, obligations=_upsert(state.obligations, obligation, "obligation_id")
        )
    elif event.event_type is ExecutionEventType.OBLIGATION_SATISFIED:
        next_state = _set_obligation_status(
            state,
            _required_string(payload, "obligation_id"),
            ObligationStatus.SATISFIED,
        )
    elif event.event_type is ExecutionEventType.EVIDENCE_RECORDED:
        evidence = _parse_evidence(payload)
        next_state = replace(
            state, evidence=_upsert(state.evidence, evidence, "evidence_id")
        )
    elif event.event_type is ExecutionEventType.EVIDENCE_INVALIDATED:
        evidence_id = _required_string(payload, "evidence_id")
        next_state = replace(
            state,
            evidence=tuple(
                replace(item, fresh=False) if item.evidence_id == evidence_id else item
                for item in state.evidence
            ),
        )
    elif event.event_type is ExecutionEventType.FINDING_OPENED:
        finding = _parse_finding(payload)
        next_state = replace(
            state, findings=_upsert(state.findings, finding, "finding_id")
        )
    elif event.event_type is ExecutionEventType.FINDING_DISPOSED:
        finding_id = _required_string(payload, "finding_id")
        status = FindingStatus(_required_string(payload, "status"))
        next_state = replace(
            state,
            findings=tuple(
                replace(item, status=status) if item.finding_id == finding_id else item
                for item in state.findings
            ),
        )
    elif event.event_type in {
        ExecutionEventType.CREATED,
        ExecutionEventType.CHECKPOINT_CREATED,
        ExecutionEventType.CHECKPOINT_REVERTED,
        ExecutionEventType.CAPABILITY_DECISION,
    }:
        next_state = state

    return replace(
        next_state,
        state_version=state.state_version + 1,
        event_sequence=event.sequence,
    )


def reduce_execution_events(
    state: ExecutionState,
    events: tuple[ExecutionEvent, ...],
) -> ExecutionState:
    for event in events:
        state = reduce_execution_event(state, event)
    return state


def _reduce_action_event(
    state: ExecutionState,
    event_type: ExecutionEventType,
    payload: Mapping[str, Any],
) -> ExecutionState:
    action_id = _required_string(payload, "action_instance_id")
    status = {
        ExecutionEventType.ACTION_PROPOSED: ActionStatus.PROPOSED,
        ExecutionEventType.ACTION_STARTED: ActionStatus.STARTED,
        ExecutionEventType.ACTION_COMPLETED: ActionStatus.COMPLETED,
        ExecutionEventType.ACTION_FAILED: ActionStatus(
            payload.get("status", ActionStatus.CORRECTABLE_ERROR.value)
        ),
    }[event_type]
    existing = next(
        (item for item in state.actions if item.action_instance_id == action_id), None
    )
    record = ActionRecord(
        action_instance_id=action_id,
        kind=_required_string(payload, "kind") if existing is None else existing.kind,
        status=status,
        actor_id=_required_string(payload, "actor_id")
        if existing is None
        else existing.actor_id,
        phase_type=PhaseType(_required_string(payload, "phase_type"))
        if existing is None
        else existing.phase_type,
        arguments_fingerprint=_optional_string(payload.get("arguments_fingerprint"))
        if existing is None
        else existing.arguments_fingerprint,
        error_code=_optional_string(payload.get("error_code")),
    )
    return replace(state, actions=_upsert(state.actions, record, "action_instance_id"))


def _set_obligation_status(
    state: ExecutionState, obligation_id: str, status: ObligationStatus
) -> ExecutionState:
    return replace(
        state,
        obligations=tuple(
            replace(item, status=status)
            if item.obligation_id == obligation_id
            else item
            for item in state.obligations
        ),
    )


def _upsert(items: tuple[Any, ...], value: Any, key: str) -> tuple[Any, ...]:
    value_id = getattr(value, key)
    replaced = False
    result: list[Any] = []
    for item in items:
        if getattr(item, key) == value_id:
            result.append(value)
            replaced = True
        else:
            result.append(item)
    if not replaced:
        result.append(value)
    return tuple(result)


def _required_string(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Execution data field {key!r} must be a non-empty string.")
    return value.strip()


def _optional_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _required_nonnegative_int(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(
            f"Execution data field {key!r} must be a non-negative integer."
        )
    return value


def _mapping_sequence(
    data: Mapping[str, Any], key: str
) -> tuple[Mapping[str, Any], ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(
        isinstance(item, Mapping) for item in value
    ):
        raise ValueError(f"Execution data field {key!r} must be an array of objects.")
    return tuple(cast(Mapping[str, Any], item) for item in value)


def _parse_artifact(data: Mapping[str, Any]) -> ExecutionArtifact:
    return ExecutionArtifact(
        artifact_id=_required_string(data, "artifact_id"),
        artifact_type=_required_string(data, "artifact_type"),
        schema_version=_required_string(data, "schema_version"),
        owner_persona_id=_required_string(data, "owner_persona_id"),
        content_ref=_required_string(data, "content_ref"),
        accepted=bool(data.get("accepted", False)),
    )


def _parse_action(data: Mapping[str, Any]) -> ActionRecord:
    return ActionRecord(
        action_instance_id=_required_string(data, "action_instance_id"),
        kind=_required_string(data, "kind"),
        status=ActionStatus(_required_string(data, "status")),
        actor_id=_required_string(data, "actor_id"),
        phase_type=PhaseType(_required_string(data, "phase_type")),
        arguments_fingerprint=_optional_string(data.get("arguments_fingerprint")),
        error_code=_optional_string(data.get("error_code")),
    )


def _parse_obligation(data: Mapping[str, Any]) -> ExecutionObligation:
    return ExecutionObligation(
        obligation_id=_required_string(data, "obligation_id"),
        description=_required_string(data, "description"),
        status=ObligationStatus(data.get("status", ObligationStatus.OPEN.value)),
        source_action_instance_id=_optional_string(
            data.get("source_action_instance_id")
        ),
        required_action=_optional_string(data.get("required_action")),
        relationship_id=_optional_string(data.get("relationship_id")),
    )


def _parse_evidence(data: Mapping[str, Any]) -> ExecutionEvidence:
    return ExecutionEvidence(
        evidence_id=_required_string(data, "evidence_id"),
        producer_action_instance_id=_required_string(
            data, "producer_action_instance_id"
        ),
        evidence_type=_required_string(data, "evidence_type"),
        input_fingerprint=_required_string(data, "input_fingerprint"),
        successful=bool(data.get("successful", False)),
        fresh=bool(data.get("fresh", True)),
    )


def _parse_finding(data: Mapping[str, Any]) -> ExecutionFinding:
    return ExecutionFinding(
        finding_id=_required_string(data, "finding_id"),
        reviewer_run_id=_required_string(data, "reviewer_run_id"),
        severity=_required_string(data, "severity"),
        description=_required_string(data, "description"),
        status=FindingStatus(data.get("status", FindingStatus.OPEN.value)),
        blocking=bool(data.get("blocking", True)),
    )
