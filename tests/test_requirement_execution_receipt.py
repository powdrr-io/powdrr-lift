from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from powdrr_service.planning.contracts import PlanningContractError
from powdrr_service.planning.coverage import (
    bind_coverage_answer,
    build_requirement_coverage_request,
)
from powdrr_service.planning.driver import (
    _requirement_bound_execution_receipt_answer,
)
from powdrr_service.planning.evidence import Evidence
from powdrr_service.planning.questions import QuestionRequest
from powdrr_service.planning.source import (
    PinnedSourceDocument,
    Requirement,
    segment_markdown_source,
)
from powdrr_service.verification.requirement_execution import (
    RequirementExecutionReceipt,
    receipt_from_verification_evidence,
    requirement_coverage_digest,
)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requirement_id", "req-9999"),
        ("requirement_digest", "sha256:" + "e" * 64),
        ("source_commit", "f" * 40),
        ("candidate_commit", "e" * 40),
        ("outcome", "failed"),
    ],
)
def test_receipt_coverage_requires_exact_requirement_and_run_binding(
    field: str, value: str
) -> None:
    text = "# Plan\n\nImplement the required behavior.\n"
    commit = "a" * 40
    document = PinnedSourceDocument(
        repository="owner/repo",
        commit_sha=commit,
        path="docs/plan.md",
        blob_sha="b" * 40,
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
    )
    source = segment_markdown_source(document)
    requirement = Requirement(
        "req-0001",
        "Implement the required behavior.",
        "functional",
        (source.anchors[0].id,),
        (),
        (),
    )
    requirement_digest = requirement_coverage_digest(
        requirement_id=requirement.id,
        statement=requirement.statement,
        kind=requirement.kind,
        source_refs=requirement.source_refs,
        source_group_ids=(),
        source_validation_groups=(),
        source_logic=(),
    )
    receipt = RequirementExecutionReceipt.observed(
        requirement_id=requirement.id,
        requirement_digest=requirement_digest,
        source_commit=commit,
        candidate_commit=commit,
        oracle_id="ci:required-behavior",
        oracle_digest="sha256:" + "c" * 64,
        command_digest="sha256:" + "d" * 64,
        outcome="passed",
        run_id="github:run/123",
        artifact_refs=("artifact:run/123/result.json",),
    )

    def make_request(
        receipt_data: dict[str, object],
    ) -> tuple[Evidence, QuestionRequest[object]]:
        excerpt = json.dumps(receipt_data, sort_keys=True) + "\n"
        evidence = Evidence(
            "evidence-0001",
            commit,
            "artifacts/receipt.json",
            1,
            1,
            hashlib.sha256(excerpt.encode()).hexdigest(),
            "execution_receipt",
        )
        request = build_requirement_coverage_request(
            requirement,
            source=source,
            evidence=(evidence,),
            evidence_excerpts={evidence.id: excerpt},
        )
        return evidence, request

    evidence, request = make_request(receipt.to_data())
    answer = _requirement_bound_execution_receipt_answer(
        requirement, (evidence,), request, pinned_commit=commit
    )
    assert answer is not None
    assert answer["status"] == "complete"
    request.parse_answer(answer)

    altered = receipt.to_data()
    altered[field] = value
    unsigned = {key: value for key, value in altered.items() if key != "receipt_id"}
    altered["receipt_id"] = (
        "receipt:"
        + hashlib.sha256(
            json.dumps(
                unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
    )
    altered_evidence, altered_request = make_request(altered)
    assert (
        _requirement_bound_execution_receipt_answer(
            requirement,
            (altered_evidence,),
            altered_request,
            pinned_commit=commit,
        )
        is None
    )
    invalid_complete_answer = {
        "status": "complete",
        "evidence_ids": [altered_evidence.id],
        "reason": "The cited execution receipt passed.",
    }
    with pytest.raises(
        PlanningContractError,
        match="execution receipt is not valid for this requirement and pinned commit",
    ):
        bind_coverage_answer(
            invalid_complete_answer,
            evidence_by_id={altered_evidence.id: altered_evidence},
            supplied_evidence_ids=(altered_evidence.id,),
            requirement=requirement,
            pinned_commit=commit,
            evidence_excerpts={
                altered_evidence.id: json.dumps(altered, sort_keys=True) + "\n"
            },
        )
    assert altered_evidence.id not in altered_request.question.context_ref_ids
    with pytest.raises(PlanningContractError, match="outside the question context"):
        altered_request.parse_answer(invalid_complete_answer)


def test_verification_adapter_requires_explicit_requirement_and_tree_binding() -> None:
    obligation = {
        "obligation_id": "obligation:unit",
        "contract_id": "contract:behavior",
        "provider": "pytest",
        "selector": "tests/test_behavior.py::test_contract",
        "profile": "python",
        "contract_fingerprint": "sha256:" + "1" * 64,
        "verifier_fingerprint": "sha256:" + "2" * 64,
    }
    evidence = {
        **obligation,
        "status": "passed",
        "candidate_tree": "sha256:" + "3" * 64,
        "command_fingerprint": "sha256:" + "4" * 64,
        "artifact_refs": ["artifact:stdout"],
    }

    receipt = receipt_from_verification_evidence(
        evidence=evidence,
        obligation=obligation,
        requirement_id="req-0001",
        requirement_digest="sha256:" + "5" * 64,
        source_commit="a" * 40,
        candidate_commit="a" * 40,
        expected_candidate_tree="sha256:" + "3" * 64,
        run_id="github:run/321",
        evidence_artifact_ref="artifact:evidence.json",
    )

    assert receipt.to_data()["outcome"] == "passed"
    assert receipt.to_data()["requirement_id"] == "req-0001"
    assert receipt.to_data()["artifact_refs"] == [
        "artifact:stdout",
        "artifact:evidence.json",
    ]
    try:
        receipt_from_verification_evidence(
            evidence=evidence,
            obligation=obligation,
            requirement_id="req-0001",
            requirement_digest="sha256:" + "5" * 64,
            source_commit="a" * 40,
            candidate_commit="a" * 40,
            expected_candidate_tree="sha256:" + "6" * 64,
            run_id="github:run/321",
            evidence_artifact_ref="artifact:evidence.json",
        )
    except ValueError as error:
        assert "different candidate tree" in str(error)
    else:
        raise AssertionError("candidate tree mismatch was accepted")


def test_receipt_binding_includes_typed_source_logic_and_validation_groups() -> None:
    text = "# Plan\n\nTrim whitespace and preserve its order.\n"
    commit = "a" * 40
    document = PinnedSourceDocument(
        repository="owner/repo",
        commit_sha=commit,
        path="docs/plan.md",
        blob_sha="b" * 40,
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
    )
    source = segment_markdown_source(document)
    block = next(item for item in source.blocks if item.kind == "paragraph")
    anchor = block.anchor_ids[0]
    source_logic = {
        "op": "all_of",
        "children": [
            {"op": "atom", "source_ref": anchor, "quote": "Trim whitespace"},
            {"op": "atom", "source_ref": anchor, "quote": "preserve its order"},
        ],
    }
    requirement = Requirement(
        "req-0001",
        "Trim whitespace and preserve its order.",
        "functional",
        (anchor,),
        ("validation:clause-01:1",),
        (),
        source_validation_groups=(("validation:clause-01:1", "all_together"),),
        source_logic=(source_logic,),
    )
    requirement_digest = requirement_coverage_digest(
        requirement_id=requirement.id,
        statement=requirement.statement,
        kind=requirement.kind,
        source_refs=requirement.source_refs,
        source_group_ids=requirement.source_group_ids,
        source_validation_groups=requirement.source_validation_groups,
        source_logic=requirement.source_logic,
    )
    receipt = RequirementExecutionReceipt.observed(
        requirement_id=requirement.id,
        requirement_digest=requirement_digest,
        source_commit=commit,
        candidate_commit=commit,
        oracle_id="oracle:trim-order",
        oracle_digest="sha256:" + "c" * 64,
        command_digest="sha256:" + "d" * 64,
        outcome="passed",
        run_id="github:run/789",
        artifact_refs=("artifact:run/789/result.json",),
    )
    excerpt = json.dumps(receipt.to_data(), sort_keys=True) + "\n"
    evidence = Evidence(
        "evidence-0001",
        commit,
        "artifacts/receipt.json",
        1,
        1,
        hashlib.sha256(excerpt.encode()).hexdigest(),
        "execution_receipt",
    )

    accepted = build_requirement_coverage_request(
        requirement,
        source=source,
        evidence=(evidence,),
        evidence_excerpts={evidence.id: excerpt},
    )
    changed_requirement = replace(
        requirement,
        source_logic=(
            {
                "op": "any_of",
                "children": list(source_logic["children"]),
            },
        ),
    )
    stale = build_requirement_coverage_request(
        changed_requirement,
        source=source,
        evidence=(evidence,),
        evidence_excerpts={evidence.id: excerpt},
    )

    assert accepted.valid_example["status"] == "complete"
    assert evidence.id in accepted.question.context_ref_ids
    assert stale.valid_example == stale.empty_example
    assert evidence.id not in stale.question.context_ref_ids
