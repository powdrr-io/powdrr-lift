"""Immutable source contracts for the procedrr control plane.

The model deliberately contains no provider, filesystem, or process APIs. It is
the shared representation consumed by validation and by a future runtime.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ProofStatus(StrEnum):
    PROVEN = "proven"
    UNKNOWN = "unknown"
    DISPROVEN = "disproven"


class Guarantee(StrEnum):
    DECISION_SAFE = "decision_safe"
    CONTROL_TERMINATION_SAFE = "control_termination_safe"
    OPERATION_TERMINATION_SAFE = "operation_termination_safe"
    TERMINATION_SAFE = "termination_safe"
    RESOURCE_SAFE = "resource_safe"


class DecisionKind(StrEnum):
    CLASSIFY_ONE = "classify_one"
    CONSTRUCT_ONE = "construct_one"
    EXTRACT_BOUNDED_SET = "extract_bounded_set"


class TerminalStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    SUSPENDED = "suspended"


def _require_positive(name: str, value: int) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class ActivationLimits:
    """Kernel-enforced limits for one LLM or operation activation."""

    wall_time_seconds: int = 60
    input_tokens: int = 8_000
    output_tokens: int = 4_000
    tool_calls: int = 1
    output_bytes: int = 1_000_000
    external_cost_microunits: int = 0
    response_repairs: int = 0

    def __post_init__(self) -> None:
        for name in (
            "wall_time_seconds",
            "input_tokens",
            "output_tokens",
            "tool_calls",
            "output_bytes",
        ):
            _require_positive(name, getattr(self, name))
        if self.external_cost_microunits < 0 or self.response_repairs < 0:
            raise ValueError("cost and response repairs cannot be negative")

    def to_data(self) -> dict[str, int]:
        return {
            "wall_time_seconds": self.wall_time_seconds,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tool_calls": self.tool_calls,
            "output_bytes": self.output_bytes,
            "external_cost_microunits": self.external_cost_microunits,
            "response_repairs": self.response_repairs,
        }


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Whole-workflow limits. These values are part of the contract."""

    llm_activations: int = 1_024
    tool_calls: int = 10_000
    llm_tokens: int = 8_000_000
    external_cost_microunits: int = 200_000_000
    max_epochs: int = 3

    def __post_init__(self) -> None:
        for name in (
            "llm_activations",
            "tool_calls",
            "llm_tokens",
            "max_epochs",
        ):
            _require_positive(name, getattr(self, name))
        if self.external_cost_microunits < 0:
            raise ValueError("external cost cannot be negative")

    def to_data(self) -> dict[str, int]:
        return {
            "llm_activations": self.llm_activations,
            "tool_calls": self.tool_calls,
            "llm_tokens": self.llm_tokens,
            "external_cost_microunits": self.external_cost_microunits,
            "max_epochs": self.max_epochs,
        }


@dataclass(frozen=True, slots=True)
class DecisionContract:
    """One model question with one typed output and no model authority."""

    kind: DecisionKind
    question: str
    subject: str
    output_name: str
    output_schema: Mapping[str, Any]
    validator: str
    transport_action_const: str
    limits: ActivationLimits = field(default_factory=ActivationLimits)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, DecisionKind):
            raise ValueError("kind must be a DecisionKind")
        for name in (
            "question",
            "subject",
            "output_name",
            "validator",
            "transport_action_const",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} cannot be empty")
        if not isinstance(self.output_schema, Mapping) or not self.output_schema:
            raise ValueError("output_schema must be a non-empty mapping")
        if self.transport_action_const in {"", "next_step", "complete", "retry"}:
            raise ValueError("transport action must be a dedicated constant")

    def to_data(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "question": self.question,
            "subject": self.subject,
            "output_name": self.output_name,
            "output_schema": dict(self.output_schema),
            "validator": self.validator,
            "transport_action_const": self.transport_action_const,
            "limits": self.limits.to_data(),
        }


@dataclass(frozen=True, slots=True)
class SnapshotSpec:
    """A sealed, keyed collection that may safely drive iteration."""

    name: str
    item_schema: Mapping[str, Any]
    key_path: str
    max_items: int

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.key_path.strip():
            raise ValueError("snapshot name and key_path cannot be empty")
        if not self.item_schema:
            raise ValueError("snapshot item_schema cannot be empty")
        _require_positive("max_items", self.max_items)

    def to_data(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "item_schema": dict(self.item_schema),
            "key_path": self.key_path,
            "max_items": self.max_items,
        }


@dataclass(frozen=True, slots=True)
class MatchCase:
    value: Any
    body: ControlNode
    label: str = ""

    def to_data(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "body": self.body.to_data(),
            "label": self.label,
        }


class ControlNode:
    """Marker base class for structured control nodes."""

    def to_data(self) -> dict[str, Any]:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class SequenceNode(ControlNode):
    nodes: tuple[ControlNode, ...]

    def __init__(self, nodes: Sequence[ControlNode]) -> None:
        object.__setattr__(self, "nodes", tuple(nodes))

    def to_data(self) -> dict[str, Any]:
        return {"kind": "sequence", "nodes": [node.to_data() for node in self.nodes]}


@dataclass(frozen=True, slots=True)
class JudgeNode(ControlNode):
    decision: DecisionContract

    def to_data(self) -> dict[str, Any]:
        return {"kind": "judge", "decision": self.decision.to_data()}


@dataclass(frozen=True, slots=True)
class OperationNode(ControlNode):
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    output_name: str | None = None
    activation_limits: ActivationLimits = field(default_factory=ActivationLimits)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("operation name cannot be empty")

    def to_data(self) -> dict[str, Any]:
        return {
            "kind": "operation",
            "name": self.name,
            "arguments": dict(self.arguments),
            "output_name": self.output_name,
            "activation_limits": self.activation_limits.to_data(),
        }


@dataclass(frozen=True, slots=True)
class MatchNode(ControlNode):
    value_binding: str
    cases: tuple[MatchCase, ...]
    otherwise: ControlNode | None = None

    def __init__(
        self,
        value_binding: str,
        cases: Sequence[MatchCase],
        otherwise: ControlNode | None = None,
    ) -> None:
        object.__setattr__(self, "value_binding", value_binding)
        object.__setattr__(self, "cases", tuple(cases))
        object.__setattr__(self, "otherwise", otherwise)

    def to_data(self) -> dict[str, Any]:
        return {
            "kind": "match",
            "value_binding": self.value_binding,
            "cases": [case.to_data() for case in self.cases],
            "otherwise": self.otherwise.to_data() if self.otherwise else None,
        }


@dataclass(frozen=True, slots=True)
class ForEachNode(ControlNode):
    snapshot: SnapshotSpec
    item_binding: str
    body: ControlNode
    max_parallel: int = 1
    item_retry_budget: int = 0

    def __post_init__(self) -> None:
        if not self.item_binding.strip():
            raise ValueError("item_binding cannot be empty")
        _require_positive("max_parallel", self.max_parallel)
        if self.item_retry_budget < 0:
            raise ValueError("item_retry_budget cannot be negative")

    def to_data(self) -> dict[str, Any]:
        return {
            "kind": "for_each",
            "snapshot": self.snapshot.to_data(),
            "item_binding": self.item_binding,
            "body": self.body.to_data(),
            "max_parallel": self.max_parallel,
            "item_retry_budget": self.item_retry_budget,
        }


@dataclass(frozen=True, slots=True)
class WorklistNode(ControlNode):
    snapshot: SnapshotSpec
    item_binding: str
    body: ControlNode
    max_admissions: int
    max_epochs: int

    def __post_init__(self) -> None:
        if not self.item_binding.strip():
            raise ValueError("item_binding cannot be empty")
        _require_positive("max_admissions", self.max_admissions)
        _require_positive("max_epochs", self.max_epochs)

    def to_data(self) -> dict[str, Any]:
        return {
            "kind": "worklist",
            "snapshot": self.snapshot.to_data(),
            "item_binding": self.item_binding,
            "body": self.body.to_data(),
            "max_admissions": self.max_admissions,
            "max_epochs": self.max_epochs,
        }


@dataclass(frozen=True, slots=True)
class RepeatNode(ControlNode):
    body: ControlNode
    budget: int

    def __post_init__(self) -> None:
        _require_positive("budget", self.budget)

    def to_data(self) -> dict[str, Any]:
        return {"kind": "repeat", "body": self.body.to_data(), "budget": self.budget}


@dataclass(frozen=True, slots=True)
class RetryNode(ControlNode):
    body: ControlNode
    budget: int
    retry_on: tuple[str, ...] = ()

    def __init__(
        self, body: ControlNode, budget: int, retry_on: Sequence[str] = ()
    ) -> None:
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "budget", budget)
        object.__setattr__(self, "retry_on", tuple(retry_on))
        _require_positive("budget", budget)

    def to_data(self) -> dict[str, Any]:
        return {
            "kind": "retry",
            "body": self.body.to_data(),
            "budget": self.budget,
            "retry_on": list(self.retry_on),
        }


@dataclass(frozen=True, slots=True)
class ParallelNode(ControlNode):
    branches: tuple[ControlNode, ...]
    max_parallel: int = 1

    def __init__(self, branches: Sequence[ControlNode], max_parallel: int = 1) -> None:
        object.__setattr__(self, "branches", tuple(branches))
        object.__setattr__(self, "max_parallel", max_parallel)
        _require_positive("max_parallel", max_parallel)

    def to_data(self) -> dict[str, Any]:
        return {
            "kind": "parallel",
            "branches": [branch.to_data() for branch in self.branches],
            "max_parallel": self.max_parallel,
        }


@dataclass(frozen=True, slots=True)
class CallNode(ControlNode):
    name: str
    body: ControlNode

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("call name cannot be empty")

    def to_data(self) -> dict[str, Any]:
        return {"kind": "call", "name": self.name, "body": self.body.to_data()}


@dataclass(frozen=True, slots=True)
class SuspendNode(ControlNode):
    reason: str
    resume_on: str

    def __post_init__(self) -> None:
        if not self.reason.strip() or not self.resume_on.strip():
            raise ValueError("suspension reason and resume event are required")

    def to_data(self) -> dict[str, str]:
        return {"kind": "suspend", "reason": self.reason, "resume_on": self.resume_on}


@dataclass(frozen=True, slots=True)
class TerminalNode(ControlNode):
    status: TerminalStatus

    def to_data(self) -> dict[str, str]:
        return {"kind": "terminal", "status": self.status.value}


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    name: str
    body: ControlNode
    limits: ResourceLimits = field(default_factory=ResourceLimits)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("workflow name cannot be empty")

    def to_data(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "body": self.body.to_data(),
            "limits": self.limits.to_data(),
        }

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(self.to_data(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "ActivationLimits",
    "CallNode",
    "ControlNode",
    "DecisionContract",
    "DecisionKind",
    "ForEachNode",
    "Guarantee",
    "JudgeNode",
    "MatchCase",
    "MatchNode",
    "OperationNode",
    "ParallelNode",
    "ProofStatus",
    "RepeatNode",
    "ResourceLimits",
    "RetryNode",
    "SequenceNode",
    "SnapshotSpec",
    "SuspendNode",
    "TerminalNode",
    "TerminalStatus",
    "WorkflowDefinition",
    "WorklistNode",
]
