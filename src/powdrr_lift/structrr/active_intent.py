"""Resolve the repository's canonical active-intent inventory."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ACTIVE_INTENT_SECTION_VERSION = 1


@dataclass(frozen=True, slots=True)
class ActiveIntentReference:
    """A normalized intent clause suitable for evidence and review packets."""

    clause_id: str
    intent_id: str
    kind: str
    statement: str
    source_ref: str
    version: int = 1
    active: bool = True
    supersedes_clause_id: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "clause_id": self.clause_id,
            "intent_id": self.intent_id,
            "kind": self.kind,
            "statement": self.statement,
            "source_ref": self.source_ref,
            "version": self.version,
            "active": self.active,
            "supersedes_clause_id": self.supersedes_clause_id,
        }


class ActiveIntentResolutionError(ValueError):
    """Raised when active intent sources conflict or resolve inconsistently."""


def resolve_active_intent(
    worktree: str | Path,
    *,
    baseline_document: Mapping[str, Any] | None = None,
    feature_document: Mapping[str, Any] | None = None,
) -> tuple[ActiveIntentReference, ...]:
    """Resolve committed Structrr intent plus a proposal overlay.

    Committed Structrr is accepted, read-only state. A feature document is an
    unaccepted overlay: an explicitly removed clause is removed, while an
    added or changed clause replaces its baseline entry. No workflow directory
    or runtime intent store participates in resolution.
    """
    baseline = _document_entries(baseline_document)
    del worktree
    accepted = _merge_accepted(baseline)
    if feature_document is not None:
        accepted = _apply_feature_overlay(
            accepted, _document_entries(feature_document, include_removed=True)
        )
    return tuple(accepted[key] for key in sorted(accepted))


def active_intent_section(
    baseline_entries: Sequence[Mapping[str, Any]],
    worktree: str | Path,
) -> list[dict[str, Any]]:
    """Build the generated bootstrap section from Structrr specification data."""
    del worktree
    entries = _merge_accepted(
        tuple(_entry_from_mapping(item) for item in baseline_entries)
    )
    return [entries[key].to_data() for key in sorted(entries)]


def validate_active_intent_section(value: object) -> tuple[str, ...]:
    """Return structural diagnostics for a generated active-intent section."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ("active_intent section must be an array",)
    diagnostics: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            diagnostics.append(f"active_intent[{index}] must be an object")
            continue
        clause_id = raw.get("clause_id")
        if not isinstance(clause_id, str) or not clause_id.strip():
            diagnostics.append(f"active_intent[{index}] requires clause_id")
        else:
            if clause_id in seen:
                diagnostics.append(f"active_intent clause is duplicated: {clause_id}")
            seen.add(clause_id)
        for key in ("intent_id", "kind", "statement", "source_ref"):
            if not isinstance(raw.get(key), str) or not raw[key].strip():
                diagnostics.append(f"active_intent[{index}] requires {key}")
        version = raw.get("version")
        if not isinstance(version, int) or version < 1:
            diagnostics.append(f"active_intent[{index}] version must be positive")
        active = raw.get("active")
        if not isinstance(active, bool):
            diagnostics.append(f"active_intent[{index}] active must be boolean")
    return tuple(diagnostics)


def _document_entries(
    document: Mapping[str, Any] | None,
    *,
    include_removed: bool = False,
) -> tuple[ActiveIntentReference, ...]:
    if document is None:
        return ()
    raw_entries = document.get("active_intent")
    if raw_entries is None:
        raw_entries = [
            item
            for section in ("invariants", "guidance")
            for item in _mappings(document.get(section))
        ]
    entries: list[ActiveIntentReference] = []
    for raw in _mappings(raw_entries):
        action = raw.get("action")
        state = raw.get("state")
        removed = action == "removed" or state in {"removed", "retired", "superseded"}
        if removed and not include_removed:
            continue
        if removed:
            raw = {**raw, "active": False}
        entries.append(_entry_from_mapping(raw))
    return tuple(entries)


def _entry_from_mapping(raw: Mapping[str, Any]) -> ActiveIntentReference:
    clause_id = _required_text(raw, "clause_id", fallback_key="id")
    statement = _required_text(raw, "statement", fallback_key="description")
    source_ref = _required_text(
        raw,
        "source_ref",
        fallback_key="source",
        fallback=f"document:{clause_id}",
    )
    intent_id = _required_text(raw, "intent_id", fallback=f"spec:{clause_id}")
    kind = _required_text(raw, "kind", fallback="invariant")
    version = raw.get("version", 1)
    if not isinstance(version, int) or version < 1:
        raise ActiveIntentResolutionError(
            f"active intent {clause_id!r} has an invalid version"
        )
    return ActiveIntentReference(
        clause_id=clause_id,
        intent_id=intent_id,
        kind=kind,
        statement=statement,
        source_ref=source_ref,
        version=version,
        active=raw.get("active", True) is True,
        supersedes_clause_id=(
            raw.get("supersedes_clause_id")
            if isinstance(raw.get("supersedes_clause_id"), str)
            else None
        ),
    )


def _merge_accepted(
    entries: Sequence[ActiveIntentReference],
) -> dict[str, ActiveIntentReference]:
    merged: dict[str, ActiveIntentReference] = {}
    for entry in entries:
        if not entry.active:
            continue
        current = merged.get(entry.clause_id)
        if current is None:
            merged[entry.clause_id] = entry
            continue
        if _semantic_fingerprint(current) != _semantic_fingerprint(entry):
            raise ActiveIntentResolutionError(
                f"conflicting active intent clause: {entry.clause_id!r}"
            )
        if entry.source_ref < current.source_ref:
            merged[entry.clause_id] = entry
    return merged


def _apply_feature_overlay(
    accepted: dict[str, ActiveIntentReference],
    entries: Sequence[ActiveIntentReference],
) -> dict[str, ActiveIntentReference]:
    result = dict(accepted)
    for entry in entries:
        if entry.active:
            result[entry.clause_id] = entry
        else:
            result.pop(entry.clause_id, None)
    return result


def _semantic_fingerprint(entry: ActiveIntentReference) -> tuple[Any, ...]:
    return (
        entry.intent_id,
        entry.kind,
        entry.statement,
        entry.version,
        entry.active,
        entry.supersedes_clause_id,
    )


def _required_text(
    raw: Mapping[str, Any],
    key: str,
    *,
    fallback_key: str | None = None,
    fallback: str | None = None,
) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        value = raw.get(fallback_key) if fallback_key else None
    if not isinstance(value, str) or not value.strip():
        value = fallback
    if not isinstance(value, str) or not value.strip():
        raise ActiveIntentResolutionError(f"active intent entry requires {key}")
    return value


def _mappings(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


__all__ = [
    "ACTIVE_INTENT_SECTION_VERSION",
    "ActiveIntentReference",
    "ActiveIntentResolutionError",
    "active_intent_section",
    "resolve_active_intent",
    "validate_active_intent_section",
]
