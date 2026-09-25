"""Compile bounded source decisions into source-anchored semantic contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from powdrr_lift.core.semantic_contract import (
    BoundSourceExtraction,
    PartialSemanticContract,
    SemanticContractError,
    SourceExtractionSpec,
    compile_partial_semantic_contract,
)
from powdrr_lift.core.semantic_decision import (
    DECISION_VALUES,
    SemanticDecision,
    SemanticDecisionProvider,
    SemanticDecisionSpec,
)
from powdrr_lift.core.semantic_faithfulness import (
    FieldEntailmentReview,
)
from powdrr_lift.core.semantic_faithfulness import (
    bind_field_entailment_reviews as bind_field_reviews,
)
from powdrr_lift.core.semantic_faithfulness import (
    finalize_source_faithfulness as finalize_faithfulness,
)
from powdrr_lift.core.semantic_faithfulness import (
    prepare_field_entailment_reviews as prepare_field_reviews,
)
from powdrr_lift.workrr.semantic_classifier import (
    resolve_deterministic_source_decision,
)

SOURCE_CLASSIFIER_REVISION = "source-classifier-v1"
SOURCE_EXTRACTOR_REVISION = "source-extractor-v1"


@dataclass(frozen=True, slots=True)
class ClassifierDefinition:
    question: str
    instructions: tuple[str, ...]


CLASSIFIER_DEFINITIONS: dict[str, ClassifierDefinition] = {
    "disposition": ClassifierDefinition(
        "Which one disposition describes this exact proposition?",
        (
            "Classify product meaning as entity, feature, interface, invariant, "
            "guidance, or non_goal.",
            "Use nonactionable only for delivery, tooling, or process instructions "
            "with no product semantics.",
            "A product prohibition is non_goal, not nonactionable; a universal "
            "product rule is invariant.",
            "Examples: 'Users can export reports' is feature; 'Every response has "
            "an ID' is invariant.",
            "Examples: 'Do not add retries' is non_goal; 'Open a pull request' is "
            "nonactionable.",
        ),
    ),
    "polarity": ClassifierDefinition(
        "Does this exact proposition require, prohibit, permit, or only describe "
        "behavior?",
        (
            "Choose required, prohibited, permitted, or descriptive from source "
            "wording only.",
            "Examples: 'All data should pickle' is required; 'Do not add retries' "
            "is prohibited.",
            "Example: 'Clients may omit the field' is permitted; 'The exporter is "
            "synchronous' is descriptive.",
        ),
    ),
    "quantifier": ClassifierDefinition(
        "What coverage quantifier does this exact proposition state for its subject?",
        (
            "Choose one, some, every, or unspecified; do not infer universal "
            "coverage from normative tone.",
            "Examples: 'Every active report' is every; 'Some reports' is some.",
            "Example: 'Reports support export' is unspecified unless accepted "
            "context states otherwise.",
        ),
    ),
    "requirement_strength": ClassifierDefinition(
        "What normative strength does this exact proposition state?",
        (
            "Choose must, should, may, descriptive, or unspecified from the source "
            "modal.",
            "Preserve should separately from must even when both express required "
            "product behavior.",
            "Examples: 'should pickle' is should; 'must have IDs' is must; 'may "
            "omit' is may.",
        ),
    ),
    "has_precondition": ClassifierDefinition(
        "Does this exact proposition explicitly state a condition that must hold "
        "before or while the behavior applies?",
        (
            "Choose present or absent; do not extract or invent the condition.",
            "Example: 'Every active report can be exported' has a precondition: "
            "active.",
            "Example: 'Users can export reports' has no explicit precondition.",
        ),
    ),
    "has_exception": ClassifierDefinition(
        "Does this exact proposition explicitly state an exception to its behavior "
        "or coverage?",
        (
            "Choose present or absent; do not treat an ordinary condition as an "
            "exception.",
            "Example: 'All reports except archived reports can be exported' has "
            "an exception.",
            "Example: 'Active reports can be exported' has no explicit exception.",
        ),
    ),
    "has_explicit_result": ClassifierDefinition(
        "Does this exact proposition explicitly state the observable result of the "
        "behavior?",
        (
            "Choose present only when the result itself appears in the proposition.",
            "Example: 'The endpoint returns CSV' has an explicit result: CSV.",
            "Example: 'All data should pickle' does not state what successful "
            "pickling preserves.",
        ),
    ),
    "temporal_scope": ClassifierDefinition(
        "What temporal applicability does this exact proposition explicitly state?",
        (
            "Choose current, future, current_and_future, event_bound, or unspecified.",
            "Do not infer future scope from every or all.",
            "Examples: 'On entry' is event_bound; a clause with no temporal wording "
            "is unspecified.",
        ),
    ),
    "source_predicate": ClassifierDefinition(
        "Does this exact proposition state the predicate that determines successful "
        "behavior?",
        (
            "Choose explicit, implied_by_registered_term, or not_stated.",
            "Use implied_by_registered_term only when supplied accepted context "
            "defines the term.",
            "Example: 'returns CSV' is explicit; 'should pickle' is not_stated "
            "without an accepted pickle definition.",
        ),
    ),
    "nonactionable_exclusion_safety": ClassifierDefinition(
        "Does this exact proposition contain only process metadata, product "
        "semantics, or a mixture of both?",
        (
            "Choose process_only only when excluding the proposition cannot remove "
            "product behavior or a product non-goal.",
            "Choose product_semantics_present for any product behavior, constraint, "
            "interface, invariant, or prohibition.",
            "Choose mixed when process instructions and product semantics appear in "
            "the same proposition.",
            "Examples: 'Open a PR' is process_only; 'Do not add retries' is "
            "product_semantics_present.",
        ),
    ),
    "behavior_family": ClassifierDefinition(
        "Which registered behavior family best describes this exact behavior phrase?",
        (
            "Choose only from the supplied behavior-family labels.",
            "Classify the quoted behavior, not a broader implementation you imagine.",
            "Examples: 'pickle' is serialize; 'load a saved report' is retrieve; "
            "'add retries' is retry.",
            "Use other for a supported but uncatalogued behavior and unresolved when "
            "the phrase is ambiguous.",
        ),
    ),
}

SOURCE_DECISION_KINDS = (
    "disposition",
    "polarity",
    "quantifier",
    "requirement_strength",
    "has_precondition",
    "has_exception",
    "has_explicit_result",
    "temporal_scope",
    "source_predicate",
    "nonactionable_exclusion_safety",
)

EXTRACTION_DEFINITIONS: dict[str, ClassifierDefinition] = {
    "subject": ClassifierDefinition(
        "Copy the smallest exact phrase naming what this proposition applies to.",
        (
            "Return an exact case-sensitive substring, not a paraphrase.",
            "Do not return only a determiner or quantifier such as all, every, a, "
            "or the.",
            "Examples: 'All data should pickle' returns 'data'; 'Users can export "
            "reports' returns 'Users'.",
        ),
    ),
    "behavior": ClassifierDefinition(
        "Copy the smallest exact phrase naming the behavior, state, or prohibition.",
        (
            "Return an exact case-sensitive substring, not a paraphrase.",
            "Keep meaning-bearing result modifiers in the behavior phrase.",
            "Examples: return 'pickle', 'export reports as CSV', or 'add retries "
            "to report exports'.",
        ),
    ),
    "precondition": ClassifierDefinition(
        "Copy the smallest exact phrase stating the behavior's precondition.",
        (
            "Return an exact case-sensitive substring and include the complete "
            "condition.",
            "Example: 'Every active report can be exported' returns 'active'.",
        ),
    ),
    "exception": ClassifierDefinition(
        "Copy the smallest exact phrase stating the exception.",
        (
            "Return an exact case-sensitive substring and include the exception "
            "boundary.",
            "Example: 'All reports except archived reports' returns 'except archived "
            "reports'.",
        ),
    ),
    "explicit_result": ClassifierDefinition(
        "Copy the smallest exact phrase stating the behavior's observable result.",
        (
            "Return an exact case-sensitive substring; do not invent an unstated "
            "success predicate.",
            "Example: 'The endpoint returns CSV' returns 'CSV'.",
        ),
    ),
}


def prepare_source_semantic_decisions(
    clause: Mapping[str, Any], *, created_at: str | None = None
) -> dict[str, Any]:
    """Prepare unresolved classifier work and bind safe lexical resolutions."""
    clause_id, text, source_fingerprint = _clause_fields(clause)
    timestamp = created_at or _created_at()
    resolved: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for kind in SOURCE_DECISION_KINDS:
        spec = _decision_spec(clause_id, text, source_fingerprint, kind)
        deterministic = resolve_deterministic_source_decision(kind, text)
        if deterministic is not None:
            decision = spec.bind(
                provider=SemanticDecisionProvider(kind="deterministic-rule"),
                provider_result={"status": "resolved", "value": deterministic.value},
                evidence_refs=(f"source-proposition:{clause_id}",),
                created_at=timestamp,
            )
            resolved.append(decision.to_data())
        else:
            pending.append(_classifier_request(spec, CLASSIFIER_DEFINITIONS[kind]))
    return {"resolved_decisions": resolved, "pending_specs": pending}


def bind_source_semantic_decisions(
    *,
    resolved_decisions: Sequence[Mapping[str, Any]],
    pending_specs: Sequence[Mapping[str, Any]],
    provider_results: Sequence[Mapping[str, Any]],
    created_at: str | None = None,
) -> list[SemanticDecision]:
    if len(pending_specs) != len(provider_results):
        raise SemanticContractError("semantic classifier result count is invalid")
    decisions = [SemanticDecision.from_data(item) for item in resolved_decisions]
    timestamp = created_at or _created_at()
    for request, result in zip(pending_specs, provider_results, strict=True):
        spec_raw = request.get("spec")
        if not isinstance(spec_raw, Mapping):
            raise SemanticContractError("semantic classifier request has no spec")
        spec = SemanticDecisionSpec.from_data(spec_raw)
        decisions.append(
            spec.bind(
                provider=SemanticDecisionProvider(kind="planning-llm"),
                provider_result=result,
                evidence_refs=(f"source-proposition:{spec.subject_ref}",),
                created_at=timestamp,
            )
        )
    return sorted(
        decisions, key=lambda item: SOURCE_DECISION_KINDS.index(item.decision_kind)
    )


def prepare_source_extractions(
    clause: Mapping[str, Any], decisions: Sequence[SemanticDecision]
) -> list[dict[str, Any]]:
    clause_id, text, source_fingerprint = _clause_fields(clause)
    values = {item.decision_kind: item.result.value for item in decisions}
    kinds = ["subject", "behavior"]
    for decision_kind, extraction_kind in (
        ("has_precondition", "precondition"),
        ("has_exception", "exception"),
        ("has_explicit_result", "explicit_result"),
    ):
        if values.get(decision_kind) == "present":
            kinds.append(extraction_kind)
    requests = []
    for kind in kinds:
        spec = SourceExtractionSpec(
            extraction_id=f"extraction:{clause_id}:{kind}",
            extraction_kind=kind,
            subject_ref=clause_id,
            proposition_text=text,
            source_fingerprint=source_fingerprint,
            contract_revision=f"{SOURCE_EXTRACTOR_REVISION}:{kind}",
        )
        definition = EXTRACTION_DEFINITIONS[kind]
        requests.append(
            {
                "spec": spec.to_data(),
                "question": definition.question,
                "instructions": list(definition.instructions),
            }
        )
    return requests


def bind_source_extractions(
    *,
    requests: Sequence[Mapping[str, Any]],
    provider_results: Sequence[Mapping[str, Any]],
    created_at: str | None = None,
) -> list[BoundSourceExtraction]:
    if len(requests) != len(provider_results):
        raise SemanticContractError("source extraction result count is invalid")
    timestamp = created_at or _created_at()
    result: list[BoundSourceExtraction] = []
    for request, provider_result in zip(requests, provider_results, strict=True):
        spec_raw = request.get("spec")
        if not isinstance(spec_raw, Mapping):
            raise SemanticContractError("source extraction request has no spec")
        spec = SourceExtractionSpec.from_data(spec_raw)
        result.append(
            spec.bind(
                provider=SemanticDecisionProvider(kind="planning-llm"),
                provider_result=provider_result,
                created_at=timestamp,
            )
        )
    return result


def prepare_behavior_family_decision(
    clause: Mapping[str, Any], behavior: BoundSourceExtraction
) -> dict[str, Any]:
    clause_id, text, source_fingerprint = _clause_fields(clause)
    if behavior.subject_ref != clause_id or behavior.extraction_kind != "behavior":
        raise SemanticContractError("behavior extraction does not match the clause")
    spec = SemanticDecisionSpec(
        decision_id=f"decision:{clause_id}:behavior_family",
        decision_kind="behavior_family",
        subject_ref=clause_id,
        proposition_text=text,
        source_fingerprint=source_fingerprint,
        candidate_set_fingerprint=behavior.input_fingerprint,
        contract_revision=f"{SOURCE_CLASSIFIER_REVISION}:behavior_family",
    )
    request = _classifier_request(spec, CLASSIFIER_DEFINITIONS["behavior_family"])
    request["subject_text"] = behavior.span.text
    return request


def bind_behavior_family_decision(
    request: Mapping[str, Any],
    provider_result: Mapping[str, Any],
    *,
    created_at: str | None = None,
) -> SemanticDecision:
    spec_raw = request.get("spec")
    if not isinstance(spec_raw, Mapping):
        raise SemanticContractError("behavior-family request has no spec")
    spec = SemanticDecisionSpec.from_data(spec_raw)
    return spec.bind(
        provider=SemanticDecisionProvider(kind="planning-llm"),
        provider_result=provider_result,
        evidence_refs=(f"source-proposition:{spec.subject_ref}",),
        created_at=created_at or _created_at(),
    )


def compile_source_contract(
    *,
    clause: Mapping[str, Any],
    decisions: Sequence[SemanticDecision],
    extractions: Sequence[BoundSourceExtraction],
    behavior_family: SemanticDecision,
) -> PartialSemanticContract:
    clause_id, text, source_fingerprint = _clause_fields(clause)
    return compile_partial_semantic_contract(
        source_ref=clause_id,
        source_fingerprint=source_fingerprint,
        proposition_text=text,
        decisions=(*decisions, behavior_family),
        extractions=extractions,
    )


def project_partial_contract_to_legacy_design(
    contract: PartialSemanticContract,
) -> dict[str, str]:
    """Render a deterministic, disposable view for legacy consumers."""
    result_text = _render_result(contract)
    return {
        "kind": contract.disposition,
        "description": _render_description(contract),
        "acceptance_criterion": _render_acceptance(contract),
        "expected_test": _render_expected_test(contract),
        "population": _render_population(contract),
        "operation": _render_operation(contract),
        "oracle": result_text,
        "evidence_case": _render_evidence_case(contract),
    }


def prepare_field_entailment_reviews(
    contract: PartialSemanticContract,
) -> list[dict[str, Any]]:
    return prepare_field_reviews(contract)


def bind_field_entailment_reviews(
    *,
    requests: Sequence[Mapping[str, Any]],
    provider_results: Sequence[Mapping[str, Any]],
    created_at: str | None = None,
) -> list[FieldEntailmentReview]:
    return bind_field_reviews(
        requests=requests,
        provider_results=provider_results,
        created_at=created_at or _created_at(),
    )


def finalize_source_faithfulness(
    contract: PartialSemanticContract,
    reviews: Sequence[FieldEntailmentReview],
) -> dict[str, Any]:
    return finalize_faithfulness(contract, reviews).to_data()


def _render_description(contract: PartialSemanticContract) -> str:
    return f"{contract.subject.span.text} {contract.behavior.span.text}."


def _render_acceptance(contract: PartialSemanticContract) -> str:
    if contract.explicit_result is not None:
        return f"The operation produces {contract.explicit_result.span.text}."
    return "The requested behavior is observed for the resolved population."


def _render_expected_test(contract: PartialSemanticContract) -> str:
    return f"Test {contract.behavior.span.text} for {contract.subject.span.text}."


def _render_population(contract: PartialSemanticContract) -> str:
    return f"{contract.quantifier} {contract.subject.span.text}"


def _render_operation(contract: PartialSemanticContract) -> str:
    return f"{contract.behavior_family}: {contract.behavior.span.text}"


def _render_result(contract: PartialSemanticContract) -> str:
    if contract.explicit_result is not None:
        return contract.explicit_result.span.text
    return "the requested behavior is observed"


def _render_evidence_case(contract: PartialSemanticContract) -> str:
    return f"Source {contract.source_ref}: {contract.proposition_text}"


def _classifier_request(
    spec: SemanticDecisionSpec, definition: ClassifierDefinition
) -> dict[str, Any]:
    return {
        "spec": spec.to_data(),
        "question": definition.question,
        "instructions": list(definition.instructions),
        "allowed_values": sorted(DECISION_VALUES[spec.decision_kind]),
        "subject_text": spec.proposition_text,
    }


def _decision_spec(
    clause_id: str, text: str, source_fingerprint: str, kind: str
) -> SemanticDecisionSpec:
    return SemanticDecisionSpec(
        decision_id=f"decision:{clause_id}:{kind}",
        decision_kind=kind,
        subject_ref=clause_id,
        proposition_text=text,
        source_fingerprint=source_fingerprint,
        contract_revision=f"{SOURCE_CLASSIFIER_REVISION}:{kind}",
    )


def _clause_fields(clause: Mapping[str, Any]) -> tuple[str, str, str]:
    clause_id = clause.get("clause_id")
    text = clause.get("text")
    source_fingerprint = clause.get("fingerprint")
    if not isinstance(clause_id, str) or not clause_id.strip():
        raise SemanticContractError("instruction clause has no ID")
    if not isinstance(text, str) or not text.strip():
        raise SemanticContractError("instruction clause has no proposition text")
    if not isinstance(source_fingerprint, str) or not source_fingerprint.strip():
        raise SemanticContractError("instruction clause has no source fingerprint")
    return clause_id, text, source_fingerprint


def _created_at() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "CLASSIFIER_DEFINITIONS",
    "EXTRACTION_DEFINITIONS",
    "SOURCE_CLASSIFIER_REVISION",
    "SOURCE_DECISION_KINDS",
    "SOURCE_EXTRACTOR_REVISION",
    "bind_behavior_family_decision",
    "bind_field_entailment_reviews",
    "bind_source_extractions",
    "bind_source_semantic_decisions",
    "compile_source_contract",
    "prepare_behavior_family_decision",
    "prepare_field_entailment_reviews",
    "prepare_source_extractions",
    "prepare_source_semantic_decisions",
    "project_partial_contract_to_legacy_design",
    "finalize_source_faithfulness",
]
