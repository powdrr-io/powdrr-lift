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
        "intent_ids",
        "clause_ids",
        "contract_fingerprint",
        "effective_contract",
        "checkpoint_ids",
    }
)


class FileContextRetrievalStore:
    """Bounded retrieval store for prompt content omitted during compaction."""

    def __init__(self, directory: str | Path, *, max_entries: int = 256) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.root = Path(directory) / "execution" / "context"
        self.max_entries = max_entries

    def save(self, context: Mapping[str, Any]) -> str:
        encoded = json.dumps(dict(context), sort_keys=True, default=str)
        reference = hashlib.sha256(encoded.encode()).hexdigest()[:24]
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / f"{reference}.json").write_text(encoded + "\n", encoding="utf-8")
        entries = sorted(
            self.root.glob("*.json"), key=lambda path: path.stat().st_mtime
        )
        for stale in entries[: -self.max_entries]:
            stale.unlink(missing_ok=True)
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
    reference = store.save(_bounded_retrieval_context(context))
    compacted = compact_execution_context(context, max_preview_chars=max_preview_chars)
    compacted["full_context_ref"] = reference
    return compacted


def _bounded_retrieval_context(
    value: Any, *, depth: int = 0, max_text: int = 4_000
) -> Any:
    """Prevent retrieval persistence from serializing unbounded prompt history."""
    if depth > 6:
        return "<nested context omitted>"
    if isinstance(value, str):
        if len(value) <= max_text:
            return value
        return (
            value[: max_text - 40] + f"… <{len(value) - max_text + 40} chars omitted>"
        )
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_retrieval_context(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        items = value[-32:] if len(value) > 32 else value
        return [_bounded_retrieval_context(item, depth=depth + 1) for item in items]
    return value


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
    if version not in {
        "execution-state-v1",
        "execution-plan-v1",
        "behavior-rule-v1",
        "intent-v1",
    }:
        return f"unsupported persisted workflow schema: {version!r}"
    return None


def compatibility_report(directory: str | Path) -> dict[str, Any]:
    """Report persisted files requiring migration without mutating them."""
    root = Path(directory)
    inspected = 0
    diagnostics: list[dict[str, str]] = []
    paths = sorted(root.rglob("*.json")) if root.exists() else ()
    for path in paths:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            diagnostics.append({"path": str(path), "diagnostic": "invalid JSON"})
            continue
        if isinstance(document, list):
            diagnostics.append(
                {
                    "path": str(path),
                    "diagnostic": "legacy behavior rules require intent migration",
                }
            )
            inspected += 1
            continue
        if not isinstance(document, Mapping):
            diagnostics.append(
                {"path": str(path), "diagnostic": "persisted document is not an object"}
            )
            continue
        inspected += 1
        diagnostic = compatibility_diagnostic(document)
        if diagnostic is not None:
            diagnostics.append({"path": str(path), "diagnostic": diagnostic})
    return {"directory": str(root), "inspected": inspected, "diagnostics": diagnostics}
