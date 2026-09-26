"""Field-level source-faithfulness reviews for partial semantic contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from powdrr_lift.core.semantic_contract import PartialSemanticContract
from powdrr_lift.core.semantic_decision import (
    SemanticDecision,
    SemanticDecisionProvider,
    SemanticDecisionSpec,
)

FAITHFULNESS_REVISION = "source-faithfulness-v1"
REVIEWABLE_FIELDS = (
    "behavior_family",
    "temporal_scope",
    "source_predicate",
    "subject",
    "behavior",
    "preconditions",
    "exceptions",
    "explicit_result",
)

REQUIRED_FIELDS = {
    "entity": ("subject",),
    "feature": ("polarity", "subject", "behavior", "predicate"),
    "interface": ("polarity", "subject", "behavior", "predicate"),
    "invariant": ("polarity", "quantifier", "subject", "behavior", "predicate"),
    "guidance": ("subject", "behavior"),
    "non_goal": ("polarity", "subject", "behavior"),
    "nonactionable": (),
    "context": (),
}


class FaithfulnessError(ValueError):
    """Raised when a faithfulness artifact is malformed."""


@dataclass(frozen=True, slots=True)
class FieldEntailmentSpec:
    field: str
    candidate_value: str
    contract_id: str
    source_ref: str
    source_text: str
    source_fingerprint: str
    contract_fingerprint: str

    @property
    def input_fingerprint(self) -> str:
        return _fingerprint(
            {
                "schema_version": "field-entailment-spec-v1",
                "field": self.field,
                "candidate_value": self.candidate_value,
                "source_ref": self.source_ref,
                "source_text": self.source_text,
                "source_fingerprint": self.source_fingerprint,
                "contract_fingerprint": self.contract_fingerprint,
            }
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": "field-entailment-spec-v1",
            "field": self.field,
            "candidate_value": self.candidate_value,
            "contract_id": self.contract_id,
            "source_ref": self.source_ref,
            "source_text": self.source_text,
            "source_fingerprint": self.source_fingerprint,
            "contract_fingerprint": self.contract_fingerprint,
            "input_fingerprint": self.input_fingerprint,
        }

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> FieldEntailmentSpec:
        expected = {
            "schema_version",
            "field",
            "candidate_value",
            "contract_id",
            "source_ref",
            "source_text",
            "source_fingerprint",
            "contract_fingerprint",
            "input_fingerprint",
        }
        if (
            set(raw) != expected
            or raw.get("schema_version") != "field-entailment-spec-v1"
        ):
            raise FaithfulnessError("field entailment spec fields are invalid")
        values = {
            key: raw.get(key)
            for key in expected - {"schema_version", "input_fingerprint"}
        }
        if not all(
            isinstance(value, str) and value.strip() for value in values.values()
        ):
            raise FaithfulnessError("field entailment spec contains an empty value")
        spec = cls(**values)  # type: ignore[arg-type]
        if raw.get("input_fingerprint") != spec.input_fingerprint:
            raise FaithfulnessError("field entailment spec fingerprint is stale")
        return spec


@dataclass(frozen=True, slots=True)
class FieldEntailmentReview:
    spec: FieldEntailmentSpec
    decision: SemanticDecision

    def to_data(self) -> dict[str, Any]:
        return {"spec": self.spec.to_data(), "decision": self.decision.to_data()}


@dataclass(frozen=True, slots=True)
class FaithfulnessOutcome:
    accepted: bool
    unresolved_fields: tuple[str, ...]
    findings: tuple[dict[str, str], ...]

    def to_data(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "unresolved_fields": list(self.unresolved_fields),
            "findings": list(self.findings),
        }


def prepare_field_entailment_reviews(
    contract: PartialSemanticContract,
) -> list[dict[str, Any]]:
    """Prepare one bounded C10 request for each meaning-bearing field."""
    if contract.disposition == "context":
        return []
    values = _field_values(contract)
    requests: list[dict[str, Any]] = []
    for field in REVIEWABLE_FIELDS:
        value = values.get(field)
        if value is None or not str(value).strip():
            continue
        spec = FieldEntailmentSpec(
            field=field,
            candidate_value=str(value),
            contract_id=contract.contract_id,
            source_ref=contract.source_ref,
            source_text=contract.proposition_text,
            source_fingerprint=contract.source_fingerprint,
            contract_fingerprint=contract.fingerprint,
        )
        requests.append(
            {
                "spec": spec.to_data(),
                "question": (
                    "Is this candidate field entailed by the exact source proposition?"
                ),
                "instructions": [
                    (
                        "Choose entailed only when the source supports this field "
                        "without adding meaning."
                    ),
                    "Choose contradicted when the source rules the candidate out.",
                    "Choose not_stated when the source does not say enough.",
                    "Evaluate only this field; do not repair or rewrite it.",
                ],
                "allowed_values": ["entailed", "contradicted", "not_stated"],
                "source_text": contract.proposition_text,
                "candidate_field": field,
                "candidate_value": str(value),
            }
        )
    return requests


def bind_field_entailment_reviews(
    *,
    requests: Sequence[Mapping[str, Any]],
    provider_results: Sequence[Mapping[str, Any]],
    created_at: str,
) -> list[FieldEntailmentReview]:
    if len(requests) != len(provider_results):
        raise FaithfulnessError("field entailment result count is invalid")
    result: list[FieldEntailmentReview] = []
    for request, provider_result in zip(requests, provider_results, strict=True):
        raw_spec = request.get("spec")
        if not isinstance(raw_spec, Mapping):
            raise FaithfulnessError("field entailment request has no spec")
        spec = FieldEntailmentSpec.from_data(raw_spec)
        decision_spec = SemanticDecisionSpec(
            decision_id=f"decision:{spec.contract_id}:{spec.field}:entailment",
            decision_kind="entailment",
            subject_ref=spec.source_ref,
            proposition_text=spec.source_text,
            source_fingerprint=spec.source_fingerprint,
            candidate_set_fingerprint=spec.input_fingerprint,
            contract_revision=FAITHFULNESS_REVISION,
        )
        decision = decision_spec.bind(
            provider=SemanticDecisionProvider(kind="planning-llm"),
            provider_result=provider_result,
            evidence_refs=(f"source-proposition:{spec.source_ref}",),
            created_at=created_at,
        )
        result.append(FieldEntailmentReview(spec=spec, decision=decision))
    return result


def finalize_source_faithfulness(
    contract: PartialSemanticContract,
    reviews: Sequence[FieldEntailmentReview],
) -> FaithfulnessOutcome:
    expected = {
        item["spec"]["field"] for item in prepare_field_entailment_reviews(contract)
    }
    actual = {review.spec.field for review in reviews}
    if actual != expected:
        raise FaithfulnessError("field entailment reviews are incomplete or duplicated")
    unresolved: list[str] = []
    findings: list[dict[str, str]] = []
    for review in reviews:
        value = review.decision.result.value
        if review.decision.result.status != "resolved":
            unresolved.append(review.spec.field)
        elif value == "not_stated":
            unresolved.append(review.spec.field)
        elif value == "contradicted":
            findings.append(
                {"field": review.spec.field, "reason_code": "intent_contradiction"}
            )
        elif value != "entailed":
            raise FaithfulnessError("field entailment returned an invalid value")
    required = set(REQUIRED_FIELDS.get(contract.disposition, ()))
    unresolved_required = sorted(required.intersection(unresolved))
    for field in unresolved_required:
        findings.append({"field": field, "reason_code": "source_underspecified"})
    return FaithfulnessOutcome(
        accepted=not findings and not unresolved_required,
        unresolved_fields=tuple(sorted(unresolved)),
        findings=tuple(findings),
    )


def _field_values(contract: PartialSemanticContract) -> dict[str, str]:
    return {
        "disposition": contract.disposition,
        "polarity": contract.polarity,
        "quantifier": contract.quantifier,
        "requirement_strength": contract.requirement_strength,
        "behavior_family": contract.behavior_family,
        "temporal_scope": contract.temporal_scope,
        "source_predicate": contract.source_predicate,
        "subject": contract.subject.span.text,
        "behavior": contract.behavior.span.text,
        "preconditions": ", ".join(item.span.text for item in contract.preconditions),
        "exceptions": ", ".join(item.span.text for item in contract.exceptions),
        "explicit_result": contract.explicit_result.span.text
        if contract.explicit_result
        else "",
    }


def _fingerprint(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


__all__ = [
    "FAITHFULNESS_REVISION",
    "FaithfulnessError",
    "FaithfulnessOutcome",
    "FieldEntailmentReview",
    "FieldEntailmentSpec",
    "bind_field_entailment_reviews",
    "finalize_source_faithfulness",
    "prepare_field_entailment_reviews",
]
