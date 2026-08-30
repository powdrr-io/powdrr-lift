"""Deterministic context compaction retaining typed execution references."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

TYPED_REFERENCE_KEYS = frozenset(
    {
        "phase",
        "persona_id",
        "artifact_ids",
        "plan_id",
        "action_ids",
        "obligation_ids",
        "evidence_ids",
        "finding_ids",
        "exception_ids",
        "rule_ids",
        "checkpoint_ids",
    }
)


def compact_execution_context(
    context: Mapping[str, Any], *, max_preview_chars: int = 1_000
) -> dict[str, Any]:
    """Drop verbose previews while preserving all typed identifiers exactly."""

    compacted: dict[str, Any] = {}
    for key, value in context.items():
        if key in TYPED_REFERENCE_KEYS:
            compacted[key] = value
        elif isinstance(value, str) and len(value) > max_preview_chars:
            compacted[key] = value[: max_preview_chars - 1] + "…"
        elif key in {"prose", "tool_output", "transcript"}:
            compacted[key] = str(value)[:max_preview_chars]
        else:
            compacted[key] = value
    return compacted


def compatibility_diagnostic(document: Mapping[str, Any]) -> str | None:
    version = document.get("schema_version")
    if version is None:
        return "persisted workflow has no schema_version; migration is required"
    if version not in {"execution-state-v1", "execution-plan-v1", "behavior-rule-v1"}:
        return f"unsupported persisted workflow schema: {version!r}"
    return None
