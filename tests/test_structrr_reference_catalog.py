from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest
from powdrr_service.decision_obligation import content_fingerprint
from powdrr_service.structrr.design import DesignHandoff, DesiredState
from powdrr_service.structrr.design_agreement import check_design_agreement
from powdrr_service.structrr.proposal import compile_proposal_revision
from powdrr_service.structrr.reference_catalog import (
    AuthorizedReferenceAmendment,
    ReferenceCatalog,
    ReferenceResolution,
    compile_reference_catalog,
    resolve_reference,
)
from powdrr_service.structrr.reference_definitions import (
    REFERENCE_KINDS,
    ReferenceDefinition,
)
from powdrr_service.structrr.source_manifest import compile_source_manifest

BASE = "a" * 40
OTHER_BASE = "b" * 40


def definition(
    ref: str = "opaque:ID", kind: str = "invariant", **changes: Any
) -> ReferenceDefinition:
    return replace(
        ReferenceDefinition(ref, kind, "Keep the exact requirement.", "spec:accepted"),
        **changes,
    )


def document(*definitions: ReferenceDefinition) -> dict[str, Any]:
    return {"reference_definitions": [item.to_data() for item in definitions]}


def amendment(
    accepted: dict[str, Any],
    *definitions: ReferenceDefinition,
    base_commit: str = BASE,
) -> dict[str, Any]:
    return AuthorizedReferenceAmendment(
        "workflow:approval-1",
        base_commit,
        content_fingerprint(accepted),
        tuple(definitions),
    ).to_data()


@pytest.mark.parametrize("kind", sorted(REFERENCE_KINDS))
def test_canonical_definitions_round_trip_before_catalog_projection(kind: str) -> None:
    original = definition(
        "not-a-kind-prefix:Exact",
        kind,
        rationale="Why this matters.",
        evidence_refs=("test:1",),
        invalidation_triggers=("Storage stops deduplicating.",)
        if kind == "assumption"
        else (),
    )
    restored = ReferenceDefinition.from_data(json.loads(json.dumps(original.to_data())))
    assert restored == original
    catalog = compile_reference_catalog(document(restored), base_commit=BASE)
    resolved = resolve_reference(catalog, original.id)
    assert resolved.status == "resolved"
    assert resolved.entry and resolved.entry.reference == original
    assert resolved.entry.authority == "accepted"
    assert resolved.to_data()["entry"]["kind"] == kind


def test_legacy_active_clauses_use_clause_ids_and_preserve_metadata() -> None:
    accepted = {
        "active_intent": [
            {
                "clause_id": "Clause/Exact",
                "intent_id": "source:1",
                "kind": "decision",
                "statement": "  Exact user wording.  ",
                "source_ref": "input:span-1",
                "version": 3,
                "active": True,
                "rationale": "Preserve compatibility.",
                "evidence_refs": ["review:1"],
            }
        ]
    }
    result = resolve_reference(
        compile_reference_catalog(accepted, base_commit=BASE), "Clause/Exact"
    )
    assert result.entry
    assert result.entry.reference.statement == "  Exact user wording.  "
    assert result.entry.reference.version == 3
    assert result.entry.reference.rationale == "Preserve compatibility."
    assert result.entry.reference.source_ref == "input:span-1"
    assert (
        resolve_reference(
            compile_reference_catalog(accepted, base_commit=BASE), "source:1"
        ).status
        == "unresolved_reference"
    )


def test_legacy_section_adapters_do_not_infer_kind_from_ids() -> None:
    accepted = {
        "features": [{"id": "decision:feature", "description": "Recover payments."}],
        "decisions": [
            {
                "id": "invariant:decision",
                "summary": "Use bounded retries.",
                "rationale": "Bound load.",
            }
        ],
        "assumptions": [
            {
                "id": "feature:assumption",
                "description": "Storage deduplicates.",
                "invalidation_triggers": ["Storage changes."],
            }
        ],
        "invariants": [{"id": "feature:invariant", "description": "IDs are stable."}],
        "guidance": [{"id": "guidance:1", "description": "Prefer local helpers."}],
    }
    catalog = compile_reference_catalog(accepted, base_commit=BASE)
    assert [
        (entry.reference.id, entry.reference.kind) for entry in catalog.entries
    ] == [
        ("decision:feature", "feature"),
        ("feature:assumption", "assumption"),
        ("feature:invariant", "invariant"),
        ("guidance:1", "guidance"),
        ("invariant:decision", "decision"),
    ]


def test_catalog_retains_inactive_history_without_redirecting() -> None:
    accepted = {
        "active_intent": [
            {
                "clause_id": "old",
                "kind": "invariant",
                "statement": "Old requirement.",
                "active": False,
            },
            {
                "clause_id": "new",
                "kind": "invariant",
                "statement": "New requirement.",
                "supersedes_clause_id": "old",
            },
        ]
    }
    catalog = compile_reference_catalog(accepted, base_commit=BASE)
    result = resolve_reference(catalog, "old")
    assert result.status == "inactive_reference"
    assert result.entry and result.entry.reference.successor == "new"
    assert result.entry.reference.statement == "Old requirement."
    assert resolve_reference(catalog, "new").status == "resolved"


def test_legacy_decision_status_and_replacement_preserve_inactive_history() -> None:
    accepted = {
        "decisions": [
            {"id": "old", "summary": "Previous decision.", "status": "superseded"},
            {
                "id": "new",
                "summary": "Current decision.",
                "status": "current",
                "replaces": "old",
            },
        ]
    }
    catalog = compile_reference_catalog(accepted, base_commit=BASE)
    result = resolve_reference(catalog, "old")
    assert result.status == "inactive_reference"
    assert result.entry and result.entry.reference.successor == "new"
    assert resolve_reference(catalog, "new").status == "resolved"


def test_inactive_history_outside_active_inventory_is_not_lost() -> None:
    accepted = {
        "active_intent": [],
        "invariants": [
            {
                "id": "retired",
                "description": "Previous invariant.",
                "action": "removed",
            },
        ],
    }
    assert (
        resolve_reference(
            compile_reference_catalog(accepted, base_commit=BASE), "retired"
        ).status
        == "inactive_reference"
    )


@pytest.mark.parametrize(
    "lifecycle",
    ["proposed", "accepted", "deprecated", "retired", "superseded", "rejected"],
)
def test_only_active_records_resolve_for_current_applicability(lifecycle: str) -> None:
    original = definition(lifecycle=lifecycle)
    result = resolve_reference(
        compile_reference_catalog(document(original), base_commit=BASE), original.id
    )
    assert result.status == "inactive_reference"
    assert result.entry and result.entry.reference.lifecycle == lifecycle


@pytest.mark.parametrize("ref", ["unknown", "OPAQUE:ID", " bad id ", "é", "x" * 257])
def test_unknown_and_invalid_ids_return_structured_resolution(ref: str) -> None:
    result = resolve_reference(
        compile_reference_catalog(document(definition()), base_commit=BASE), ref
    )
    assert result.status == "unresolved_reference"
    assert result.entry is None
    assert result.ref == ref


def test_accepted_and_amended_authority_are_distinct_and_version_bound() -> None:
    accepted = document(definition(), definition("unchanged"))
    newer = definition(version=2, statement="Keep the amended requirement.")
    catalog = compile_reference_catalog(
        accepted, base_commit=BASE, authorized_amendment=amendment(accepted, newer)
    )
    amended = resolve_reference(catalog, "opaque:ID")
    unchanged = resolve_reference(catalog, "unchanged")
    assert amended.entry and amended.entry.authority == "authorized_amendment"
    assert amended.entry.reference == newer
    assert unchanged.entry and unchanged.entry.authority == "accepted"
    assert catalog.authorized_amendment_digest
    assert accepted == document(definition(), definition("unchanged"))
    assert AuthorizedReferenceAmendment.from_data(
        amendment(accepted, newer)
    ).to_data() == amendment(accepted, newer)


def test_authorized_retirement_retains_record_for_diagnostics() -> None:
    accepted = document(definition())
    newer = definition(version=2, lifecycle="retired")
    catalog = compile_reference_catalog(
        accepted, base_commit=BASE, authorized_amendment=amendment(accepted, newer)
    )
    result = resolve_reference(catalog, newer.id)
    assert result.status == "inactive_reference"
    assert result.entry and result.entry.authority == "authorized_amendment"


@pytest.mark.parametrize(
    "change", ["candidate", "base", "document", "approval", "version"]
)
def test_candidate_overlays_and_stale_or_unapproved_amendments_fail(
    change: str,
) -> None:
    accepted = document(definition())
    raw = amendment(accepted, definition(version=2))
    if change == "candidate":
        raw = {
            "invariants": [
                {
                    "id": "opaque:ID",
                    "description": "A candidate's own claim.",
                    "action": "changed",
                }
            ]
        }
    elif change == "base":
        raw["base_commit"] = "other"
    elif change == "document":
        raw["accepted_document_digest"] = "sha256:" + "0" * 64
    elif change == "approval":
        raw["authorization_ref"] = ""
    else:
        raw["definitions"][0]["version"] = 1
    with pytest.raises(ValueError):
        compile_reference_catalog(accepted, base_commit=BASE, authorized_amendment=raw)


def test_unrelated_candidate_data_is_not_a_catalog_input() -> None:
    accepted = document(definition())
    accepted["candidate_feature"] = {"id": "forged", "description": "A worker's claim."}
    assert (
        resolve_reference(
            compile_reference_catalog(accepted, base_commit=BASE), "forged"
        ).status
        == "unresolved_reference"
    )


def test_duplicate_ids_deduplicate_only_identical_definitions() -> None:
    accepted = document(definition(), definition())
    assert len(compile_reference_catalog(accepted, base_commit=BASE).entries) == 1
    accepted = document(definition(), definition(statement="Conflicting meaning."))
    with pytest.raises(ValueError, match="conflicting reference ID"):
        compile_reference_catalog(accepted, base_commit=BASE)
    with pytest.raises(ValueError, match="duplicate amendment"):
        amendment({}, definition(), definition())


def test_legacy_conflicts_and_implicit_supersession_do_not_silently_pass() -> None:
    accepted = {
        "active_intent": [
            {"id": "old", "description": "Old."},
            {"id": "new", "description": "New.", "supersedes_clause_id": "old"},
        ]
    }
    with pytest.raises(ValueError, match="explicitly inactive"):
        compile_reference_catalog(accepted, base_commit=BASE)


def test_catalog_is_immutable_and_serialization_is_detached() -> None:
    catalog = compile_reference_catalog(document(definition()), base_commit=BASE)
    data = catalog.to_data()
    data["entries"][0]["statement"] = "tampered"
    assert catalog.entries[0].reference.statement == definition().statement
    with pytest.raises(FrozenInstanceError):
        catalog.entries[0].reference.statement = "tampered"  # type: ignore[misc]
    restored = ReferenceCatalog.from_data(json.loads(json.dumps(catalog.to_data())))
    assert restored == catalog


def test_input_dictionary_order_is_irrelevant_and_every_context_field_is_bound() -> (
    None
):
    accepted = document(definition())
    catalog = compile_reference_catalog(accepted, base_commit=BASE)
    reordered = json.loads(json.dumps(accepted, sort_keys=True))
    assert (
        compile_reference_catalog(reordered, base_commit=BASE).digest == catalog.digest
    )
    assert (
        compile_reference_catalog(accepted, base_commit=OTHER_BASE).digest
        != catalog.digest
    )
    for changed in (
        definition(statement="New wording."),
        definition(rationale="New reason."),
        definition(source_ref="other:source"),
        definition(version=2),
    ):
        assert (
            compile_reference_catalog(document(changed), base_commit=BASE).digest
            != catalog.digest
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", "future"),
        ("renderer_revision", "future"),
        ("catalog_digest", "tampered"),
        ("entries", []),
        ("base_commit", "other"),
        ("accepted_document_digest", "bad"),
    ],
)
def test_catalog_loading_rejects_corruption(field: str, value: Any) -> None:
    data = compile_reference_catalog(document(definition()), base_commit=BASE).to_data()
    data[field] = value
    with pytest.raises(ValueError):
        ReferenceCatalog.from_data(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", "future"),
        ("id", "bad id"),
        ("kind", "unknown"),
        ("version", True),
        ("version", 0),
        ("statement", ""),
        ("rationale", 42),
        ("source_ref", ""),
        ("evidence_refs", "not-a-list"),
        ("successor", "opaque:ID"),
        ("lifecycle", "made-up"),
    ],
)
def test_canonical_definition_schema_rejects_invalid_data(
    field: str, value: Any
) -> None:
    data = definition().to_data()
    data[field] = value
    with pytest.raises(ValueError):
        ReferenceDefinition.from_data(data)


def test_canonical_feature_and_assumption_definitions_survive_desired_state() -> None:
    accepted = document(
        definition("feature", "feature"),
        definition(
            "assumption", "assumption", invalidation_triggers=("Dependency changes.",)
        ),
    )
    state = DesiredState.from_snapshot(accepted)
    restored = DesiredState.from_data(state.to_data())
    assert restored == state
    assert (
        len(compile_reference_catalog(restored.document, base_commit=BASE).entries) == 2
    )
    wrapped = compile_reference_catalog(restored.to_data(), base_commit=BASE)
    assert len(wrapped.entries) == 2
    assert wrapped.accepted_document_digest == content_fingerprint(restored.to_data())
    tampered = restored.to_data()
    tampered["fingerprint"] = "tampered"
    with pytest.raises(ValueError, match="fingerprint"):
        compile_reference_catalog(tampered, base_commit=BASE)


def test_delivery_status_is_insufficient_to_invent_feature_meaning() -> None:
    with pytest.raises(ValueError, match="feature statement"):
        compile_reference_catalog(
            {"features": [{"id": "feature", "state": "implemented"}]},
            base_commit=BASE,
        )


def test_resolutions_round_trip_and_reject_inconsistent_lifecycle_claims() -> None:
    catalog = compile_reference_catalog(document(definition()), base_commit=BASE)
    for ref in ("opaque:ID", "unknown", " invalid id "):
        result = resolve_reference(catalog, ref)
        assert ReferenceResolution.from_data(result.to_data()) == result
    data = resolve_reference(catalog, "opaque:ID").to_data()
    data["status"] = "inactive_reference"
    with pytest.raises(ValueError, match="lifecycle"):
        ReferenceResolution.from_data(data)


def test_conflicting_active_flags_and_canonical_kinds_are_rejected() -> None:
    with pytest.raises(ValueError, match="active flag"):
        compile_reference_catalog(
            {
                "invariants": [
                    {
                        "id": "invalid",
                        "description": "Conflicting flags.",
                        "lifecycle": "active",
                        "active": False,
                    }
                ]
            },
            base_commit=BASE,
        )
    with pytest.raises(ValueError, match="kind disagrees"):
        compile_reference_catalog(
            {"features": [definition().to_data()]}, base_commit=BASE
        )


def test_frozen_catalog_requires_full_commit_identity() -> None:
    with pytest.raises(ValueError, match="full Git commit"):
        compile_reference_catalog(document(definition()), base_commit="main")


def test_assumption_triggers_cannot_be_attached_to_other_kinds() -> None:
    with pytest.raises(ValueError, match="only assumptions"):
        definition(invalidation_triggers=("Dependency changes.",))


def test_canonical_invariants_cannot_bypass_design_agreement(tmp_path: Path) -> None:
    accepted = document(definition(), definition("historical", lifecycle="retired"))
    proposal = compile_proposal_revision(
        "no-change",
        accepted,
        {},
        acceptance_criteria=(),
        must_preserve=(),
        non_goals=(),
        allowed_paths=("source.py",),
        source_refs=(),
    )
    handoff = DesignHandoff.compile(accepted, proposal, base_commit=BASE)
    manifest = compile_source_manifest(
        tmp_path,
        (),
        submission_base=BASE,
        source_revision=BASE,
        taxonomy_fingerprint="taxonomy",
    )
    report = check_design_agreement(handoff, root=tmp_path, manifest=manifest)
    assert report["passed"] is False
    assert [(item["subject"], item["status"]) for item in report["findings"]] == [
        ("reference_definitions:opaque:ID", "unknown")
    ]
    evidence = [
        {
            "subject": "reference_definitions:opaque:ID",
            "handoff_fingerprint": handoff.to_data()["fingerprint"],
            "candidate_product_digest": manifest.product_digest,
            "status": "satisfied",
            "reason": "Independent validation passed.",
            "evidence_refs": ["validation:run-1"],
        }
    ]
    assert (
        check_design_agreement(
            handoff, root=tmp_path, manifest=manifest, evidence=evidence
        )["passed"]
        is True
    )
