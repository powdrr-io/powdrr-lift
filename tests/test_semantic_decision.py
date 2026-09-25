from __future__ import annotations

import pytest

from powdrr_lift.core.semantic_decision import (
    DECISION_VALUES,
    ExactSourceSpan,
    SemanticDecision,
    SemanticDecisionError,
    SemanticDecisionProvider,
    SemanticDecisionSpec,
    resolve_exact_source_span,
)
from powdrr_lift.workrr.semantic_classifier import (
    resolve_deterministic_source_decision,
)


def _spec(
    *,
    kind: str = "quantifier",
    text: str = "All data should pickle.",
) -> SemanticDecisionSpec:
    return SemanticDecisionSpec(
        decision_id=f"decision:instruction-001:{kind}",
        decision_kind=kind,
        subject_ref="instruction-001",
        proposition_text=text,
        source_fingerprint="sha256:source",
        contract_revision=f"{kind}-v1",
    )


def test_decision_spec_binds_only_provider_result_fields() -> None:
    spec = _spec()
    assert SemanticDecisionSpec.from_data(spec.to_data()) == spec
    decision = spec.bind(
        provider=SemanticDecisionProvider(
            kind="local-classifier", name="quantifier", model_revision="v1"
        ),
        provider_result={"status": "resolved", "value": "every"},
        evidence_refs=("source-span:instruction-001:0-3",),
        created_at="2026-09-25T00:00:00Z",
    )

    decision.validate_current(spec)
    restored = SemanticDecision.from_data(decision.to_data())

    assert restored == decision
    assert restored.result.value == "every"
    assert restored.decision_id == "decision:instruction-001:quantifier"


def test_decision_binding_rejects_provider_owned_identity() -> None:
    with pytest.raises(SemanticDecisionError, match="compiler-owned fields"):
        _spec().bind(
            provider=SemanticDecisionProvider(kind="planning-llm"),
            provider_result={
                "status": "resolved",
                "value": "every",
                "decision_id": "invented",
            },
            evidence_refs=("source:instruction-001",),
            created_at="2026-09-25T00:00:00Z",
        )


def test_decision_binding_validates_closed_value_vocabularies() -> None:
    assert "unresolved" not in DECISION_VALUES["quantifier"]

    with pytest.raises(SemanticDecisionError, match="not allowed"):
        _spec().bind(
            provider=SemanticDecisionProvider(kind="planning-llm"),
            provider_result={"status": "resolved", "value": "most"},
            evidence_refs=("source:instruction-001",),
            created_at="2026-09-25T00:00:00Z",
        )


def test_unresolved_result_requires_a_closed_reason_and_no_value() -> None:
    decision = _spec().bind(
        provider=SemanticDecisionProvider(kind="planning-llm"),
        provider_result={
            "status": "unresolved",
            "reason_code": "source_ambiguous",
        },
        evidence_refs=("source:instruction-001",),
        created_at="2026-09-25T00:00:00Z",
    )

    assert decision.result.value is None
    assert decision.result.reason_code == "source_ambiguous"

    with pytest.raises(SemanticDecisionError, match="cannot have a value"):
        _spec().bind(
            provider=SemanticDecisionProvider(kind="planning-llm"),
            provider_result={
                "status": "unresolved",
                "value": "every",
                "reason_code": "source_ambiguous",
            },
            evidence_refs=("source:instruction-001",),
            created_at="2026-09-25T00:00:00Z",
        )


def test_input_fingerprint_invalidates_a_changed_source_or_contract() -> None:
    decision = _spec().bind(
        provider=SemanticDecisionProvider(kind="deterministic-rule"),
        provider_result={"status": "resolved", "value": "every"},
        evidence_refs=("source:instruction-001",),
        created_at="2026-09-25T00:00:00Z",
    )

    with pytest.raises(SemanticDecisionError, match="input fingerprint is stale"):
        decision.validate_current(
            SemanticDecisionSpec(
                decision_id=decision.decision_id,
                decision_kind="quantifier",
                subject_ref="instruction-001",
                proposition_text="Every data entity should pickle.",
                source_fingerprint="sha256:changed",
                contract_revision="quantifier-v1",
            )
        )


def test_exact_source_span_is_case_sensitive_and_occurrence_bounded() -> None:
    span = resolve_exact_source_span(
        source_ref="instruction-001",
        source="data then data",
        quote="data",
        occurrence=2,
    )

    assert (span.start, span.end, span.text) == (10, 14, "data")
    assert span.evidence_ref == "source-span:instruction-001:10-14"
    assert ExactSourceSpan.from_data(span.to_data()) == span

    with pytest.raises(SemanticDecisionError, match="occurrence is required"):
        resolve_exact_source_span(
            source_ref="instruction-001", source="data then data", quote="data"
        )
    with pytest.raises(SemanticDecisionError, match="exact source substring"):
        resolve_exact_source_span(
            source_ref="instruction-001", source="Data", quote="data"
        )


def test_decisions_require_evidence_and_serialized_artifacts_are_strict() -> None:
    with pytest.raises(SemanticDecisionError, match="requires non-empty evidence"):
        _spec().bind(
            provider=SemanticDecisionProvider(kind="planning-llm"),
            provider_result={"status": "resolved", "value": "every"},
            evidence_refs=(),
            created_at="2026-09-25T00:00:00Z",
        )

    malformed = _spec().to_data() | {"provider_instruction": "trust me"}
    with pytest.raises(SemanticDecisionError, match="fields are invalid"):
        SemanticDecisionSpec.from_data(malformed)


def test_modifier_presence_decisions_are_independent() -> None:
    expected = {"present", "absent"}

    assert DECISION_VALUES["has_precondition"] == expected
    assert DECISION_VALUES["has_exception"] == expected
    assert DECISION_VALUES["has_explicit_result"] == expected
    assert "modifier_presence" not in DECISION_VALUES


@pytest.mark.parametrize(
    ("kind", "source", "expected"),
    [
        ("polarity", "Do not add retries.", "prohibited"),
        ("polarity", "Clients may omit the field.", "permitted"),
        ("polarity", "All data should pickle.", "required"),
        ("quantifier", "Every active report can be exported.", "every"),
        ("quantifier", "Some reports can be exported.", "some"),
        ("requirement_strength", "All data should pickle.", "should"),
        ("requirement_strength", "Responses must have IDs.", "must"),
        ("requirement_strength", "Clients may omit the field.", "may"),
    ],
)
def test_deterministic_rules_resolve_only_explicit_lexical_cases(
    kind: str, source: str, expected: str
) -> None:
    result = resolve_deterministic_source_decision(kind, source)

    assert result is not None
    assert result.value == expected
    assert result.evidence in source.casefold()


def test_deterministic_rules_fall_through_instead_of_guessing() -> None:
    assert (
        resolve_deterministic_source_decision("quantifier", "Reports support export.")
        is None
    )
    assert (
        resolve_deterministic_source_decision("disposition", "Open a pull request.")
        is None
    )
