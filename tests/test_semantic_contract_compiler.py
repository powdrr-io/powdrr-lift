from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from powdrr_lift.core.semantic_contract import (
    BoundSourceExtraction,
    SemanticContractError,
)
from powdrr_lift.core.semantic_decision import (
    DECISION_VALUES,
    UNRESOLVED_REASON_CODES,
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
    bind_behavior_family_decision,
    bind_source_extractions,
    bind_source_semantic_decisions,
    compile_source_contract,
    prepare_behavior_family_decision,
    prepare_dependent_source_semantic_decisions,
    prepare_source_extractions,
    prepare_source_semantic_decisions,
    project_partial_contract_to_legacy_design,
)

NOW = "2026-09-25T00:00:00Z"


def test_every_source_classifier_has_twenty_examples_and_label_coverage() -> None:
    assert set(CLASSIFIER_DEFINITIONS) == {
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
        assert len(definition.examples) >= 20, kind
        assert DECISION_VALUES[kind] <= {
            example.value
            for example in definition.examples
            if example.value is not None
        }, kind
        assert all(
            example.value in DECISION_VALUES[kind]
            if example.value is not None
            else example.unresolved_reason in UNRESOLVED_REASON_CODES
            for example in definition.examples
        ), kind


def test_behavior_family_classifies_public_api_availability_as_other() -> None:
    assert any(
        example.proposition
        == "DataVar and DataChangeInfo are importable from the package."
        and example.value == "other"
        for example in CLASSIFIER_DEFINITIONS["behavior_family"].examples
    )


def test_background_clause_takes_context_branch_before_child_classifiers() -> None:
    clause = _clause(
        "States lack built-in data ownership, forcing manual variable management "
        "without scoping or lifecycle."
    )
    root_plan = prepare_source_semantic_decisions(clause, created_at=NOW)
    assert root_plan["pending_specs"][0]["spec"]["decision_kind"] == "disposition"
    assert any(
        item.value == "context" and item.proposition == clause["text"]
        for item in CLASSIFIER_DEFINITIONS["disposition"].examples
    )
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


def test_nonactionable_clause_takes_process_only_branch() -> None:
    clause = _clause("Create a new branch and commit everything when done.")
    root_plan = prepare_source_semantic_decisions(clause, created_at=NOW)
    root = bind_source_semantic_decisions(
        resolved_decisions=[],
        pending_specs=root_plan["pending_specs"],
        provider_results=[{"status": "resolved", "value": "nonactionable"}],
        created_at=NOW,
    )

    child_plan = prepare_dependent_source_semantic_decisions(
        clause, root, created_at=NOW
    )

    assert [item["spec"]["decision_kind"] for item in child_plan["pending_specs"]] == [
        "nonactionable_exclusion_safety"
    ]
    child_values = {
        item["decision_kind"]: item["result"]["value"]
        for item in child_plan["resolved_decisions"]
    }
    assert child_values["polarity"] == "descriptive"


def _clause(text: str = "All data should pickle.") -> dict[str, Any]:
    return {
        "clause_id": "instruction-001",
        "text": text,
        "fingerprint": "sha256:clause",
    }


def _source_result(kind: str, *, disposition: str = "invariant") -> str:
    return {
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
        provider_results=[{"status": "resolved", "value": disposition}],
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
        "disposition"
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
    assert data["schema_version"] == "partial-semantic-contract-v1"
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


def test_nonactionable_requires_independent_process_only_confirmation() -> None:
    clause = _clause("Do not add retries.")
    with pytest.raises(SemanticContractError, match="requires process-only evidence"):
        _bind_source_decisions(
            clause,
            disposition="nonactionable",
            overrides={"nonactionable_exclusion_safety": "product_semantics_present"},
        )


def test_actionable_disposition_rejects_process_only_confirmation() -> None:
    clause = _clause()
    with pytest.raises(SemanticContractError, match="conflicts with process-only"):
        _bind_source_decisions(
            clause, overrides={"nonactionable_exclusion_safety": "process_only"}
        )
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

    compile_source_contract(
        clause=clause,
        decisions=decisions,
        extractions=extractions,
        behavior_family=family,
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
    extraction_plan = runtime.dispatch(
        "prepare_source_extractions",
        ["prepare_source_extractions"],
        {"clause": clause, "decisions": bound["decisions"]},
    )
    extracted = runtime.dispatch(
        "bind_source_extractions",
        ["bind_source_extractions"],
        {
            "clause": clause,
            "requests": extraction_plan["requests"],
            "results": [
                {"result": {"quote": "data", "occurrence": None}},
                {"result": {"quote": "pickle", "occurrence": None}},
            ],
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
    assert projection["description"] == "data pickle."
    assert projection["operation"] == "serialize: pickle"
