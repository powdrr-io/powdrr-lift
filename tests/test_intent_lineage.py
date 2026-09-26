from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from powdrr_lift.cli import main
from powdrr_lift.structrr.intent_lineage import (
    INTENT_LINEAGE_HISTORY_SCHEMA,
    audit_intent_lineage,
)
from powdrr_lift.structrr.proposal import compile_proposal_revision
from powdrr_lift.structrr.rebase import snapshot_digest
from powdrr_lift.workrr.actualization import reconcile_actualization


def _history(*, current_outcome: str = "pass") -> dict[str, Any]:
    source_ref = "chat:update-1"
    proposal = compile_proposal_revision(
        "audit-fixture",
        {},
        {"invariants": [{"id": "inv-1", "action": "added"}]},
        acceptance_criteria=("The invariant is enforced.",),
        must_preserve=("clause-1",),
        non_goals=(),
        allowed_paths=("src/",),
        source_refs=(source_ref,),
    )
    operation_id = proposal.operations[0].operation_id
    actualization = reconcile_actualization(
        proposal_fingerprint=proposal.fingerprint,
        diff_fingerprint="diff-1",
        operation_ids=(operation_id,),
        retained_clause_ids=("clause-1",),
        unexplained_changes=("semantic-change-review",),
        decisions=(
            {
                "decision_id": f"operation:{operation_id}",
                "outcome": "pass",
                "evidence_fingerprint": "diff-1",
            },
            {
                "decision_id": "intent:clause-1",
                "outcome": "pass",
                "evidence_fingerprint": "diff-1",
            },
            {
                "decision_id": "unexplained:semantic-change-review",
                "outcome": "pass",
                "evidence_fingerprint": "diff-1",
            },
        ),
    )
    exact_text = "Add and preserve the requested invariant."
    source = {
        "update_id": "update-1",
        "source_ref": source_ref,
        "exact_text": exact_text,
        "content_fingerprint": "sha256:"
        + hashlib.sha256(exact_text.encode("utf-8")).hexdigest(),
    }
    accepted_snapshot = {
        "repository_tree": "tree-1",
        "active_intent": [{"clause_id": "clause-1", "active": True}],
    }
    accepted_state_fingerprint = snapshot_digest(accepted_snapshot)
    clause = {
        "clause_id": "clause-1",
        "source_ref": source_ref,
        "active": True,
        "version": 1,
        "supersedes_clause_id": None,
    }
    history: dict[str, Any] = {
        "schema_version": INTENT_LINEAGE_HISTORY_SCHEMA,
        "sources": [source],
        "intent_clause_history": [clause],
        "active_intent_clauses": [clause],
        "revisions": [
            {
                "revision": 1,
                "proposal_revision": proposal.to_data(),
                "proposal_review": {
                    "accepted": True,
                    "proposal_fingerprint": proposal.fingerprint,
                },
                "lineage": {
                    "source_update_ids": ["update-1"],
                    "intent_clause_ids": ["clause-1"],
                    "execution_unit_ids": ["unit-1"],
                    "intent_packet_fingerprints": ["packet-1"],
                    "validation_evidence_fingerprints": ["evidence-1"],
                    "accepted_structrr_fingerprint": accepted_state_fingerprint,
                    "repository_tree": "tree-1",
                },
                "execution_units": [
                    {
                        "unit_id": "unit-1",
                        "proposal_fingerprint": proposal.fingerprint,
                        "operation_ids": [operation_id],
                    }
                ],
                "intent_packets": [
                    {
                        "packet_fingerprint": "packet-1",
                        "execution_unit_id": "unit-1",
                        "proposal_fingerprint": proposal.fingerprint,
                        "operation_ids": [operation_id],
                    }
                ],
                "actualization_report": actualization,
                "validation_evidence": [
                    {
                        "fingerprint": "evidence-1",
                        "candidate_tree": "tree-1",
                        "status": "passed",
                        "contract_id": "contract-1",
                    }
                ],
                "accepted_structrr_state": {
                    "state_fingerprint": accepted_state_fingerprint,
                    "repository_tree": "tree-1",
                    "snapshot": accepted_snapshot,
                },
            }
        ],
        "current_invariant_evidence": [
            {
                "clause_id": "clause-1",
                "outcome": current_outcome,
                "evidence_fingerprint": "current-evidence-1",
                "candidate_tree": "tree-1",
            }
        ],
    }
    return history


def test_lineage_audit_replays_deterministically_with_complete_artifacts() -> None:
    history = _history()

    first = audit_intent_lineage(history)
    second = audit_intent_lineage(deepcopy(history))

    assert first == second
    assert first["passed"] is True
    assert first["first_drift_revision"] is None
    assert first["severity_counts"] == {}


def test_lineage_audit_reports_missing_edges_and_stale_evidence() -> None:
    history = _history()
    revision = history["revisions"][0]
    revision["lineage"]["intent_packet_fingerprints"] = ["missing-packet"]
    revision["validation_evidence"][0]["candidate_tree"] = "old-tree"
    revision["intent_packets"] = []

    report = audit_intent_lineage(history)
    codes = {item["code"] for item in report["findings"]}

    assert report["passed"] is False
    assert "missing_lineage_edge" in codes
    assert "operation_packet_lineage_incomplete" in codes
    assert "stale_validation_evidence" in codes


def test_current_invariant_failure_is_not_hidden_by_historical_acceptance() -> None:
    report = audit_intent_lineage(_history(current_outcome="fail"))

    assert report["passed"] is False
    assert report["first_drift_revision"] == 1
    assert any(
        item["code"] == "current_invariant_failure" for item in report["findings"]
    )


def test_superseded_clause_must_not_remain_active() -> None:
    history = _history()
    old_clause = {
        "clause_id": "clause-old",
        "source_ref": "chat:update-1",
        "active": True,
        "version": 1,
        "supersedes_clause_id": None,
    }
    replacement = {
        "clause_id": "clause-new",
        "source_ref": "chat:update-1",
        "active": True,
        "version": 2,
        "supersedes_clause_id": "clause-old",
    }
    history["intent_clause_history"].extend([old_clause, replacement])
    history["active_intent_clauses"].extend([old_clause, replacement])

    report = audit_intent_lineage(history)

    assert any(
        item["code"] == "superseded_clause_still_active" for item in report["findings"]
    )


def test_tampered_actualization_fingerprint_is_rejected() -> None:
    history = _history()
    history["revisions"][0]["actualization_report"]["passed"] = False

    report = audit_intent_lineage(history)
    codes = {item["code"] for item in report["findings"]}

    assert report["passed"] is False
    assert "actualization_fingerprint_mismatch" in codes
    assert "actualization_drift" in codes


def test_audit_intent_lineage_cli_writes_replayable_report(
    tmp_path: Path, capsys: Any
) -> None:
    input_path = tmp_path / "history.json"
    output_path = tmp_path / "audit.json"
    input_path.write_text(json.dumps(_history()), encoding="utf-8")

    result = main(
        [
            "audit-intent-lineage",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
        ]
    )

    assert result == 0
    assert json.loads(output_path.read_text(encoding="utf-8"))["passed"] is True
    assert json.loads(capsys.readouterr().out)["passed"] is True
