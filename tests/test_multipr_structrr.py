from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from powdrr_service.planning.multipr_structrr import (
    MultiPRStructrrProposal,
    active_decision_drift_findings,
    compile_multi_pr_proposal,
    load_multi_pr_proposal,
    rebase_final_plan_state,
    rebase_item_proposal,
    write_multi_pr_proposal,
)
from powdrr_service.structrr.snapshot import snapshot_digest


def _plan_item(item_id: str) -> dict[str, object]:
    return {
        "id": item_id,
        "acceptance_criteria": [f"{item_id} works"],
        "allowed_paths": [f"src/{item_id}"],
    }


def _proposal() -> MultiPRStructrrProposal:
    baseline = {
        "entities": [{"id": "existing", "type": "Module", "description": "unchanged"}],
        "entity_relationships": [],
        "source_subjects": [],
    }
    return compile_multi_pr_proposal(
        plan_id="feature-v1",
        baseline_snapshot=baseline,
        items=[_plan_item("first"), _plan_item("second")],
        proposed_operations=[
            {
                "operation_id": "first-add",
                "item_id": "first",
                "section": "entities",
                "subject_id": "first-module",
                "action": "added",
                "content": {"id": "first-module", "type": "Module"},
            },
            {
                "operation_id": "second-add",
                "item_id": "second",
                "section": "entities",
                "subject_id": "second-module",
                "action": "added",
                "content": {"id": "second-module", "type": "Module"},
            },
        ],
        source_ref="docs/plans/feature.md",
    )


def test_multi_pr_proposal_partitions_operations_over_projected_states() -> None:
    proposal = _proposal()

    assert proposal.full_proposal.structrr_baseline_fingerprint == snapshot_digest(
        proposal.baseline_snapshot
    )
    assert [
        operation.subject_id
        for operation in proposal.item_proposals["first"].operations
    ] == ["first-module"]
    assert [
        operation.subject_id
        for operation in proposal.item_proposals["second"].operations
    ] == ["second-module"]
    assert any(
        entity["id"] == "first-module"
        for entity in proposal.item_baselines["second"]["entities"]
    )


def test_changed_existing_file_materializes_missing_source_subject() -> None:
    path = "attestation/scripts/verify-all.sh"
    proposal = compile_multi_pr_proposal(
        plan_id="feature-v1",
        baseline_snapshot={
            "entities": [],
            "entity_relationships": [],
            "files": [{"path": path}],
            "source_subjects": [],
        },
        items=[_plan_item("first"), _plan_item("second")],
        proposed_operations=[
            {
                "operation_id": "first-change-file",
                "item_id": "first",
                "section": "files",
                "subject_id": f"path::{path}",
                "action": "changed",
                "content": {"stable_key": f"path::{path}", "path": path},
            }
        ],
        source_ref="docs/plans/feature.md",
    )

    assert proposal.item_baselines["second"]["source_subjects"] == [
        {"stable_key": f"path::{path}", "path": path}
    ]


def test_rebase_ignores_interlaced_unrelated_change_but_reports_target_conflict() -> (
    None
):
    proposal = _proposal()
    item_base = proposal.item_baselines["second"]
    item_proposal = proposal.item_proposals["second"]

    unrelated = copy.deepcopy(item_base)
    unrelated["entities"].append(
        {"id": "other-feature", "type": "Module", "description": "unrelated"}
    )
    unrelated_report = rebase_item_proposal(item_base, unrelated, item_proposal)
    assert unrelated_report["classification"] == "clean"

    conflicting = copy.deepcopy(item_base)
    conflicting["entities"].append(
        {"id": "second-module", "type": "Service", "description": "landed elsewhere"}
    )
    conflict_report = rebase_item_proposal(item_base, conflicting, item_proposal)
    assert conflict_report["classification"] == "context_refresh_required"
    assert conflict_report["affected_changes"]


def test_final_rebase_carries_unrelated_changes_and_blocks_plan_subject_drift() -> None:
    proposal = _proposal()
    unrelated = copy.deepcopy(proposal.expected_final_snapshot)
    unrelated["entities"].append(
        {"id": "other-feature", "type": "Module", "description": "interlaced"}
    )
    report = rebase_final_plan_state(
        proposal.expected_final_snapshot, unrelated, proposal.full_proposal
    )
    assert report["classification"] == "clean"

    drifted: dict[str, Any] = dict(copy.deepcopy(proposal.expected_final_snapshot))
    drifted["entities"] = [
        {
            **entity,
            **(
                {"description": "changed by another feature"}
                if entity["id"] == "first-module"
                else {}
            ),
        }
        for entity in drifted["entities"]
    ]
    conflict = rebase_final_plan_state(
        proposal.expected_final_snapshot, drifted, proposal.full_proposal
    )
    assert conflict["classification"] == "context_refresh_required"
    assert conflict["affected_changes"]


def test_multi_pr_proposal_round_trips_with_fingerprints(tmp_path: Path) -> None:
    proposal = _proposal()
    path = tmp_path / "proposal.json"

    write_multi_pr_proposal(path, proposal)
    loaded = load_multi_pr_proposal(path)

    assert loaded.plan_id == proposal.plan_id
    assert loaded.full_proposal.fingerprint == proposal.full_proposal.fingerprint
    assert {key: value.fingerprint for key, value in loaded.item_proposals.items()} == {
        key: value.fingerprint for key, value in proposal.item_proposals.items()
    }


def test_active_decision_drift_detects_edit_or_removal_but_allows_new_decisions() -> (
    None
):
    baseline = {
        "active_intent": [
            {"clause_id": "decision-1", "statement": "Keep the API stable."}
        ],
        "human-decisions": [{"id": "human-1", "decision": "Use SQLite."}],
        "invariants": [{"id": "invariant-1", "description": "No data loss."}],
    }
    latest = {
        "active_intent": [
            {"clause_id": "decision-1", "statement": "Keep the API stable."},
            {"clause_id": "decision-2", "statement": "Support old clients."},
        ],
        "human-decisions": [{"id": "human-1", "decision": "Use SQLite."}],
        "invariants": [{"id": "invariant-1", "description": "No data loss."}],
    }
    assert active_decision_drift_findings(baseline, latest) == ()

    latest["human-decisions"] = [{"id": "human-1", "decision": "Use PostgreSQL."}]
    assert active_decision_drift_findings(baseline, latest) == (
        "accepted human-decisions record 'human-1' was changed",
    )
