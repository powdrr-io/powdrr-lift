"""Service-owned interpretation of repository evidence for planning."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from powdrr_service.planning.contracts import (
    COVERAGE_ANSWER_JSON_SCHEMA,
    CompleteCoverage,
    CoverageAnswer,
    MissingCoverage,
    PartialCoverage,
    PlanningContractError,
    Question,
    parse_coverage_answer,
)
from powdrr_service.planning.evidence import Evidence, EvidenceError
from powdrr_service.planning.questions import ContextRecord, QuestionRequest
from powdrr_service.planning.source import Requirement, SegmentedSource


@dataclass(frozen=True, slots=True)
class ResidualRequirement:
    id: str
    original_requirement_id: str
    revision: int
    statement: str
    kind: str
    source_refs: tuple[str, ...]
    source_group_ids: tuple[str, ...]
    related_requirement_ids: tuple[str, ...]

    def to_data(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "original_requirement_id": self.original_requirement_id,
            "revision": self.revision,
            "statement": self.statement,
            "kind": self.kind,
            "source_refs": list(self.source_refs),
            "source_group_ids": list(self.source_group_ids),
            "related_requirement_ids": list(self.related_requirement_ids),
        }


@dataclass(frozen=True, slots=True)
class RequirementCoverage:
    requirement_id: str
    answer: CoverageAnswer
    residual: ResidualRequirement | None = None


def _validated_evidence_excerpt(evidence: Evidence, excerpt: str | None) -> str:
    if not isinstance(excerpt, str):
        raise EvidenceError(f"missing pinned excerpt for evidence {evidence.id}")
    lines = excerpt.splitlines(keepends=True)
    expected_lines = evidence.end_line - evidence.start_line + 1
    if expected_lines < 1 or len(lines) != expected_lines:
        raise EvidenceError("evidence excerpt does not match its pinned line range")
    if hashlib.sha256(excerpt.encode("utf-8")).hexdigest() != evidence.content_sha256:
        raise EvidenceError("evidence content digest does not match its pinned range")
    return excerpt


__all__ = [
    "RequirementCoverage",
    "ResidualRequirement",
    "bind_coverage_answer",
    "build_requirement_coverage_request",
    "create_requirement_coverage",
]


def bind_coverage_answer(
    value: Mapping[str, Any],
    *,
    evidence_by_id: Mapping[str, Evidence],
    supplied_evidence_ids: Sequence[str],
) -> CoverageAnswer:
    """Validate answer references and enforce evidence required by its status."""
    answer = parse_coverage_answer(value)
    supplied = set(supplied_evidence_ids)
    if len(supplied) != len(supplied_evidence_ids):
        raise PlanningContractError(
            "/evidence_ids", "supplied evidence IDs must be unique"
        )
    answer_ids = set(answer.evidence_ids)
    if not answer_ids <= supplied:
        raise PlanningContractError(
            "/evidence_ids",
            "coverage answer cites evidence outside the question context",
        )
    evidence: list[Evidence] = []
    for evidence_id in answer.evidence_ids:
        record = evidence_by_id.get(evidence_id)
        if record is None:
            raise PlanningContractError(
                "/evidence_ids", f"coverage answer cites unknown evidence {evidence_id}"
            )
        evidence.append(record)
    if isinstance(answer, PartialCoverage) and not any(
        record.kind == "implementation" for record in evidence
    ):
        raise PlanningContractError(
            "/evidence_ids", "partial coverage requires implementation evidence"
        )
    if isinstance(answer, CompleteCoverage):
        kinds = {record.kind for record in evidence}
        has_execution_receipt = "execution_receipt" in kinds
        has_code_and_test = (
            bool(kinds & {"implementation", "configuration"}) and "test" in kinds
        )
        if not has_execution_receipt and not has_code_and_test:
            raise PlanningContractError(
                "/evidence_ids",
                "complete coverage needs implementation/configuration plus test "
                "evidence, or an execution receipt",
            )
    return answer


def build_requirement_coverage_request(
    requirement: Requirement,
    *,
    source: SegmentedSource,
    evidence: Sequence[Evidence],
    evidence_excerpts: Mapping[str, str],
    repository_search: Mapping[str, Any] | None = None,
) -> QuestionRequest[CoverageAnswer]:
    """Build one coverage question bound to one requirement and supplied evidence."""
    known_anchor_ids = {item.id for item in source.anchors}
    if not set(requirement.source_refs) <= known_anchor_ids:
        raise EvidenceError("requirement cites unknown source anchor")
    evidence_records = tuple(evidence)
    evidence_by_id = {item.id: item for item in evidence_records}
    if len(evidence_by_id) != len(evidence_records):
        raise EvidenceError("evidence IDs must be unique")
    evidence_context: list[ContextRecord] = []
    for item in evidence_records:
        if item.base_commit != source.document.commit_sha:
            raise EvidenceError(
                "evidence and requirement source use different base commits"
            )
        excerpt = _validated_evidence_excerpt(item, evidence_excerpts.get(item.id))
        evidence_context.append(
            ContextRecord(item.id, {**item.to_data(), "source_excerpt": excerpt})
        )
    requirement_context = ContextRecord(
        requirement.id,
        {
            "statement": requirement.statement,
            "kind": requirement.kind,
            "source_group_ids": list(requirement.source_group_ids),
        },
    )
    search_record = (
        None
        if repository_search is None
        else ContextRecord("repository-search", dict(repository_search))
    )
    repository_context = tuple(evidence_context)
    if search_record is not None:
        repository_context += (search_record,)
    digest_material = {
        "requirement": {
            "id": requirement.id,
            "statement": requirement.statement,
            "kind": requirement.kind,
            "source_refs": list(requirement.source_refs),
            "source_group_ids": list(requirement.source_group_ids),
        },
        "evidence": [item.to_data() for item in evidence_records],
        "repository_search": (
            None if repository_search is None else dict(repository_search)
        ),
    }
    input_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(digest_material, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
    )
    supplied_ids = tuple(item.id for item in evidence_records)
    receipt = next(
        (item for item in evidence_records if item.kind == "execution_receipt"), None
    )
    implementation = next(
        (item for item in evidence_records if item.kind == "implementation"), None
    )
    configuration = next(
        (item for item in evidence_records if item.kind == "configuration"), None
    )
    test = next((item for item in evidence_records if item.kind == "test"), None)
    if receipt is not None:
        positive_example: Mapping[str, Any] = {
            "status": "complete",
            "evidence_ids": [receipt.id],
            "reason": "An observed execution receipt covers this requirement.",
        }
    elif (implementation is not None or configuration is not None) and test is not None:
        code_evidence = implementation or configuration
        assert code_evidence is not None
        positive_example = {
            "status": "complete",
            "evidence_ids": [code_evidence.id, test.id],
            "reason": "Pinned implementation and relevant tests cover it.",
        }
    elif implementation is not None:
        positive_example = {
            "status": "partial",
            "evidence_ids": [implementation.id],
            "reason": "Implementation exists, but part of the obligation remains.",
            "remaining_statement": "Add the specific missing behavior or test.",
        }
    else:
        positive_example = {
            "status": "missing",
            "evidence_ids": [],
            "reason": "The inspected evidence has no matching implementation.",
        }
    negative_example = {
        "status": "missing",
        "evidence_ids": [],
        "reason": "No matching implementation was found in the inspected evidence.",
    }
    return QuestionRequest(
        question=Question(
            question_id=f"q-coverage-{input_digest[-16:]}",
            kind="assess_requirement_coverage",
            contract_version=1,
            subject_ids=(requirement.id,),
            input_digest=input_digest,
            context_ref_ids=tuple(
                list(supplied_ids)
                + [requirement.id]
                + ([] if search_record is None else [search_record.ref_id])
            ),
            expected_answer_schema="coverage-answer-v1",
            attempt=1,
        ),
        exact_question=(
            "This source-validated requirement is the unit of assessment. Decide "
            "whether it is already implemented in the pinned repository. Use only "
            "the supplied code evidence and repository-search record. Search hits "
            "are candidates; ignore irrelevant matches. If the supplied matches "
            "do not implement this behavior, mark it missing. Missing means this "
            "requirement is work to plan, not a reason to stop. A search with no "
            "matches is a valid missing result; do not invent evidence."
        ),
        answer_field_definitions={
            "status": "missing, partial, or complete.",
            "evidence_ids": "Supplied evidence IDs that support this assessment.",
            "reason": "What the evidence establishes or fails to establish.",
            "remaining_statement": "For partial, the concrete unmet obligation.",
        },
        # The requirement statement has already passed source-fidelity review.
        # Repeating every cited source paragraph can exceed the bounded question
        # size without adding information needed for coverage judgment.
        source_excerpts=(),
        repository_evidence=repository_context,
        accepted_facts=(requirement_context,),
        valid_example=positive_example,
        empty_example=negative_example,
        answer_schema=COVERAGE_ANSWER_JSON_SCHEMA,
        parse_answer=lambda answer: bind_coverage_answer(
            answer,
            evidence_by_id=evidence_by_id,
            supplied_evidence_ids=supplied_ids,
        ),
    )


def create_requirement_coverage(
    requirement: Requirement | ResidualRequirement,
    answer: CoverageAnswer,
    *,
    evidence_by_id: Mapping[str, Evidence],
    supplied_evidence_ids: Sequence[str],
    revision: int,
) -> RequirementCoverage:
    """Keep the accepted result and compile a provenance-preserving residual."""
    raw: dict[str, Any]
    if isinstance(answer, MissingCoverage):
        raw = {"status": answer.status, "evidence_ids": [], "reason": answer.reason}
    elif isinstance(answer, CompleteCoverage):
        raw = {
            "status": answer.status,
            "evidence_ids": list(answer.evidence_ids),
            "reason": answer.reason,
        }
    else:
        raw = {
            "status": answer.status,
            "evidence_ids": list(answer.evidence_ids),
            "reason": answer.reason,
            "remaining_statement": answer.remaining_statement,
        }
    bound = bind_coverage_answer(
        raw,
        evidence_by_id=evidence_by_id,
        supplied_evidence_ids=supplied_evidence_ids,
    )
    if not isinstance(bound, PartialCoverage):
        return RequirementCoverage(requirement.id, bound)
    previous_revision = (
        requirement.revision if isinstance(requirement, ResidualRequirement) else 0
    )
    if revision <= previous_revision:
        raise EvidenceError(
            "residual requirement revision must advance its prior revision"
        )
    original_requirement_id = (
        requirement.original_requirement_id
        if isinstance(requirement, ResidualRequirement)
        else requirement.id
    )
    residual = ResidualRequirement(
        id=f"{original_requirement_id}-residual-{revision:04d}",
        original_requirement_id=original_requirement_id,
        revision=revision,
        statement=bound.remaining_statement,
        kind=requirement.kind,
        source_refs=requirement.source_refs,
        source_group_ids=requirement.source_group_ids,
        related_requirement_ids=requirement.related_requirement_ids,
    )
    return RequirementCoverage(requirement.id, bound, residual)
