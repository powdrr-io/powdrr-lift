from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from powdrr_lift.core.semantic_contract import (
    BoundSourceExtraction,
    SemanticContractError,
)
from powdrr_lift.core.semantic_decision import (
    DECISION_VALUES,
    SEMANTIC_DIMENSION_DECISION_KINDS,
    SemanticDecision,
)
from powdrr_lift.core.semantic_faithfulness import (
    FaithfulnessError,
    bind_field_entailment_reviews,
    finalize_source_faithfulness,
    prepare_field_entailment_reviews,
)
from powdrr_lift.workrr.command_catalog import (
    FeatureCommandRuntime,
    feature_command_catalog,
)
from powdrr_lift.workrr.semantic_contract_compiler import (
    CLASSIFIER_DEFINITIONS,
    SEMANTIC_DIMENSION_DEFINITIONS,
    applicable_source_semantic_dimensions,
    bind_behavior_family_decision,
    bind_source_extractions,
    bind_source_semantic_decisions,
    compile_deterministic_source_extractions,
    compile_source_contract,
    prepare_behavior_family_decision,
    prepare_dependent_source_semantic_decisions,
    prepare_source_extractions,
    prepare_source_semantic_decisions,
    project_partial_contract_to_legacy_design,
)

NOW = "2026-09-25T00:00:00Z"


def test_every_source_classifier_has_a_question_and_decision_rules() -> None:
    assert set(CLASSIFIER_DEFINITIONS) == {
        "routing",
        "disposition",
        "polarity",
        "quantifier",
        "requirement_strength",
        "behavior_family",
        "has_precondition",
        "has_exception",
        "has_explicit_result",
        "temporal_scope",
        "source_predicate",
        "nonactionable_exclusion_safety",
    }
    for kind, definition in CLASSIFIER_DEFINITIONS.items():
        assert definition.question, kind
        assert definition.instructions, kind
        assert DECISION_VALUES[kind]
    assert set(SEMANTIC_DIMENSION_DEFINITIONS) == set(SEMANTIC_DIMENSION_DECISION_KINDS)
    assert all(
        definition.examples for definition in SEMANTIC_DIMENSION_DEFINITIONS.values()
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Deeply duplicate the nested settings.", "copy_depth"),
        (
            "Changes made through the handle are reflected in storage.",
            "mutation_propagation",
        ),
        ("Each call returns a distinct instance.", "object_identity"),
        ("The value survives until the next transition.", "persistence_boundary"),
        ("The factory is used when the argument is omitted.", "argument_presence"),
    ],
)
def test_dimension_screen_covers_generic_paraphrases(
    source: str, expected: str
) -> None:
    assert expected in applicable_source_semantic_dimensions(source)


def test_dimension_screen_skips_ordinary_clauses() -> None:
    assert (
        applicable_source_semantic_dimensions("The endpoint supports report export.")
        == ()
    )


def test_dimension_classifiers_are_requested_only_for_relevant_clauses() -> None:
    clause = _clause("Create a fresh copy of the defaults.")
    root_plan = prepare_source_semantic_decisions(clause, created_at=NOW)
    root = bind_source_semantic_decisions(
        resolved_decisions=root_plan["resolved_decisions"],
        pending_specs=root_plan["pending_specs"],
        provider_results=[{"status": "resolved", "value": "include"}],
        created_at=NOW,
    )
    plan = prepare_dependent_source_semantic_decisions(clause, root, created_at=NOW)

    dimension_requests = [
        item
        for item in plan["pending_specs"]
        if item["spec"]["decision_kind"] in SEMANTIC_DIMENSION_DECISION_KINDS
    ]
    assert [item["spec"]["decision_kind"] for item in dimension_requests] == [
        "copy_depth"
    ]
    assert dimension_requests[0]["subject_text"] == clause["text"]


def test_context_clause_does_not_activate_semantic_dimension_classifiers() -> None:
    clause = _clause("The current defaults are copied from the configuration.")
    root_plan = prepare_source_semantic_decisions(clause, created_at=NOW)
    root = bind_source_semantic_decisions(
        resolved_decisions=root_plan["resolved_decisions"],
        pending_specs=root_plan["pending_specs"],
        provider_results=[{"status": "resolved", "value": "context"}],
        created_at=NOW,
    )

    plan = prepare_dependent_source_semantic_decisions(clause, root, created_at=NOW)

    assert not any(
        item["spec"]["decision_kind"] in SEMANTIC_DIMENSION_DECISION_KINDS
        for item in plan["pending_specs"]
    )


def test_unspecified_source_dimension_survives_contract_compilation() -> None:
    clause = _clause("Create a fresh copy of the defaults.")
    decisions = _bind_source_decisions(
        clause, disposition="feature", overrides={"copy_depth": "unspecified"}
    )
    extractions = compile_deterministic_source_extractions(
        clause, decisions, clause["text"], created_at=NOW
    )
    behavior_request = prepare_behavior_family_decision(
        clause, next(item for item in extractions if item.extraction_kind == "behavior")
    )
    family = bind_behavior_family_decision(
        behavior_request,
        {"status": "resolved", "value": "create"},
        created_at=NOW,
    )
    contract = compile_source_contract(
        clause=clause,
        decisions=decisions,
        extractions=extractions,
        behavior_family=family,
    )

    data = contract.to_data()
    assert data["semantic_dimensions"] == {"copy_depth": "unspecified"}
    assert {item["field"] for item in data["unresolved"]} >= {
        "semantic_dimensions.copy_depth"
    }


def test_explicit_semantic_dimension_answers_survive_contract_compilation() -> None:
    clause = _clause(
        "Recursively copy settings; writes through the view update storage; "
        "each call gets a distinct mapping object; data persists until exit; "
        "reject when both arguments are supplied, even if one is null."
    )
    expected = {
        "copy_depth": "recursive",
        "mutation_propagation": "write_through",
        "object_identity": "distinct_objects",
        "persistence_boundary": "boundary_stated",
        "argument_presence": "argument_supplied",
    }
    decisions = _bind_source_decisions(
        clause, disposition="feature", overrides=expected
    )
    extractions = compile_deterministic_source_extractions(
        clause, decisions, clause["text"], created_at=NOW
    )
    behavior = next(item for item in extractions if item.extraction_kind == "behavior")
    family_request = prepare_behavior_family_decision(clause, behavior, decisions)
    family = bind_behavior_family_decision(
        family_request,
        {"status": "resolved", "value": "other"},
        created_at=NOW,
    )
    contract = compile_source_contract(
        clause=clause,
        decisions=decisions,
        extractions=extractions,
        behavior_family=family,
    )

    assert dict(contract.semantic_dimensions) == expected
    assert not any(
        item.field.startswith("semantic_dimensions.") for item in contract.unresolved
    )


def test_split_clause_classifier_receives_parent_sentence_as_context() -> None:
    source = "Callbacks receive merged ancestor data; the getter reads local data."
    clause = {
        **_clause("the getter reads local data."),
        "source_span": {"start": 0, "end": len(source)},
    }
    plan = prepare_source_semantic_decisions(clause, source_text=source, created_at=NOW)
    request = plan["pending_specs"][0]

    assert "Containing source sentence: " + source in request["subject_text"]
    assert (
        "Proposition to classify:\nthe getter reads local data."
        in request["subject_text"]
    )
    assert request["spec"]["context_text"] == source


def test_classifier_prompts_do_not_emit_task_specific_worked_examples() -> None:
    clause = _clause("Archived records retain their original field values.")
    root_request = prepare_source_semantic_decisions(clause, created_at=NOW)[
        "pending_specs"
    ][0]
    root = bind_source_semantic_decisions(
        resolved_decisions=[],
        pending_specs=[root_request],
        provider_results=[{"status": "resolved", "value": "include"}],
        created_at=NOW,
    )
    child_requests = prepare_dependent_source_semantic_decisions(
        clause, root, created_at=NOW
    )["pending_specs"]

    all_instructions = " ".join(
        " ".join(request["instructions"]) for request in [root_request, *child_requests]
    )

    assert "Worked examples" not in all_instructions
    assert "DataVar" not in all_instructions
    assert "state data" not in all_instructions
    assert any(
        "Use unspecified when the proposition does not explicitly state how many"
        in rule
        for rule in CLASSIFIER_DEFINITIONS["quantifier"].instructions
    )
    result_rules = next(
        request["instructions"]
        for request in child_requests
        if request["spec"]["decision_kind"] == "has_explicit_result"
    )
    assert any("state change" in rule for rule in result_rules)
    assert any("observable" in rule for rule in result_rules)
    exception_rules = next(
        request["instructions"]
        for request in child_requests
        if request["spec"]["decision_kind"] == "has_exception"
    )
    assert any("negative contrast" in rule for rule in exception_rules)


@pytest.mark.parametrize(
    "provider_result",
    [
        {"status": "unresolved", "value": None, "reason_code": "no_candidate"},
        {"status": "resolved", "value": "context", "reason_code": None},
    ],
)
def test_included_clause_with_missing_product_kind_falls_back_to_invariant(
    provider_result: Mapping[str, Any],
) -> None:
    clause = _clause("Data survives pickle.")
    root_plan = prepare_source_semantic_decisions(clause, created_at=NOW)
    root = bind_source_semantic_decisions(
        resolved_decisions=root_plan["resolved_decisions"],
        pending_specs=root_plan["pending_specs"],
        provider_results=[{"status": "resolved", "value": "include"}],
        created_at=NOW,
    )
    plan = prepare_dependent_source_semantic_decisions(clause, root, created_at=NOW)
    disposition_request = next(
        request
        for request in plan["pending_specs"]
        if request["spec"]["decision_kind"] == "disposition"
    )

    decisions = bind_source_semantic_decisions(
        resolved_decisions=plan["resolved_decisions"],
        pending_specs=[disposition_request],
        provider_results=[provider_result],
        created_at=NOW,
    )
    disposition = next(
        item for item in decisions if item.decision_kind == "disposition"
    )

    assert disposition.result.value == "invariant"
    assert disposition.provider.kind == "deterministic-rule"
    assert (
        "fallback:include-without-product-kind:invariant" in disposition.evidence_refs
    )


def test_context_route_takes_context_branch_before_child_classifiers() -> None:
    clause = _clause("The library currently has no per-instance data ownership.")
    root_plan = prepare_source_semantic_decisions(clause, created_at=NOW)
    assert root_plan["pending_specs"][0]["spec"]["decision_kind"] == "routing"
    root = bind_source_semantic_decisions(
        resolved_decisions=[],
        pending_specs=root_plan["pending_specs"],
        provider_results=[{"status": "resolved", "value": "context"}],
        created_at=NOW,
    )
    child_plan = prepare_dependent_source_semantic_decisions(
        clause, root, created_at=NOW
    )
    assert child_plan["pending_specs"] == []
    child_values = {
        item["decision_kind"]: item["result"]["value"]
        for item in child_plan["resolved_decisions"]
    }
    assert child_values["polarity"] == "descriptive"
    assert child_values["requirement_strength"] == "descriptive"
    assert child_values["source_predicate"] == "not_stated"
    assert child_values["disposition"] == "context"
    assert child_values["routing"] == "context"


def test_process_instruction_remains_excluded_from_product_routing() -> None:
    clause = _clause("Run the unit tests before submitting.")
    root_plan = prepare_source_semantic_decisions(clause, created_at=NOW)
    root = bind_source_semantic_decisions(
        resolved_decisions=[],
        pending_specs=root_plan["pending_specs"],
        provider_results=[{"status": "resolved", "value": "exclude"}],
        created_at=NOW,
    )

    child_plan = prepare_dependent_source_semantic_decisions(
        clause, root, created_at=NOW
    )

    assert child_plan["pending_specs"] == []
    child_values = {
        item["decision_kind"]: item["result"]["value"]
        for item in child_plan["resolved_decisions"]
    }
    assert child_values["routing"] == "exclude"
    assert child_values["disposition"] == "context"


def test_normative_defaults_resolve_uncertain_optional_source_modifiers() -> None:
    clause = _clause("A report preserves its original fields.")
    root_plan = prepare_source_semantic_decisions(clause, created_at=NOW)
    root = bind_source_semantic_decisions(
        resolved_decisions=[],
        pending_specs=root_plan["pending_specs"],
        provider_results=[{"status": "resolved", "value": "include"}],
        created_at=NOW,
    )
    child_plan = prepare_dependent_source_semantic_decisions(
        clause, root, created_at=NOW
    )
    unresolved_values = {
        "disposition": "feature",
        "has_precondition": None,
        "has_exception": None,
        "has_explicit_result": None,
        "source_predicate": None,
        "nonactionable_exclusion_safety": None,
    }
    results = [
        {
            "status": "unresolved" if value is None else "resolved",
            "value": value,
            "reason_code": "source_underspecified" if value is None else None,
        }
        for value in (
            unresolved_values[request["spec"]["decision_kind"]]
            for request in child_plan["pending_specs"]
        )
    ]

    decisions = bind_source_semantic_decisions(
        resolved_decisions=child_plan["resolved_decisions"],
        pending_specs=child_plan["pending_specs"],
        provider_results=results,
        benchmark_mode=True,
        created_at=NOW,
    )
    by_kind = {item.decision_kind: item for item in decisions}

    assert by_kind["has_precondition"].result.value == "absent"
    assert by_kind["has_exception"].result.value == "absent"
    assert by_kind["has_explicit_result"].result.value == "absent"
    assert by_kind["source_predicate"].result.value == "explicit"
    assert (
        by_kind["nonactionable_exclusion_safety"].result.value
        == "product_semantics_present"
    )
    for kind in (
        "has_precondition",
        "has_exception",
        "has_explicit_result",
        "source_predicate",
    ):
        assert by_kind[kind].provider.kind == "deterministic-rule"
        assert f"normative-default:{kind}:" in " ".join(by_kind[kind].evidence_refs)
    assert (
        by_kind["nonactionable_exclusion_safety"].provider.kind == "deterministic-rule"
    )


def test_normative_defaults_compile_unresolved_include_as_invariant() -> None:
    clause = _clause(
        "Implement the requested operation while preserving the caller's data."
    )
    root_plan = prepare_source_semantic_decisions(clause, created_at=NOW)
    root = bind_source_semantic_decisions(
        resolved_decisions=root_plan["resolved_decisions"],
        pending_specs=root_plan["pending_specs"],
        provider_results=[
            {"status": "unresolved", "value": None, "reason_code": "no_candidate"}
        ],
        benchmark_mode=True,
        created_at=NOW,
    )
    assert root[0].result.value == "include"
    assert root[0].provider.kind == "deterministic-rule"

    decision_plan = prepare_dependent_source_semantic_decisions(
        clause, root, created_at=NOW
    )
    decisions = bind_source_semantic_decisions(
        resolved_decisions=decision_plan["resolved_decisions"],
        pending_specs=decision_plan["pending_specs"],
        provider_results=[
            {"status": "unresolved", "value": None, "reason_code": "no_candidate"}
            for _ in decision_plan["pending_specs"]
        ],
        benchmark_mode=True,
        created_at=NOW,
    )
    by_kind = {item.decision_kind: item for item in decisions}
    assert by_kind["disposition"].result.value == "invariant"
    assert by_kind["disposition"].provider.kind == "deterministic-rule"
    assert (
        "normative-default:disposition:invariant"
        in by_kind["disposition"].evidence_refs
    )
    assert by_kind["source_predicate"].result.value == "explicit"
    assert all(item.result.status == "resolved" for item in decisions)

    disposition_request = next(
        request
        for request in decision_plan["pending_specs"]
        if request["spec"]["decision_kind"] == "disposition"
    )
    conflicting_kind = bind_source_semantic_decisions(
        resolved_decisions=decision_plan["resolved_decisions"],
        pending_specs=[disposition_request],
        provider_results=[
            {"status": "resolved", "value": "context", "reason_code": None}
        ],
        benchmark_mode=True,
        created_at=NOW,
    )
    assert (
        next(
            item for item in conflicting_kind if item.decision_kind == "disposition"
        ).result.value
        == "invariant"
    )

    extraction_requests = prepare_source_extractions(clause, decisions)
    extractions = bind_source_extractions(
        requests=extraction_requests,
        provider_results=[{} for _ in extraction_requests],
        benchmark_mode=True,
        created_at=NOW,
    )
    family_request = prepare_behavior_family_decision(
        clause,
        next(item for item in extractions if item.extraction_kind == "behavior"),
        decisions,
    )
    family = bind_behavior_family_decision(
        family_request,
        {"status": "unresolved", "value": None, "reason_code": "no_candidate"},
        benchmark_mode=True,
        created_at=NOW,
    )
    contract = compile_source_contract(
        clause=clause,
        decisions=decisions,
        extractions=extractions,
        behavior_family=family,
    )

    assert family.result.value == "other"
    assert all(item.span.text == clause["text"] for item in extractions)
    assert all(item.provider.kind == "deterministic-rule" for item in extractions)
    assert contract.disposition == "invariant"
    design = project_partial_contract_to_legacy_design(contract)
    assert design["evidence_case"] == f"Source instruction-001: {clause['text']}"

    review_requests = prepare_field_entailment_reviews(contract)
    reviews = bind_field_entailment_reviews(
        requests=review_requests,
        provider_results=[
            {"status": "unresolved", "value": None, "reason_code": "no_candidate"}
            for _ in review_requests
        ],
        benchmark_mode=True,
        created_at=NOW,
    )
    assert finalize_source_faithfulness(contract, reviews).accepted


def test_nonactionable_clause_takes_process_only_branch() -> None:
    clause = _clause("Create a new branch and commit everything when done.")
    root_plan = prepare_source_semantic_decisions(clause, created_at=NOW)
    root = bind_source_semantic_decisions(
        resolved_decisions=[],
        pending_specs=root_plan["pending_specs"],
        provider_results=[{"status": "resolved", "value": "exclude"}],
        created_at=NOW,
    )

    child_plan = prepare_dependent_source_semantic_decisions(
        clause, root, created_at=NOW
    )

    assert child_plan["pending_specs"] == []
    child_values = {
        item["decision_kind"]: item["result"]["value"]
        for item in child_plan["resolved_decisions"]
    }
    assert child_values["polarity"] == "descriptive"
    assert child_values["disposition"] == "context"


def test_nonactionable_behavior_family_uses_deterministic_placeholder() -> None:
    clause = _clause("Create a new branch and commit everything when done.")
    decisions = _bind_source_decisions(
        clause,
        disposition="nonactionable",
        overrides={"nonactionable_exclusion_safety": "process_only"},
    )
    extraction_requests = prepare_source_extractions(clause, decisions)
    extractions = bind_source_extractions(
        requests=extraction_requests,
        provider_results=[{"quote": "branch"}, {"quote": "commit everything"}],
        created_at=NOW,
    )

    request = prepare_behavior_family_decision(clause, extractions[1], decisions)
    family = bind_behavior_family_decision(
        request,
        {"status": "unresolved", "value": None, "reason_code": "no_candidate"},
        created_at=NOW,
    )

    assert family.result.status == "resolved"
    assert family.result.value == "other"
    assert family.provider.kind == "deterministic-rule"


def _clause(text: str = "All data should pickle.") -> dict[str, Any]:
    return {
        "clause_id": "instruction-001",
        "text": text,
        "source_span": {"start": 0, "end": len(text)},
        "fingerprint": "sha256:clause",
    }


def _source_result(kind: str, *, disposition: str = "invariant") -> str:
    if kind in SEMANTIC_DIMENSION_DECISION_KINDS:
        return "unspecified"
    return {
        "routing": "include",
        "disposition": disposition,
        "polarity": "required",
        "quantifier": "every",
        "requirement_strength": "should",
        "has_precondition": "absent",
        "has_exception": "absent",
        "has_explicit_result": "absent",
        "temporal_scope": "unspecified",
        "source_predicate": "not_stated",
        "nonactionable_exclusion_safety": "product_semantics_present",
    }[kind]


def _bind_source_decisions(
    clause: Mapping[str, Any],
    *,
    disposition: str = "invariant",
    overrides: Mapping[str, str] | None = None,
) -> list[SemanticDecision]:
    root_plan = prepare_source_semantic_decisions(clause, created_at=NOW)
    root = bind_source_semantic_decisions(
        resolved_decisions=root_plan["resolved_decisions"],
        pending_specs=root_plan["pending_specs"],
        provider_results=[
            {
                "status": "resolved",
                "value": (
                    "exclude"
                    if disposition in {"context", "nonactionable"}
                    else "include_prohibition"
                    if disposition == "non_goal"
                    else "include"
                ),
            }
        ],
        created_at=NOW,
    )
    plan = prepare_dependent_source_semantic_decisions(clause, root, created_at=NOW)
    overrides = overrides or {}
    results = [
        {
            "status": "resolved",
            "value": overrides.get(kind, _source_result(kind, disposition=disposition)),
        }
        for kind in (
            request["spec"]["decision_kind"] for request in plan["pending_specs"]
        )
    ]
    return bind_source_semantic_decisions(
        resolved_decisions=plan["resolved_decisions"],
        pending_specs=plan["pending_specs"],
        provider_results=results,
        created_at=NOW,
    )


def test_pipeline_compiles_source_anchored_partial_contract() -> None:
    clause = _clause()
    plan = prepare_source_semantic_decisions(clause, created_at=NOW)

    assert plan["resolved_decisions"] == []
    assert [item["spec"]["decision_kind"] for item in plan["pending_specs"]] == [
        "routing"
    ]
    decisions = _bind_source_decisions(clause)
    extraction_requests = prepare_source_extractions(clause, decisions)
    assert [item["spec"]["extraction_kind"] for item in extraction_requests] == [
        "subject",
        "behavior",
    ]
    extractions = bind_source_extractions(
        requests=extraction_requests,
        provider_results=[{"quote": "data"}, {"quote": "pickle"}],
        created_at=NOW,
    )
    behavior_request = prepare_behavior_family_decision(clause, extractions[1])
    behavior_family = bind_behavior_family_decision(
        behavior_request,
        {"status": "resolved", "value": "serialize"},
        created_at=NOW,
    )
    contract = compile_source_contract(
        clause=clause,
        decisions=decisions,
        extractions=extractions,
        behavior_family=behavior_family,
    )

    data = contract.to_data()
    assert data["schema_version"] == "partial-semantic-contract-v3"
    assert data["routing"] == "include"
    assert data["disposition"] == "invariant"
    assert data["quantifier"] == "every"
    assert data["subject"]["source_text"] == "data"
    assert data["behavior"]["source_text"] == "pickle"
    assert data["behavior"]["family"] == "serialize"
    assert data["predicate"]["source_classification"] == "not_stated"
    assert {item["field"] for item in data["unresolved"]} == {
        "subject.binding_refs",
        "behavior.ontology_ref",
        "predicate",
    }
    assert "description" not in data
    assert "acceptance_criterion" not in data


def test_explicit_behavior_falls_back_to_other_when_no_family_fits() -> None:
    clause = _clause("The operation exposes a result mapping.")
    decisions = _bind_source_decisions(
        clause,
        disposition="feature",
        overrides={"source_predicate": "explicit"},
    )
    extraction_requests = prepare_source_extractions(clause, decisions)
    quotes = {
        "subject": "operation",
        "behavior": "exposes a result mapping",
        "precondition": "operation",
        "exception": "operation",
        "explicit_result": "a result mapping",
    }
    extractions = bind_source_extractions(
        requests=extraction_requests,
        provider_results=[
            {"quote": quotes[request["spec"]["extraction_kind"]]}
            for request in extraction_requests
        ],
        created_at=NOW,
    )

    request = prepare_behavior_family_decision(clause, extractions[1], decisions)
    family = bind_behavior_family_decision(
        request,
        {"status": "unresolved", "value": None, "reason_code": "source_ambiguous"},
        created_at=NOW,
    )

    assert request["fallback_to_other_if_unresolved"] is True
    assert family.result.status == "resolved"
    assert family.result.value == "other"
    assert family.provider.kind == "deterministic-rule"


def test_legacy_projection_copies_source_instead_of_paraphrasing() -> None:
    clause = _clause()
    decisions = _bind_source_decisions(clause)
    requests = prepare_source_extractions(clause, decisions)
    extractions = bind_source_extractions(
        requests=requests,
        provider_results=[{"quote": "data"}, {"quote": "pickle"}],
        created_at=NOW,
    )
    family_request = prepare_behavior_family_decision(clause, extractions[1])
    family = bind_behavior_family_decision(
        family_request,
        {"status": "resolved", "value": "serialize"},
        created_at=NOW,
    )
    contract = compile_source_contract(
        clause=clause,
        decisions=decisions,
        extractions=extractions,
        behavior_family=family,
    )

    projection = project_partial_contract_to_legacy_design(contract)

    assert projection == {
        "kind": "invariant",
        "description": "data pickle.",
        "acceptance_criterion": (
            "The requested behavior is observed for the resolved population."
        ),
        "expected_test": "Test pickle for data.",
        "population": "every data",
        "operation": "serialize: pickle",
        "oracle": "the requested behavior is observed",
        "evidence_case": "Source instruction-001: All data should pickle.",
    }


def test_field_faithfulness_rejects_invented_candidate() -> None:
    clause = _clause()
    decisions = _bind_source_decisions(clause)
    extractions = bind_source_extractions(
        requests=prepare_source_extractions(clause, decisions),
        provider_results=[{"quote": "data"}, {"quote": "pickle"}],
        created_at=NOW,
    )
    family_request = prepare_behavior_family_decision(clause, extractions[1])
    contract = compile_source_contract(
        clause=clause,
        decisions=decisions,
        extractions=extractions,
        behavior_family=bind_behavior_family_decision(
            family_request, {"status": "resolved", "value": "serialize"}, created_at=NOW
        ),
    )
    requests = prepare_field_entailment_reviews(contract)
    family_review = next(
        item for item in requests if item["spec"]["field"] == "behavior_family"
    )
    assert any(
        "semantic action-family label" in instruction
        for instruction in family_review["instructions"]
    )
    results = [
        {"status": "resolved", "value": "contradicted", "reason_code": None}
        if request["spec"]["field"] == "behavior"
        else {"status": "resolved", "value": "entailed", "reason_code": None}
        for request in requests
    ]
    reviews = bind_field_entailment_reviews(
        requests=requests, provider_results=results, created_at=NOW
    )
    outcome = finalize_source_faithfulness(contract, reviews)
    assert not outcome.accepted
    assert {item["reason_code"] for item in outcome.findings} == {
        "intent_contradiction"
    }


def test_field_faithfulness_requires_reviews_and_routes_not_stated() -> None:
    clause = _clause()
    decisions = _bind_source_decisions(clause)
    extractions = bind_source_extractions(
        requests=prepare_source_extractions(clause, decisions),
        provider_results=[{"quote": "data"}, {"quote": "pickle"}],
        created_at=NOW,
    )
    family_request = prepare_behavior_family_decision(clause, extractions[1])
    contract = compile_source_contract(
        clause=clause,
        decisions=decisions,
        extractions=extractions,
        behavior_family=bind_behavior_family_decision(
            family_request, {"status": "resolved", "value": "serialize"}, created_at=NOW
        ),
    )
    requests = prepare_field_entailment_reviews(contract)
    with pytest.raises(FaithfulnessError, match="incomplete"):
        finalize_source_faithfulness(contract, [])
    reviews = bind_field_entailment_reviews(
        requests=requests,
        provider_results=[
            {"status": "resolved", "value": "not_stated", "reason_code": None}
            if request["spec"]["field"] == "behavior_family"
            else {"status": "resolved", "value": "entailed", "reason_code": None}
            for request in requests
        ],
        created_at=NOW,
    )
    outcome = finalize_source_faithfulness(contract, reviews)
    assert outcome.accepted
    assert "behavior_family" in outcome.unresolved_fields
    assert outcome.findings == ()


def test_benchmark_field_review_defaults_resolved_result_with_reason_code() -> None:
    clause = _clause()
    decisions = _bind_source_decisions(clause)
    extractions = bind_source_extractions(
        requests=prepare_source_extractions(clause, decisions),
        provider_results=[{"quote": "data"}, {"quote": "pickle"}],
        created_at=NOW,
    )
    family_request = prepare_behavior_family_decision(clause, extractions[1])
    contract = compile_source_contract(
        clause=clause,
        decisions=decisions,
        extractions=extractions,
        behavior_family=bind_behavior_family_decision(
            family_request, {"status": "resolved", "value": "serialize"}, created_at=NOW
        ),
    )
    requests = prepare_field_entailment_reviews(contract)
    reviews = bind_field_entailment_reviews(
        requests=requests,
        provider_results=[
            {
                "status": "resolved",
                "value": "entailed",
                "reason_code": "source_ambiguous",
            }
            if request["spec"]["field"] == "behavior_family"
            else {"status": "resolved", "value": "entailed", "reason_code": None}
            for request in requests
        ],
        benchmark_mode=True,
        created_at=NOW,
    )

    review = next(item for item in reviews if item.spec.field == "behavior_family")
    assert review.decision.result.value == "not_stated"
    assert review.decision.provider.kind == "deterministic-rule"
    assert (
        "normative-default:behavior_family:not_stated" in review.decision.evidence_refs
    )


def test_exact_extractor_rejects_an_invented_or_case_changed_quote() -> None:
    clause = _clause()
    requests = prepare_source_extractions(clause, _bind_source_decisions(clause))

    with pytest.raises(SemanticContractError, match="exact source substring"):
        bind_source_extractions(
            requests=requests,
            provider_results=[{"quote": "Data"}, {"quote": "round trip"}],
            created_at=NOW,
        )
    with pytest.raises(SemanticContractError, match="determiner or quantifier"):
        bind_source_extractions(
            requests=requests[:1],
            provider_results=[{"quote": "All"}],
            created_at=NOW,
        )


def test_deterministic_extractions_bind_the_complete_clause_span() -> None:
    source_text = "On exit, data is removed."
    clause = _clause("Data is removed on exit.") | {
        "source_span": {"start": 0, "end": len(source_text)}
    }
    extractions = compile_deterministic_source_extractions(
        clause, _bind_source_decisions(clause), source_text, created_at=NOW
    )

    assert {item.span.text for item in extractions} == {source_text}
    assert all(item.span.start == 0 for item in extractions)
    assert all(item.span.end == len(source_text) for item in extractions)
    assert all(item.provider.kind == "deterministic-rule" for item in extractions)


def test_modifier_presence_controls_exact_extraction_requests() -> None:
    clause = _clause("Every active report returns CSV except archived reports.")
    decisions = _bind_source_decisions(
        clause,
        overrides={
            "polarity": "required",
            "quantifier": "every",
            "requirement_strength": "must",
            "has_precondition": "present",
            "has_exception": "present",
            "has_explicit_result": "present",
            "temporal_scope": "unspecified",
            "source_predicate": "explicit",
            "nonactionable_exclusion_safety": "product_semantics_present",
        },
    )

    requests = prepare_source_extractions(clause, decisions)

    assert [item["spec"]["extraction_kind"] for item in requests] == [
        "subject",
        "behavior",
        "precondition",
        "exception",
        "explicit_result",
    ]


def test_prohibition_route_forces_negative_product_contract() -> None:
    clause = _clause("Do not add retries.")
    decisions = _bind_source_decisions(clause, disposition="non_goal")
    values = {item.decision_kind: item.result.value for item in decisions}
    assert values["routing"] == "include_prohibition"
    assert values["disposition"] == "non_goal"
    assert values["polarity"] == "prohibited"


def test_non_directive_cannot_wording_is_not_a_product_prohibition() -> None:
    clause = _clause(
        "An implementation hint says that a type cannot extend a base type "
        "because it violates a required law."
    )
    plan = prepare_source_semantic_decisions(clause, created_at=NOW)
    decisions = bind_source_semantic_decisions(
        resolved_decisions=plan["resolved_decisions"],
        pending_specs=plan["pending_specs"],
        provider_results=[{"status": "resolved", "value": "include_prohibition"}],
        created_at=NOW,
    )

    routing = decisions[0]
    assert routing.decision_kind == "routing"
    assert routing.result.value == "include"
    assert routing.provider.kind == "deterministic-rule"
    assert "fallback:non-directive-negative-wording:include" in routing.evidence_refs


def test_unclear_route_continues_as_headless_review_candidate() -> None:
    clause = _clause("It should work well.")
    plan = prepare_source_semantic_decisions(clause, created_at=NOW)
    root = bind_source_semantic_decisions(
        resolved_decisions=[],
        pending_specs=plan["pending_specs"],
        provider_results=[{"status": "resolved", "value": "unclear"}],
        created_at=NOW,
    )
    dependent = prepare_dependent_source_semantic_decisions(
        clause, root, created_at=NOW
    )

    assert dependent["pending_specs"][0]["spec"]["decision_kind"] == "disposition"
    assert (
        "headless review packet"
        in " ".join(dependent["pending_specs"][0]["instructions"]).casefold()
    )


def test_bound_source_extraction_round_trips_strictly() -> None:
    clause = _clause()
    requests = prepare_source_extractions(clause, _bind_source_decisions(clause))
    extraction = bind_source_extractions(
        requests=requests[:1],
        provider_results=[{"quote": "data"}],
        created_at=NOW,
    )[0]

    assert BoundSourceExtraction.from_data(extraction.to_data()) == extraction
    with pytest.raises(SemanticContractError, match="fields are invalid"):
        BoundSourceExtraction.from_data(extraction.to_data() | {"summary": "data"})


def test_procedrr_command_boundary_persists_intermediate_artifacts(
    tmp_path: Path,
) -> None:
    catalog = feature_command_catalog()
    runtime = FeatureCommandRuntime(
        config=None,
        runner=None,
        worktree=tmp_path,
        output_root=tmp_path,
        branch="feature/test",
        slug="test",
        state={},
        catalog=catalog,
    )
    clause = _clause()
    plan = runtime.dispatch(
        "prepare_source_semantic_decisions",
        ["prepare_source_semantic_decisions"],
        {"clause": clause},
    )
    provider_results = [
        {
            "result": {
                "status": "resolved",
                "value": _source_result(item["spec"]["decision_kind"]),
                "reason_code": None,
            }
        }
        for item in plan["pending_specs"]
    ]
    root_bound = runtime.dispatch(
        "bind_source_semantic_decisions",
        ["bind_source_semantic_decisions"],
        {"clause": clause, "plan": plan, "results": provider_results},
    )
    child_plan = runtime.dispatch(
        "prepare_dependent_source_semantic_decisions",
        ["prepare_dependent_source_semantic_decisions"],
        {"clause": clause, "decisions": root_bound["decisions"]},
    )
    child_results = [
        {
            "result": {
                "status": "resolved",
                "value": _source_result(item["spec"]["decision_kind"]),
                "reason_code": None,
            }
        }
        for item in child_plan["pending_specs"]
    ]
    bound = runtime.dispatch(
        "bind_source_semantic_decisions",
        ["bind_source_semantic_decisions"],
        {"clause": clause, "plan": child_plan, "results": child_results},
    )
    extracted = runtime.dispatch(
        "compile_deterministic_source_extractions",
        ["compile_deterministic_source_extractions"],
        {
            "clause": clause,
            "decisions": bound["decisions"],
            "source_text": clause["text"],
        },
    )
    family_request = runtime.dispatch(
        "prepare_behavior_family_decision",
        ["prepare_behavior_family_decision"],
        {
            "clause": clause,
            "extractions": extracted["extractions"],
            "decisions": bound["decisions"],
        },
    )
    projection = runtime.dispatch(
        "compile_partial_semantic_contract",
        ["compile_partial_semantic_contract"],
        {
            "clause": clause,
            "decisions": bound["decisions"],
            "extractions": extracted["extractions"],
            "behavior_family_request": family_request,
            "behavior_family_result": {
                "status": "resolved",
                "value": "serialize",
                "reason_code": None,
            },
        },
    )

    artifact_root = tmp_path / "semantic-contracts" / "instruction-001"
    assert (artifact_root / "source-decisions.json").is_file()
    assert (artifact_root / "source-extractions.json").is_file()
    assert (artifact_root / "partial-contract.json").is_file()
    assert projection["description"] == clause["text"]
    assert projection["operation"] == clause["text"]


def test_benchmark_source_faithfulness_failure_becomes_source_invariant(
    tmp_path: Path,
) -> None:
    clause = _clause()
    decisions = _bind_source_decisions(clause)
    extraction_requests = prepare_source_extractions(clause, decisions)
    extractions = bind_source_extractions(
        requests=extraction_requests,
        provider_results=[{"quote": "data"}, {"quote": "pickle"}],
        created_at=NOW,
    )
    family_request = prepare_behavior_family_decision(clause, extractions[1], decisions)
    family = bind_behavior_family_decision(
        family_request,
        {"status": "resolved", "value": "serialize", "reason_code": None},
        created_at=NOW,
    )
    contract = compile_source_contract(
        clause=clause,
        decisions=decisions,
        extractions=extractions,
        behavior_family=family,
    )
    requests = prepare_field_entailment_reviews(contract)
    reviews = bind_field_entailment_reviews(
        requests=requests,
        provider_results=[
            {
                "status": "resolved",
                "value": (
                    "contradicted"
                    if request["spec"]["field"] == "behavior"
                    else "entailed"
                ),
                "reason_code": None,
            }
            for request in requests
        ],
        created_at=NOW,
    )
    state: dict[str, Any] = {}
    runtime = FeatureCommandRuntime(
        config=SimpleNamespace(
            benchmark_mode=True,
            design_only=False,
            capture_worker_prompts_only=False,
        ),
        runner=None,
        worktree=tmp_path,
        output_root=tmp_path,
        branch="main",
        slug="benchmark-fallback",
        state=state,
        catalog=feature_command_catalog(),
    )

    faithfulness = runtime.dispatch(
        "finalize_source_faithfulness",
        ["finalize_source_faithfulness"],
        {
            "contract": contract.to_data(),
            "reviews": [item.to_data() for item in reviews],
        },
    )
    design = project_partial_contract_to_legacy_design(contract) | {
        "partial_contract": contract.to_data()
    }
    merged = runtime.dispatch(
        "merge_behavior_scenario",
        ["merge_behavior_scenario"],
        {"clause": clause, "design": design, "scenario": {}},
    )

    assert faithfulness["accepted"] is True
    assert faithfulness["fallback"]["kind"] == "invariant"
    assert merged["kind"] == "invariant"
    assert merged["description"] == clause["text"]
    assert merged["acceptance_criterion"] == clause["text"]
    assert merged["behavior_scenario"]["then"] == clause["text"]
    fallback_path = (
        tmp_path
        / "semantic-contracts"
        / clause["clause_id"]
        / "invariant-fallback.json"
    )
    assert json.loads(fallback_path.read_text())["source_text"] == clause["text"]


def test_benchmark_unresolved_scenario_becomes_source_invariant(
    tmp_path: Path,
) -> None:
    clause = _clause("Every active state resets its data on re-entry.")
    state: dict[str, Any] = {}
    runtime = FeatureCommandRuntime(
        config=SimpleNamespace(
            benchmark_mode=True,
            design_only=False,
            capture_worker_prompts_only=False,
        ),
        runner=None,
        worktree=tmp_path,
        output_root=tmp_path,
        branch="main",
        slug="benchmark-scenario-fallback",
        state=state,
        catalog=feature_command_catalog(),
    )
    design = {
        "kind": "feature",
        "description": "active states reset data",
        "acceptance_criterion": "Data resets on re-entry.",
        "expected_test": "Test state data after re-entry.",
        "population": "active states",
        "operation": "reset data",
        "oracle": "data resets",
        "evidence_case": clause["text"],
        "partial_contract": {"routing": "include"},
    }
    unresolved_scenario = {
        "status": "needs_clarification",
        "unresolved_dimensions": ["error_behavior"],
        "scenario": {
            "subject": "active state data",
            "given": "an active state is re-entered",
            "when": "the state is entered again",
            "then": "the state data is reset",
            "dimensions": {
                "normal_result": "state data is reset",
                "error_behavior": "unresolved by source",
                "continuation": "not_applicable",
                "unsupported_behavior": "not_applicable",
                "cancellation_cleanup": "not_applicable",
                "compatibility": "not_applicable",
                "negative_boundaries": "not_applicable",
            },
            "assumptions": [],
            "capability_matrix": [],
            "related_requirements": [],
        },
    }

    merged = runtime.dispatch(
        "merge_behavior_scenario",
        ["merge_behavior_scenario"],
        {"clause": clause, "design": design, "scenario": unresolved_scenario},
    )

    assert merged["kind"] == "invariant"
    assert merged["description"] == clause["text"]
    assert merged["acceptance_criterion"] == clause["text"]
    assert merged["behavior_scenario"]["then"] == clause["text"]
    assert state["benchmark_invariant_fallbacks"][clause["clause_id"]]["reason"]
