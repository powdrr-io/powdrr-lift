"""Deterministic context compaction retaining typed execution references."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
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


class FileContextRetrievalStore:
    """Bounded retrieval store for prompt content omitted during compaction."""

    def __init__(self, directory: str | Path) -> None:
        self.root = Path(directory) / "execution" / "context"

    def save(self, context: Mapping[str, Any]) -> str:
        encoded = json.dumps(dict(context), sort_keys=True, default=str)
        reference = hashlib.sha256(encoded.encode()).hexdigest()[:24]
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / f"{reference}.json").write_text(encoded + "\n", encoding="utf-8")
        return reference

    def load(self, reference: str) -> dict[str, Any]:
        value = json.loads(
            (self.root / f"{reference}.json").read_text(encoding="utf-8")
        )
        if not isinstance(value, dict):
            raise ValueError("stored execution context must be an object")
        return value


def compact_with_retrieval(
    context: Mapping[str, Any],
    store: FileContextRetrievalStore,
    *,
    max_preview_chars: int = 1_000,
) -> dict[str, Any]:
    """Compact prompt data and expose a reference to the complete payload."""
    reference = store.save(context)
    compacted = compact_execution_context(context, max_preview_chars=max_preview_chars)
    compacted["full_context_ref"] = reference
    return compacted


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
