"""Versioned evidence expectations for instruction obligations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

OBLIGATION_EVIDENCE_SCHEMA_VERSION = "obligation-evidence-v1"


class NormativeStrength(StrEnum):
    MUST = "must"
    SHOULD = "should"
    MAY = "may"
    UNSPECIFIED = "unspecified"


class DiffExpectation(StrEnum):
    REQUIRED = "required"
    EXPECTED = "expected"
    NONE = "none"
    UNRESOLVED = "unresolved"


class EvidenceRoute(StrEnum):
    STRUCTURAL_DIFF = "structural_diff"
    BEHAVIOR_VALIDATION = "behavior_validation"
    INVARIANT_REVIEW = "invariant_review"
    SCOPE_REVIEW = "scope_review"
    PROCESS_RECEIPT = "process_receipt"


@dataclass(frozen=True, slots=True)
class ObligationEvidenceContract:
    """Compiler-owned evidence expectations tied to one source obligation."""

    obligation_id: str
    clause_id: str
    normative_strength: NormativeStrength
    diff_expectation: DiffExpectation
    review_routes: tuple[EvidenceRoute, ...]
    rationale: str
    schema_version: str = OBLIGATION_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.obligation_id.strip() or not self.clause_id.strip():
            raise ValueError("evidence contract requires obligation and clause IDs")
        if not self.review_routes:
            raise ValueError("evidence contract requires at least one review route")
        if len(set(self.review_routes)) != len(self.review_routes):
            raise ValueError("evidence contract review routes must be unique")
        if not self.rationale.strip():
            raise ValueError("evidence contract requires a classification rationale")
        if self.schema_version != OBLIGATION_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("unsupported obligation evidence schema version")

    def to_data(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "obligation_id": self.obligation_id,
            "clause_id": self.clause_id,
            "normative_strength": self.normative_strength.value,
            "diff_expectation": self.diff_expectation.value,
            "review_routes": [route.value for route in self.review_routes],
            "rationale": self.rationale,
        }
        if include_fingerprint:
            data["fingerprint"] = _fingerprint(data)
        return data

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> ObligationEvidenceContract:
        if raw.get("schema_version") != OBLIGATION_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("unsupported obligation evidence schema version")
        try:
            strength = NormativeStrength(_required_text(raw, "normative_strength"))
            expectation = DiffExpectation(_required_text(raw, "diff_expectation"))
            routes_raw = raw.get("review_routes")
            if not isinstance(routes_raw, list):
                raise ValueError("review_routes must be a list")
            routes = tuple(EvidenceRoute(item) for item in routes_raw)
        except ValueError as error:
            raise ValueError(
                f"invalid obligation evidence contract: {error}"
            ) from error
        contract = cls(
            obligation_id=_required_text(raw, "obligation_id"),
            clause_id=_required_text(raw, "clause_id"),
            normative_strength=strength,
            diff_expectation=expectation,
            review_routes=routes,
            rationale=_required_text(raw, "rationale"),
        )
        if raw.get("fingerprint") != contract.to_data()["fingerprint"]:
            raise ValueError("obligation evidence fingerprint does not match content")
        return contract


def compile_obligation_evidence_contract(
    *,
    obligation_id: str,
    clause_id: str,
    requirement_strength: str,
    kind: str,
    polarity: str,
) -> ObligationEvidenceContract:
    """Compile source strength and design kind into explicit evidence routes.

    A behavior may be implemented by changing an existing body, so a feature
    does not require a new structural entity. Public entities and interfaces
    do. Invariants are checked in their own review route because structure
    alone cannot establish that behavior was preserved.
    """
    strength = NormativeStrength(requirement_strength.strip().casefold())
    normalized_kind = kind.strip().casefold()
    normalized_polarity = polarity.strip().casefold()
    if normalized_kind not in {
        "entity",
        "feature",
        "interface",
        "invariant",
        "guidance",
        "non_goal",
    }:
        raise ValueError(f"unsupported actionable obligation kind: {kind!r}")
    if normalized_polarity not in {
        "required",
        "prohibited",
        "permitted",
        "descriptive",
    }:
        raise ValueError(f"unsupported obligation polarity: {polarity!r}")

    if normalized_kind in {"entity", "interface"}:
        expectation = DiffExpectation.REQUIRED
        routes = [EvidenceRoute.STRUCTURAL_DIFF, EvidenceRoute.BEHAVIOR_VALIDATION]
        rationale = (
            "the requested entity or interface must be present in the candidate diff"
        )
    elif normalized_kind == "invariant":
        expectation = DiffExpectation.NONE
        routes = [EvidenceRoute.INVARIANT_REVIEW, EvidenceRoute.BEHAVIOR_VALIDATION]
        rationale = (
            "preservation or prohibition is established by targeted review and "
            "behavior evidence"
        )
    elif normalized_kind == "non_goal":
        expectation = DiffExpectation.NONE
        routes = [EvidenceRoute.SCOPE_REVIEW]
        rationale = (
            "a non-goal constrains scope and does not require a positive "
            "structural operation"
        )
    elif normalized_kind == "guidance":
        expectation = DiffExpectation.EXPECTED
        routes = [EvidenceRoute.SCOPE_REVIEW, EvidenceRoute.INVARIANT_REVIEW]
        rationale = (
            "guidance is reviewed for scope and intent without requiring a named entity"
        )
    else:
        expectation = (
            DiffExpectation.REQUIRED
            if strength is NormativeStrength.MUST
            else DiffExpectation.EXPECTED
        )
        routes = [EvidenceRoute.STRUCTURAL_DIFF, EvidenceRoute.BEHAVIOR_VALIDATION]
        rationale = (
            "the behavior should be represented by an implementation change, "
            "which may modify an existing entity"
        )

    if strength is NormativeStrength.UNSPECIFIED:
        expectation = DiffExpectation.UNRESOLVED
        rationale += (
            "; source wording has no explicit normative strength, so review "
            "must resolve whether omission is acceptable"
        )

    if (
        normalized_polarity == "prohibited"
        and EvidenceRoute.BEHAVIOR_VALIDATION not in routes
    ):
        routes.append(EvidenceRoute.BEHAVIOR_VALIDATION)
    if strength is NormativeStrength.MAY:
        expectation = DiffExpectation.NONE
        routes = [EvidenceRoute.SCOPE_REVIEW]
        rationale += (
            "; source strength is permissive, so absence of a structural "
            "change is not a failure"
        )
    elif strength is NormativeStrength.SHOULD:
        if expectation is DiffExpectation.REQUIRED:
            expectation = DiffExpectation.EXPECTED
        rationale += (
            "; a missed recommendation requires an explicit acceptance decision"
        )
    if normalized_polarity == "permitted":
        expectation = DiffExpectation.NONE
        routes = [EvidenceRoute.INVARIANT_REVIEW, EvidenceRoute.SCOPE_REVIEW]
        rationale += "; permitted behavior is protected from accidental restriction"
    elif normalized_polarity == "descriptive":
        expectation = DiffExpectation.NONE
        routes = [EvidenceRoute.INVARIANT_REVIEW]
        rationale += (
            "; descriptive source context is reviewed for preservation, not "
            "implemented as a new operation"
        )

    return ObligationEvidenceContract(
        obligation_id=obligation_id,
        clause_id=clause_id,
        normative_strength=strength,
        diff_expectation=expectation,
        review_routes=tuple(routes),
        rationale=rationale,
    )


def assert_obligation_evidence_complete(
    contracts: Sequence[ObligationEvidenceContract],
    expected_obligation_ids: Sequence[str],
) -> None:
    """Require exactly one evidence contract for every expected obligation."""
    actual = [item.obligation_id for item in contracts]
    if len(actual) != len(set(actual)):
        raise ValueError("obligation evidence contracts contain duplicate IDs")
    if set(actual) != set(expected_obligation_ids):
        missing = sorted(set(expected_obligation_ids) - set(actual))
        unexpected = sorted(set(actual) - set(expected_obligation_ids))
        raise ValueError(
            "obligation evidence coverage mismatch: "
            f"missing={missing}, unexpected={unexpected}"
        )


def _required_text(raw: Mapping[str, Any], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


__all__ = [
    "DiffExpectation",
    "EvidenceRoute",
    "NormativeStrength",
    "OBLIGATION_EVIDENCE_SCHEMA_VERSION",
    "ObligationEvidenceContract",
    "assert_obligation_evidence_complete",
    "compile_obligation_evidence_contract",
]
