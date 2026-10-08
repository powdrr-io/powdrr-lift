from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from powdrr_lift.core.acceptance_contract import BehavioralContract
from powdrr_lift.core.instruction_ledger import compile_instruction_ledger
from powdrr_lift.core.semantic_contract import (
    BoundSourceExtraction,
    PartialSemanticContract,
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
    finalize_scenario_claim_reviews,
    finalize_source_faithfulness,
    prepare_field_entailment_reviews,
    prepare_scenario_claim_reviews,
)
from powdrr_lift.core.source_interpretation import (
    SourceInterpretation,
    SourceInterpretationError,
)
from powdrr_lift.workrr.acceptance_contract_compiler import (
    MAX_CONTRACT_GROUP_SIZE,
    _partition,
    bind_behavioral_contracts,
    prepare_behavioral_contracts,
)
from powdrr_lift.workrr.command_catalog import (
    FeatureCommandRuntime,
    _attach_behavioral_contract_context,
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


def test_split_clause_classifier_receives_bounded_neighbor_context() -> None:
    source = (
        "Current behavior lacks state-scoped data. The getter reads local data. "
        "Callbacks receive merged ancestor data."
    )
    target = "The getter reads local data."
    start = source.index(target)
    clause = {
        **_clause(target),
        "source_span": {"start": start, "end": start + len(target)},
    }
    plan = prepare_source_semantic_decisions(clause, source_text=source, created_at=NOW)
    request = plan["pending_specs"][0]

    assert "Current behavior lacks state-scoped data." in request["subject_text"]
    assert "The getter reads local data." in request["subject_text"]
    assert "Callbacks receive merged ancestor data." in request["subject_text"]
    assert request["spec"]["context_text"] == "\n".join(
        (
            "Current behavior lacks state-scoped data.",
            "The getter reads local data.",
            "Callbacks receive merged ancestor data.",
        )
    )


def test_source_decision_fingerprint_includes_neighbor_context() -> None:
    target = "State data resets to its defaults on re-entry."
    source_a = f"The current API lacks scoped state. {target} Data is per instance."
    source_b = f"The API already stores state data. {target} Data is per instance."

    def prepare(source: str) -> dict[str, Any]:
        start = source.index(target)
        clause = {
            **_clause(target),
            "source_span": {"start": start, "end": start + len(target)},
        }
        return prepare_source_semantic_decisions(
            clause, source_text=source, created_at=NOW
        )["pending_specs"][0]["spec"]

    spec_a = prepare(source_a)
    spec_b = prepare(source_b)

    assert spec_a["proposition_text"] == spec_b["proposition_text"] == target
    assert spec_a["context_text"] != spec_b["context_text"]
    assert spec_a["input_fingerprint"] != spec_b["input_fingerprint"]


def test_split_scope_relations_reach_source_semantic_decisions() -> None:
    clause = {
        **_clause("Support nested paths through list indexes."),
        "semantic_relations": [
            {
                "relation_type": "list_relation",
                "label": "independent_required",
                "child_clause_ids": ["instruction-001", "instruction-002"],
                "evidence": "list indexes and null values",
            }
        ],
        "modifier_attachments": [
            {
                "relation_type": "modifier_attachment",
                "label": "entire_group",
                "child_clause_ids": ["instruction-001", "instruction-002"],
                "evidence": "nested paths",
            }
        ],
    }
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

    assert (
        root_request["scope_relations"]["semantic_relations"]
        == clause["semantic_relations"]
    )
    assert child_requests
    assert all("scope_relations" in request for request in child_requests)


def test_boolean_combination_reaches_each_split_clause_classifier() -> None:
    combination = {
        "parent_clause_id": "instruction-001",
        "child_clause_ids": ["instruction-001", "instruction-002", "instruction-003"],
        "expression": {
            "op": "and",
            "args": [
                {"atom": 1},
                {"op": "or", "args": [{"atom": 2}, {"atom": 3}]},
            ],
        },
        "reconstructed_sentence": "A happens and either B happens or C happens.",
        "current_child_id": "instruction-002",
        "current_child_index": 2,
    }
    clause = {
        **_clause("B happens."),
        "boolean_combination": combination,
    }

    request = prepare_source_semantic_decisions(clause, created_at=NOW)[
        "pending_specs"
    ][0]

    assert request["scope_relations"]["boolean_combination"] == combination


def test_graphql_field_overwrite_classifier_has_no_list_navigation_attachment() -> None:
    parent = (
        "Support nested paths navigating through lists by index, null values, "
        "field overwrites, and concurrent deferred/streamed fields."
    )
    clause = {
        **_clause("Support field overwrites."),
        "clause_id": "instruction-003",
        "source_span": {"start": 0, "end": len(parent)},
        "semantic_relations": [],
        "modifier_attachments": [
            {
                "relation_type": "modifier_attachment",
                "label": "one_child",
                "child_clause_ids": ["instruction-001"],
                "evidence": "navigating through lists by index",
            }
        ],
    }

    request = prepare_source_semantic_decisions(clause, source_text=parent)[
        "pending_specs"
    ][0]

    assert request["spec"]["subject_ref"] == "instruction-003"
    assert (
        "Proposition to classify:\nSupport field overwrites." in request["subject_text"]
    )
    assert request["scope_relations"]["modifier_attachments"][0][
        "child_clause_ids"
    ] == ["instruction-001"]
    assert (
        "instruction-003"
        not in request["scope_relations"]["modifier_attachments"][0]["child_clause_ids"]
    )


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


def test_unresolved_disposition_does_not_default_to_an_invariant() -> None:
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
        provider_results=[
            {"status": "unresolved", "value": None, "reason_code": "no_candidate"}
        ],
        created_at=NOW,
    )
    disposition = next(
        item for item in decisions if item.decision_kind == "disposition"
    )

    assert disposition.result.status == "unresolved"
    assert disposition.result.value is None


def test_context_route_takes_context_branch_before_child_classifiers() -> None:
    clause = _clause(
        "States lack built-in data ownership, forcing manual variable management "
        "without scoping or lifecycle."
    )
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


def test_unresolved_routing_stays_unresolved_and_cannot_compile_as_obligation() -> None:
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
    assert root[0].result.status == "unresolved"
    assert root[0].result.value is None

    decision_plan = prepare_dependent_source_semantic_decisions(
        clause, root, created_at=NOW
    )
    assert decision_plan["pending_specs"] == []
    assert decision_plan["resolved_decisions"][0]["result"]["status"] == "unresolved"
    with pytest.raises(SemanticContractError, match="unresolved decisions"):
        compile_source_contract(
            clause=clause,
            decisions=root,
            extractions=(),
            behavior_family=root[0],
        )


def test_unclear_route_is_bound_as_unresolved() -> None:
    clause = _clause("States lack built-in data ownership.")
    plan = prepare_source_semantic_decisions(clause, created_at=NOW)
    decisions = bind_source_semantic_decisions(
        resolved_decisions=[],
        pending_specs=plan["pending_specs"],
        provider_results=[{"status": "resolved", "value": "unclear"}],
        created_at=NOW,
    )

    assert decisions[0].decision_kind == "routing"
    assert decisions[0].result.status == "unresolved"
    assert decisions[0].result.reason_code == "source_ambiguous"


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


def _faithfulness_contract(source: str) -> PartialSemanticContract:
    clause = _clause(source)
    decisions = _bind_source_decisions(
        clause,
        disposition="feature",
        overrides={"copy_depth": "unspecified"},
    )
    requests = prepare_source_extractions(clause, decisions)
    extractions = bind_source_extractions(
        requests=requests,
        provider_results=[{"quote": source}, {"quote": source}],
        created_at=NOW,
    )
    behavior = next(item for item in extractions if item.extraction_kind == "behavior")
    family_request = prepare_behavior_family_decision(clause, behavior)
    family = bind_behavior_family_decision(
        family_request,
        {"status": "resolved", "value": "other"},
        created_at=NOW,
    )
    return compile_source_contract(
        clause=clause,
        decisions=decisions,
        extractions=extractions,
        behavior_family=family,
    )


def test_scenario_claim_review_covers_later_prompt_fields_and_exact_quotes() -> None:
    source = "The API returns a shallow copy of each mapping."
    contract = _faithfulness_contract(source)
    scenario = {
        "then": source,
        "dimensions": {
            "normal_result": "The API returns the original mapping.",
            "error_behavior": "not_applicable",
        },
        "related_requirements": ["The getter exposes merged ancestor values."],
        "capability_matrix": [
            {
                "capability": "nested mappings",
                "behavior": "support",
                "evidence": ["Nested mappings are returned by identity."],
            }
        ],
    }

    plan = prepare_scenario_claim_reviews(
        contract,
        scenario=scenario,
        ledger_clauses=[{"clause_id": contract.source_ref, "text": source}],
    )

    assert len(plan["deterministic_claims"]) == 1
    assert plan["deterministic_claims"][0]["candidate_value"] == source
    reviewed_paths = {
        path for request in plan["requests"] for path in request["claim"]["field_paths"]
    }
    assert {
        "dimensions.normal_result",
        "related_requirements[0]",
        "capability_matrix[0]",
    } <= reviewed_paths


def test_scenario_claim_review_separates_contradictions_and_assumptions() -> None:
    source = "The API initializes values from defaults."
    contract = _faithfulness_contract(source)
    scenario = {
        "then": (
            "The API returns the same mapping instance after recursively copying it."
        ),
        "dimensions": {
            "normal_result": "The API returns the same mapping instance.",
            "error_behavior": "not_applicable",
            "copy_depth": "The operation makes a recursive copy of nested values.",
        },
        "assumptions": [
            {
                "dimension": "copy_depth",
                "resolution": "Use a recursive copy of nested values.",
                "rationale": "The source does not define copy depth.",
                "basis": "conservative_default",
                "basis_reference": "No specific normative source identified.",
                "confidence": "low",
            }
        ],
        "related_requirements": [],
        "capability_matrix": [],
    }
    plan = prepare_scenario_claim_reviews(
        contract,
        scenario=scenario,
        ledger_clauses=[{"clause_id": contract.source_ref, "text": source}],
    )
    provider_results = [
        {
            "status": "resolved",
            "value": (
                "contradicted"
                if "then" in request["claim"]["field_paths"]
                else "not_stated"
            ),
            "reason_code": None,
        }
        for request in plan["requests"]
    ]

    outcome = finalize_scenario_claim_reviews(
        plan, provider_results, benchmark_mode=True, created_at=NOW
    )

    assert not outcome["accepted"]
    assert any(
        finding["reason_code"] == "intent_contradiction"
        for finding in outcome["findings"]
    )
    assumption_review = next(
        review
        for review in outcome["reviews"]
        if "dimensions.copy_depth" in review["field_paths"]
    )
    assert assumption_review["decision"]["result"]["value"] == "not_stated"
    assert "dimensions.copy_depth" in assumption_review["assumption_backed_paths"]


def test_unstated_scenario_claim_without_recorded_assumption_is_rejected() -> None:
    source = "The API initializes values from defaults."
    contract = _faithfulness_contract(source)
    scenario = {
        "then": "The API always returns the same object instance.",
        "dimensions": {"normal_result": "The API returns its defaults."},
    }
    plan = prepare_scenario_claim_reviews(
        contract,
        scenario=scenario,
        ledger_clauses=[{"clause_id": contract.source_ref, "text": source}],
    )
    results = [
        {
            "status": "resolved",
            "value": "not_stated",
            "reason_code": None,
        }
        for request in plan["requests"]
    ]

    outcome = finalize_scenario_claim_reviews(
        plan, results, benchmark_mode=True, created_at=NOW
    )

    assert not outcome["accepted"]
    assert any(
        finding["reason_code"] == "source_underspecified"
        for finding in outcome["findings"]
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


def test_source_interpretation_preserves_sequence_rule_and_contrast() -> None:
    source = "The .data dict is accumulated across payloads, not raw deltas."
    interpretation = SourceInterpretation.bind(
        {
            "subject": "the .data dict",
            "operation": "accumulated",
            "affected_value": ".data",
            "rule": "accumulated across payloads",
            "contrast": "raw deltas",
            "behavior_form": "state_transition",
            "result_presence": "explicit",
            "event_scope": "event_sequence",
            "contrast_presence": "explicit",
            "unresolved_fields": [],
            "field_evidence": [
                "subject|.data dict",
                "operation|accumulated",
                "affected_value|.data",
                "rule|accumulated across payloads",
                "contrast|raw deltas",
            ],
        },
        source_ref="instruction-001",
        source_text=source,
        conditions=(),
        exceptions=(),
        decision_fingerprints={"behavior_family": "sha256:family"},
    )

    assert interpretation.meaning_status == "interpreted"
    assert interpretation.event_scope == "event_sequence"
    assert interpretation.contrast == "raw deltas"
    assert interpretation.to_data()["field_evidence"][-1] == "contrast|raw deltas"


def test_source_interpretation_rejects_evidence_not_in_source() -> None:
    with pytest.raises(SourceInterpretationError, match="not present in the source"):
        SourceInterpretation.bind(
            {
                "subject": "the .data dict",
                "operation": "accumulated",
                "affected_value": None,
                "rule": "accumulated across payloads",
                "contrast": None,
                "behavior_form": "state_transition",
                "result_presence": "unspecified",
                "event_scope": "event_sequence",
                "contrast_presence": "absent",
                "unresolved_fields": ["affected_value|not_stated"],
                "field_evidence": [
                    "subject|the .data dict",
                    "operation|accumulated",
                    "rule|accumulated across payloads",
                ],
            },
            source_ref="instruction-001",
            source_text="The .data dict accumulates.",
            conditions=(),
            exceptions=(),
            decision_fingerprints={},
        )


def test_unknown_family_keeps_interpreted_rule_in_contract_and_projection() -> None:
    source = "The .data dict is accumulated across payloads, not raw deltas."
    clause = _clause(source)
    decisions = _bind_source_decisions(
        clause,
        disposition="invariant",
        overrides={"source_predicate": "explicit"},
    )
    extractions = compile_deterministic_source_extractions(
        clause, decisions, source, created_at=NOW
    )
    family_request = prepare_behavior_family_decision(clause, extractions[1], decisions)
    family = bind_behavior_family_decision(
        family_request,
        {"status": "unresolved", "value": None, "reason_code": "no_candidate"},
        created_at=NOW,
    )
    interpretation = SourceInterpretation.bind(
        {
            "subject": "the .data dict",
            "operation": "accumulated",
            "affected_value": ".data",
            "rule": "accumulated across payloads",
            "contrast": "raw deltas",
            "behavior_form": "state_transition",
            "result_presence": "explicit",
            "event_scope": "event_sequence",
            "contrast_presence": "explicit",
            "unresolved_fields": [],
            "field_evidence": [
                "subject|.data dict",
                "operation|accumulated",
                "affected_value|.data",
                "rule|accumulated across payloads",
                "contrast|raw deltas",
            ],
        },
        source_ref=clause["clause_id"],
        source_text=source,
        conditions=(),
        exceptions=(),
        decision_fingerprints={},
    )
    contract = compile_source_contract(
        clause=clause,
        decisions=decisions,
        extractions=extractions,
        behavior_family=family,
        source_interpretation=interpretation,
    )

    data = contract.to_data()
    restored = PartialSemanticContract.from_data(data)
    projection = project_partial_contract_to_legacy_design(restored)

    assert data["behavior"]["family"] == "other"
    assert data["gaps"]["source_meaning"]["status"] == "interpreted"
    assert data["gaps"]["registry_label"]["status"] == "unregistered"
    assert restored.fingerprint == contract.fingerprint
    assert "across successive events" in projection["expected_test"]
    assert "rather than raw deltas" in projection["expected_test"]


def test_behavioral_contracts_group_explicit_roles_and_keep_context_separate() -> None:
    ledger = compile_instruction_ledger(
        "demo",
        "The result mapping accumulates entries across payloads.\n"
        "The result mapping exposes accumulated entries after each payload.\n"
        "Current transport context is multipart.",
    )
    semantic_designs = []
    for clause, disposition, operation, rule, evidence in (
        (
            ledger.clauses[0].to_data(),
            "feature",
            "accumulate entries",
            "entries accumulate across payloads",
            "accumulates entries across payloads",
        ),
        (
            ledger.clauses[1].to_data(),
            "feature",
            "accumulate entries",
            "expose accumulated entries after each payload",
            "exposes accumulated entries after each payload",
        ),
        (ledger.clauses[2].to_data(), "context", "", "", ""),
    ):
        decisions = _bind_source_decisions(
            clause,
            disposition=disposition,
            overrides={"source_predicate": "explicit"},
        )
        extractions = compile_deterministic_source_extractions(
            clause, decisions, ledger.source.text, created_at=NOW
        )
        family_request = prepare_behavior_family_decision(
            clause,
            next(item for item in extractions if item.extraction_kind == "behavior"),
            decisions,
        )
        family = bind_behavior_family_decision(
            family_request,
            {"status": "resolved", "value": "serialize", "reason_code": None},
            created_at=NOW,
        )
        interpretation = None
        if operation:
            interpretation = SourceInterpretation.bind(
                {
                    "subject": "result mapping",
                    "operation": operation,
                    "affected_value": "entries",
                    "rule": rule,
                    "contrast": None,
                    "behavior_form": "state_transition",
                    "result_presence": "explicit",
                    "event_scope": "event_sequence",
                    "contrast_presence": "absent",
                    "unresolved_fields": [],
                    "field_evidence": [
                        "subject|result mapping",
                        f"operation|{evidence}",
                        "affected_value|entries",
                        f"rule|{evidence}",
                    ],
                },
                source_ref=clause["clause_id"],
                source_text=clause["text"],
                conditions=(),
                exceptions=(),
                decision_fingerprints={},
            )
        contract = compile_source_contract(
            clause=clause,
            decisions=decisions,
            extractions=extractions,
            behavior_family=family,
            source_interpretation=interpretation,
        )
        semantic_designs.append({"partial_contract": contract.to_data()})

    plan = prepare_behavioral_contracts(ledger.to_data(), semantic_designs)
    assert len(plan["requests"]) == 1
    result = bind_behavioral_contracts(
        plan,
        [
            {
                "member_indexes": [0, 1],
                "context_indexes": [0],
                "relationships": [
                    {
                        "kind": "constrains_output",
                        "source_evidence": "accumulates entries across payloads",
                        "target_indexes": [0, 1],
                    }
                ],
                "unresolved_questions": [],
            }
        ],
    )

    behavioral_contract = BehavioralContract.from_data(result["contracts"][0])
    assert behavioral_contract.member_requirement_ids == tuple(
        clause.clause_id for clause in ledger.clauses[:2]
    )
    assert behavioral_contract.supporting_context_ids == (ledger.clauses[2].clause_id,)
    assert behavioral_contract.relationships[0].kind == "constrains_output"
    assert ledger.clauses[2].clause_id not in behavioral_contract.member_requirement_ids
    attached = _attach_behavioral_contract_context(
        ledger,
        [
            {
                "partial_contract": {"source_ref": clause.clause_id},
                "behavior_scenario": {"related_requirements": []},
            }
            for clause in ledger.clauses[:2]
        ],
        (behavioral_contract,),
    )
    first_context = attached[0]["behavior_scenario"]["related_requirements"]
    assert any(ledger.clauses[1].text in item for item in first_context)
    assert any(
        "Context only; this is not an implementation requirement" in item
        and ledger.clauses[2].text in item
        for item in first_context
    )


def test_invalid_behavioral_group_indexes_fall_back_without_dropping_requirements() -> (
    None
):
    ledger = compile_instruction_ledger(
        "demo",
        "The result mapping accumulates entries across payloads.\n"
        "The result mapping exposes accumulated entries after each payload.",
    )
    semantic_designs = []
    for clause, evidence, rule in (
        (
            ledger.clauses[0].to_data(),
            "accumulates entries across payloads",
            "accumulate",
        ),
        (
            ledger.clauses[1].to_data(),
            "exposes accumulated entries after each payload",
            "expose",
        ),
    ):
        decisions = _bind_source_decisions(
            clause,
            disposition="feature",
            overrides={"source_predicate": "explicit"},
        )
        extractions = compile_deterministic_source_extractions(
            clause, decisions, ledger.source.text, created_at=NOW
        )
        family_request = prepare_behavior_family_decision(
            clause,
            next(item for item in extractions if item.extraction_kind == "behavior"),
            decisions,
        )
        family = bind_behavior_family_decision(
            family_request,
            {"status": "resolved", "value": "serialize", "reason_code": None},
            created_at=NOW,
        )
        interpretation = SourceInterpretation.bind(
            {
                "subject": "result mapping",
                "operation": "share result mapping behavior",
                "affected_value": "entries",
                "rule": rule,
                "contrast": None,
                "behavior_form": "state_transition",
                "result_presence": "explicit",
                "event_scope": "event_sequence",
                "contrast_presence": "absent",
                "unresolved_fields": [],
                "field_evidence": [
                    "subject|result mapping",
                    f"operation|{evidence}",
                    "affected_value|entries",
                    f"rule|{evidence}",
                ],
            },
            source_ref=clause["clause_id"],
            source_text=clause["text"],
            conditions=(),
            exceptions=(),
            decision_fingerprints={},
        )
        semantic_designs.append(
            {
                "partial_contract": compile_source_contract(
                    clause=clause,
                    decisions=decisions,
                    extractions=extractions,
                    behavior_family=family,
                    source_interpretation=interpretation,
                ).to_data()
            }
        )
    plan = prepare_behavioral_contracts(ledger.to_data(), semantic_designs)
    result = bind_behavioral_contracts(
        plan,
        [
            {
                "member_indexes": [0, 99],
                "context_indexes": [],
                "relationships": [],
                "unresolved_questions": [],
            }
        ],
    )

    contracts = [BehavioralContract.from_data(item) for item in result["contracts"]]
    assert set(result["covered_requirement_ids"]) == {
        clause.clause_id for clause in ledger.clauses
    }
    assert len(contracts) == 2
    assert all(contract.unresolved_questions for contract in contracts)

    rejected_edge_result = bind_behavioral_contracts(
        plan,
        [
            {
                "member_indexes": [0, 1],
                "context_indexes": [],
                "relationships": ["not a serialized relationship"],
                "unresolved_questions": [],
            }
        ],
    )
    grouped = BehavioralContract.from_data(rejected_edge_result["contracts"][0])
    assert grouped.member_requirement_ids == tuple(
        clause.clause_id for clause in ledger.clauses
    )
    assert grouped.relationships == ()
    assert any(
        "relationship edges were discarded" in question
        for question in grouped.unresolved_questions
    )

    duplicate_indexes_result = bind_behavioral_contracts(
        plan,
        [
            {
                "member_indexes": [0, 0, 1],
                "context_indexes": [],
                "relationships": [],
                "unresolved_questions": [],
            }
        ],
    )
    duplicate_index_contracts = [
        BehavioralContract.from_data(item)
        for item in duplicate_indexes_result["contracts"]
    ]
    assert len(duplicate_index_contracts) == 2
    assert all(
        "member indexes contain duplicates" in contract.unresolved_questions[0]
        for contract in duplicate_index_contracts
    )


def test_behavioral_contract_partitioning_is_bounded_and_covers_all_members() -> None:
    members = tuple(f"instruction-{index:02d}" for index in range(17))

    partitions = _partition(members, MAX_CONTRACT_GROUP_SIZE)

    assert all(
        1 < len(partition) <= MAX_CONTRACT_GROUP_SIZE for partition in partitions
    )
    assert set().union(*(set(partition) for partition in partitions)) == set(members)
    assert set(partitions[0]).intersection(partitions[1])


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


def test_unclear_route_stays_unresolved_without_dependent_decisions() -> None:
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

    assert root[0].result.status == "unresolved"
    assert dependent["pending_specs"] == []
    assert dependent["resolved_decisions"][0]["result"]["status"] == "unresolved"


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
    interpretation_plan = runtime.dispatch(
        "prepare_source_interpretation",
        ["prepare_source_interpretation"],
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
    unresolved_interpretation = {
        "subject": None,
        "operation": None,
        "affected_value": None,
        "rule": None,
        "contrast": None,
        "behavior_form": "unclear",
        "result_presence": "unspecified",
        "event_scope": "unspecified",
        "contrast_presence": "absent",
        "unresolved_fields": [
            "subject|source_underspecified",
            "operation|source_underspecified",
            "affected_value|source_underspecified",
            "rule|source_underspecified",
            "behavior_form|source_underspecified",
        ],
        "field_evidence": [],
    }
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
            "behavior_family_decision": interpretation_plan["behavior_family_decision"],
            "source_interpretation_request": interpretation_plan["request"],
            "source_interpretation_result": unresolved_interpretation,
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
