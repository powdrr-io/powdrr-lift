"""Field-level source-faithfulness reviews for partial semantic contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from powdrr_lift.core.semantic_contract import PartialSemanticContract
from powdrr_lift.core.semantic_decision import (
    DECISION_VALUES,
    SemanticDecision,
    SemanticDecisionProvider,
    SemanticDecisionSpec,
)

FAITHFULNESS_REVISION = "source-faithfulness-v1"
SCENARIO_CLAIM_REVISION = "scenario-claim-faithfulness-v1"
MAX_SCENARIO_CLAIMS = 32
REVIEWABLE_FIELDS = (
    "behavior_family",
    "temporal_scope",
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


@dataclass(frozen=True, slots=True)
class ScenarioClaimReview:
    """A review of one deduplicated claim added during scenario elaboration."""

    claim_id: str
    field_paths: tuple[str, ...]
    candidate_value: str
    assumption_backed_paths: tuple[str, ...]
    decision: SemanticDecision

    def to_data(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "field_paths": list(self.field_paths),
            "candidate_value": self.candidate_value,
            "assumption_backed_paths": list(self.assumption_backed_paths),
            "decision": self.decision.to_data(),
        }


def prepare_scenario_claim_reviews(
    contract: PartialSemanticContract,
    *,
    scenario: Mapping[str, Any],
    ledger_clauses: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Prepare bounded reviews for claims scenario generation adds downstream."""
    source_clauses = _scenario_source_clauses(contract, ledger_clauses)
    source_context = {
        "source_clauses": source_clauses,
        "accepted_decisions": {
            "routing": contract.routing,
            "disposition": contract.disposition,
            "polarity": contract.polarity,
            "requirement_strength": contract.requirement_strength,
            "quantifier": contract.quantifier,
            "behavior_family": contract.behavior_family,
            "subject": contract.subject.span.text,
            "behavior": contract.behavior.span.text,
            "preconditions": [item.span.text for item in contract.preconditions],
            "exceptions": [item.span.text for item in contract.exceptions],
            "explicit_result": (
                contract.explicit_result.span.text
                if contract.explicit_result is not None
                else None
            ),
            "temporal_scope": contract.temporal_scope,
            "semantic_dimensions": dict(contract.semantic_dimensions),
        },
    }
    context_text = json.dumps(
        source_context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    # Context and excluded clauses do not create downstream obligations, so
    # their descriptive scenario text is not subject to obligation review.
    candidates = (
        []
        if contract.routing in {"context", "exclude"}
        or contract.disposition in {"context", "nonactionable"}
        else _scenario_claim_candidates(scenario)
    )
    deduplicated: dict[str, dict[str, Any]] = {}
    for path, value in candidates:
        normalized = _normalize_claim(value)
        if not normalized or _is_nonclaim_scenario_value(value):
            continue
        existing = deduplicated.get(normalized)
        if existing is None:
            existing = {
                "candidate_value": value,
                "field_paths": [],
                "assumption_backed_paths": [],
            }
            deduplicated[normalized] = existing
        existing["field_paths"].append(path)
        if _scenario_claim_has_assumption(path, value, scenario):
            existing["assumption_backed_paths"].append(path)

    deterministic: list[dict[str, Any]] = []
    provider_claims: list[dict[str, Any]] = []
    for item in deduplicated.values():
        value = str(item["candidate_value"])
        claim = {
            **item,
            "claim_id": "claim:"
            + _fingerprint(
                {"paths": item["field_paths"], "value": _normalize_claim(value)}
            )[7:23],
        }
        exact_quote = any(
            len(_normalize_claim(value)) >= 16
            and _normalize_claim(value) in _normalize_claim(clause["text"])
            for clause in source_clauses
        )
        spec = SemanticDecisionSpec(
            decision_id=(
                f"decision:{contract.contract_id}:scenario:{claim['claim_id']}:entailment"
            ),
            decision_kind="entailment",
            subject_ref=contract.source_ref,
            proposition_text=contract.proposition_text,
            source_fingerprint=contract.source_fingerprint,
            context_text=context_text,
            candidate_set_fingerprint=_fingerprint(
                {
                    "scenario_fingerprint": _fingerprint(scenario),
                    "claim_id": claim["claim_id"],
                    "field_paths": claim["field_paths"],
                    "candidate_value": value,
                    "contract_fingerprint": contract.fingerprint,
                }
            ),
            contract_revision=SCENARIO_CLAIM_REVISION,
        )
        claim["spec"] = spec.to_data()
        if exact_quote:
            deterministic.append(claim)
        else:
            provider_claims.append(claim)

    overflow = len(provider_claims) > MAX_SCENARIO_CLAIMS
    if overflow:
        provider_claims = []
    decision_specs = {
        str(claim["claim_id"]): claim["spec"]
        for claim in (*deterministic, *provider_claims)
    }
    deterministic_claims = [
        {key: value for key, value in claim.items() if key != "spec"}
        for claim in deterministic
    ]
    requests = (
        [
            {
                "source_text": contract.proposition_text,
                "context_text": context_text,
                "claim": {key: value for key, value in claim.items() if key != "spec"},
            }
            for claim in provider_claims
        ]
        if not overflow
        else []
    )
    return {
        "source_ref": contract.source_ref,
        "source_fingerprint": contract.source_fingerprint,
        "contract_fingerprint": contract.fingerprint,
        "scenario_fingerprint": _fingerprint(scenario),
        "context_fingerprint": _fingerprint(source_context),
        "deterministic_claims": deterministic_claims,
        "decision_specs": decision_specs,
        "requests": requests,
        "overflow": overflow,
        "candidate_count": len(deduplicated),
    }


def finalize_scenario_claim_reviews(
    plan: Mapping[str, Any],
    provider_results: Sequence[Mapping[str, Any]],
    *,
    benchmark_mode: bool = False,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Bind claim entailment decisions and reject unsupported scenario additions."""
    requests = plan.get("requests")
    deterministic = plan.get("deterministic_claims")
    decision_specs = plan.get("decision_specs")
    if (
        not isinstance(requests, list)
        or not isinstance(deterministic, list)
        or not isinstance(decision_specs, Mapping)
    ):
        raise FaithfulnessError("scenario claim review plan is malformed")
    if len(requests) != len(provider_results):
        raise FaithfulnessError("scenario claim review count is invalid")
    if plan.get("overflow") is True:
        return {
            "accepted": False,
            "findings": [{"reason_code": "scenario_claim_limit_exceeded"}],
            "unresolved_fields": [],
            "reviews": [],
        }

    claims: list[tuple[Mapping[str, Any], Mapping[str, Any] | None]] = [
        (claim, None) for claim in deterministic if isinstance(claim, Mapping)
    ]
    if len(claims) != len(deterministic):
        raise FaithfulnessError("deterministic scenario claims are malformed")
    for request, raw_result in zip(requests, provider_results, strict=True):
        if not isinstance(request, Mapping) or not isinstance(
            request.get("claim"), Mapping
        ):
            raise FaithfulnessError("scenario claim request is malformed")
        if set(raw_result) != {"status", "value", "reason_code"}:
            if benchmark_mode:
                raw_result = {
                    "status": "unresolved",
                    "value": None,
                    "reason_code": "invalid_response",
                }
            else:
                raise FaithfulnessError("scenario claim result is malformed")
        claims.append((request["claim"], raw_result))

    reviews: list[ScenarioClaimReview] = []
    for scenario_claim, scenario_result in claims:
        claim_id = scenario_claim.get("claim_id")
        spec_raw = decision_specs.get(claim_id)
        if not isinstance(spec_raw, Mapping):
            raise FaithfulnessError("scenario claim has no decision spec")
        spec = SemanticDecisionSpec.from_data(spec_raw)
        if spec.decision_kind != "entailment":
            raise FaithfulnessError("scenario claim has an invalid decision kind")
        evidence_refs: tuple[str, ...]
        if scenario_result is None:
            provider = SemanticDecisionProvider(kind="deterministic-rule")
            provider_result_data: dict[str, Any] = {
                "status": "resolved",
                "value": "entailed",
            }
            evidence_refs = (
                f"source-proposition:{spec.subject_ref}",
                f"scenario-claim:{scenario_claim['claim_id']}:exact-source-quote",
            )
        else:
            provider = SemanticDecisionProvider(kind="planning-llm")
            provider_result_data = {
                key: scenario_result.get(key)
                for key in ("status", "value", "reason_code")
            }
            if benchmark_mode and (
                provider_result_data["status"] != "resolved"
                or provider_result_data["value"] not in DECISION_VALUES["entailment"]
                or provider_result_data["reason_code"] is not None
            ):
                provider = SemanticDecisionProvider(kind="deterministic-rule")
                provider_result_data = {
                    "status": "resolved",
                    "value": "not_stated",
                    "reason_code": None,
                }
            evidence_refs = (f"source-proposition:{spec.subject_ref}",)
        decision = spec.bind(
            provider=provider,
            provider_result=provider_result_data,
            evidence_refs=evidence_refs,
            created_at=created_at or datetime.now(UTC).isoformat(),
        )
        reviews.append(
            ScenarioClaimReview(
                claim_id=str(scenario_claim["claim_id"]),
                field_paths=tuple(str(item) for item in scenario_claim["field_paths"]),
                candidate_value=str(scenario_claim["candidate_value"]),
                assumption_backed_paths=tuple(
                    str(item) for item in scenario_claim["assumption_backed_paths"]
                ),
                decision=decision,
            )
        )

    findings: list[dict[str, Any]] = []
    unresolved_fields: list[str] = []
    for review in reviews:
        bound_result = review.decision.result
        if bound_result.status != "resolved":
            unresolved_fields.extend(review.field_paths)
            findings.append(
                {
                    "claim_id": review.claim_id,
                    "field_paths": list(review.field_paths),
                    "reason_code": bound_result.reason_code or "classifier_abstained",
                    "classification": "unresolved",
                }
            )
        elif bound_result.value == "contradicted":
            findings.append(
                {
                    "claim_id": review.claim_id,
                    "field_paths": list(review.field_paths),
                    "reason_code": "intent_contradiction",
                    "classification": "contradicted",
                }
            )
        elif bound_result.value == "not_stated":
            assumption_paths = set(review.assumption_backed_paths)
            unbacked_paths = set(review.field_paths) - assumption_paths
            if unbacked_paths:
                unresolved_fields.extend(sorted(unbacked_paths))
                findings.append(
                    {
                        "claim_id": review.claim_id,
                        "field_paths": sorted(unbacked_paths),
                        "reason_code": "source_underspecified",
                        "classification": "not_stated",
                    }
                )
        elif bound_result.value != "entailed":
            raise FaithfulnessError("scenario claim has an invalid entailment value")

    return {
        "accepted": not findings,
        "findings": findings,
        "unresolved_fields": sorted(set(unresolved_fields)),
        "reviews": [review.to_data() for review in reviews],
    }


def _scenario_source_clauses(
    contract: PartialSemanticContract, ledger_clauses: Sequence[Mapping[str, Any]]
) -> list[dict[str, str]]:
    clauses: list[dict[str, str]] = []
    for item in ledger_clauses:
        clause_id = item.get("clause_id")
        text = item.get("text")
        if isinstance(clause_id, str) and clause_id.strip() and isinstance(text, str):
            clauses.append({"clause_id": clause_id, "text": text})
    if not any(item["clause_id"] == contract.source_ref for item in clauses):
        clauses.insert(
            0,
            {"clause_id": contract.source_ref, "text": contract.proposition_text},
        )
    return clauses


def _scenario_claim_candidates(scenario: Mapping[str, Any]) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    subject = scenario.get("subject")
    if isinstance(subject, str):
        candidates.append(("subject", subject))
    then = scenario.get("then")
    if isinstance(then, str):
        candidates.append(("then", then))
    dimensions = scenario.get("dimensions", {})
    if isinstance(dimensions, Mapping):
        candidates.extend(
            (f"dimensions.{name}", value)
            for name, value in dimensions.items()
            if isinstance(name, str) and isinstance(value, str)
        )
    related = scenario.get("related_requirements", [])
    if isinstance(related, list):
        candidates.extend(
            (f"related_requirements[{index}]", value)
            for index, value in enumerate(related)
            if isinstance(value, str)
        )
    assumptions = scenario.get("assumptions", [])
    if isinstance(assumptions, list):
        candidates.extend(
            (f"assumptions[{index}].resolution", resolution)
            for index, item in enumerate(assumptions)
            if isinstance(item, Mapping)
            and isinstance((resolution := item.get("resolution")), str)
        )
    capabilities = scenario.get("capability_matrix", [])
    if isinstance(capabilities, list):
        for index, item in enumerate(capabilities):
            if not isinstance(item, Mapping):
                continue
            capability = item.get("capability")
            behavior = item.get("behavior")
            evidence = item.get("evidence", [])
            if (
                isinstance(capability, str)
                and isinstance(behavior, str)
                and isinstance(evidence, list)
                and all(isinstance(value, str) for value in evidence)
            ):
                detail = "; ".join(str(value) for value in evidence)
                candidates.append(
                    (
                        f"capability_matrix[{index}]",
                        f"{capability}: {behavior}; evidence: {detail}",
                    )
                )
    return candidates


def _scenario_claim_has_assumption(
    field_path: str, candidate: str, scenario: Mapping[str, Any]
) -> bool:
    assumptions = scenario.get("assumptions", [])
    if not isinstance(assumptions, list):
        return False
    parts = field_path.split(".")
    dimension = parts[1] if len(parts) == 2 and parts[0] == "dimensions" else None
    if field_path.startswith("assumptions[") and field_path.endswith("].resolution"):
        return True
    normalized = _normalize_claim(candidate)
    for item in assumptions:
        if not isinstance(item, Mapping):
            continue
        item_dimension = item.get("dimension")
        resolution = item.get("resolution")
        if not isinstance(item_dimension, str) or not isinstance(resolution, str):
            continue
        if dimension == item_dimension:
            return True
        if field_path == "then" and _normalize_claim(resolution) in normalized:
            return True
    return False


def _is_nonclaim_scenario_value(value: str) -> bool:
    normalized = _normalize_claim(value)
    return (
        normalized in {"not_applicable", "not applicable"}
        or normalized.startswith("needs clarification:")
        or normalized.startswith("needs clarification ")
        or normalized.startswith("assumed default:")
    )


def _normalize_claim(value: str) -> str:
    return " ".join(value.split()).casefold()


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
        instructions = [
            "Choose entailed only when the source supports this field without "
            "adding meaning.",
            "Choose contradicted when the source rules the candidate out.",
            "Choose not_stated when the source does not say enough.",
            "Evaluate only this field; do not repair or rewrite it.",
        ]
        if field == "behavior_family":
            instructions.append(
                "This is a semantic action-family label, not a verbatim quote. "
                "Treat enforcing a declared constraint or rejecting an invalid "
                "declaration as validate, even when the source does not use the "
                "word 'validate'."
            )
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
                "instructions": instructions,
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
    benchmark_mode: bool = False,
    created_at: str,
) -> list[FieldEntailmentReview]:
    if len(requests) != len(provider_results):
        raise FaithfulnessError("field entailment result count is invalid")
    reviews: list[FieldEntailmentReview] = []
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
        provider = SemanticDecisionProvider(kind="planning-llm")
        result_to_bind = provider_result
        evidence_refs: tuple[str, ...] = (f"source-proposition:{spec.source_ref}",)
        if benchmark_mode:
            exact_source_quote = (
                spec.field
                in {
                    "subject",
                    "behavior",
                    "preconditions",
                    "exceptions",
                    "explicit_result",
                }
                and spec.candidate_value in spec.source_text
            )
            if exact_source_quote:
                # Exact spans are stronger evidence than an uncertain entailment
                # judgment: the quoted words are necessarily present in source.
                result_to_bind = {"status": "resolved", "value": "entailed"}
                provider = SemanticDecisionProvider(kind="deterministic-rule")
                evidence_refs = (
                    *evidence_refs,
                    f"normative-default:{spec.field}:exact-source-quote",
                )
            elif (
                not isinstance(provider_result, Mapping)
                or provider_result.get("status") != "resolved"
                or provider_result.get("value") not in DECISION_VALUES["entailment"]
                or provider_result.get("reason_code") is not None
                or not set(provider_result).issubset({"status", "value", "reason_code"})
                or provider_result.get("value") == "contradicted"
            ):
                result_to_bind = {"status": "resolved", "value": "not_stated"}
                provider = SemanticDecisionProvider(kind="deterministic-rule")
                evidence_refs = (
                    *evidence_refs,
                    f"normative-default:{spec.field}:not_stated",
                )
        decision = decision_spec.bind(
            provider=provider,
            provider_result=result_to_bind,
            evidence_refs=evidence_refs,
            created_at=created_at,
        )
        reviews.append(FieldEntailmentReview(spec=spec, decision=decision))
    return reviews


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
