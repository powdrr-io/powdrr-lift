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
from powdrr_service.verification.requirement_execution import (
    RequirementExecutionReceipt,
    requirement_coverage_digest,
)


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
    source_validation_groups: tuple[tuple[str, str], ...] = ()
    source_logic: tuple[Mapping[str, Any], ...] = ()

    def to_data(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "original_requirement_id": self.original_requirement_id,
            "revision": self.revision,
            "statement": self.statement,
            "kind": self.kind,
            "source_refs": list(self.source_refs),
            "source_group_ids": list(self.source_group_ids),
            "source_validation_groups": [
                {"id": group_id, "relation": relation}
                for group_id, relation in self.source_validation_groups
            ],
            "source_logic": [dict(item) for item in self.source_logic],
            "related_requirement_ids": list(self.related_requirement_ids),
        }


@dataclass(frozen=True, slots=True)
class RequirementCoverage:
    requirement_id: str
    answer: CoverageAnswer
    residual: ResidualRequirement | None = None


@dataclass(frozen=True, slots=True)
class RequirementEvidenceSearch:
    """Bounded repository evidence results supplied to planning policy."""

    evidence: tuple[Evidence, ...]
    terms: tuple[str, ...]
    matching_line_count: int
    matching_path_count: int
    base_commit: str
    basedpyright_query_count: int = 0
    basedpyright_match_count: int = 0
    rg_match_count: int = 0
    basedpyright_available: bool = True
    code_symbol_coverage: Mapping[str, Any] | None = None
    rg_search_status: str = "complete"

    def to_data(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "method": "basedpyright-symbols-and-rg-v1",
            "base_commit": self.base_commit,
            "terms": list(self.terms),
            "matching_line_count": self.matching_line_count,
            "matching_path_count": self.matching_path_count,
            "evidence_count": len(self.evidence),
            "basedpyright_query_count": self.basedpyright_query_count,
            "basedpyright_match_count": self.basedpyright_match_count,
            "rg_match_count": self.rg_match_count,
            "basedpyright_available": self.basedpyright_available,
            "rg_search_status": self.rg_search_status,
            "scope": "tracked source, test, and configuration files",
        }
        if self.code_symbol_coverage is not None:
            result["code_symbol_coverage"] = dict(self.code_symbol_coverage)
        return result


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
    "RequirementEvidenceSearch",
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
    requirement: Requirement | ResidualRequirement | None = None,
    pinned_commit: str | None = None,
    evidence_excerpts: Mapping[str, str] | None = None,
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

    for record in evidence:
        if record.kind != "execution_receipt":
            continue
        excerpt = (evidence_excerpts or {}).get(record.id)
        if (
            requirement is None
            or pinned_commit is None
            or not _receipt_proves_requirement(
                record,
                excerpt,
                requirement=requirement,
                pinned_commit=pinned_commit,
            )
        ):
            raise PlanningContractError(
                "/evidence_ids",
                "execution receipt is not valid for this requirement and pinned commit",
            )

    supplied_evidence = tuple(
        evidence_by_id[evidence_id]
        for evidence_id in supplied_evidence_ids
        if evidence_id in evidence_by_id
    )

    def available_ids(kinds: set[str]) -> list[str]:
        return sorted(record.id for record in supplied_evidence if record.kind in kinds)

    if isinstance(answer, PartialCoverage) and not any(
        record.kind == "implementation" for record in evidence
    ):
        implementation_ids = available_ids({"implementation"})
        guidance = (
            "cite one of the supplied implementation evidence IDs: "
            + ", ".join(implementation_ids)
            if implementation_ids
            else "no implementation evidence was supplied; use missing instead"
        )
        raise PlanningContractError(
            "/evidence_ids",
            f"partial coverage requires implementation evidence; {guidance}",
        )
    if isinstance(answer, CompleteCoverage):
        kinds = {record.kind for record in evidence}
        has_execution_receipt = "execution_receipt" in kinds
        has_code_and_test = (
            bool(kinds & {"implementation", "configuration"}) and "test" in kinds
        )
        if not has_execution_receipt and not has_code_and_test:
            missing: list[str] = []
            if not kinds & {"implementation", "configuration"}:
                code_ids = available_ids({"implementation", "configuration"})
                missing.append(
                    "implementation/configuration evidence IDs: "
                    + (", ".join(code_ids) if code_ids else "none supplied")
                )
            if "test" not in kinds:
                test_ids = available_ids({"test"})
                missing.append(
                    "test evidence IDs: "
                    + (", ".join(test_ids) if test_ids else "none supplied")
                )
            receipt_ids = available_ids({"execution_receipt"})
            if receipt_ids:
                missing.append(
                    "or cite an execution receipt ID: " + ", ".join(receipt_ids)
                )
            raise PlanningContractError(
                "/evidence_ids",
                "complete coverage needs implementation/configuration plus test "
                "evidence, or an execution receipt; include the missing supplied "
                "evidence IDs (" + "; ".join(missing) + ")",
            )
    return answer


def _receipt_proves_requirement(
    evidence: Evidence,
    excerpt: str | None,
    *,
    requirement: Requirement | ResidualRequirement,
    pinned_commit: str,
) -> bool:
    if evidence.base_commit != pinned_commit or excerpt is None:
        return False
    try:
        raw = json.loads(_validated_evidence_excerpt(evidence, excerpt))
        if not isinstance(raw, Mapping):
            return False
        receipt = RequirementExecutionReceipt.from_data(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    digest = requirement_coverage_digest(
        requirement_id=requirement.id,
        statement=requirement.statement,
        kind=requirement.kind,
        source_refs=requirement.source_refs,
    )
    return receipt.proves_coverage(
        requirement_id=requirement.id,
        requirement_digest=digest,
        source_commit=pinned_commit,
        candidate_commit=pinned_commit,
    )


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
    supplied_evidence = tuple(evidence)
    supplied_by_id = {item.id: item for item in supplied_evidence}
    if len(supplied_by_id) != len(supplied_evidence):
        raise EvidenceError("evidence IDs must be unique")
    evidence_context: list[ContextRecord] = []
    trusted_evidence: list[Evidence] = []
    for item in supplied_evidence:
        if item.base_commit != source.document.commit_sha:
            raise EvidenceError(
                "evidence and requirement source use different base commits"
            )
        excerpt = _validated_evidence_excerpt(item, evidence_excerpts.get(item.id))
        if item.kind == "execution_receipt" and not _receipt_proves_requirement(
            item,
            excerpt,
            requirement=requirement,
            pinned_commit=source.document.commit_sha,
        ):
            continue
        trusted_evidence.append(item)
        evidence_context.append(
            ContextRecord(item.id, {**item.to_data(), "source_excerpt": excerpt})
        )
    evidence_records = tuple(trusted_evidence)
    evidence_by_id = {item.id: item for item in evidence_records}
    requirement_context = ContextRecord(
        requirement.id,
        {
            "statement": requirement.statement,
            "kind": requirement.kind,
            "source_group_ids": list(requirement.source_group_ids),
            "source_validation_groups": [
                {"id": group_id, "relation": relation}
                for group_id, relation in requirement.source_validation_groups
            ],
            "source_logic": [dict(item) for item in requirement.source_logic],
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
        "contract_version": 3,
        "requirement": {
            "id": requirement.id,
            "statement": requirement.statement,
            "kind": requirement.kind,
            "source_refs": list(requirement.source_refs),
            "source_group_ids": list(requirement.source_group_ids),
            "source_validation_groups": [
                {"id": group_id, "relation": relation}
                for group_id, relation in requirement.source_validation_groups
            ],
            "source_logic": [dict(item) for item in requirement.source_logic],
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
    negative_example = {
        "status": "missing",
        "evidence_ids": [],
        "reason": "No supplied evidence establishes the requested behavior.",
    }
    if receipt is not None:
        positive_example: Mapping[str, Any] = {
            "status": "complete",
            "evidence_ids": [receipt.id],
            "reason": "An observed execution receipt covers this requirement.",
        }
    else:
        positive_example = negative_example
    return QuestionRequest(
        question=Question(
            question_id=f"q-coverage-{input_digest[-16:]}",
            kind="assess_requirement_coverage",
            contract_version=3,
            subject_ids=(requirement.id,),
            input_digest=input_digest,
            context_ref_ids=tuple(
                list(supplied_ids)
                + [requirement.id]
                + ([] if search_record is None else [search_record.ref_id])
            ),
            expected_answer_schema="coverage-answer-v3",
            attempt=1,
        ),
        exact_question=(
            "This source-validated requirement is the unit of assessment. Decide "
            "whether it is already implemented in the pinned repository. Use only "
            "the supplied code evidence and repository-search record. Search hits "
            "are candidates; ignore irrelevant matches. Examples demonstrate answer "
            "shape only and do not establish that supplied evidence is relevant. "
            "Code/test co-occurrence alone does not prove coverage. If the "
            "requirement includes "
            "source_logic, treat its exact quoted atoms and conditions as source "
            "authority. Preserve nested all_of, any_of, ordered, and conditional "
            "meaning when judging coverage; do not flatten branches or infer missing "
            "conditions. If the supplied matches "
            "do not implement this behavior, mark it missing. Missing means this "
            "requirement is work to plan, not a reason to stop. Empty symbol "
            "matches do not establish absence on paths omitted by partial code-symbol "
            "coverage. A no-match result supports missing only when repository-search "
            "reports rg_search_status=complete for its configured source paths; do "
            "not invent evidence. To mark "
            "complete, cite either an execution_receipt or both relevant "
            "implementation/configuration evidence and relevant test evidence. "
            "Include the IDs for both kinds in evidence_ids; implementation alone "
            "is partial, not complete. Test evidence alone does not show that "
            "behavior is implemented; if only tests match, mark it missing, not "
            "partial. Keep reason to one concise sentence of at "
            "most 300 characters; put evidence citations in evidence_ids instead "
            "of listing files in reason."
        ),
        answer_field_definitions={
            "status": "missing, partial, or complete.",
            "evidence_ids": (
                "Supplied evidence IDs that support this assessment. Complete "
                "requires an execution_receipt, or relevant implementation/"
                "configuration and test evidence together."
            ),
            "reason": (
                "One concise sentence of at most 300 characters stating what the "
                "evidence establishes or fails to establish. Put evidence IDs in "
                "evidence_ids; do not list filenames here."
            ),
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
            requirement=requirement,
            pinned_commit=source.document.commit_sha,
            evidence_excerpts=evidence_excerpts,
        ),
    )


def create_requirement_coverage(
    requirement: Requirement | ResidualRequirement,
    answer: CoverageAnswer,
    *,
    evidence_by_id: Mapping[str, Evidence],
    supplied_evidence_ids: Sequence[str],
    evidence_excerpts: Mapping[str, str] | None = None,
    pinned_commit: str | None = None,
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
        requirement=requirement,
        pinned_commit=pinned_commit,
        evidence_excerpts=evidence_excerpts,
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
        source_validation_groups=requirement.source_validation_groups,
        source_logic=requirement.source_logic,
    )
    return RequirementCoverage(requirement.id, bound, residual)
