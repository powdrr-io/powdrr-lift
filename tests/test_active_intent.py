from __future__ import annotations

from pathlib import Path

import pytest

from powdrr_lift.structrr.active_intent import (
    ActiveIntentResolutionError,
    resolve_active_intent,
    validate_active_intent_section,
)
from powdrr_lift.structrr.intent import (
    IntentClause,
    IntentContract,
    IntentKind,
    IntentStore,
    make_intent_source,
)


def _capture(store: IntentStore, clause_id: str, text: str) -> None:
    source = make_intent_source(
        intent_id=f"captured:{clause_id}",
        exact_text=text,
        source_ref=f"conversation:{clause_id}",
        supplied_by="user:test",
    )
    store.capture(
        source,
        (
            IntentClause(
                clause_id,
                source.intent_id,
                (0, len(text)),
                IntentKind.INVARIANT,
                IntentContract(),
            ),
        ),
    )


def test_resolver_merges_baseline_and_captured_intent(tmp_path: Path) -> None:
    _capture(IntentStore(tmp_path), "captured-clause", "Captured intent.")

    resolved = resolve_active_intent(
        tmp_path,
        baseline_document={
            "invariants": [
                {
                    "id": "baseline-clause",
                    "description": "Baseline intent.",
                    "source": "docs/spec.yaml",
                }
            ]
        },
    )

    assert [item.clause_id for item in resolved] == [
        "baseline-clause",
        "captured-clause",
    ]
    assert resolved[0].to_data()["statement"] == "Baseline intent."
    assert resolved[1].source_ref == "conversation:captured-clause"


def test_feature_overlay_replaces_and_removes_baseline_intent(tmp_path: Path) -> None:
    resolved = resolve_active_intent(
        tmp_path,
        baseline_document={
            "invariants": [
                {"id": "keep", "description": "Keep this.", "source": "base"},
                {
                    "id": "replace",
                    "description": "Old wording.",
                    "source": "base",
                },
                {
                    "id": "remove",
                    "description": "Retire this.",
                    "source": "base",
                },
            ]
        },
        feature_document={
            "invariants": [
                {
                    "id": "replace",
                    "action": "added",
                    "description": "New wording.",
                },
                {
                    "id": "remove",
                    "action": "removed",
                    "description": "Retire this.",
                },
            ]
        },
    )

    assert [(item.clause_id, item.statement) for item in resolved] == [
        ("keep", "Keep this."),
        ("replace", "New wording."),
    ]


def test_conflicting_accepted_sources_block_resolution(tmp_path: Path) -> None:
    _capture(IntentStore(tmp_path), "same-clause", "Captured wording.")

    with pytest.raises(ActiveIntentResolutionError, match="same-clause"):
        resolve_active_intent(
            tmp_path,
            baseline_document={
                "active_intent": [
                    {
                        "clause_id": "same-clause",
                        "intent_id": "captured:same-clause",
                        "kind": "invariant",
                        "statement": "Different wording.",
                        "source_ref": "docs/spec.yaml",
                    }
                ]
            },
        )


def test_active_intent_section_validation_rejects_duplicate_ids() -> None:
    diagnostics = validate_active_intent_section(
        [
            {
                "clause_id": "duplicate",
                "intent_id": "intent",
                "kind": "invariant",
                "statement": "One.",
                "source_ref": "docs/spec.yaml",
                "version": 1,
                "active": True,
            },
            {
                "clause_id": "duplicate",
                "intent_id": "intent",
                "kind": "invariant",
                "statement": "Two.",
                "source_ref": "docs/spec.yaml",
                "version": 1,
                "active": True,
            },
        ]
    )

    assert diagnostics == ("active_intent clause is duplicated: duplicate",)
