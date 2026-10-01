"""Candidate-bound, fail-closed receipts for targeted invariant reviews."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
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
from powdrr_lift.structrr.actual_diff import StructrrActualDiff
from powdrr_lift.structrr.candidate_comparison import (
    CANDIDATE_COMPARISON_SCHEMA_VERSION,
)
from powdrr_lift.structrr.obligation_evidence import NormativeStrength
from powdrr_lift.structrr.proposal import ProposalRevision

INVARIANT_REVIEW_RECEIPT_SCHEMA_VERSION = "invariant-review-receipt-v2"
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
class InvariantReviewEvidencePacket:
    """One invariant's bounded, candidate-specific review context."""

    obligation: InvariantReviewObligation
    accepted_proposal: Mapping[str, Any]
    proposal_fingerprint: str
    actual_diff_fingerprint: str
    candidate_product_digest: str
    extraction_version: str
    reviewer_version: str
    baseline_source_context: tuple[tuple[str, str], ...]
    candidate_source_context: tuple[tuple[str, str], ...]
    validation_evidence: tuple[tuple[str, str], ...]
    structural_operations: tuple[Mapping[str, Any], ...]
    missing_evidence: tuple[str, ...]
    relevant_paths: tuple[str, ...]
    context_broadened: bool
    extraction_complete: bool

    def to_data(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": "invariant-review-evidence-packet-v1",
            "invariant_id": self.obligation.invariant_id,
            "obligation": self.obligation.to_data(),
            "obligation_fingerprint": self.obligation.fingerprint,
            "accepted_proposal": dict(self.accepted_proposal),
            "proposal_fingerprint": self.proposal_fingerprint,
            "actual_diff_fingerprint": self.actual_diff_fingerprint,
            "candidate_product_digest": self.candidate_product_digest,
            "extraction_version": self.extraction_version,
            "reviewer_version": self.reviewer_version,
            "baseline_source_context": dict(self.baseline_source_context),
            "candidate_source_context": dict(self.candidate_source_context),
            "validation_evidence": dict(self.validation_evidence),
            "structural_operations": [
                dict(item) for item in self.structural_operations
            ],
            "missing_evidence": list(self.missing_evidence),
            "relevant_paths": list(self.relevant_paths),
            "context_broadened": self.context_broadened,
            "extraction_complete": self.extraction_complete,
        }
        if include_fingerprint:
            data["fingerprint"] = content_fingerprint(data)
        return data

    @property
    def fingerprint(self) -> str:
        return str(self.to_data()["fingerprint"])


@dataclass(frozen=True, slots=True)
class InvariantReviewAssessment:
    outcome: DecisionOutcome
    explanation: str

    def __post_init__(self) -> None:
        if self.outcome not in {
            DecisionOutcome.PASS,
            DecisionOutcome.FAIL,
            DecisionOutcome.UNKNOWN,
        }:
            raise ValueError("invariant review outcomes must be pass, fail, or unknown")
        if not self.explanation.strip():
            raise ValueError("invariant review assessment requires an explanation")


@dataclass(frozen=True, slots=True)
class InvariantReviewRun:
    packets: tuple[InvariantReviewEvidencePacket, ...]
    receipt: InvariantReviewReceipt

    def to_data(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": "invariant-review-run-v1",
            "packets": [packet.to_data() for packet in self.packets],
            "receipt": self.receipt.to_data(),
        }
        if include_fingerprint:
            data["fingerprint"] = content_fingerprint(data)
        return data


@dataclass(frozen=True, slots=True)
class InvariantReviewReceipt:
    proposal_fingerprint: str
    candidate_product_digest: str
    extraction_version: str
    reviewer_version: str
    obligation_fingerprints: tuple[tuple[str, str], ...]
    review_packet_fingerprints: tuple[tuple[str, str], ...]
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
            "review_packet_fingerprints": [
                {"invariant_id": invariant_id, "fingerprint": fingerprint}
                for invariant_id, fingerprint in self.review_packet_fingerprints
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
        review_packet_fingerprints: Sequence[tuple[str, str]] = (),
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
        expected_packets = _packet_bindings(review_packet_fingerprints)
        if self.review_packet_fingerprints != expected_packets:
            raise ValueError(
                "invariant review receipt references stale evidence packets"
            )

        worklist = compile_invariant_review_worklist(
            obligations,
            proposal_fingerprint=proposal_fingerprint,
            candidate_product_digest=candidate_product_digest,
            extraction_version=extraction_version,
            reviewer_version=reviewer_version,
            review_packet_fingerprints=dict(expected_packets),
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
    review_packet_fingerprints: Mapping[str, str] | None = None,
) -> DecisionWorklist:
    """Compile a single-decision work item for each invariant disposition."""
    _require_context(
        proposal_fingerprint,
        candidate_product_digest,
        extraction_version,
        reviewer_version,
    )
    _assert_unique_obligations(obligations)
    packet_fingerprints = dict(review_packet_fingerprints or {})
    _packet_bindings(tuple(packet_fingerprints.items()))
    expected_ids = {item.invariant_id for item in obligations}
    if set(packet_fingerprints) - expected_ids:
        raise ValueError("invariant review worklist has unknown evidence packets")
    specifications: list[DecisionSpecification] = []
    for obligation in obligations:
        decision_id = f"invariant:{obligation.invariant_id}"
        bound_evidence = (
            f"proposal:{proposal_fingerprint}",
            f"candidate:{candidate_product_digest}",
            f"extraction:{extraction_version}",
            f"reviewer:{reviewer_version}",
            f"invariant:{obligation.fingerprint}",
            *(
                (f"review-packet:{packet_fingerprints[obligation.invariant_id]}",)
                if obligation.invariant_id in packet_fingerprints
                else ()
            ),
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
                "review_packet_fingerprint": packet_fingerprints.get(
                    obligation.invariant_id
                ),
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


def compile_invariant_review_packets(
    obligations: Sequence[InvariantReviewObligation],
    *,
    proposal: ProposalRevision,
    actual_diff: StructrrActualDiff,
    comparison_report: Mapping[str, Any],
    extraction_version: str,
    reviewer_version: str,
    baseline_source: Mapping[str, str],
    candidate_source: Mapping[str, str],
    validation_evidence: Mapping[str, str],
) -> tuple[InvariantReviewEvidencePacket, ...]:
    """Build one evidence packet per invariant from a matching comparison.

    Source maps use repository paths as keys. Validation evidence uses the
    invariant's declared ``required_evidence`` selectors as keys. When impact
    selectors do not locate a narrower context, all changed source paths are
    retained and the packet records that it broadened the review.
    """
    proposal_fingerprint = proposal.fingerprint
    _require_context(proposal_fingerprint, extraction_version, reviewer_version)
    if (
        comparison_report.get("schema_version") != CANDIDATE_COMPARISON_SCHEMA_VERSION
        or comparison_report.get("scope") != "structural_operations_only"
        or comparison_report.get("separate_behavior_and_invariant_review_required")
        is not True
    ):
        raise ValueError("invariant review requires a supported structural comparison")
    if comparison_report.get("proposal_fingerprint") != proposal_fingerprint:
        raise ValueError("invariant review comparison references a different proposal")
    if (
        comparison_report.get("actual_diff_fingerprint") != actual_diff.fingerprint
        or comparison_report.get("candidate_fingerprint")
        != actual_diff.candidate_snapshot_fingerprint
    ):
        raise ValueError("invariant review comparison references a different candidate")
    if (
        comparison_report.get("candidate_product_digest")
        != actual_diff.candidate_product_digest
    ):
        raise ValueError(
            "invariant review comparison references a different product tree"
        )
    _assert_unique_obligations(obligations)

    changed_paths = set(actual_diff.behavioral_review_candidates)
    packets: list[InvariantReviewEvidencePacket] = []
    for obligation in obligations:
        impact_paths = tuple(
            selector.split("::", 1)[0]
            for selector in obligation.impact_selectors
            if "/" in selector.split("::", 1)[0]
        )
        matching_paths = {
            path
            for path in changed_paths
            if any(
                path == selected
                or path.startswith(selected.rstrip("/") + "/")
                or selected.startswith(path.rstrip("/") + "/")
                for selected in impact_paths
            )
        }
        context_broadened = bool(changed_paths and not matching_paths)
        relevant_paths = matching_paths or changed_paths
        if not relevant_paths:
            relevant_paths = _matching_source_paths(
                obligation.impact_selectors,
                (*baseline_source.keys(), *candidate_source.keys()),
            )
            if not relevant_paths and (baseline_source or candidate_source):
                relevant_paths = set(baseline_source) | set(candidate_source)
                context_broadened = True

        required_source_selectors = {
            item for item in obligation.required_evidence if item.startswith("source:")
        }
        baseline_context = _selected_source_evidence(
            baseline_source, relevant_paths, required_source_selectors
        )
        candidate_context = _selected_source_evidence(
            candidate_source, relevant_paths, required_source_selectors
        )
        validation = {
            selector: validation_evidence[selector]
            for selector in obligation.required_evidence
            if selector in validation_evidence and validation_evidence[selector].strip()
        }
        missing = tuple(
            selector
            for selector in obligation.required_evidence
            if not _has_required_evidence(
                selector,
                baseline_context,
                candidate_context,
                validation,
            )
        )
        packets.append(
            InvariantReviewEvidencePacket(
                obligation=obligation,
                accepted_proposal=proposal.to_data(),
                proposal_fingerprint=proposal_fingerprint,
                actual_diff_fingerprint=actual_diff.fingerprint,
                candidate_product_digest=actual_diff.candidate_product_digest,
                extraction_version=extraction_version,
                reviewer_version=reviewer_version,
                baseline_source_context=tuple(sorted(baseline_context.items())),
                candidate_source_context=tuple(sorted(candidate_context.items())),
                validation_evidence=tuple(sorted(validation.items())),
                structural_operations=tuple(
                    operation
                    for operation in actual_diff.structural_operations
                    if not relevant_paths
                    or not operation.get("evidence_paths")
                    or bool(set(operation.get("evidence_paths", ())) & relevant_paths)
                ),
                missing_evidence=missing,
                relevant_paths=tuple(sorted(relevant_paths)),
                context_broadened=context_broadened,
                extraction_complete=actual_diff.extraction_complete,
            )
        )
    return tuple(packets)


def review_candidate_invariants(
    obligations: Sequence[InvariantReviewObligation],
    *,
    proposal: ProposalRevision,
    actual_diff: StructrrActualDiff,
    comparison_report: Mapping[str, Any],
    extraction_version: str,
    reviewer_version: str,
    baseline_source: Mapping[str, str],
    candidate_source: Mapping[str, str],
    validation_evidence: Mapping[str, str],
    reviewer: Callable[[InvariantReviewEvidencePacket], InvariantReviewAssessment],
) -> InvariantReviewRun:
    """Review invariants one packet at a time and compile the fail-closed receipt."""
    packets = compile_invariant_review_packets(
        obligations,
        proposal=proposal,
        actual_diff=actual_diff,
        comparison_report=comparison_report,
        extraction_version=extraction_version,
        reviewer_version=reviewer_version,
        baseline_source=baseline_source,
        candidate_source=candidate_source,
        validation_evidence=validation_evidence,
    )
    worklist = compile_invariant_review_worklist(
        obligations,
        proposal_fingerprint=proposal.fingerprint,
        candidate_product_digest=actual_diff.candidate_product_digest,
        extraction_version=extraction_version,
        reviewer_version=reviewer_version,
        review_packet_fingerprints={
            packet.obligation.invariant_id: packet.fingerprint for packet in packets
        },
    )
    packets_by_id = {item.obligation.invariant_id: item for item in packets}
    decisions: list[DecisionResult] = []
    for specification in worklist.specifications:
        invariant_id = specification.subject
        packet = packets_by_id[invariant_id]
        if not packet.extraction_complete:
            assessment = InvariantReviewAssessment(
                DecisionOutcome.UNKNOWN,
                "candidate extraction is incomplete; "
                "invariant preservation cannot be established",
            )
        elif packet.missing_evidence:
            assessment = InvariantReviewAssessment(
                DecisionOutcome.UNKNOWN,
                "required targeted evidence is missing: "
                + ", ".join(packet.missing_evidence),
            )
        else:
            assessment = reviewer(packet)
            if not isinstance(assessment, InvariantReviewAssessment):
                raise ValueError("invariant reviewer returned an invalid assessment")
        decisions.append(
            build_invariant_review_result(
                specification,
                outcome=assessment.outcome,
                explanation=assessment.explanation,
            )
        )
    receipt = compile_invariant_review_receipt(
        obligations,
        decisions,
        proposal_fingerprint=proposal.fingerprint,
        candidate_product_digest=actual_diff.candidate_product_digest,
        extraction_version=extraction_version,
        reviewer_version=reviewer_version,
        review_packet_fingerprints=tuple(
            (packet.obligation.invariant_id, packet.fingerprint) for packet in packets
        ),
    )
    return InvariantReviewRun(packets, receipt)


def compile_invariant_review_receipt(
    obligations: Sequence[InvariantReviewObligation],
    decisions: Sequence[DecisionResult],
    *,
    proposal_fingerprint: str,
    candidate_product_digest: str,
    extraction_version: str,
    reviewer_version: str,
    review_packet_fingerprints: Sequence[tuple[str, str]] = (),
) -> InvariantReviewReceipt:
    """Compile and validate the separate invariant-review acceptance receipt."""
    worklist = compile_invariant_review_worklist(
        obligations,
        proposal_fingerprint=proposal_fingerprint,
        candidate_product_digest=candidate_product_digest,
        extraction_version=extraction_version,
        reviewer_version=reviewer_version,
        review_packet_fingerprints=dict(_packet_bindings(review_packet_fingerprints)),
    )
    normalized_decisions = tuple(sorted(decisions, key=lambda item: item.decision_id))
    _validate_decisions(normalized_decisions, worklist, obligations=obligations)
    receipt = InvariantReviewReceipt(
        proposal_fingerprint=proposal_fingerprint,
        candidate_product_digest=candidate_product_digest,
        extraction_version=extraction_version,
        reviewer_version=reviewer_version,
        obligation_fingerprints=_bindings(obligations),
        review_packet_fingerprints=_packet_bindings(review_packet_fingerprints),
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
        review_packet_fingerprints=receipt.review_packet_fingerprints,
    )
    return receipt


def _matching_source_paths(selectors: Sequence[str], paths: Sequence[str]) -> set[str]:
    selected = {item.split("::", 1)[0] for item in selectors}
    return {
        path
        for path in paths
        if any(
            path == selector
            or path.startswith(selector.rstrip("/") + "/")
            or selector.startswith(path.rstrip("/") + "/")
            for selector in selected
            if "/" in selector
        )
    }


def _selected_source_evidence(
    source: Mapping[str, str], relevant_paths: set[str], required_selectors: set[str]
) -> dict[str, str]:
    return {
        key: value
        for key, value in source.items()
        if value.strip()
        and (
            key in required_selectors
            or key in relevant_paths
            or any(key.startswith(path.rstrip("/") + "/") for path in relevant_paths)
        )
    }


def _has_required_evidence(
    selector: str,
    baseline: Mapping[str, str],
    candidate: Mapping[str, str],
    validation: Mapping[str, str],
) -> bool:
    if selector.startswith("source:"):
        return selector in baseline or selector in candidate
    if selector.startswith(("test:", "validation:")):
        return selector in validation
    return selector in baseline or selector in candidate or selector in validation


def load_invariant_review_receipt(path: Path) -> InvariantReviewReceipt:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"could not read invariant review receipt {path}: {error}"
        ) from error
    if not isinstance(raw, Mapping):
        raise ValueError("invariant review receipt must contain an object")
    schema_version = raw.get("schema_version")
    if schema_version == "invariant-review-receipt-v1":
        raise ValueError(
            "legacy invariant review receipt lacks evidence packet bindings; "
            "rerun review"
        )
    if schema_version != INVARIANT_REVIEW_RECEIPT_SCHEMA_VERSION:
        raise ValueError("unsupported invariant review receipt schema version")
    raw_bindings = raw.get("obligation_fingerprints")
    raw_packet_bindings = raw.get("review_packet_fingerprints")
    raw_decisions = raw.get("decisions")
    if (
        not isinstance(raw_bindings, list)
        or not all(isinstance(item, Mapping) for item in raw_bindings)
        or not isinstance(raw_packet_bindings, list)
        or not all(isinstance(item, Mapping) for item in raw_packet_bindings)
        or not isinstance(raw_decisions, list)
        or not all(isinstance(item, Mapping) for item in raw_decisions)
    ):
        raise ValueError("invariant review receipt collections are malformed")
    bindings = tuple(
        (_text(item, "invariant_id"), _text(item, "fingerprint"))
        for item in raw_bindings
    )
    packet_bindings = tuple(
        (_text(item, "invariant_id"), _text(item, "fingerprint"))
        for item in raw_packet_bindings
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
        review_packet_fingerprints=packet_bindings,
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


def write_invariant_review_run(path: Path, run: InvariantReviewRun) -> None:
    """Persist the focused evidence packets together with their acceptance receipt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(run.to_data(), indent=2, sort_keys=True) + "\n")


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


def _packet_bindings(
    bindings: Sequence[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    identifiers = [identifier for identifier, _ in bindings]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("invariant review evidence packets contain duplicate IDs")
    if any(
        not identifier.strip() or not fingerprint.strip()
        for identifier, fingerprint in bindings
    ):
        raise ValueError("invariant review evidence packet bindings must be non-empty")
    return tuple(sorted(bindings))


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
    "InvariantReviewAssessment",
    "InvariantReviewEvidencePacket",
    "InvariantReviewObligation",
    "InvariantReviewReceipt",
    "InvariantReviewRun",
    "build_invariant_review_result",
    "compile_invariant_review_receipt",
    "compile_invariant_review_packets",
    "compile_invariant_review_worklist",
    "load_invariant_review_receipt",
    "review_candidate_invariants",
    "write_invariant_review_run",
    "write_invariant_review_receipt",
]
