"""Static capability effects used by workflow-definition liveness checks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CapabilityEffect:
    """Conservative, execution-free summary of one bounded capability."""

    operation: str
    determinism: str
    idempotence: str
    reads: frozenset[str]
    writes: frozenset[str]
    produces: frozenset[str] = frozenset()


_EFFECTS: dict[tuple[str, str], CapabilityEffect] = {
    ("git", "status"): CapabilityEffect(
        "status",
        "deterministic",
        "idempotent",
        frozenset({"repository"}),
        frozenset(),
        frozenset({"tool_result"}),
    ),
    ("git", "add"): CapabilityEffect(
        "add",
        "deterministic",
        "idempotent",
        frozenset({"files"}),
        frozenset({"repository_index"}),
    ),
    ("git", "move"): CapabilityEffect(
        "move",
        "deterministic",
        "conditional",
        frozenset({"files", "repository_index"}),
        frozenset({"files", "repository_index"}),
    ),
    ("git", "commit"): CapabilityEffect(
        "commit",
        "conditional",
        "non_idempotent",
        frozenset({"repository_index"}),
        frozenset({"repository_history"}),
    ),
    ("git", "push"): CapabilityEffect(
        "push",
        "conditional",
        "conditional",
        frozenset({"repository_history"}),
        frozenset({"remote_repository"}),
    ),
    ("internal", "repository-state"): CapabilityEffect(
        "repository-state",
        "deterministic",
        "idempotent",
        frozenset({"repository"}),
        frozenset(),
        frozenset({"repository_state"}),
    ),
}


def capability_effect(invocation: Mapping[str, Any]) -> CapabilityEffect | None:
    """Return a bounded effect summary without executing the invocation."""
    tool = invocation.get("tool")
    if not isinstance(tool, str):
        return None
    operation = invocation.get("operation")
    if isinstance(operation, str):
        return _EFFECTS.get((tool, operation))
    command = invocation.get("command")
    if not isinstance(command, Sequence) or isinstance(
        command, (str, bytes, bytearray)
    ):
        return None
    if not command or not all(isinstance(item, str) for item in command):
        return None
    operation = command[0]
    return _EFFECTS.get((tool, operation))


def is_fixed_deterministic(effect: CapabilityEffect | None) -> bool:
    return effect is not None and effect.determinism == "deterministic"


def is_idempotent(effect: CapabilityEffect | None) -> bool:
    return effect is not None and effect.idempotence == "idempotent"
