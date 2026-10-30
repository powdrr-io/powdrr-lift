"""Bounded source context helpers for instruction classification."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

CONTEXT_RETRY_REASONS = frozenset({"source_ambiguous", "source_underspecified"})


def context_retry_reason(response: Mapping[str, Any]) -> str | None:
    """Return the unresolved reason that should trigger a local-context retry."""
    if response.get("status") != "unresolved":
        return None
    reason = response.get("reason_code")
    return reason if reason in CONTEXT_RETRY_REASONS else None


def local_instruction_context(
    instruction: str,
    source_clauses: Sequence[Mapping[str, Any]],
    target_clause: Mapping[str, Any],
) -> dict[str, str] | None:
    """Get the exact containing source sentence and its immediate neighbors."""
    target_span = target_clause.get("source_span")
    if not isinstance(target_span, Mapping):
        return None
    target_key = (target_span.get("start"), target_span.get("end"))

    source_index = next(
        (
            index
            for index, clause in enumerate(source_clauses)
            if _span_key(clause.get("source_span")) == target_key
        ),
        None,
    )
    if source_index is None:
        parent_id = target_clause.get("parent_clause_id")
        if isinstance(parent_id, str) and parent_id.startswith("candidate:"):
            parent_id = parent_id.removeprefix("candidate:")
        source_index = next(
            (
                index
                for index, clause in enumerate(source_clauses)
                if clause.get("clause_id") == parent_id
            ),
            None,
        )
    if source_index is None:
        return None

    result: dict[str, str] = {}
    for key, index in (
        ("previous_sentence", source_index - 1),
        ("source_sentence", source_index),
        ("next_sentence", source_index + 1),
    ):
        if not 0 <= index < len(source_clauses):
            continue
        span_key = _span_key(source_clauses[index].get("source_span"))
        if span_key is None:
            continue
        start, end = span_key
        text = instruction[start:end].strip()
        if text:
            result[key] = text

    proposition = target_clause.get("text")
    if not result or (
        result.get("source_sentence") == proposition
        and "previous_sentence" not in result
        and "next_sentence" not in result
    ):
        return None
    return result


def _span_key(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, Mapping):
        return None
    start = value.get("start")
    end = value.get("end")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or start < 0
        or end <= start
    ):
        return None
    return start, end
