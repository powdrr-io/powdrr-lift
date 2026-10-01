"""Candidate-bound, fail-closed receipts for targeted invariant reviews."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from powdrr_lift.core.decision_obligation import (
    DecisionOutcome,
    DecisionResult,
    DecisionSpecification,
    DecisionWorklist,
    content_fingerprint,
    evidence_fingerprint,
)
from powdrr_lift.structrr.obligation_evidence import NormativeStrength

INVARIANT_REVIEW_RECEIPT_SCHEMA_VERSION = "invariant-review-receipt-v1"
INVARIANT_REVIEW_PREDICATE_VERSION = "invariant-review-v1"


class InvariantOrigin(StrEnum):
    INSTRUCTION = "instruction"
    RETAINED_BASELINE_INTENT = "retained_baseline_intent"


class InvariantApplicability(StrEnum):
    APPLICABLE = "applicable"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class InvariantRequirementBasis(StrEnum):
    NORMATIVE_MUST = "normative_must"
    ACCEPTED_SHOULD = "accepted_should"
    ADVISORY = "advisory"


@dataclass(frozen=True, slots=True)
class InvariantReviewObligation:
    """Review scope and evidence requirements for one preserved invariant."""

    invariant_id: str
    origin: InvariantOrigin
    normative_strength: NormativeStrength
    required: bool
    requirement_basis: InvariantRequirementBasis
    applicability: InvariantApplicability
    applicability_rationale: str
    protected_subjects: tuple[str, ...]
    impact_selectors: tuple[str, ...]
    review_method: str
    required_evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            "invariant_id",
            "applicability_rationale",
            "review_method",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"invariant review {name} must not be empty")
        if not self.protected_subjects:
            raise ValueError("invariant review requires protected subjects")
        if not self.impact_selectors:
            raise ValueError("invariant review requires impact selectors")
        if not self.required_evidence:
            raise ValueError("invariant review requires evidence selectors")
        if any(not item.strip() for item in self.required_evidence):
            raise ValueError("invariant evidence selectors must not be empty")
        if self.normative_strength is NormativeStrength.UNSPECIFIED:
            raise ValueError("resolve invariant normative strength before review")
        expected_basis = {
            (NormativeStrength.MUST, True): InvariantRequirementBasis.NORMATIVE_MUST,
            (NormativeStrength.SHOULD, True): InvariantRequirementBasis.ACCEPTED_SHOULD,
            (NormativeStrength.SHOULD, False): InvariantRequirementBasis.ADVISORY,
            (NormativeStrength.MAY, False): InvariantRequirementBasis.ADVISORY,
        }.get((self.normative_strength, self.required))
        if self.requirement_basis is not expected_basis:
            raise ValueError(
                "invariant requirement basis does not match its accepted strength"
            )
        if (
            self.applicability is InvariantApplicability.NOT_APPLICABLE
            and not self.required_evidence
        ):
            raise ValueError(
                "non-applicability requires evidence that supports the decision"
            )

    @property
    def fingerprint(self) -> str:
        return content_fingerprint(self.to_data())

    def to_data(self) -> dict[str, Any]:
        return {
            "invariant_id": self.invariant_id,
            "origin": self.origin.value,
            "normative_strength": self.normative_strength.value,
            "required": self.required,
            "requirement_basis": self.requirement_basis.value,
            "applicability": self.applicability.value,
            "applicability_rationale": self.applicability_rationale,
            "protected_subjects": list(self.protected_subjects),
            "impact_selectors": list(self.impact_selectors),
            "review_method": self.review_method,
            "required_evidence": list(self.required_evidence),
        }

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> InvariantReviewObligation:
        required = raw.get("required")
        if not isinstance(required, bool):
            raise ValueError("invariant review required must be a boolean")
        try:
            origin = InvariantOrigin(_text(raw, "origin"))
            strength = NormativeStrength(_text(raw, "normative_strength"))
            requirement_basis = InvariantRequirementBasis(
                _text(raw, "requirement_basis")
            )
            applicability = InvariantApplicability(_text(raw, "applicability"))
        except ValueError as error:
            raise ValueError(f"invalid invariant review obligation: {error}") from error
        return cls(
            invariant_id=_text(raw, "invariant_id"),
            origin=origin,
            normative_strength=strength,
            required=required,
            requirement_basis=requirement_basis,
            applicability=applicability,
            applicability_rationale=_text(raw, "applicability_rationale"),
            protected_subjects=_text_tuple(raw, "protected_subjects"),
            impact_selectors=_text_tuple(raw, "impact_selectors"),
            review_method=_text(raw, "review_method"),
            required_evidence=_text_tuple(raw, "required_evidence"),
        )


@dataclass(frozen=True, slots=True)
class InvariantReviewReceipt:
    proposal_fingerprint: str
    candidate_product_digest: str
    extraction_version: str
    reviewer_version: str
    obligation_fingerprints: tuple[tuple[str, str], ...]
    worklist_fingerprint: str
    decisions: tuple[DecisionResult, ...]
    passed: bool
    schema_version: str = INVARIANT_REVIEW_RECEIPT_SCHEMA_VERSION

    def to_data(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "proposal_fingerprint": self.proposal_fingerprint,
            "candidate_product_digest": self.candidate_product_digest,
            "extraction_version": self.extraction_version,
            "reviewer_version": self.reviewer_version,
            "obligation_fingerprints": [
                {"invariant_id": invariant_id, "fingerprint": fingerprint}
                for invariant_id, fingerprint in self.obligation_fingerprints
            ],
            "worklist_fingerprint": self.worklist_fingerprint,
            "decisions": [item.to_data() for item in self.decisions],
            "passed": self.passed,
        }
        if include_fingerprint:
            data["fingerprint"] = content_fingerprint(data)
        return data

    def assert_current(
        self,
        obligations: Sequence[InvariantReviewObligation],
        *,
        proposal_fingerprint: str,
        candidate_product_digest: str,
        extraction_version: str,
        reviewer_version: str,
    ) -> None:
        if self.schema_version != INVARIANT_REVIEW_RECEIPT_SCHEMA_VERSION:
            raise ValueError("unsupported invariant review receipt schema version")
        expected_bindings = _bindings(obligations)
        if self.proposal_fingerprint != proposal_fingerprint:
            raise ValueError("invariant review receipt references a stale proposal")
        if self.candidate_product_digest != candidate_product_digest:
            raise ValueError("invariant review receipt references a stale candidate")
        if self.extraction_version != extraction_version:
            raise ValueError("invariant review receipt uses a stale extraction version")
        if self.reviewer_version != reviewer_version:
            raise ValueError("invariant review receipt uses a stale reviewer version")
        if self.obligation_fingerprints != expected_bindings:
            raise ValueError("invariant review receipt references stale obligations")

        worklist = compile_invariant_review_worklist(
            obligations,
            proposal_fingerprint=proposal_fingerprint,
            candidate_product_digest=candidate_product_digest,
            extraction_version=extraction_version,
            reviewer_version=reviewer_version,
        )
        if self.worklist_fingerprint != worklist.fingerprint:
            raise ValueError("invariant review receipt references a stale worklist")
        _validate_decisions(self.decisions, worklist, obligations=obligations)
        expected_passed = _review_passed(obligations, self.decisions)
        if self.passed != expected_passed:
            raise ValueError("invariant review receipt acceptance is inconsistent")


def compile_invariant_review_worklist(
    obligations: Sequence[InvariantReviewObligation],
    *,
    proposal_fingerprint: str,
    candidate_product_digest: str,
    extraction_version: str,
    reviewer_version: str,
) -> DecisionWorklist:
    """Compile a single-decision work item for each invariant disposition."""
    _require_context(
        proposal_fingerprint,
        candidate_product_digest,
        extraction_version,
        reviewer_version,
    )
    _assert_unique_obligations(obligations)
    specifications: list[DecisionSpecification] = []
    for obligation in obligations:
        decision_id = f"invariant:{obligation.invariant_id}"
        bound_evidence = (
            f"proposal:{proposal_fingerprint}",
            f"candidate:{candidate_product_digest}",
            f"extraction:{extraction_version}",
            f"reviewer:{reviewer_version}",
            f"invariant:{obligation.fingerprint}",
            *obligation.required_evidence,
        )
        input_fingerprint = content_fingerprint(
            {
                "decision_id": decision_id,
                "obligation": obligation.to_data(),
                "proposal_fingerprint": proposal_fingerprint,
                "candidate_product_digest": candidate_product_digest,
                "extraction_version": extraction_version,
                "reviewer_version": reviewer_version,
            }
        )
        specifications.append(
            DecisionSpecification(
                decision_id=decision_id,
                family="candidate-invariant-review",
                subject=obligation.invariant_id,
                predicate=(
                    "the recorded non-applicability decision is supported by its "
                    "targeted evidence"
                    if obligation.applicability is InvariantApplicability.NOT_APPLICABLE
                    else "the candidate preserves this invariant, based on its "
                    "targeted source and behavioral evidence"
                ),
                input_fingerprint=input_fingerprint,
                predicate_version=INVARIANT_REVIEW_PREDICATE_VERSION,
                required=obligation.required,
                evidence_requirements=bound_evidence,
            )
        )
    return DecisionWorklist.compile(tuple(specifications))


def build_invariant_review_result(
    specification: DecisionSpecification,
    *,
    outcome: DecisionOutcome,
    explanation: str,
) -> DecisionResult:
    """Create a correctly bound reviewer result for one worklist item."""
    if outcome not in {
        DecisionOutcome.PASS,
        DecisionOutcome.FAIL,
        DecisionOutcome.UNKNOWN,
    }:
        raise ValueError("invariant review outcomes must be pass, fail, or unknown")
    references = specification.evidence_requirements
    result = DecisionResult(
        decision_id=specification.decision_id,
        outcome=outcome,
        explanation=explanation,
        predicate_version=specification.predicate_version,
        subject=specification.subject,
        input_fingerprint=specification.input_fingerprint,
        evidence_fingerprint=evidence_fingerprint(
            specification.input_fingerprint, references
        ),
        evidence_refs=references,
    )
    result.validate_against(specification)
    return result


def compile_invariant_review_receipt(
    obligations: Sequence[InvariantReviewObligation],
    decisions: Sequence[DecisionResult],
    *,
    proposal_fingerprint: str,
    candidate_product_digest: str,
    extraction_version: str,
    reviewer_version: str,
) -> InvariantReviewReceipt:
    """Compile and validate the separate invariant-review acceptance receipt."""
    worklist = compile_invariant_review_worklist(
        obligations,
        proposal_fingerprint=proposal_fingerprint,
        candidate_product_digest=candidate_product_digest,
        extraction_version=extraction_version,
        reviewer_version=reviewer_version,
    )
    normalized_decisions = tuple(sorted(decisions, key=lambda item: item.decision_id))
    _validate_decisions(normalized_decisions, worklist, obligations=obligations)
    receipt = InvariantReviewReceipt(
        proposal_fingerprint=proposal_fingerprint,
        candidate_product_digest=candidate_product_digest,
        extraction_version=extraction_version,
        reviewer_version=reviewer_version,
        obligation_fingerprints=_bindings(obligations),
        worklist_fingerprint=worklist.fingerprint,
        decisions=normalized_decisions,
        passed=_review_passed(obligations, normalized_decisions),
    )
    receipt.assert_current(
        obligations,
        proposal_fingerprint=proposal_fingerprint,
        candidate_product_digest=candidate_product_digest,
        extraction_version=extraction_version,
        reviewer_version=reviewer_version,
    )
    return receipt


def load_invariant_review_receipt(path: Path) -> InvariantReviewReceipt:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"could not read invariant review receipt {path}: {error}"
        ) from error
    if not isinstance(raw, Mapping):
        raise ValueError("invariant review receipt must contain an object")
    if raw.get("schema_version") != INVARIANT_REVIEW_RECEIPT_SCHEMA_VERSION:
        raise ValueError("unsupported invariant review receipt schema version")
    raw_bindings = raw.get("obligation_fingerprints")
    raw_decisions = raw.get("decisions")
    if (
        not isinstance(raw_bindings, list)
        or not all(isinstance(item, Mapping) for item in raw_bindings)
        or not isinstance(raw_decisions, list)
        or not all(isinstance(item, Mapping) for item in raw_decisions)
    ):
        raise ValueError("invariant review receipt collections are malformed")
    bindings = tuple(
        (_text(item, "invariant_id"), _text(item, "fingerprint"))
        for item in raw_bindings
    )
    decisions = tuple(DecisionResult.from_data(item) for item in raw_decisions)
    passed = raw.get("passed")
    if not isinstance(passed, bool):
        raise ValueError("invariant review receipt passed must be a boolean")
    receipt = InvariantReviewReceipt(
        proposal_fingerprint=_text(raw, "proposal_fingerprint"),
        candidate_product_digest=_text(raw, "candidate_product_digest"),
        extraction_version=_text(raw, "extraction_version"),
        reviewer_version=_text(raw, "reviewer_version"),
        obligation_fingerprints=bindings,
        worklist_fingerprint=_text(raw, "worklist_fingerprint"),
        decisions=decisions,
        passed=passed,
    )
    if raw.get("fingerprint") != receipt.to_data()["fingerprint"]:
        raise ValueError("invariant review receipt fingerprint does not match content")
    return receipt


def write_invariant_review_receipt(path: Path, receipt: InvariantReviewReceipt) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt.to_data(), indent=2, sort_keys=True) + "\n")


def _validate_decisions(
    decisions: Sequence[DecisionResult],
    worklist: DecisionWorklist,
    *,
    obligations: Sequence[InvariantReviewObligation],
) -> None:
    expected = {item.decision_id: item for item in worklist.specifications}
    actual: dict[str, list[DecisionResult]] = {}
    for decision in decisions:
        actual.setdefault(decision.decision_id, []).append(decision)
    duplicates = sorted(key for key, values in actual.items() if len(values) != 1)
    if duplicates:
        raise ValueError(f"invariant review contains duplicate decisions: {duplicates}")
    unexpected = sorted(set(actual) - set(expected))
    if unexpected:
        raise ValueError(f"invariant review contains unknown decisions: {unexpected}")
    missing = sorted(
        item.decision_id
        for item in worklist.specifications
        if item.required and item.decision_id not in actual
    )
    if missing:
        raise ValueError(f"invariant review is incomplete: {missing}")
    for decision_id, results in actual.items():
        result = results[0]
        specification = expected[decision_id]
        result.validate_against(specification)
        if result.outcome is DecisionOutcome.CLARIFICATION:
            raise ValueError("invariant review outcomes must be pass, fail, or unknown")
        obligation = next(
            item
            for item in obligations
            if f"invariant:{item.invariant_id}" == decision_id
        )
        if (
            obligation.applicability is InvariantApplicability.UNKNOWN
            and result.outcome is not DecisionOutcome.UNKNOWN
        ):
            raise ValueError("unknown invariant applicability requires unknown outcome")


def _review_passed(
    obligations: Sequence[InvariantReviewObligation],
    decisions: Sequence[DecisionResult],
) -> bool:
    required_ids = {
        f"invariant:{item.invariant_id}" for item in obligations if item.required
    }
    by_id = {item.decision_id: item for item in decisions}
    obligations_by_decision = {
        f"invariant:{item.invariant_id}": item for item in obligations if item.required
    }
    return all(
        obligations_by_decision[decision_id].applicability
        is not InvariantApplicability.UNKNOWN
        and decision_id in by_id
        and by_id[decision_id].outcome is DecisionOutcome.PASS
        for decision_id in required_ids
    )


def _bindings(
    obligations: Sequence[InvariantReviewObligation],
) -> tuple[tuple[str, str], ...]:
    _assert_unique_obligations(obligations)
    return tuple(sorted((item.invariant_id, item.fingerprint) for item in obligations))


def _assert_unique_obligations(
    obligations: Sequence[InvariantReviewObligation],
) -> None:
    identifiers = [item.invariant_id for item in obligations]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("invariant review obligations contain duplicate IDs")


def _require_context(*values: str) -> None:
    if any(not value.strip() for value in values):
        raise ValueError(
            "invariant review context fingerprints and versions are required"
        )


def _text(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _text_tuple(raw: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{key} must be a list of non-empty strings")
    return tuple(dict.fromkeys(value))


__all__ = [
    "INVARIANT_REVIEW_PREDICATE_VERSION",
    "INVARIANT_REVIEW_RECEIPT_SCHEMA_VERSION",
    "InvariantApplicability",
    "InvariantOrigin",
    "InvariantRequirementBasis",
    "InvariantReviewObligation",
    "InvariantReviewReceipt",
    "build_invariant_review_result",
    "compile_invariant_review_receipt",
    "compile_invariant_review_worklist",
    "load_invariant_review_receipt",
    "write_invariant_review_receipt",
]
