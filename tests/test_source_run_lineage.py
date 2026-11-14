from __future__ import annotations

from copy import deepcopy
from typing import Any

from powdrr_service.structrr.source_run_lineage import (
    audit_source_run_lineage,
    bind_source_run_patch,
    build_source_run_lineage,
)


def _record(*, prompt_fingerprint: str = "sha256:prompt-integrity") -> dict[str, Any]:
    design = {
        "required_test_cases": [
            {
                "id": "case:retry",
                "behavior_scenario": {"scenario_id": "scenario:retry"},
            }
        ]
    }
    handoff = {
        "worker_prompt": {
            "fingerprint": prompt_fingerprint,
            "contract": {
                "base_commit": "a" * 40,
                "design_fingerprint": "sha256:design-integrity",
                "catalog_digest": "sha256:catalog-integrity",
                "definitions": [{"id": "invariant:retry", "kind": "invariant"}],
                "obligations": [
                    {
                        "obligation_id": "place:retry",
                        "ref": "invariant:retry",
                        "verification_refs": ["case:retry"],
                    }
                ],
                "preservation": [],
                "behavior_case_ids": {"scenario:retry": ["case:retry"]},
            },
        },
        "validation_manifest": {
            "fingerprint": "sha256:manifest-integrity",
            "private_validation_data": {
                "obligation_evidence_contracts": [
                    {
                        "obligation_id": "place:retry",
                        "review_routes": ["behavior_validation"],
                    }
                ]
            },
        },
    }
    terminal = {
        "outcome": "accepted",
        "validation": {"results": [{"profile": "pytest", "status": "passed"}]},
        "obligation_route_evidence": {
            "candidate_tree": "tree-id-1",
            "routes": [
                {
                    "obligation_id": "place:retry",
                    "route": "behavior_validation",
                    "candidate_tree": "tree-id-1",
                    "case_ids": ["case:retry"],
                    "status": "passed",
                    "evidence_paths": ["tests/test_retry.py"],
                }
            ],
        },
    }
    return build_source_run_lineage(
        run_id="run-1",
        lineage_context={
            "plan_id": "retry-plan",
            "item_id": "implement-retry",
            "design_path": "docs/structrr/retry.yaml",
        },
        accepted_design=design,
        handoff=handoff,
        terminal=terminal,
    )


def test_lineage_uses_stable_domain_ids_for_links() -> None:
    record = _record()
    ids = {item["artifact_id"] for item in record["artifacts"]}
    changed_integrity_fingerprint = _record(prompt_fingerprint="sha256:changed")
    changed_ids = {
        item["artifact_id"] for item in changed_integrity_fingerprint["artifacts"]
    }

    assert "design:retry-plan:implement-retry" in ids
    assert "structrr-obligation:place:retry" in ids
    assert "verification-case:case:retry" in ids
    assert ids == changed_ids
    assert any(
        item["source_id"] == "verification-case:case:retry"
        and item["relation"] == "supported_by"
        for item in record["links"]
    )
    assert audit_source_run_lineage(record)["passed"] is True


def test_audit_is_deterministic_for_an_unchanged_record() -> None:
    record = _record()

    assert audit_source_run_lineage(record) == audit_source_run_lineage(
        deepcopy(record)
    )


def test_audit_rejects_each_missing_emitted_lineage_edge() -> None:
    record = _record()

    for link in record["links"]:
        changed = deepcopy(record)
        changed["links"].remove(link)

        audit = audit_source_run_lineage(changed)
        assert audit["passed"] is False, link
        assert audit["findings"], link


def test_audit_rejects_duplicate_and_unresolved_artifact_ids() -> None:
    record = _record()
    changed = deepcopy(record)
    changed["artifacts"].append(deepcopy(changed["artifacts"][0]))
    changed["links"].append(
        {
            "source_id": "source-run:run-1",
            "relation": "mentions",
            "target_id": "missing:artifact",
        }
    )

    codes = {item["code"] for item in audit_source_run_lineage(changed)["findings"]}
    assert {"duplicate_artifact_id", "unresolved_link"} <= codes


def test_audit_reports_malformed_artifact_attributes_without_crashing() -> None:
    record = _record()
    attempt = next(
        item for item in record["artifacts"] if item["kind"] == "worker_attempt"
    )
    attempt["attributes"] = None

    codes = {item["code"] for item in audit_source_run_lineage(record)["findings"]}
    assert {"invalid_artifact", "attempt_id_mismatch"} <= codes


def test_audit_rejects_cross_run_evidence_without_joining_by_fingerprint() -> None:
    record = _record()
    evidence = next(
        item for item in record["artifacts"] if item["kind"] == "validation_evidence"
    )
    evidence["artifact_id"] = "validation-evidence:other:pytest"
    evidence["attributes"]["candidate_tree"] = "different-tree-fingerprint"
    for link in record["links"]:
        if link["target_id"] == "validation-evidence:run-1:pytest":
            link["target_id"] = evidence["artifact_id"]

    codes = {item["code"] for item in audit_source_run_lineage(record)["findings"]}
    assert "cross_run_artifact" in codes
    assert "evidence_candidate_mismatch" not in codes


def test_patch_transfer_requires_audited_patch_and_eligible_decision() -> None:
    record = bind_source_run_patch(_record(), patch_sha256="a" * 64)

    assert audit_source_run_lineage(record)["passed"] is True

    transfer = next(
        item for item in record["artifacts"] if item["kind"] == "transfer_decision"
    )
    transfer["status"] = "blocked"
    codes = {item["code"] for item in audit_source_run_lineage(record)["findings"]}
    assert "transfer_not_eligible" in codes


def test_patch_transfer_requires_every_declared_obligation_route() -> None:
    record = bind_source_run_patch(_record(), patch_sha256="a" * 64)
    record["artifacts"] = [
        item for item in record["artifacts"] if item["kind"] != "obligation_evidence"
    ]
    record["links"] = [
        item
        for item in record["links"]
        if not item["target_id"].startswith("route-evidence:")
    ]

    codes = {item["code"] for item in audit_source_run_lineage(record)["findings"]}
    assert "required_route_missing" in codes
