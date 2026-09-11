"""Immutable boundary values exchanged with an LLM agent.

These values intentionally contain projections rather than implementation
objects.  An agent sees the procedure and current execution facts it needs to
make a proposal; it does not receive a mutable workflow definition, runtime,
or tool adapter.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ProcedureView:
    """The immutable, agent-relevant projection of a compiled procedure."""

    definition_id: str
    contract_fingerprint: str
    step_id: str
    step_description: str
    allowed_actions: tuple[str, ...]
    allowed_outcomes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StateProjection:
    """Current execution facts supplied to an agent as read-only data."""

    execution_id: str
    step_activation_id: str
    step_id: str
    operation_statuses: Mapping[str, str]
    outputs: Mapping[str, Any]
    open_obligations: tuple[str, ...] = ()
    valid_evidence: tuple[str, ...] = ()
    legal_actions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Observation:
    """The newest typed fact available to the agent."""

    kind: str
    data: Mapping[str, Any]
    source: str


@dataclass(frozen=True, slots=True)
class AgentInput:
    """Complete input for one model proposal."""

    procedure: ProcedureView
    state: StateProjection
    observation: Observation | None = None


@dataclass(frozen=True, slots=True)
class AgentProposal:
    """Untrusted model output awaiting kernel validation."""

    action: Mapping[str, Any] | None = None
    decision: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class KernelResult:
    """Kernel-owned result returned after a proposal is processed."""

    operation_id: str
    operation_status: str
    observation: Observation
    transition: str | None = None
