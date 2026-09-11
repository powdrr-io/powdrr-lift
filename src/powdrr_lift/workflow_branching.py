"""Deterministic branch selection shared by workflow execution engines."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def resolve_handoff_path(
    path: str, handoff_records: Mapping[str, Mapping[str, Any]]
) -> Any:
    """Resolve a dotted path from the value portion of a handoff record."""
    parts = [part for part in path.split(".") if part]
    if not parts or parts[0] not in handoff_records:
        return None
    value: Any = handoff_records[parts[0]].get("value")
    for part in parts[1:]:
        if isinstance(value, Mapping):
            value = value.get(part)
        else:
            return None
    return value


def select_branch_target(
    branch: Any, handoff_records: Mapping[str, Mapping[str, Any]]
) -> str:
    """Return the first matching branch target, or the required default."""
    for case in branch.cases:
        if resolve_handoff_path(case.path, handoff_records) == case.equals:
            return case.goto_step
    return branch.default_goto_step
