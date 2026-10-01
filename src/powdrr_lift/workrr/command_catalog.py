"""The typed internal command catalog used by the feature Procedrr flow."""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from powdrr_lift.core.behavior_contract import (
    BEHAVIOR_DIMENSIONS,
    compile_behavior_scenarios,
    validate_normative_assumptions,
)
from powdrr_lift.core.decision_obligation import content_fingerprint
from powdrr_lift.core.feature_obligation import (
    SEMANTIC_KINDS,
    FeatureObligationError,
    compile_feature_design,
)
from powdrr_lift.core.instruction_ledger import (
    InstructionLedger,
    InstructionLedgerError,
    apply_atomicity_decisions,
    compile_instruction_ledger,
)
from powdrr_lift.core.repository_inventory import (
    CandidateSet,
    InventoryError,
    LookupQuery,
    RepositoryInventory,
    StructrrLookupContext,
    build_inventory,
    build_python_lookup_context,
    enumerate_population,
    inventory_from_source_subjects,
)
from powdrr_lift.core.semantic_contract import (
    BoundSourceExtraction,
    PartialSemanticContract,
    SemanticContractError,
)
from powdrr_lift.core.semantic_decision import SemanticDecision, SemanticDecisionError
from powdrr_lift.core.semantic_faithfulness import (
    FaithfulnessError,
    FieldEntailmentReview,
    FieldEntailmentSpec,
)
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.structrr.obligation_evidence import (
    ObligationEvidenceContract,
    assert_obligation_evidence_complete,
    compile_obligation_evidence_contract,
)
from powdrr_lift.workrr.external_contract_research import (
    bind_external_contract_assessments,
    bind_external_contract_claims,
    bind_external_contract_search_selections,
    capture_external_sources,
    extract_external_contract_evidence,
    finalize_external_contract_context,
    prepare_external_contract_projection_requests,
    project_external_contract_requirements,
    search_external_contract_sources,
)
from powdrr_lift.workrr.repository_subject_binding import (
    add_candidate_source_excerpts,
    bind_candidate_relation_decisions,
    finalize_subject_binding,
    infer_contextual_qualified_names,
    prepare_candidate_relation_decisions,
    prepare_subject_lookup_query,
    retrieve_subject_candidates,
)
from powdrr_lift.workrr.semantic_contract_compiler import (
    bind_behavior_family_decision,
    bind_field_entailment_reviews,
    bind_source_extractions,
    bind_source_semantic_decisions,
    compile_deterministic_source_extractions,
    compile_source_contract,
    finalize_source_faithfulness,
    prepare_behavior_family_decision,
    prepare_dependent_source_semantic_decisions,
    prepare_field_entailment_reviews,
    prepare_source_extractions,
    prepare_source_semantic_decisions,
    project_partial_contract_to_legacy_design,
)
from procedrr.command_catalog import CommandCatalog, CommandSpec, object_schema


def feature_command_catalog(
    implementations: Mapping[str, Callable[[Mapping[str, Any]], Any]] | None = None,
) -> CommandCatalog:
    """Return the complete implement-feature command catalog."""
    implementations = implementations or {}
    commands: dict[str, CommandSpec] = {
        "ensure_current_structrr": CommandSpec(
            name="ensure_current_structrr",
            input_schema=object_schema(
                {},
                required=(),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("ensure_current_structrr"),
        ),
        "discover_validation_profiles": CommandSpec(
            name="discover_validation_profiles",
            input_schema=object_schema(
                {"baseline": {}},
                required=("baseline",),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("discover_validation_profiles"),
        ),
        "capture_external_contract_sources": CommandSpec(
            name="capture_external_contract_sources",
            input_schema=object_schema(
                {
                    "requests": {
                        "type": "array",
                        "maxItems": 8,
                        "items": {
                            "type": "object",
                            "required": ["url", "research_question", "why_applicable"],
                            "additionalProperties": False,
                            "properties": {
                                "url": {"type": "string", "minLength": 1},
                                "search_source_ref": {"type": "string"},
                                "search_title": {"type": "string"},
                                "search_query": {"type": "string"},
                                "profile": {"type": "string"},
                                "research_question": {
                                    "type": "string",
                                    "minLength": 1,
                                },
                                "why_applicable": {
                                    "type": "string",
                                    "minLength": 1,
                                },
                            },
                        },
                    },
                    "decision": {"type": "string", "enum": ["research", "skip"]},
                    "rationale": {"type": "string", "minLength": 1},
                },
                required=("requests", "decision", "rationale"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("capture_external_contract_sources"),
        ),
        "search_external_contract_sources": CommandSpec(
            name="search_external_contract_sources",
            input_schema=object_schema(
                {
                    "decision": {"type": "string", "enum": ["research", "skip"]},
                    "queries": {
                        "type": "array",
                        "maxItems": 4,
                        "items": {
                            "type": "object",
                            "required": [
                                "query",
                                "research_question",
                                "profile",
                                "why_applicable",
                            ],
                            "additionalProperties": False,
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 600,
                                },
                                "research_question": {"type": "string", "minLength": 1},
                                "profile": {"type": "string", "minLength": 1},
                                "why_applicable": {"type": "string", "minLength": 1},
                            },
                        },
                    },
                },
                required=("decision", "queries"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("search_external_contract_sources"),
        ),
        "bind_external_contract_search_selections": CommandSpec(
            name="bind_external_contract_search_selections",
            input_schema=object_schema(
                {"search_results": {}, "selections": {"type": "array", "maxItems": 8}},
                required=("search_results", "selections"),
                additional_properties=False,
            ),
            output_schema={
                "type": "object",
                "required": ["requests"],
                "properties": {"requests": {"type": "array"}},
            },
            logic=implementations.get("bind_external_contract_search_selections"),
        ),
        "extract_external_contract_evidence": CommandSpec(
            name="extract_external_contract_evidence",
            input_schema=object_schema(
                {
                    "sources": {"type": "array"},
                    "feature_description": {"type": "string"},
                },
                required=("sources", "feature_description"),
                additional_properties=False,
            ),
            output_schema={"type": "object"},
            logic=implementations.get("extract_external_contract_evidence"),
        ),
        "bind_external_contract_claims": CommandSpec(
            name="bind_external_contract_claims",
            input_schema=object_schema(
                {"evidence": {"type": "object"}, "claims": {"type": "array"}},
                required=("evidence", "claims"),
                additional_properties=False,
            ),
            output_schema={"type": "object"},
            logic=implementations.get("bind_external_contract_claims"),
        ),
        "bind_external_contract_assessments": CommandSpec(
            name="bind_external_contract_assessments",
            input_schema=object_schema(
                {
                    "claims": {"type": "array"},
                    "assessments": {"type": "array"},
                    "benchmark_mode": {"type": "boolean"},
                },
                required=("claims", "assessments", "benchmark_mode"),
                additional_properties=False,
            ),
            output_schema={"type": "object"},
            logic=implementations.get("bind_external_contract_assessments"),
        ),
        "prepare_external_contract_projection_requests": CommandSpec(
            name="prepare_external_contract_projection_requests",
            input_schema=object_schema(
                {"requirements": {"type": "array"}, "claims": {"type": "array"}},
                required=("requirements", "claims"),
                additional_properties=False,
            ),
            output_schema={"type": "object"},
            logic=implementations.get("prepare_external_contract_projection_requests"),
        ),
        "project_external_contract_requirements": CommandSpec(
            name="project_external_contract_requirements",
            input_schema=object_schema(
                {"requests": {"type": "array"}},
                required=("requests",),
                additional_properties=False,
            ),
            output_schema={"type": "object"},
            logic=implementations.get("project_external_contract_requirements"),
        ),
        "finalize_external_contract_context": CommandSpec(
            name="finalize_external_contract_context",
            input_schema=object_schema(
                {
                    "evidence": {"type": "object"},
                    "claims": {"type": "array"},
                    "assessment_result": {"type": "object"},
                    "projections": {"type": "array"},
                },
                required=("evidence", "claims", "assessment_result", "projections"),
                additional_properties=False,
            ),
            output_schema={"type": "object"},
            logic=implementations.get("finalize_external_contract_context"),
        ),
        "apply_external_contract_context": CommandSpec(
            name="apply_external_contract_context",
            input_schema=object_schema(
                {
                    "feature_design": {"type": "object"},
                    "external_contract_context": {"type": "object"},
                },
                required=("feature_design", "external_contract_context"),
                additional_properties=False,
            ),
            output_schema={"type": "object"},
            logic=implementations.get("apply_external_contract_context"),
        ),
        "compile_instruction_ledger": CommandSpec(
            name="compile_instruction_ledger",
            input_schema=object_schema(
                {"work_item_name": {}, "feature_description": {}},
                required=("work_item_name", "feature_description"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("compile_instruction_ledger"),
        ),
        "prepare_atomicity_split_requests": CommandSpec(
            name="prepare_atomicity_split_requests",
            input_schema=object_schema(
                {"decisions": {}},
                required=("decisions",),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("prepare_atomicity_split_requests"),
        ),
        "apply_atomicity_splits": CommandSpec(
            name="apply_atomicity_splits",
            input_schema=object_schema(
                {"decisions": {}, "splits": {}},
                required=("decisions", "splits"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("apply_atomicity_splits"),
        ),
        "prepare_source_semantic_decisions": CommandSpec(
            name="prepare_source_semantic_decisions",
            input_schema=object_schema(
                {"clause": {}}, required=("clause",), additional_properties=False
            ),
            output_schema={},
            logic=implementations.get("prepare_source_semantic_decisions"),
        ),
        "prepare_dependent_source_semantic_decisions": CommandSpec(
            name="prepare_dependent_source_semantic_decisions",
            input_schema=object_schema(
                {"clause": {}, "decisions": {}},
                required=("clause", "decisions"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("prepare_dependent_source_semantic_decisions"),
        ),
        "bind_source_semantic_decisions": CommandSpec(
            name="bind_source_semantic_decisions",
            input_schema=object_schema(
                {"clause": {}, "plan": {}, "results": {}},
                required=("clause", "plan", "results"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("bind_source_semantic_decisions"),
        ),
        "prepare_source_extractions": CommandSpec(
            name="prepare_source_extractions",
            input_schema=object_schema(
                {"clause": {}, "decisions": {}},
                required=("clause", "decisions"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("prepare_source_extractions"),
        ),
        "bind_source_extractions": CommandSpec(
            name="bind_source_extractions",
            input_schema=object_schema(
                {"clause": {}, "requests": {}, "results": {}},
                required=("clause", "requests", "results"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("bind_source_extractions"),
        ),
        "compile_deterministic_source_extractions": CommandSpec(
            name="compile_deterministic_source_extractions",
            input_schema=object_schema(
                {
                    "clause": {},
                    "decisions": {},
                    "source_text": {"type": "string"},
                },
                required=("clause", "decisions"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("compile_deterministic_source_extractions"),
        ),
        "prepare_behavior_family_decision": CommandSpec(
            name="prepare_behavior_family_decision",
            input_schema=object_schema(
                {"clause": {}, "extractions": {}, "decisions": {}},
                required=("clause", "extractions", "decisions"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("prepare_behavior_family_decision"),
        ),
        "compile_partial_semantic_contract": CommandSpec(
            name="compile_partial_semantic_contract",
            input_schema=object_schema(
                {
                    "clause": {},
                    "decisions": {},
                    "extractions": {},
                    "behavior_family_request": {},
                    "behavior_family_result": {},
                },
                required=(
                    "clause",
                    "decisions",
                    "extractions",
                    "behavior_family_request",
                    "behavior_family_result",
                ),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("compile_partial_semantic_contract"),
        ),
        "prepare_field_entailment_reviews": CommandSpec(
            name="prepare_field_entailment_reviews",
            input_schema=object_schema(
                {"contract": {}}, required=("contract",), additional_properties=False
            ),
            output_schema={},
            logic=implementations.get("prepare_field_entailment_reviews"),
        ),
        "bind_field_entailment_reviews": CommandSpec(
            name="bind_field_entailment_reviews",
            input_schema=object_schema(
                {"requests": {}, "results": {}},
                required=("requests", "results"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("bind_field_entailment_reviews"),
        ),
        "finalize_source_faithfulness": CommandSpec(
            name="finalize_source_faithfulness",
            input_schema=object_schema(
                {"contract": {}, "reviews": {}},
                required=("contract", "reviews"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("finalize_source_faithfulness"),
        ),
        "build_semantic_repository_inventory": CommandSpec(
            name="build_semantic_repository_inventory",
            input_schema=object_schema({}, required=(), additional_properties=False),
            output_schema={},
            logic=implementations.get("build_semantic_repository_inventory"),
        ),
        "prepare_subject_lookup_query": CommandSpec(
            name="prepare_subject_lookup_query",
            input_schema=object_schema(
                {"contract": {}, "inventory": {}},
                required=("contract", "inventory"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("prepare_subject_lookup_query"),
        ),
        "retrieve_subject_candidates": CommandSpec(
            name="retrieve_subject_candidates",
            input_schema=object_schema(
                {"query": {}, "inventory": {}},
                required=("query", "inventory"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("retrieve_subject_candidates"),
        ),
        "prepare_candidate_relation_decisions": CommandSpec(
            name="prepare_candidate_relation_decisions",
            input_schema=object_schema(
                {"query": {}, "candidates": {}},
                required=("query", "candidates"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("prepare_candidate_relation_decisions"),
        ),
        "bind_candidate_relation_decisions": CommandSpec(
            name="bind_candidate_relation_decisions",
            input_schema=object_schema(
                {"requests": {}, "results": {}},
                required=("requests", "results"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("bind_candidate_relation_decisions"),
        ),
        "finalize_subject_binding": CommandSpec(
            name="finalize_subject_binding",
            input_schema=object_schema(
                {"candidates": {}, "decisions": {}, "quantifier": {}},
                required=("candidates", "decisions", "quantifier"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("finalize_subject_binding"),
        ),
        "prepare_repository_subject_binding": CommandSpec(
            name="prepare_repository_subject_binding",
            input_schema=object_schema(
                {"contract": {}},
                required=("contract",),
                additional_properties=False,
            ),
            output_schema={"type": "object"},
            logic=implementations.get("prepare_repository_subject_binding"),
        ),
        "finalize_repository_subject_binding": CommandSpec(
            name="finalize_repository_subject_binding",
            input_schema=object_schema(
                {
                    "candidates": {},
                    "requests": {},
                    "results": {},
                    "query": {},
                    "quantifier": {},
                },
                required=("candidates", "requests", "results", "quantifier"),
                additional_properties=False,
            ),
            output_schema={"type": "object"},
            logic=implementations.get("finalize_repository_subject_binding"),
        ),
        "enumerate_subject_population": CommandSpec(
            name="enumerate_subject_population",
            input_schema=object_schema(
                {
                    "inventory": {},
                    "population_ref": {},
                    "membership_rule_ref": {},
                    "member_ids": {},
                    "complete": {},
                },
                required=(
                    "inventory",
                    "population_ref",
                    "membership_rule_ref",
                    "member_ids",
                    "complete",
                ),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("enumerate_subject_population"),
        ),
        "merge_semantic_design": CommandSpec(
            name="merge_semantic_design",
            input_schema=object_schema(
                {
                    "kind": {"type": "string", "enum": sorted(SEMANTIC_KINDS)},
                    "description": {},
                    "acceptance_criterion": {},
                    "population": {},
                    "operation": {},
                    "oracle": {},
                    "evidence_case": {},
                },
                required=(
                    "kind",
                    "description",
                    "acceptance_criterion",
                    "population",
                    "operation",
                    "oracle",
                    "evidence_case",
                ),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("merge_semantic_design"),
        ),
        "merge_behavior_scenario": CommandSpec(
            name="merge_behavior_scenario",
            input_schema=object_schema(
                {
                    "clause": {},
                    "design": {},
                    "scenario": {},
                    "repository_binding": {},
                },
                required=("clause", "design", "scenario"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("merge_behavior_scenario"),
        ),
        "compile_canonical_feature_design": CommandSpec(
            name="compile_canonical_feature_design",
            input_schema=object_schema(
                {
                    "work_item_name": {},
                    "design_decisions": {},
                    "scenario_consistency_review": {},
                },
                required=(
                    "work_item_name",
                    "design_decisions",
                    "scenario_consistency_review",
                ),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("compile_canonical_feature_design"),
        ),
        "plan_structrr_diff": CommandSpec(
            name="plan_structrr_diff",
            input_schema=object_schema(
                {
                    "baseline": {},
                    "work_item_name": {},
                    "feature_description": {},
                    "feature_design": {},
                },
                required=(
                    "baseline",
                    "work_item_name",
                    "feature_description",
                    "feature_design",
                ),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("plan_structrr_diff"),
        ),
        "materialize_feature_intents": CommandSpec(
            name="materialize_feature_intents",
            input_schema=object_schema(
                {"plan": {}, "obligations": {}},
                required=("plan", "obligations"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("materialize_feature_intents"),
        ),
        "compile_verification_obligations": CommandSpec(
            name="compile_verification_obligations",
            input_schema=object_schema(
                {"baseline": {}, "plan": {}, "feature_description": {}},
                required=("baseline", "plan", "feature_description"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("compile_verification_obligations"),
        ),
        "assert_verification_obligations_complete": CommandSpec(
            name="assert_verification_obligations_complete",
            input_schema=object_schema(
                {"verification_obligations": {}},
                required=("verification_obligations",),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("assert_verification_obligations_complete"),
        ),
        "prepare_proposal_review": CommandSpec(
            name="prepare_proposal_review",
            input_schema=object_schema(
                {
                    "baseline": {},
                    "plan": {},
                    "feature_description": {},
                    "verification_obligations": {},
                },
                required=(
                    "baseline",
                    "plan",
                    "feature_description",
                    "verification_obligations",
                ),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("prepare_proposal_review"),
        ),
        "bind_proposal_decision_results": CommandSpec(
            name="bind_proposal_decision_results",
            input_schema=object_schema(
                {"worklist": {}, "decisions": {}},
                required=("worklist", "decisions"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("bind_proposal_decision_results"),
        ),
        "finalize_proposal_review": CommandSpec(
            name="finalize_proposal_review",
            input_schema=object_schema(
                {"proposal_revision_path": {}, "worklist_path": {}, "decisions": {}},
                required=("proposal_revision_path", "worklist_path", "decisions"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("finalize_proposal_review"),
        ),
        "run_code_agent": CommandSpec(
            name="run_code_agent",
            input_schema=object_schema(
                {
                    "baseline": {},
                    "plan": {},
                    "proposal_review_receipt": {},
                    "work_item_name": {},
                    "feature_description": {},
                    "obligations": {},
                    "verification_obligations": {},
                    "repair_issue": {},
                    "repair_request": {},
                    "review_verdict": {},
                },
                required=(),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("run_code_agent"),
        ),
        # Obligation-driven implementation flow. These commands deliberately
        # have open object inputs/outputs at the catalog boundary because their
        # exact evidence records are compiled from the current repository and
        # are validated by the command implementations.
        **{
            name: CommandSpec(
                name=name,
                input_schema=object_schema({}, additional_properties=True),
                output_schema=object_schema({}, additional_properties=True),
                logic=implementations.get(name),
            )
            for name in (
                "compile_obligation_verification_plans",
                "evaluate_deterministic_decision",
                "finalize_obligation_verification_plan_review",
                "resolve_obligation_populations",
                "finalize_population_review",
                "run_obligation_baseline",
                "compile_code_task_plan",
                "finalize_code_task_plan_review",
                "compile_code_task_preconditions",
                "finalize_code_task_preconditions",
                "capture_code_task_before_state",
                "run_code_task_agent",
                "compile_code_task_postconditions",
                "finalize_code_task_receipt",
                "run_final_obligation_evidence",
                "finalize_obligation_closure",
                "prepare_final_implementation_review",
                "correct_candidate_from_structrr_diff",
                "get_candidate_correction_review",
                "finalize_implementation_review",
            )
        },
        "run_validation_profile": CommandSpec(
            name="run_validation_profile",
            input_schema=object_schema(
                {"profile": {}, "implementation": {}, "task_receipts": {}},
                required=("profile",),
                additional_properties=True,
            ),
            output_schema={},
            logic=implementations.get("run_validation_profile"),
        ),
        "aggregate_validation": CommandSpec(
            name="aggregate_validation",
            input_schema=object_schema(
                {"implementation": {}, "task_receipts": {}, "results": {}},
                required=("results",),
                additional_properties=True,
            ),
            output_schema={},
            logic=implementations.get("aggregate_validation"),
        ),
        "validate_required_test_cases": CommandSpec(
            name="validate_required_test_cases",
            input_schema=object_schema(
                {"plan": {}},
                required=("plan",),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("validate_required_test_cases"),
        ),
        "run_verification_evidence": CommandSpec(
            name="run_verification_evidence",
            input_schema=object_schema(
                {"obligations": {}},
                required=("obligations",),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("run_verification_evidence"),
        ),
        "reconcile_verification_evidence": CommandSpec(
            name="reconcile_verification_evidence",
            input_schema=object_schema(
                {"obligations": {}, "evidence": {}, "candidate_tree": {}},
                required=("obligations", "evidence", "candidate_tree"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("reconcile_verification_evidence"),
        ),
        "review_worker_diff": CommandSpec(
            name="review_worker_diff",
            input_schema=object_schema(
                {"implementation": {}, "validation": {}},
                required=("implementation", "validation"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("review_worker_diff"),
        ),
        "prepare_implementation_review": CommandSpec(
            name="prepare_implementation_review",
            input_schema=object_schema(
                {"implementation": {}, "validation": {}, "review": {}},
                required=("implementation", "validation", "review"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("prepare_implementation_review"),
        ),
        "compile_obligation_review_packets": CommandSpec(
            name="compile_obligation_review_packets",
            input_schema=object_schema(
                {"obligations": {}, "evidence": {}},
                required=("obligations", "evidence"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("compile_obligation_review_packets"),
        ),
        "bind_obligation_reviews": CommandSpec(
            name="bind_obligation_reviews",
            input_schema=object_schema(
                {"packets": {}, "decisions": {}},
                required=("packets", "decisions"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("bind_obligation_reviews"),
        ),
        "aggregate_obligation_reviews": CommandSpec(
            name="aggregate_obligation_reviews",
            input_schema=object_schema(
                {"packets": {}, "reviews": {}, "verification_reconciliation": {}},
                required=("packets", "reviews", "verification_reconciliation"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("aggregate_obligation_reviews"),
        ),
        "collect_repair_issues": CommandSpec(
            name="collect_repair_issues",
            input_schema=object_schema(
                {
                    "validation": {},
                    "review": {},
                    "reconciliation": {},
                    "required_test_validation": {},
                },
                required=("validation", "review", "reconciliation"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("collect_repair_issues"),
        ),
        "open_pull_request": CommandSpec(
            name="open_pull_request",
            input_schema=object_schema(
                {
                    "review": {},
                    "plan": {},
                    "work_item_name": {},
                    "feature_description": {},
                },
                required=("review", "plan", "work_item_name", "feature_description"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("open_pull_request"),
        ),
        "create_pr_changelog": CommandSpec(
            name="create_pr_changelog",
            input_schema=object_schema(
                {
                    "pull_request": {},
                    "plan": {},
                    "work_item_name": {},
                    "feature_description": {},
                },
                required=(
                    "pull_request",
                    "plan",
                    "work_item_name",
                    "feature_description",
                ),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("create_pr_changelog"),
        ),
        "update_pull_request": CommandSpec(
            name="update_pull_request",
            input_schema=object_schema(
                {"pull_request": {}, "changelog": {}},
                required=("pull_request", "changelog"),
                additional_properties=False,
            ),
            output_schema={},
            logic=implementations.get("update_pull_request"),
        ),
    }
    return CommandCatalog(tuple(commands.values()))


@dataclass(slots=True)
class FeatureCommandRuntime:
    """Runtime containing the implement-feature command implementations."""

    config: Any
    runner: Any
    worktree: Path
    output_root: Path
    branch: str
    slug: str
    state: dict[str, Any]
    catalog: CommandCatalog

    def dispatch(
        self,
        name: str,
        command: list[Any],
        parameters: Mapping[str, Any],
    ) -> Any:
        from powdrr_lift.workrr import feature_endpoint

        config = self.config
        runner = self.runner
        worktree = self.worktree
        output_root = self.output_root
        branch = self.branch
        slug = self.slug
        state = self.state

        def benchmark_mode() -> bool:
            return bool(getattr(config, "benchmark_mode", False))

        if name == "ensure_current_structrr":
            state["baseline_path"] = feature_endpoint._ensure_current_baseline(
                worktree,
                runner,
                bootstrap_path=output_root / "validation-bootstrap.yaml",
            )
            return {"path": str(state["baseline_path"])}
        if name == "discover_validation_profiles":
            return [
                {
                    "name": profile.name,
                    "command": list(profile.command),
                    "source": profile.source,
                }
                for profile in state["validation_profiles"]
            ]
        if name == "capture_external_contract_sources":
            return capture_external_sources(
                parameters.get("requests", []),
                artifact_root=output_root,
                decision=str(parameters.get("decision", "research")),
                rationale=str(parameters.get("rationale", "")),
            )
        if name == "search_external_contract_sources":
            cache_root = (
                Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
                / "powdrr-lift"
            )
            return search_external_contract_sources(
                parameters.get("queries", []),
                decision=str(parameters.get("decision", "research")),
                api_key=os.environ.get("TAVILY_API_KEY"),
                cache_path=cache_root / "external-contract-search.sqlite3",
            )
        if name == "bind_external_contract_search_selections":
            search_results = parameters.get("search_results")
            if not isinstance(search_results, Mapping):
                raise PowdrrExecutionError("external search results are malformed")
            return {
                "requests": bind_external_contract_search_selections(
                    search_results, parameters.get("selections", [])
                )
            }
        if name == "extract_external_contract_evidence":
            try:
                return extract_external_contract_evidence(
                    parameters.get("sources", []),
                    feature_description=str(parameters.get("feature_description", "")),
                    artifact_root=output_root,
                )
            except (OSError, TypeError, ValueError) as error:
                raise PowdrrExecutionError(
                    f"external contract evidence extraction failed: {error}"
                ) from error
        if name == "bind_external_contract_claims":
            try:
                result = bind_external_contract_claims(
                    parameters.get("evidence", {}), parameters.get("claims", [])
                )
            except (TypeError, ValueError) as error:
                raise PowdrrExecutionError(str(error)) from error
            return result
        if name == "bind_external_contract_assessments":
            try:
                return bind_external_contract_assessments(
                    parameters.get("claims", []),
                    parameters.get("assessments", []),
                    benchmark_mode=bool(parameters.get("benchmark_mode", False)),
                )
            except (TypeError, ValueError) as error:
                raise PowdrrExecutionError(str(error)) from error
        if name == "prepare_external_contract_projection_requests":
            try:
                return prepare_external_contract_projection_requests(
                    parameters.get("requirements", []), parameters.get("claims", [])
                )
            except (TypeError, ValueError) as error:
                raise PowdrrExecutionError(str(error)) from error
        if name == "project_external_contract_requirements":
            try:
                return project_external_contract_requirements(
                    parameters.get("requests", [])
                )
            except (TypeError, ValueError) as error:
                raise PowdrrExecutionError(str(error)) from error
        if name == "finalize_external_contract_context":
            try:
                return finalize_external_contract_context(
                    evidence=parameters.get("evidence", {}),
                    claims=parameters.get("claims", []),
                    assessment_result=parameters.get("assessment_result", {}),
                    projections=parameters.get("projections", []),
                    artifact_root=output_root,
                )
            except (OSError, TypeError, ValueError) as error:
                raise PowdrrExecutionError(
                    f"external contract context is invalid: {error}"
                ) from error
        if name == "apply_external_contract_context":
            feature_design = parameters.get("feature_design")
            context = parameters.get("external_contract_context")
            if not isinstance(feature_design, Mapping) or not isinstance(
                context, Mapping
            ):
                raise PowdrrExecutionError(
                    "external contract projection inputs are malformed"
                )
            obligations = feature_design.get("obligations")
            projected = context.get("projected_obligations")
            if not isinstance(obligations, list) or not isinstance(projected, list):
                raise PowdrrExecutionError("external contract projection is incomplete")
            design_path = feature_design.get("path")
            canonical_path: Path | None = None
            canonical_design: Mapping[str, Any] = feature_design
            if isinstance(design_path, str) and design_path.strip():
                canonical_path = Path(design_path).resolve()
                if not canonical_path.is_relative_to(output_root.resolve()):
                    raise PowdrrExecutionError(
                        "external design path escapes the run artifacts"
                    )
                if canonical_path.is_file():
                    try:
                        loaded_design = json.loads(
                            canonical_path.read_text(encoding="utf-8")
                        )
                    except (OSError, json.JSONDecodeError) as error:
                        raise PowdrrExecutionError(
                            "canonical feature design could not be read"
                        ) from error
                    if isinstance(loaded_design, Mapping):
                        canonical_design = loaded_design
            canonical_obligations = canonical_design.get("obligations", obligations)
            if not isinstance(canonical_obligations, list):
                raise PowdrrExecutionError(
                    "canonical feature design has no obligations"
                )
            enriched = dict(feature_design)
            enriched_obligations = list(obligations)
            existing_ids = {
                item.get("id")
                for item in enriched_obligations
                if isinstance(item, Mapping)
            }
            raw_contracts = feature_design.get("verification_contracts", [])
            if not isinstance(raw_contracts, list):
                raise PowdrrExecutionError(
                    "feature design verification contracts are malformed"
                )
            contracts = list(raw_contracts)
            for item in projected:
                if not isinstance(item, Mapping):
                    raise PowdrrExecutionError(
                        "external projected obligation is malformed"
                    )
                identifier = item.get("id")
                design = item.get("design")
                if (
                    not isinstance(identifier, str)
                    or identifier in existing_ids
                    or not isinstance(design, Mapping)
                ):
                    raise PowdrrExecutionError(
                        "external projected obligation identity is invalid"
                    )
                scenario = design.get("behavior_scenario")
                if not isinstance(scenario, Mapping):
                    raise PowdrrExecutionError(
                        "external projected obligation has no scenario"
                    )
                enriched_obligations.append(dict(item))
                existing_ids.add(identifier)
                contracts.append(
                    {
                        "id": f"test:{identifier}",
                        "obligation_ref": identifier,
                        "population": str(scenario.get("given", "")),
                        "operation": str(scenario.get("when", "")),
                        "oracle": str(scenario.get("then", "")),
                        "evidence_case": str(design.get("expected_test", "")),
                    }
                )
            enriched["obligations"] = enriched_obligations
            enriched["verification_contracts"] = contracts
            enriched["fingerprint"] = content_fingerprint(
                {key: value for key, value in enriched.items() if key != "fingerprint"}
            )
            design_path = feature_design.get("path")
            if (
                projected
                and isinstance(design_path, str)
                and design_path.strip()
                and canonical_design.get("schema_version") == "feature-design-v2"
            ):
                path = Path(design_path).resolve()
                if not path.is_relative_to(output_root.resolve()):
                    raise PowdrrExecutionError(
                        "external design path escapes the run artifacts"
                    )
                canonical_enriched = dict(canonical_design)
                canonical_external = canonical_design.get(
                    "external_contract_obligations", []
                )
                if not isinstance(canonical_external, list):
                    raise PowdrrExecutionError(
                        "canonical external obligations are malformed"
                    )
                canonical_enriched["external_contract_obligations"] = [
                    *canonical_external,
                    *[
                        item
                        for item in projected
                        if isinstance(item, Mapping)
                        and item.get("id")
                        not in {
                            existing.get("id")
                            for existing in canonical_external
                            if isinstance(existing, Mapping)
                        }
                    ],
                ]
                canonical_contracts = canonical_design.get(
                    "verification_contracts", raw_contracts
                )
                if not isinstance(canonical_contracts, list):
                    raise PowdrrExecutionError(
                        "canonical verification contracts are malformed"
                    )
                canonical_enriched["verification_contracts"] = [
                    *canonical_contracts,
                    *contracts[len(raw_contracts) :],
                ]
                canonical_enriched["fingerprint"] = content_fingerprint(
                    {
                        key: value
                        for key, value in canonical_enriched.items()
                        if key != "fingerprint"
                    }
                )
                path.write_text(
                    json.dumps(canonical_enriched, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            state["external_contract_context"] = dict(context)
            state["external_contract_requirements"] = [
                dict(item)
                for item in context.get("requirements", [])
                if isinstance(item, Mapping)
            ]
            state["external_contract_notes"] = [
                dict(item)
                for item in context.get("unresolved_claims", [])
                if isinstance(item, Mapping)
            ]
            return {"feature_design": enriched, "feature_obligations": enriched}
        if command[:2] == ["powdrr-lift", "design-interview-input"]:
            feature_endpoint._run(runner, worktree, command)
            work_item_name = feature_endpoint._command_option(
                command, "--work-item-name"
            )
            return {
                "path": str(
                    worktree
                    / "docs"
                    / "proposals"
                    / work_item_name
                    / "design-interview-input.json"
                )
            }
        if command[:2] == ["powdrr-lift", "feature-pr-specification"]:
            feature_endpoint._run(runner, worktree, command)
            work_item_name = feature_endpoint._command_option(
                command, "--work-item-name"
            )
            return {
                "path": str(
                    worktree
                    / "docs"
                    / "proposals"
                    / work_item_name
                    / "feature-pr-specification.yaml"
                )
            }
        if command[:2] == ["powdrr-lift", "evaluate"]:
            return feature_endpoint._evaluate_proposal_command(
                runner, worktree, command
            )

        def extract_proposal_issues() -> Any:
            evaluation = parameters.get("evaluation")
            return (
                list(evaluation.get("issues", []))
                if isinstance(evaluation, Mapping)
                else []
            )

        def aggregate_category_edits() -> Any:
            decisions = parameters.get("decisions")
            if not isinstance(decisions, Mapping):
                raise PowdrrExecutionError(
                    "aggregate_category_edits requires category decisions"
                )
            return feature_endpoint._aggregate_category_edits(
                decisions,
                inventory=state.get("provider_inventory", ()),
                validation_profiles=state.get("validation_profiles", ()),
            )

        def decompose_feature_description() -> Any:
            feature_description = parameters.get("feature_description")
            if (
                not isinstance(feature_description, str)
                or not feature_description.strip()
            ):
                raise PowdrrExecutionError("feature description is empty")
            return feature_endpoint._decompose_feature_description(feature_description)

        def compile_instruction_ledger_operation() -> Any:
            feature_description = parameters.get("feature_description")
            work_item_name = parameters.get("work_item_name")
            if (
                not isinstance(feature_description, str)
                or not feature_description.strip()
            ):
                raise PowdrrExecutionError("feature description is empty")
            if not isinstance(work_item_name, str) or not work_item_name.strip():
                raise PowdrrExecutionError("work item name is empty")
            ledger = compile_instruction_ledger(work_item_name, feature_description)
            path = output_root / "instruction-ledger.json"
            path.write_text(
                json.dumps(ledger.to_data(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            state["instruction_ledger_path"] = path
            state["instruction_ledger_fingerprint"] = ledger.fingerprint
            return {
                "path": str(path),
                "fingerprint": ledger.fingerprint,
                "clauses": [item.to_data() for item in ledger.clauses],
            }

        def load_instruction_ledger() -> InstructionLedger:
            ledger_path = state.get("instruction_ledger_path")
            if not isinstance(ledger_path, Path):
                raise PowdrrExecutionError("instruction ledger is unavailable")
            try:
                return InstructionLedger.from_data(
                    json.loads(ledger_path.read_text(encoding="utf-8"))
                )
            except (OSError, json.JSONDecodeError, InstructionLedgerError) as exc:
                raise PowdrrExecutionError(
                    f"instruction ledger cannot be loaded: {exc}"
                ) from exc

        def collected_atomicity_decisions() -> list[dict[str, Any]]:
            raw_decisions = feature_endpoint._collected_results(
                parameters.get("decisions")
            )
            if raw_decisions is None or not all(
                isinstance(item, Mapping) for item in raw_decisions
            ):
                raise PowdrrExecutionError(
                    "atomicity decisions are missing or malformed"
                )
            return [dict(item) for item in raw_decisions]

        def prepare_atomicity_split_requests_operation() -> Any:
            ledger = load_instruction_ledger()
            decisions = collected_atomicity_decisions()
            if len(decisions) != len(ledger.clauses):
                raise PowdrrExecutionError(
                    "atomicity decision count does not match instruction clauses"
                )
            try:
                multiple = [
                    decision["multiple"]
                    for decision in decisions
                    if set(decision) == {"multiple"}
                    and isinstance(decision["multiple"], bool)
                ]
            except KeyError as exc:  # defensive: schema validation should catch this.
                raise PowdrrExecutionError("atomicity decision is malformed") from exc
            if len(multiple) != len(decisions):
                raise PowdrrExecutionError(
                    "atomicity decisions may contain only boolean multiple"
                )
            state["atomicity_decisions"] = decisions
            return {
                "split_requests": [
                    {"clause": clause.to_data()}
                    for clause, is_multiple in zip(
                        ledger.clauses, multiple, strict=True
                    )
                    if is_multiple
                ]
            }

        def apply_atomicity_splits_operation() -> Any:
            ledger = load_instruction_ledger()
            decisions = collected_atomicity_decisions()
            stored_decisions = state.get("atomicity_decisions")
            if decisions != stored_decisions:
                raise PowdrrExecutionError(
                    "atomicity decisions changed between classification and split"
                )
            split_results = feature_endpoint._collected_results(
                parameters.get("splits")
            )
            if split_results is None or not all(
                isinstance(item, Mapping) for item in split_results
            ):
                raise PowdrrExecutionError("atomicity splits are missing or malformed")
            multiple_ids = [
                clause.clause_id
                for clause, decision in zip(ledger.clauses, decisions, strict=True)
                if decision["multiple"]
            ]
            if len(split_results) != len(multiple_ids):
                raise PowdrrExecutionError(
                    "atomicity split count does not match multi-requirement clauses"
                )
            compiler_decisions: dict[str, dict[str, Any]] = {
                clause.clause_id: {"multiple": False} for clause in ledger.clauses
            }
            for clause_id, split in zip(multiple_ids, split_results, strict=True):
                if set(split) != {"statements", "validation_groups"}:
                    raise PowdrrExecutionError(
                        "atomicity split requires statements and validation_groups"
                    )
                compiler_decisions[clause_id] = {
                    "multiple": True,
                    "statements": split.get("statements"),
                    "validation_groups": split.get("validation_groups"),
                }
            try:
                refined = apply_atomicity_decisions(ledger, compiler_decisions)
            except InstructionLedgerError as exc:
                raise PowdrrExecutionError(
                    f"atomicity split is invalid: {exc}"
                ) from exc
            ledger_path = state["instruction_ledger_path"]
            assert isinstance(ledger_path, Path)
            ledger_path.write_text(
                json.dumps(refined.to_data(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            state["instruction_ledger_fingerprint"] = refined.fingerprint
            return {
                "path": str(ledger_path),
                "fingerprint": refined.fingerprint,
                "clauses": [item.to_data() for item in refined.clauses],
                "split_diagnostics": [
                    item.to_data() for item in refined.split_diagnostics
                ],
            }

        def merge_semantic_design_operation() -> Any:
            """Join the independently elicited semantic fields for one clause."""
            return _merge_semantic_design_values(parameters)

        def record_benchmark_invariant_fallback(
            clause: Mapping[str, Any],
            *,
            reason: str,
            details: Mapping[str, Any] | None = None,
            partial_contract: Mapping[str, Any] | None = None,
        ) -> Mapping[str, Any]:
            clause_id = clause.get("clause_id")
            text = clause.get("text")
            if not isinstance(clause_id, str) or not isinstance(text, str):
                raise PowdrrExecutionError(
                    "benchmark invariant fallback requires a source clause"
                )
            fallback: dict[str, Any] = {
                "clause_id": clause_id,
                "source_text": text,
                "reason": reason,
                "disposition": "invariant",
                "details": dict(details or {}),
            }
            if isinstance(partial_contract, Mapping):
                fallback["partial_contract"] = dict(partial_contract)
                fallback["source_contract_id"] = partial_contract.get("contract_id")
                fallback["source_contract_fingerprint"] = partial_contract.get(
                    "fingerprint"
                )
            fallbacks = state.setdefault("benchmark_invariant_fallbacks", {})
            if not isinstance(fallbacks, dict):
                raise PowdrrExecutionError(
                    "benchmark invariant fallback state is malformed"
                )
            fallbacks[clause_id] = fallback
            artifact_directory = output_root / "semantic-contracts" / clause_id
            artifact_directory.mkdir(parents=True, exist_ok=True)
            (artifact_directory / "invariant-fallback.json").write_text(
                json.dumps(fallback, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return fallback

        def merge_as_source_invariant(clause: Mapping[str, Any]) -> Any:
            text = clause.get("text")
            clause_id = clause.get("clause_id")
            if not isinstance(text, str) or not isinstance(clause_id, str):
                raise PowdrrExecutionError(
                    "benchmark invariant fallback requires a source clause"
                )
            fallbacks = state.get("benchmark_invariant_fallbacks", {})
            fallback = (
                fallbacks.get(clause_id) if isinstance(fallbacks, Mapping) else None
            )
            partial_contract = (
                fallback.get("partial_contract")
                if isinstance(fallback, Mapping)
                else None
            )
            if not isinstance(partial_contract, Mapping):
                artifact_path = (
                    output_root
                    / "semantic-contracts"
                    / clause_id
                    / "partial-contract.json"
                )
                if artifact_path.is_file():
                    loaded_contract = json.loads(
                        artifact_path.read_text(encoding="utf-8")
                    )
                    if isinstance(loaded_contract, Mapping):
                        partial_contract = loaded_contract
            if not isinstance(partial_contract, Mapping):
                raise PowdrrExecutionError(
                    f"benchmark invariant fallback for {clause_id!r} has no "
                    "source semantic contract"
                )
            fallback_design = _benchmark_invariant_design(clause, partial_contract)
            dimensions = {name: "not_applicable" for name in BEHAVIOR_DIMENSIONS}
            dimensions["normal_result"] = text
            scenario = {
                "status": "resolved",
                "unresolved_dimensions": [],
                "scenario": {
                    "subject": "The source instruction",
                    "given": f"The implementation is evaluated against: {text}",
                    "when": "The instruction's behavior is exercised",
                    "then": text,
                    "dimensions": dimensions,
                    "related_requirements": [],
                    "assumptions": [],
                    "capability_matrix": [],
                },
            }
            return _merge_behavior_scenario_values(
                {"clause": clause, "design": fallback_design, "scenario": scenario},
                benchmark_mode=True,
            )

        def merge_behavior_scenario_operation() -> Any:
            call_parameters = parameters
            repository_binding = parameters.get("repository_binding")
            if isinstance(repository_binding, Mapping):
                design = parameters.get("design")
                if not isinstance(design, Mapping):
                    raise PowdrrExecutionError("behavior scenario has no source design")
                call_parameters = {
                    **parameters,
                    "design": {
                        **dict(design),
                        "repository_binding": dict(repository_binding),
                    },
                }
            clause = parameters.get("clause")
            if not isinstance(clause, Mapping):
                raise PowdrrExecutionError("behavior scenario has no source clause")
            clause_id = clause.get("clause_id")
            fallbacks = state.get("benchmark_invariant_fallbacks", {})
            fallback = (
                fallbacks.get(clause_id)
                if isinstance(fallbacks, Mapping) and isinstance(clause_id, str)
                else None
            )
            if config is not None and getattr(config, "benchmark_mode", False):
                if isinstance(fallback, Mapping):
                    return merge_as_source_invariant(clause)
                try:
                    return _merge_behavior_scenario_values(
                        call_parameters,
                        benchmark_mode=benchmark_mode(),
                    )
                except PowdrrExecutionError as error:
                    design = parameters.get("design")
                    partial_contract = (
                        design.get("partial_contract")
                        if isinstance(design, Mapping)
                        else None
                    )
                    record_benchmark_invariant_fallback(
                        clause,
                        reason=str(error),
                        partial_contract=(
                            partial_contract
                            if isinstance(partial_contract, Mapping)
                            else None
                        ),
                    )
                    return merge_as_source_invariant(clause)
            return _merge_behavior_scenario_values(
                call_parameters,
                benchmark_mode=benchmark_mode(),
                allow_clarification=bool(
                    config is not None
                    and (
                        getattr(config, "design_only", False)
                        or getattr(config, "capture_worker_prompts_only", False)
                    )
                ),
            )

        def semantic_artifact_directory(clause: Mapping[str, Any]) -> Path:
            clause_id = clause.get("clause_id")
            if not isinstance(clause_id, str) or not clause_id.strip():
                raise PowdrrExecutionError("semantic operation requires a clause ID")
            path = output_root / "semantic-contracts" / clause_id
            path.mkdir(parents=True, exist_ok=True)
            return path

        def semantic_clause() -> Mapping[str, Any]:
            clause = parameters.get("clause")
            if not isinstance(clause, Mapping):
                raise PowdrrExecutionError("semantic operation requires a clause")
            return clause

        def semantic_decisions(raw: Any) -> list[SemanticDecision]:
            if not isinstance(raw, list) or not all(
                isinstance(item, Mapping) for item in raw
            ):
                raise PowdrrExecutionError("semantic decisions are malformed")
            try:
                return [SemanticDecision.from_data(item) for item in raw]
            except SemanticDecisionError as exc:
                raise PowdrrExecutionError(str(exc)) from exc

        def semantic_extractions(raw: Any) -> list[BoundSourceExtraction]:
            if not isinstance(raw, list) or not all(
                isinstance(item, Mapping) for item in raw
            ):
                raise PowdrrExecutionError("source extractions are malformed")
            try:
                return [BoundSourceExtraction.from_data(item) for item in raw]
            except SemanticContractError as exc:
                raise PowdrrExecutionError(str(exc)) from exc

        def semantic_contract(raw: Any) -> PartialSemanticContract:
            if not isinstance(raw, Mapping):
                raise PowdrrExecutionError("semantic contract is malformed")
            try:
                return PartialSemanticContract.from_data(raw)
            except SemanticContractError as exc:
                raise PowdrrExecutionError(str(exc)) from exc

        def semantic_inventory(raw: Any) -> RepositoryInventory:
            if not isinstance(raw, Mapping):
                raise PowdrrExecutionError("repository inventory is malformed")
            try:
                return RepositoryInventory.from_data(raw)
            except InventoryError as exc:
                raise PowdrrExecutionError(str(exc)) from exc

        def semantic_query(raw: Any) -> LookupQuery:
            if not isinstance(raw, Mapping):
                raise PowdrrExecutionError("subject lookup query is malformed")
            try:
                return LookupQuery.from_data(raw)
            except InventoryError as exc:
                raise PowdrrExecutionError(str(exc)) from exc

        def semantic_candidates(raw: Any) -> CandidateSet:
            if not isinstance(raw, Mapping):
                raise PowdrrExecutionError("candidate set is malformed")
            try:
                return CandidateSet.from_data(raw)
            except InventoryError as exc:
                raise PowdrrExecutionError(str(exc)) from exc

        def prepare_source_semantic_decisions_operation() -> Any:
            try:
                return prepare_source_semantic_decisions(semantic_clause())
            except SemanticContractError as exc:
                raise PowdrrExecutionError(str(exc)) from exc

        def prepare_dependent_source_semantic_decisions_operation() -> Any:
            try:
                return prepare_dependent_source_semantic_decisions(
                    semantic_clause(), semantic_decisions(parameters.get("decisions"))
                )
            except (SemanticContractError, SemanticDecisionError) as exc:
                raise PowdrrExecutionError(str(exc)) from exc

        def build_semantic_repository_inventory_operation() -> Any:
            cached = state.get("semantic_repository_inventory")
            if isinstance(cached, Mapping):
                return dict(cached)
            try:
                bootstrap_path = output_root / "validation-bootstrap.yaml"
                if bootstrap_path.exists():
                    bootstrap = feature_endpoint._load_yaml_mapping(bootstrap_path)
                    source_subjects = bootstrap.get("source_subjects")
                    if not isinstance(source_subjects, list) or not all(
                        isinstance(item, Mapping) for item in source_subjects
                    ):
                        raise InventoryError(
                            "Structrr bootstrap has malformed source_subjects"
                        )
                    if source_subjects:
                        inventory = inventory_from_source_subjects(
                            source_subjects,
                            commit_ref="working-tree",
                            structrr_revision=str(
                                bootstrap.get("snapshot_digest", "structrr:bootstrap")
                            ),
                        )
                    else:
                        inventory = build_inventory(
                            worktree,
                            commit_ref="working-tree",
                            structrr_revision="structrr:current",
                        )
                else:
                    inventory = build_inventory(
                        worktree,
                        commit_ref="working-tree",
                        structrr_revision="structrr:current",
                    )
                data = inventory.to_data()
                state["semantic_repository_inventory"] = data
                return data
            except (InventoryError, OSError) as exc:
                raise PowdrrExecutionError(str(exc)) from exc

        def prepare_subject_lookup_query_operation() -> Any:
            try:
                inventory = semantic_inventory(parameters.get("inventory"))
                query = prepare_subject_lookup_query(
                    semantic_contract(parameters.get("contract")), inventory
                )
                return query.to_data()
            except (InventoryError, SemanticContractError) as exc:
                raise PowdrrExecutionError(str(exc)) from exc

        def retrieve_subject_candidates_operation() -> Any:
            try:
                inventory = semantic_inventory(parameters.get("inventory"))
                candidates = retrieve_subject_candidates(
                    semantic_query(parameters.get("query")), inventory
                )
                return candidates.to_data()
            except InventoryError as exc:
                raise PowdrrExecutionError(str(exc)) from exc

        def prepare_candidate_relation_decisions_operation() -> Any:
            try:
                requests = prepare_candidate_relation_decisions(
                    semantic_query(parameters.get("query")),
                    semantic_candidates(parameters.get("candidates")),
                )
                return {"requests": requests}
            except InventoryError as exc:
                raise PowdrrExecutionError(str(exc)) from exc

        def bind_candidate_relation_decisions_operation() -> Any:
            requests = parameters.get("requests")
            raw_results = feature_endpoint._collected_results(parameters.get("results"))
            if not isinstance(requests, list) or raw_results is None:
                raise PowdrrExecutionError("candidate relation binding is malformed")
            try:
                decisions = bind_candidate_relation_decisions(requests, raw_results)
                return {"decisions": [decision.to_data() for decision in decisions]}
            except (InventoryError, SemanticDecisionError) as exc:
                raise PowdrrExecutionError(str(exc)) from exc

        def finalize_subject_binding_operation() -> Any:
            raw_decisions = parameters.get("decisions")
            if not isinstance(raw_decisions, list):
                raise PowdrrExecutionError("candidate relation decisions are malformed")
            try:
                decisions = [SemanticDecision.from_data(item) for item in raw_decisions]
                candidates = semantic_candidates(parameters.get("candidates"))
                result = finalize_subject_binding(
                    candidates,
                    decisions,
                    quantifier=str(parameters.get("quantifier")),
                )
                return {
                    **result,
                    "candidate_ids": [
                        item.record.inventory_id for item in candidates.candidates
                    ],
                    "retrieval_status": candidates.retrieval_status,
                }
            except (InventoryError, SemanticDecisionError) as exc:
                raise PowdrrExecutionError(str(exc)) from exc

        def prepare_repository_subject_binding_operation() -> Any:
            try:
                inventory = semantic_inventory(
                    build_semantic_repository_inventory_operation()
                )
                context = state.get("semantic_repository_lookup_context")
                if not isinstance(context, StructrrLookupContext):
                    context = build_python_lookup_context(worktree, inventory)
                    state["semantic_repository_lookup_context"] = context
                contract = semantic_contract(parameters.get("contract"))
                ledger = load_instruction_ledger()
                clause = next(
                    (
                        item
                        for item in ledger.clauses
                        if item.clause_id == contract.source_ref
                    ),
                    None,
                )
                contextual_names = (
                    infer_contextual_qualified_names(
                        ledger.source.text,
                        clause.source_span[0],
                        contract.proposition_text,
                    )
                    if clause is not None
                    else ()
                )
                query = prepare_subject_lookup_query(
                    contract,
                    inventory,
                    explicit_names=contextual_names,
                    structrr_context_fingerprint=context.fingerprint,
                )
                candidates = retrieve_subject_candidates(query, inventory, context)
                requests = add_candidate_source_excerpts(
                    prepare_candidate_relation_decisions(query, candidates, context),
                    worktree,
                )
                return {
                    "query": query.to_data(),
                    "candidates": candidates.to_data(),
                    "requests": requests,
                }
            except (InventoryError, SemanticContractError, OSError) as exc:
                raise PowdrrExecutionError(str(exc)) from exc

        def finalize_repository_subject_binding_operation() -> Any:
            requests = parameters.get("requests")
            raw_results = feature_endpoint._collected_results(parameters.get("results"))
            if (
                not isinstance(requests, list)
                or not all(isinstance(item, Mapping) for item in requests)
                or raw_results is None
            ):
                raise PowdrrExecutionError("repository subject review is malformed")
            try:
                candidates = semantic_candidates(parameters.get("candidates"))
                decisions = bind_candidate_relation_decisions(requests, raw_results)
                raw_query = parameters.get("query")
                subject_text = (
                    LookupQuery.from_data(raw_query).subject_text
                    if isinstance(raw_query, Mapping)
                    else ""
                )
                result = finalize_subject_binding(
                    candidates,
                    decisions,
                    quantifier=str(parameters.get("quantifier")),
                    subject_text=subject_text,
                )
                return {
                    **result,
                    "candidate_ids": [
                        item.record.inventory_id for item in candidates.candidates
                    ],
                    "retrieval_status": candidates.retrieval_status,
                }
            except (InventoryError, SemanticDecisionError) as exc:
                raise PowdrrExecutionError(str(exc)) from exc

        def enumerate_subject_population_operation() -> Any:
            member_ids = parameters.get("member_ids")
            if not isinstance(member_ids, list) or not all(
                isinstance(item, str) for item in member_ids
            ):
                raise PowdrrExecutionError("population member IDs are malformed")
            try:
                return enumerate_population(
                    semantic_inventory(parameters.get("inventory")),
                    population_ref=str(parameters.get("population_ref")),
                    membership_rule_ref=str(parameters.get("membership_rule_ref")),
                    member_ids=member_ids,
                    complete=bool(parameters.get("complete")),
                ).to_data()
            except InventoryError as exc:
                raise PowdrrExecutionError(str(exc)) from exc

        def bind_source_semantic_decisions_operation() -> Any:
            clause = semantic_clause()
            plan = parameters.get("plan")
            raw_results = feature_endpoint._collected_results(parameters.get("results"))
            if not isinstance(plan, Mapping) or raw_results is None:
                raise PowdrrExecutionError("semantic decision binding is malformed")
            resolved = plan.get("resolved_decisions")
            pending = plan.get("pending_specs")
            if not isinstance(resolved, list) or not isinstance(pending, list):
                raise PowdrrExecutionError("semantic decision plan is malformed")
            try:
                decisions = bind_source_semantic_decisions(
                    resolved_decisions=resolved,
                    pending_specs=pending,
                    provider_results=raw_results,
                    benchmark_mode=benchmark_mode(),
                )
            except (SemanticContractError, SemanticDecisionError) as exc:
                raise PowdrrExecutionError(str(exc)) from exc
            data = [item.to_data() for item in decisions]
            path = semantic_artifact_directory(clause) / "source-decisions.json"
            path.write_text(
                json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            return {"path": str(path), "decisions": data}

        def prepare_source_extractions_operation() -> Any:
            try:
                requests = prepare_source_extractions(
                    semantic_clause(), semantic_decisions(parameters.get("decisions"))
                )
            except SemanticContractError as exc:
                raise PowdrrExecutionError(str(exc)) from exc
            return {"requests": requests}

        def bind_source_extractions_operation() -> Any:
            clause = semantic_clause()
            requests = parameters.get("requests")
            raw_results = feature_endpoint._collected_results(parameters.get("results"))
            if not isinstance(requests, list) or raw_results is None:
                raise PowdrrExecutionError("source extraction binding is malformed")
            try:
                extractions = bind_source_extractions(
                    requests=requests,
                    provider_results=raw_results,
                    benchmark_mode=benchmark_mode(),
                )
            except SemanticContractError as exc:
                raise PowdrrExecutionError(str(exc)) from exc
            data = [item.to_data() for item in extractions]
            path = semantic_artifact_directory(clause) / "source-extractions.json"
            path.write_text(
                json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            return {"path": str(path), "extractions": data}

        def compile_deterministic_source_extractions_operation() -> Any:
            clause = semantic_clause()
            source_text = parameters.get("source_text")
            if not isinstance(source_text, str):
                source_text = load_instruction_ledger().source.text
            try:
                extractions = compile_deterministic_source_extractions(
                    clause,
                    semantic_decisions(parameters.get("decisions")),
                    source_text,
                )
            except SemanticContractError as exc:
                raise PowdrrExecutionError(str(exc)) from exc
            data = [item.to_data() for item in extractions]
            path = semantic_artifact_directory(clause) / "source-extractions.json"
            path.write_text(
                json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            return {"path": str(path), "extractions": data}

        def prepare_behavior_family_decision_operation() -> Any:
            extractions = semantic_extractions(parameters.get("extractions"))
            behaviors = [
                item for item in extractions if item.extraction_kind == "behavior"
            ]
            if len(behaviors) != 1:
                raise PowdrrExecutionError(
                    "behavior-family classification requires one behavior extraction"
                )
            try:
                return prepare_behavior_family_decision(
                    semantic_clause(),
                    behaviors[0],
                    semantic_decisions(parameters.get("decisions")),
                )
            except SemanticContractError as exc:
                raise PowdrrExecutionError(str(exc)) from exc

        def compile_partial_semantic_contract_operation() -> Any:
            clause = semantic_clause()
            family_request = parameters.get("behavior_family_request")
            family_result = parameters.get("behavior_family_result")
            if not isinstance(family_request, Mapping) or not isinstance(
                family_result, Mapping
            ):
                raise PowdrrExecutionError("behavior-family decision is malformed")
            try:
                family = bind_behavior_family_decision(
                    family_request,
                    family_result,
                    benchmark_mode=benchmark_mode(),
                )
                contract = compile_source_contract(
                    clause=clause,
                    decisions=semantic_decisions(parameters.get("decisions")),
                    extractions=semantic_extractions(parameters.get("extractions")),
                    behavior_family=family,
                )
            except (SemanticContractError, SemanticDecisionError) as exc:
                raise PowdrrExecutionError(str(exc)) from exc
            path = semantic_artifact_directory(clause) / "partial-contract.json"
            document = contract.to_data()
            path.write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return project_partial_contract_to_legacy_design(contract) | {
                "partial_contract_path": str(path),
                "partial_contract_fingerprint": contract.fingerprint,
                "partial_contract": document,
            }

        def prepare_field_entailment_reviews_operation() -> Any:
            contract = semantic_contract(parameters.get("contract"))
            try:
                return {"requests": prepare_field_entailment_reviews(contract)}
            except (SemanticContractError, FaithfulnessError) as exc:
                raise PowdrrExecutionError(str(exc)) from exc

        def bind_field_entailment_reviews_operation() -> Any:
            requests = parameters.get("requests")
            raw_results = feature_endpoint._collected_results(parameters.get("results"))
            if not isinstance(requests, list) or raw_results is None:
                raise PowdrrExecutionError("field entailment binding is malformed")
            try:
                reviews = bind_field_entailment_reviews(
                    requests=requests,
                    provider_results=raw_results,
                    benchmark_mode=benchmark_mode(),
                )
            except (
                SemanticContractError,
                FaithfulnessError,
                SemanticDecisionError,
            ) as exc:
                raise PowdrrExecutionError(str(exc)) from exc
            return {"reviews": [item.to_data() for item in reviews]}

        def finalize_source_faithfulness_operation() -> Any:
            contract = semantic_contract(parameters.get("contract"))
            raw_reviews = parameters.get("reviews")
            if not isinstance(raw_reviews, list):
                raise PowdrrExecutionError("field entailment reviews are malformed")
            try:
                reviews = []
                for raw in raw_reviews:
                    if not isinstance(raw, Mapping):
                        raise FaithfulnessError("field entailment review is malformed")
                    spec_raw = raw.get("spec")
                    decision_raw = raw.get("decision")
                    if not isinstance(spec_raw, Mapping) or not isinstance(
                        decision_raw, Mapping
                    ):
                        raise FaithfulnessError("field entailment review is incomplete")
                    reviews.append(
                        FieldEntailmentReview(
                            spec=FieldEntailmentSpec.from_data(spec_raw),
                            decision=SemanticDecision.from_data(decision_raw),
                        )
                    )
                outcome = finalize_source_faithfulness(contract, reviews)
                if not outcome.get("accepted", False):
                    if config is None or not getattr(config, "benchmark_mode", False):
                        raise PowdrrExecutionError(
                            "source-faithfulness gate failed: "
                            + json.dumps(outcome, sort_keys=True)
                        )
                    fallback = record_benchmark_invariant_fallback(
                        {
                            "clause_id": contract.source_ref,
                            "text": contract.proposition_text,
                        },
                        reason="source-faithfulness gate rejected the derived design",
                        details=outcome,
                        partial_contract=contract.to_data(),
                    )
                    return {
                        "accepted": True,
                        "unresolved_fields": list(outcome.get("unresolved_fields", [])),
                        "findings": [],
                        "fallback": {
                            "kind": "invariant",
                            "artifact": str(
                                output_root
                                / "semantic-contracts"
                                / contract.source_ref
                                / "invariant-fallback.json"
                            ),
                            "reason": fallback["reason"],
                        },
                    }
                return outcome
            except (
                SemanticContractError,
                FaithfulnessError,
                SemanticDecisionError,
            ) as exc:
                raise PowdrrExecutionError(str(exc)) from exc

        def compile_canonical_feature_design_operation() -> Any:
            ledger = load_instruction_ledger()
            raw_design_decisions = feature_endpoint._collected_results(
                parameters.get("design_decisions")
            )
            raw_design_entries = parameters.get("design_decisions")
            if raw_design_decisions is None:
                raise PowdrrExecutionError(
                    "canonical design decisions are missing or malformed"
                )
            consistency_review = parameters.get("scenario_consistency_review")
            if not isinstance(consistency_review, Mapping):
                raise PowdrrExecutionError(
                    "scenario consistency review is missing or malformed"
                )
            raw_design_decisions = _apply_scenario_consistency_updates(
                raw_design_decisions,
                consistency_review,
                benchmark_mode=benchmark_mode(),
            )
            (output_root / "scenario-consistency-review.json").write_text(
                json.dumps(dict(consistency_review), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            work_item_name = feature_endpoint._require_flow_text(
                parameters, "work_item_name"
            )
            decisions_by_clause_id = {
                str(entry.get("item", {}).get("clause_id")): entry.get("result", {})
                for entry in (raw_design_entries or [])
                if isinstance(entry, Mapping)
                and isinstance(entry.get("item"), Mapping)
                and isinstance(entry.get("result"), Mapping)
                and isinstance(entry.get("item", {}).get("clause_id"), str)
            }
            decisions_by_clause_id.update(
                {
                    clause.clause_id: decision
                    for clause, decision in zip(
                        ledger.clauses, raw_design_decisions, strict=True
                    )
                    if clause.clause_id not in decisions_by_clause_id
                    and isinstance(decision, Mapping)
                }
            )
            coverage_path = output_root / "instruction-coverage-audit.json"
            source_records: list[dict[str, Any]] = []
            source_errors: list[str] = []
            for clause in ledger.clauses:
                decision = decisions_by_clause_id.get(clause.clause_id)
                raw_contract = (
                    decision.get("partial_contract")
                    if isinstance(decision, Mapping)
                    else None
                )
                source_record: dict[str, Any] = {
                    "clause_id": clause.clause_id,
                    "clause_fingerprint": clause.fingerprint,
                    "source_span": {
                        "start": clause.source_span[0],
                        "end": clause.source_span[1],
                    },
                }
                try:
                    if not isinstance(raw_contract, Mapping):
                        raise ValueError("source semantic contract is missing")
                    contract = PartialSemanticContract.from_data(raw_contract)
                    contract_artifact = (
                        output_root
                        / "semantic-contracts"
                        / clause.clause_id
                        / "partial-contract.json"
                    )
                    if not contract_artifact.is_file():
                        raise ValueError("source semantic contract artifact is missing")
                    persisted_contract = json.loads(
                        contract_artifact.read_text(encoding="utf-8")
                    )
                    if not isinstance(persisted_contract, Mapping):
                        raise ValueError(
                            "persisted source semantic contract is malformed"
                        )
                    if (
                        PartialSemanticContract.from_data(
                            persisted_contract
                        ).fingerprint
                        != contract.fingerprint
                    ):
                        raise ValueError("persisted source semantic contract is stale")
                    source_record.update(
                        {
                            "source_contract_id": contract.contract_id,
                            "source_contract_fingerprint": contract.fingerprint,
                            "source_contract_artifact": str(contract_artifact),
                            "routing": contract.routing,
                            "disposition": contract.disposition,
                        }
                    )
                    if contract.source_ref != clause.clause_id:
                        raise ValueError("source reference does not match clause")
                    if contract.source_fingerprint != clause.fingerprint:
                        raise ValueError("source fingerprint does not match clause")
                    if contract.proposition_text != clause.text:
                        raise ValueError("source text does not match clause")
                    _kind_from_semantic_contract(contract, clause.clause_id)
                    source_record["status"] = "source_validated"
                except (TypeError, ValueError, SemanticContractError) as exc:
                    source_record["status"] = "failed"
                    source_record["error"] = str(exc)
                    source_errors.append(f"{clause.clause_id}: {exc}")
                source_records.append(source_record)
            if source_errors:
                coverage_path.write_text(
                    json.dumps(
                        {
                            "schema_version": "instruction-coverage-audit-v1",
                            "instruction_ledger_fingerprint": ledger.fingerprint,
                            "instruction_ledger_artifact": str(
                                state["instruction_ledger_path"]
                            ),
                            "status": "failed",
                            "records": source_records,
                            "errors": source_errors,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                raise PowdrrExecutionError(
                    "instruction source audit failed: "
                    + json.dumps(source_errors, sort_keys=True)
                )
            try:
                design = compile_feature_design(
                    ledger,
                    work_item_name,
                    raw_design_decisions,
                )
            except FeatureObligationError as exc:
                for record in source_records:
                    record["status"] = "design_compilation_failed"
                    record["error"] = str(exc)
                coverage_path.write_text(
                    json.dumps(
                        {
                            "schema_version": "instruction-coverage-audit-v1",
                            "instruction_ledger_fingerprint": ledger.fingerprint,
                            "instruction_ledger_artifact": str(
                                state["instruction_ledger_path"]
                            ),
                            "status": "failed",
                            "records": source_records,
                            "errors": [str(exc)],
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                raise PowdrrExecutionError(str(exc)) from exc
            obligation_clause_ids = {item.clause_id for item in design.obligations}
            coverage_records: list[dict[str, Any]] = []
            coverage_errors: list[str] = []
            for clause in ledger.clauses:
                decision = decisions_by_clause_id.get(clause.clause_id)
                raw_contract = (
                    decision.get("partial_contract")
                    if isinstance(decision, Mapping)
                    else None
                )
                final_record: dict[str, Any] = {
                    "clause_id": clause.clause_id,
                    "clause_fingerprint": clause.fingerprint,
                    "source_span": {
                        "start": clause.source_span[0],
                        "end": clause.source_span[1],
                    },
                    "obligation_created": clause.clause_id in obligation_clause_ids,
                }
                if not isinstance(raw_contract, Mapping):
                    final_record["status"] = "failed"
                    final_record["error"] = "source semantic contract is missing"
                    coverage_errors.append(
                        f"{clause.clause_id}: source contract missing"
                    )
                else:
                    try:
                        contract = PartialSemanticContract.from_data(raw_contract)
                        expected_kind = _kind_from_semantic_contract(
                            contract, clause.clause_id
                        )
                        design_kind = next(
                            item.kind
                            for item in design.projections
                            if item.clause_id == clause.clause_id
                        )
                        final_record.update(
                            {
                                "source_contract_id": contract.contract_id,
                                "source_contract_fingerprint": contract.fingerprint,
                                "source_contract_source_fingerprint": (
                                    contract.source_fingerprint
                                ),
                                "source_contract_artifact": str(
                                    output_root
                                    / "semantic-contracts"
                                    / clause.clause_id
                                    / "partial-contract.json"
                                ),
                                "routing": contract.routing,
                                "disposition": contract.disposition,
                                "design_kind": design_kind,
                            }
                        )
                        expected_obligation = contract.routing in {
                            "include",
                            "include_prohibition",
                        }
                        if contract.source_ref != clause.clause_id:
                            raise ValueError("source reference does not match clause")
                        if contract.source_fingerprint != clause.fingerprint:
                            raise ValueError("source fingerprint does not match clause")
                        if contract.proposition_text != clause.text:
                            raise ValueError("source text does not match clause")
                        allowed_design_kinds = {expected_kind}
                        if contract.routing == "exclude":
                            allowed_design_kinds.add("nonactionable")
                        if design_kind not in allowed_design_kinds:
                            raise ValueError(
                                "compiled design kind does not match source disposition"
                            )
                        if expected_obligation != final_record["obligation_created"]:
                            raise ValueError(
                                "source route and compiled obligation coverage disagree"
                            )
                        final_record["status"] = "covered"
                    except (StopIteration, TypeError, ValueError) as exc:
                        final_record["status"] = "failed"
                        final_record["error"] = str(exc)
                        coverage_errors.append(f"{clause.clause_id}: {exc}")
                coverage_records.append(final_record)
            coverage_path = output_root / "instruction-coverage-audit.json"
            coverage_path.write_text(
                json.dumps(
                    {
                        "schema_version": "instruction-coverage-audit-v1",
                        "instruction_ledger_fingerprint": ledger.fingerprint,
                        "instruction_ledger_artifact": str(
                            state["instruction_ledger_path"]
                        ),
                        "status": "failed" if coverage_errors else "complete",
                        "records": coverage_records,
                        "errors": coverage_errors,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            if coverage_errors:
                raise PowdrrExecutionError(
                    "instruction coverage audit failed: "
                    + json.dumps(coverage_errors, sort_keys=True)
                )
            repository_bindings_by_clause = {
                clause_id: decision["repository_binding"]
                for clause_id, decision in decisions_by_clause_id.items()
                if isinstance(decision, Mapping)
                and isinstance(decision.get("repository_binding"), Mapping)
            }
            scenarios_by_clause_id = {
                clause.clause_id: decision.get("behavior_scenario")
                for clause, decision in zip(
                    ledger.clauses, raw_design_decisions, strict=True
                )
                if isinstance(decision, Mapping)
                and isinstance(decision.get("behavior_scenario"), Mapping)
            }
            evidence_by_clause: dict[str, dict[str, Any]] = {}
            evidence_provenance_by_clause: dict[str, dict[str, Any]] = {}
            evidence_provenance_path = (
                output_root / "obligation-evidence-provenance.json"
            )

            def write_evidence_provenance() -> None:
                evidence_provenance_path.write_text(
                    json.dumps(
                        {
                            "schema_version": "obligation-evidence-provenance-v1",
                            "instruction_ledger_fingerprint": ledger.fingerprint,
                            "records": list(evidence_provenance_by_clause.values()),
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )

            for item in design.obligations:
                decision = decisions_by_clause_id.get(item.clause_id)
                partial = (
                    decision.get("partial_contract")
                    if isinstance(decision, Mapping)
                    else None
                )
                partial_contract_path = (
                    output_root
                    / "semantic-contracts"
                    / item.clause_id
                    / "partial-contract.json"
                )
                provenance = _obligation_evidence_provenance_record(
                    obligation_id=item.obligation_id,
                    clause_id=item.clause_id,
                    design_kind=item.projection.kind,
                    partial_contract_path=partial_contract_path,
                    partial_contract=partial,
                )
                evidence_provenance_by_clause[item.clause_id] = provenance
                if not isinstance(partial, Mapping):
                    provenance["status"] = "failed"
                    provenance["error"] = "source semantic contract is missing"
                    write_evidence_provenance()
                    raise PowdrrExecutionError(
                        "obligation evidence compilation failed: "
                        + json.dumps(provenance, sort_keys=True)
                    )
                try:
                    evidence_contract = compile_obligation_evidence_contract(
                        obligation_id=item.obligation_id,
                        clause_id=item.clause_id,
                        requirement_strength=str(
                            partial.get("requirement_strength", "")
                        ),
                        kind=item.projection.kind,
                        polarity=str(partial.get("polarity", "")),
                    )
                except ValueError as exc:
                    provenance["status"] = "failed"
                    provenance["error"] = str(exc)
                    write_evidence_provenance()
                    raise PowdrrExecutionError(
                        "obligation evidence compilation failed: "
                        + json.dumps(provenance, sort_keys=True)
                    ) from exc
                evidence_by_clause[item.clause_id] = evidence_contract.to_data()
                provenance["status"] = "compiled"
                provenance["evidence_contract_fingerprint"] = (
                    evidence_contract.to_data()["fingerprint"]
                )
                write_evidence_provenance()
            try:
                assert_obligation_evidence_complete(
                    tuple(
                        ObligationEvidenceContract.from_data(raw)
                        for raw in evidence_by_clause.values()
                    ),
                    tuple(item.obligation_id for item in design.obligations),
                )
            except ValueError as exc:
                raise PowdrrExecutionError(str(exc)) from exc
            obligations = []
            for index, item in enumerate(design.obligations, start=1):
                projection = item.projection.to_data()
                projection["evidence_contract"] = evidence_by_clause[item.clause_id]
                repository_binding = repository_bindings_by_clause.get(item.clause_id)
                if repository_binding is not None:
                    projection["repository_binding"] = dict(repository_binding)
                obligations.append(
                    {
                        "id": f"sentence-{index}",
                        "description": item.projection.description,
                        "design": projection,
                    }
                )
            semantic_cases = [
                {
                    "id": f"test-sentence-{index}",
                    "description": item.projection.expected_test,
                    "intent_refs": [f"feature-obligation-sentence-{index}"],
                    "expected_outcome": item.projection.acceptance_criterion,
                    "test_selection": feature_endpoint._select_matching_test_inventory(
                        item.projection.expected_test,
                        state.get("provider_inventory", ()),
                    ),
                    "behavior_scenario": scenarios_by_clause_id.get(item.clause_id),
                }
                for index, item in enumerate(design.obligations, start=1)
            ]
            if any(
                not isinstance(item.get("behavior_scenario"), Mapping)
                for item in semantic_cases
            ):
                raise PowdrrExecutionError(
                    "every actionable feature obligation requires one typed "
                    "behavior scenario"
                )
            required_test_cases = feature_endpoint._compile_required_test_case_edits(
                semantic_cases,
                tuple(state.get("provider_inventory", ())),
                validation_profiles=tuple(state.get("validation_profiles", ())),
                include_existing_name_hint=True,
            )
            verification_contracts = []
            for item in design.obligations:
                decision = decisions_by_clause_id.get(item.clause_id, {})
                verification_contracts.append(
                    {
                        "id": f"test:{item.obligation_id}",
                        "obligation_ref": item.obligation_id,
                        "population": str(decision.get("population", "")),
                        "operation": str(decision.get("operation", "")),
                        "oracle": str(decision.get("oracle", "")),
                        "evidence_case": str(decision.get("evidence_case", "")),
                    }
                )
            canonical_document = design.to_data()
            for canonical_obligation in canonical_document["obligations"]:
                clause_id = canonical_obligation.get("clause_id")
                if isinstance(clause_id, str):
                    canonical_obligation["evidence_contract"] = evidence_by_clause[
                        clause_id
                    ]
                    repository_binding = repository_bindings_by_clause.get(clause_id)
                    if repository_binding is not None:
                        canonical_obligation["repository_binding"] = dict(
                            repository_binding
                        )
            for projection in canonical_document["projections"]:
                clause_id = projection.get("clause_id")
                if isinstance(clause_id, str):
                    repository_binding = repository_bindings_by_clause.get(clause_id)
                    if repository_binding is not None:
                        projection["repository_binding"] = dict(repository_binding)
            canonical_document["verification_contracts"] = verification_contracts
            (output_root / "repository-subject-bindings.json").write_text(
                json.dumps(
                    {
                        "schema_version": "repository-subject-bindings-v1",
                        "bindings": repository_bindings_by_clause,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            path = output_root / "canonical-feature-design.json"
            path.write_text(
                json.dumps(canonical_document, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            normative_assumptions = [
                {"clause_id": clause_id, **dict(assumption)}
                for clause_id, scenario in scenarios_by_clause_id.items()
                if isinstance(scenario, Mapping)
                for assumption in scenario.get("assumptions", ())
                if isinstance(assumption, Mapping)
            ]
            assumptions_path = output_root / "normative-assumptions.json"
            assumptions_path.write_text(
                json.dumps(
                    {
                        "schema_version": "normative-assumptions-v1",
                        "benchmark_mode": benchmark_mode(),
                        "assumptions": normative_assumptions,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            compatibility_packets = {
                "packets": [
                    {
                        "obligation_id": f"obligation:{index:03d}",
                        "description": item["description"],
                        "evidence_refs": [
                            "canonical:"
                            + str(
                                cast(Any, item.get("design", {})).get("clause_id", "")
                            )
                            if isinstance(cast(Any, item.get("design")), Mapping)
                            else "canonical:unknown"
                        ],
                    }
                    for index, item in enumerate(obligations, start=1)
                ]
            }
            (output_root / "obligation-review-packets.json").write_text(
                json.dumps(compatibility_packets, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            if config is not None and getattr(config, "design_only", False):
                prompt_path = feature_endpoint._compile_design_only_prompt(
                    config=config,
                    canonical_design=canonical_document,
                    required_test_cases=required_test_cases,
                    base_commit=feature_endpoint._git_output(
                        runner, worktree, ["git", "rev-parse", "HEAD"]
                    ),
                    validation_profiles=tuple(state.get("validation_profiles", ())),
                    existing_tests=tuple(state.get("provider_inventory", ())),
                    output_root=output_root,
                )
                state["implementation_prompt_path"] = prompt_path
                state["structrr_diff_path"] = output_root / "structrr-diff.yaml"
            state["canonical_feature_design_path"] = path
            state["feature_obligations_path"] = path
            state["normative_assumptions_path"] = assumptions_path
            return {
                "path": str(path),
                "fingerprint": content_fingerprint(canonical_document),
                "obligations": obligations,
                "verification_contracts": verification_contracts,
                "required_test_cases": required_test_cases,
            }

        def apply_sentence_design_trace() -> Any:
            return feature_endpoint._apply_sentence_design_trace(
                parameters, state=state
            )

        def materialize_feature_intents() -> Any:
            return feature_endpoint._materialize_feature_intents(
                parameters, state=state
            )

        def compile_verification_obligations() -> Any:
            return feature_endpoint._compile_verification_obligations(
                parameters,
                worktree=worktree,
                output_root=output_root,
                state=state,
                allowed_paths=config.allowed_paths,
            )

        def assert_verification_obligations_complete() -> Any:
            compilation = parameters.get("verification_obligations")
            if not isinstance(compilation, Mapping):
                raise PowdrrExecutionError(
                    "verification obligation assertion requires compiler output"
                )
            failures = compilation.get("failures")
            if not isinstance(failures, list):
                raise PowdrrExecutionError(
                    "verification obligation compiler output has no failure list"
                )
            return {"passed": not failures, "failure_count": len(failures)}

        def bind_handler(
            handler: Callable[[], Any],
        ) -> Callable[[Mapping[str, Any]], Any]:
            def dispatch(_parameters: Mapping[str, Any]) -> Any:
                return handler()

            return dispatch

        runtime_catalog = feature_command_catalog(
            implementations={
                "extract_proposal_issues": bind_handler(extract_proposal_issues),
                "aggregate_category_edits": bind_handler(aggregate_category_edits),
                "decompose_feature_description": bind_handler(
                    decompose_feature_description
                ),
                "compile_instruction_ledger": bind_handler(
                    compile_instruction_ledger_operation
                ),
                "prepare_atomicity_split_requests": bind_handler(
                    prepare_atomicity_split_requests_operation
                ),
                "apply_atomicity_splits": bind_handler(
                    apply_atomicity_splits_operation
                ),
                "prepare_source_semantic_decisions": bind_handler(
                    prepare_source_semantic_decisions_operation
                ),
                "prepare_dependent_source_semantic_decisions": bind_handler(
                    prepare_dependent_source_semantic_decisions_operation
                ),
                "bind_source_semantic_decisions": bind_handler(
                    bind_source_semantic_decisions_operation
                ),
                "prepare_source_extractions": bind_handler(
                    prepare_source_extractions_operation
                ),
                "bind_source_extractions": bind_handler(
                    bind_source_extractions_operation
                ),
                "compile_deterministic_source_extractions": bind_handler(
                    compile_deterministic_source_extractions_operation
                ),
                "prepare_behavior_family_decision": bind_handler(
                    prepare_behavior_family_decision_operation
                ),
                "compile_partial_semantic_contract": bind_handler(
                    compile_partial_semantic_contract_operation
                ),
                "prepare_field_entailment_reviews": bind_handler(
                    prepare_field_entailment_reviews_operation
                ),
                "bind_field_entailment_reviews": bind_handler(
                    bind_field_entailment_reviews_operation
                ),
                "finalize_source_faithfulness": bind_handler(
                    finalize_source_faithfulness_operation
                ),
                "build_semantic_repository_inventory": bind_handler(
                    build_semantic_repository_inventory_operation
                ),
                "prepare_subject_lookup_query": bind_handler(
                    prepare_subject_lookup_query_operation
                ),
                "retrieve_subject_candidates": bind_handler(
                    retrieve_subject_candidates_operation
                ),
                "prepare_candidate_relation_decisions": bind_handler(
                    prepare_candidate_relation_decisions_operation
                ),
                "bind_candidate_relation_decisions": bind_handler(
                    bind_candidate_relation_decisions_operation
                ),
                "finalize_subject_binding": bind_handler(
                    finalize_subject_binding_operation
                ),
                "prepare_repository_subject_binding": bind_handler(
                    prepare_repository_subject_binding_operation
                ),
                "finalize_repository_subject_binding": bind_handler(
                    finalize_repository_subject_binding_operation
                ),
                "enumerate_subject_population": bind_handler(
                    enumerate_subject_population_operation
                ),
                "merge_semantic_design": bind_handler(merge_semantic_design_operation),
                "merge_behavior_scenario": bind_handler(
                    merge_behavior_scenario_operation
                ),
                "compile_canonical_feature_design": bind_handler(
                    compile_canonical_feature_design_operation
                ),
                "apply_sentence_design_trace": bind_handler(
                    apply_sentence_design_trace
                ),
                "materialize_feature_intents": bind_handler(
                    materialize_feature_intents
                ),
                "compile_verification_obligations": bind_handler(
                    compile_verification_obligations
                ),
                "assert_verification_obligations_complete": bind_handler(
                    assert_verification_obligations_complete
                ),
            }
        )
        spec = runtime_catalog.get(name)
        if spec is not None and spec.logic is not None:
            return runtime_catalog.dispatch(
                name,
                {key: value for key, value in parameters.items() if key != "command"},
            )
        if len(command) != 1:
            raise PowdrrExecutionError("feature flow operation command is malformed")
        if name == "plan_structrr_diff":
            baseline = feature_endpoint._require_flow_text(parameters, "baseline")
            if baseline != str(state["baseline_path"]):
                raise PowdrrExecutionError(
                    "planning baseline does not match ensured baseline"
                )
            plan_config = replace(
                config,
                work_item_name=feature_endpoint._require_flow_text(
                    parameters, "work_item_name"
                ),
                feature_description=feature_endpoint._require_flow_text(
                    parameters, "feature_description"
                ),
            )
            state["plan_path"] = feature_endpoint._write_structrr_plan(
                worktree,
                plan_config,
                interview_input=parameters.get(
                    "feature_design", parameters.get("interview_input")
                ),
                inventory=tuple(state.get("provider_inventory", ())),
                validation_profiles=tuple(state.get("validation_profiles", ())),
            )
            feature_endpoint._commit(runner, worktree, "Record Structrr feature diff")
            return {"path": str(state["plan_path"])}
        if name == "prepare_proposal_review":
            return feature_endpoint._prepare_proposal_review(
                config,
                runner=runner,
                worktree=worktree,
                output_root=output_root,
                slug=slug,
                state=state,
                parameters=parameters,
            )
        if name == "finalize_proposal_review":
            review = feature_endpoint._finalize_proposal_review(
                worktree=worktree,
                output_root=output_root,
                parameters=parameters,
            )
            state["proposal_review_receipt_path"] = review["receipt_path"]
            return review
        if name == "bind_proposal_decision_results":
            return feature_endpoint._bind_proposal_decision_results(parameters)
        if name == "compile_feature_obligations":
            return feature_endpoint._compile_feature_obligations(
                parameters,
                worktree=worktree,
                output_root=output_root,
                state=state,
            )
        if name == "update_plan_from_sentence_trace":
            return feature_endpoint._update_plan_from_sentence_trace(
                parameters, state=state
            )
        if name == "run_code_agent":
            return feature_endpoint._run_code_agent_phase(
                config,
                runner=runner,
                worktree=worktree,
                output_root=output_root,
                branch=branch,
                slug=slug,
                state=state,
                parameters=parameters,
            )
        if name in {
            "compile_obligation_verification_plans",
            "resolve_obligation_populations",
            "run_obligation_baseline",
            "compile_code_task_plan",
            "compile_code_task_preconditions",
            "capture_code_task_before_state",
            "run_code_task_agent",
            "compile_code_task_postconditions",
            "run_final_obligation_evidence",
            "prepare_final_implementation_review",
        }:
            handler = getattr(feature_endpoint, f"_{name}")
            result = handler(
                parameters,
                config=config,
                runner=runner,
                worktree=worktree,
                output_root=output_root,
                branch=branch,
                slug=slug,
                state=state,
            )
            if name == "prepare_final_implementation_review":
                state["latest_candidate_review"] = result
            return result
        if name == "correct_candidate_from_structrr_diff":
            return feature_endpoint._correct_candidate_from_structrr_diff(
                parameters,
                config=config,
                runner=runner,
                worktree=worktree,
                output_root=output_root,
                branch=branch,
                slug=slug,
                state=state,
            )
        if name == "get_candidate_correction_review":
            return feature_endpoint._get_candidate_correction_review(state)
        if name == "evaluate_deterministic_decision":
            return feature_endpoint._evaluate_deterministic_decision(parameters)
        if name in {
            "finalize_obligation_verification_plan_review",
            "finalize_population_review",
            "finalize_code_task_plan_review",
            "finalize_code_task_preconditions",
            "finalize_code_task_receipt",
            "finalize_obligation_closure",
            "finalize_implementation_review",
        }:
            result = getattr(feature_endpoint, f"_{name}")(
                parameters, output_root=output_root
            )
            if name == "finalize_implementation_review":
                state["review"] = {"passed": result.get("accepted") is True, **result}
            return result
        if name == "run_validation_profile":
            return feature_endpoint._run_validation_profile(
                parameters, worktree=worktree, state=state
            )
        if name == "aggregate_validation":
            return feature_endpoint._aggregate_validation(
                parameters, worktree=worktree, state=state
            )
        if name == "validate_required_test_cases":
            return feature_endpoint._validate_required_test_cases(
                parameters, worktree=worktree, state=state
            )
        if name == "run_verification_evidence":
            return feature_endpoint._run_verification_evidence(
                parameters,
                worktree=worktree,
                output_root=output_root,
                state=state,
                runner=runner,
            )
        if name == "reconcile_verification_evidence":
            return feature_endpoint._reconcile_verification_evidence(
                parameters, state=state
            )
        if name == "review_worker_diff":
            if not isinstance(parameters.get("implementation"), Mapping):
                raise PowdrrExecutionError(
                    "review did not receive implementation state"
                )
            if not isinstance(parameters.get("validation"), Mapping):
                raise PowdrrExecutionError("review did not receive validation state")
            review = feature_endpoint.review_feature_diff(
                worktree,
                state["request"],
                state["attempt"],
                validation=state["validation"],
                reconciliation=state.get("verification_reconciliation"),
                runner=runner,
            )
            required_tests = state.get("required_test_case_validation")
            if (
                isinstance(required_tests, Mapping)
                and required_tests.get("passed") is not True
            ):
                review = {
                    **review,
                    "passed": False,
                    "required_test_case_validation": dict(required_tests),
                }
            state["review"] = review
            return review
        if name == "prepare_implementation_review":
            return feature_endpoint._prepare_implementation_review(
                worktree=worktree,
                output_root=output_root,
                runner=runner,
                state=state,
                parameters=parameters,
            )
        if name == "compile_obligation_review_packets":
            return feature_endpoint._compile_obligation_review_packets(
                parameters, output_root=output_root
            )
        if name == "bind_obligation_reviews":
            return feature_endpoint._bind_obligation_reviews(parameters)
        if name == "aggregate_obligation_reviews":
            return feature_endpoint._aggregate_obligation_reviews(parameters)
        if name == "aggregate_intent_review":
            return feature_endpoint._aggregate_intent_review(parameters)
        if name == "collect_repair_issues":
            validation = parameters.get("validation")
            review_value = parameters.get("review")
            issues: list[dict[str, Any]] = []
            if isinstance(validation, Mapping):
                results = validation.get("results")
                if isinstance(results, list):
                    issues.extend(
                        {
                            "kind": "validation",
                            "issue": result,
                        }
                        for result in results
                        if isinstance(result, Mapping)
                        and result.get("status") != "passed"
                    )
                if validation.get("error"):
                    issues.append(
                        {"kind": "validation_report", "issue": validation["error"]}
                    )
            reconciliation = parameters.get("reconciliation")
            if isinstance(reconciliation, Mapping):
                issues.extend(
                    {"kind": "verification", "issue": issue}
                    for issue in reconciliation.get("issues", [])
                    if isinstance(issue, Mapping)
                )
            if (
                isinstance(review_value, Mapping)
                and review_value.get("passed") is not True
            ):
                issues.append({"kind": "worker_review", "issue": dict(review_value)})
            return issues
        if name == "open_pull_request":
            review_value = parameters.get("review")
            if (
                not isinstance(review_value, Mapping)
                or review_value.get("passed") is not True
            ):
                raise PowdrrExecutionError("cannot open a PR before a passing review")
            work_item_name = feature_endpoint._require_flow_text(
                parameters, "work_item_name"
            )
            if (
                Path(feature_endpoint._require_flow_text(parameters, "plan"))
                != state["plan_path"]
            ):
                raise PowdrrExecutionError(
                    "PR input plan does not match the planned diff"
                )
            feature_config = replace(
                config,
                work_item_name=work_item_name,
                feature_description=feature_endpoint._require_flow_text(
                    parameters, "feature_description"
                ),
            )
            feature_endpoint._commit(runner, worktree, f"Implement {work_item_name}")
            if config.push_changes:
                feature_endpoint._run(
                    runner,
                    worktree,
                    ["git", "push", "--set-upstream", "origin", branch],
                )
            if not config.open_pr:
                return None
            state["pull_request_url"] = feature_endpoint._open_pull_request(
                runner, worktree, feature_config, branch
            )
            return state["pull_request_url"]
        if name == "create_pr_changelog":
            pull_request_value = parameters.get("pull_request")
            if pull_request_value is None:
                return None
            pull_request = feature_endpoint._require_flow_text(
                parameters, "pull_request"
            )
            if (
                Path(feature_endpoint._require_flow_text(parameters, "plan"))
                != state["plan_path"]
            ):
                raise PowdrrExecutionError(
                    "changelog input plan does not match the planned diff"
                )
            feature_config = replace(
                config,
                work_item_name=feature_endpoint._require_flow_text(
                    parameters, "work_item_name"
                ),
                feature_description=feature_endpoint._require_flow_text(
                    parameters, "feature_description"
                ),
            )
            state["changelog_path"] = feature_endpoint._create_pr_changelog(
                runner,
                worktree,
                branch,
                pull_request,
                feature_config,
            )
            return str(state["changelog_path"])
        if name == "update_pull_request":
            pull_request_value = parameters.get("pull_request")
            changelog = parameters.get("changelog")
            if isinstance(pull_request_value, str) and isinstance(changelog, str):
                feature_endpoint._update_pull_request_description(
                    runner,
                    worktree,
                    pull_request_value,
                    config,
                    Path(changelog).relative_to(worktree),
                )
            return pull_request_value
        raise PowdrrExecutionError(f"feature flow requested unknown operation {name!r}")


def _benchmark_invariant_design(
    clause: Mapping[str, Any], partial_contract: Mapping[str, Any]
) -> dict[str, Any]:
    """Keep source-contract fields when benchmark mode falls back to an invariant."""
    text = clause.get("text")
    if not isinstance(text, str) or not text.strip():
        raise PowdrrExecutionError("benchmark invariant fallback has no source text")
    if not isinstance(partial_contract, Mapping):
        raise PowdrrExecutionError(
            "benchmark invariant fallback has no source semantic contract"
        )
    return {
        "kind": "invariant",
        "description": text,
        "acceptance_criterion": text,
        "expected_test": f"Verify the invariant stated by the source: {text}",
        "population": "The scope stated by the source instruction",
        "operation": "Preserve the source instruction as an invariant",
        "oracle": text,
        "evidence_case": f"Exact source instruction: {text}",
        "partial_contract": dict(partial_contract),
    }


def _kind_from_semantic_contract(
    contract: PartialSemanticContract, clause_id: str
) -> str:
    if contract.routing == "include" and contract.disposition in {
        "entity",
        "feature",
        "interface",
        "invariant",
        "guidance",
    }:
        return contract.disposition
    if contract.routing == "include_prohibition" and contract.disposition == "non_goal":
        return "non_goal"
    if contract.routing in {"context", "exclude"} and contract.disposition == "context":
        return "context"
    raise ValueError(
        f"source semantic contract for {clause_id} has unsupported route "
        f"{contract.routing!r} and disposition {contract.disposition!r}"
    )


def _obligation_evidence_provenance_record(
    *,
    obligation_id: str,
    clause_id: str,
    design_kind: str,
    partial_contract_path: Path,
    partial_contract: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Describe which source contract supplied evidence-classification fields."""
    contract = partial_contract if isinstance(partial_contract, Mapping) else {}
    return {
        "obligation_id": obligation_id,
        "clause_id": clause_id,
        "design_kind": design_kind,
        "partial_contract_path": str(partial_contract_path),
        "source_contract_id": contract.get("contract_id"),
        "source_contract_fingerprint": contract.get("fingerprint"),
        "source_ref": contract.get("source_ref"),
        "requirement_strength": contract.get("requirement_strength"),
        "polarity": contract.get("polarity"),
    }


def _merge_semantic_design_values(parameters: Mapping[str, Any]) -> dict[str, str]:
    """Validate one semantic design, including trace-only clauses."""
    kind = parameters.get("kind")
    description = parameters.get("description")
    acceptance_criterion = parameters.get("acceptance_criterion")
    population = parameters.get("population")
    operation = parameters.get("operation")
    oracle = parameters.get("oracle")
    evidence_case = parameters.get("evidence_case")
    legacy_expected_test = parameters.get("expected_test")
    if all(
        isinstance(value, str) and value.strip()
        for value in (kind, description, acceptance_criterion, legacy_expected_test)
    ) and not all(
        isinstance(value, str) and value.strip()
        for value in (population, operation, oracle, evidence_case)
    ):
        population = "the implementation covered by this clause"
        operation = "execute the clause's verification test"
        oracle = acceptance_criterion
        evidence_case = legacy_expected_test
    values = (
        kind,
        description,
        acceptance_criterion,
        population,
        operation,
        oracle,
        evidence_case,
    )
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise PowdrrExecutionError(
            "merge_semantic_design requires non-empty semantic fields"
        )
    if kind not in SEMANTIC_KINDS:
        raise PowdrrExecutionError("merge_semantic_design kind is invalid")
    return {
        "kind": kind,
        "description": description,
        "acceptance_criterion": acceptance_criterion,
        # The legacy feature-design compiler still requires expected_test. Keep
        # it compiler-owned by deriving it from the model's evidence contract;
        # the model never supplies a selector or executable test identity.
        "expected_test": f"{evidence_case} Oracle: {oracle}",
        "population": population,
        "operation": operation,
        "oracle": oracle,
        "evidence_case": evidence_case,
    }


def _is_not_applicable_resolution(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().casefold()
    return normalized == "not_applicable" or normalized.startswith(
        ("not_applicable ", "not_applicable-", "not_applicable—", "not_applicable:")
    )


def _apply_scenario_consistency_updates(
    design_decisions: list[Any],
    review: Mapping[str, Any],
    *,
    benchmark_mode: bool,
) -> list[dict[str, Any]]:
    """Apply review edits only to recorded defaults, never source requirements."""
    consistency_review = review.get("consistency_review")
    if not isinstance(consistency_review, Mapping):
        raise PowdrrExecutionError("scenario consistency review is malformed")
    updates = consistency_review.get("updates")
    if not isinstance(updates, list) or any(
        not isinstance(update, Mapping) for update in updates
    ):
        raise PowdrrExecutionError("scenario consistency updates are malformed")
    if not benchmark_mode and updates:
        raise PowdrrExecutionError(
            "scenario consistency review cannot add defaults outside benchmark mode"
        )

    decisions = copy.deepcopy(design_decisions)
    changed_targets: set[tuple[str, ...]] = set()
    for raw_update in updates:
        assert isinstance(raw_update, Mapping)
        dimension = raw_update.get("dimension")
        previous_resolution = raw_update.get("previous_resolution")
        selector_values = [
            raw_update.get(key) for key in ("subject", "given", "when", "then")
        ]
        if not isinstance(dimension, str) or not isinstance(previous_resolution, str):
            raise PowdrrExecutionError(
                "scenario consistency update has no dimension or prior resolution"
            )
        if not all(isinstance(item, str) and item.strip() for item in selector_values):
            raise PowdrrExecutionError(
                "scenario consistency update has no exact scenario selector"
            )
        selector = tuple(cast(str, item) for item in selector_values)
        selector_key = (*selector, dimension, previous_resolution)
        if selector_key in changed_targets:
            raise PowdrrExecutionError(
                "scenario consistency review repeats an assumption target"
            )
        changed_targets.add(selector_key)

        candidate = dict(raw_update)
        for key in ("subject", "given", "when", "then", "previous_resolution"):
            candidate.pop(key, None)
        try:
            validated = validate_normative_assumptions([candidate])[0]
        except (ValueError, IndexError) as error:
            raise PowdrrExecutionError(
                f"scenario consistency update is invalid: {error}"
            ) from error

        targets: list[tuple[dict[str, Any], list[Any], dict[str, Any], int]] = []
        for decision in decisions:
            if not isinstance(decision, dict):
                continue
            scenario = decision.get("behavior_scenario")
            if not isinstance(scenario, dict):
                continue
            if (
                tuple(scenario.get(key) for key in ("subject", "given", "when", "then"))
                != selector
            ):
                continue
            assumptions = scenario.get("assumptions")
            dimensions = scenario.get("dimensions")
            if not isinstance(assumptions, list) or not isinstance(dimensions, dict):
                continue
            for index, assumption in enumerate(assumptions):
                if (
                    isinstance(assumption, Mapping)
                    and assumption.get("dimension") == dimension
                    and assumption.get("resolution") == previous_resolution
                    and dimensions.get(dimension)
                    == "ASSUMED DEFAULT: " + previous_resolution
                ):
                    targets.append((scenario, assumptions, dimensions, index))
        if not targets:
            raise PowdrrExecutionError(
                "scenario consistency update did not match an existing default"
            )
        for _scenario, assumptions, dimensions, index in targets:
            assumptions[index] = validated
            dimensions[dimension] = "ASSUMED DEFAULT: " + validated["resolution"]
    return decisions


def _merge_behavior_scenario_values(
    parameters: Mapping[str, Any],
    *,
    allow_clarification: bool = False,
    benchmark_mode: bool = False,
) -> dict[str, Any]:
    """Bind a resolved scenario, a provisional draft, or recorded defaults."""
    clause = parameters.get("clause")
    design = parameters.get("design")
    result = parameters.get("scenario")
    if not isinstance(clause, Mapping) or not isinstance(design, Mapping):
        raise PowdrrExecutionError("behavior scenario is missing its source design")
    if not isinstance(result, Mapping):
        raise PowdrrExecutionError("behavior scenario response is malformed")
    raw_scenario = result.get("scenario")
    if not isinstance(raw_scenario, Mapping):
        raise PowdrrExecutionError("behavior scenario has no scenario object")
    status = result.get("status")
    unresolved = result.get("unresolved_dimensions", [])
    if status not in {"resolved", "needs_clarification"} or not isinstance(
        unresolved, list
    ):
        raise PowdrrExecutionError("behavior scenario status is malformed")
    if (status == "resolved") != (not unresolved):
        raise PowdrrExecutionError(
            "behavior scenario status conflicts with unresolved_dimensions"
        )
    raw_scenario = dict(raw_scenario)
    assumptions = raw_scenario.get("assumptions", [])
    if status == "needs_clarification" and benchmark_mode:
        dimensions = raw_scenario.get("dimensions")
        if not isinstance(dimensions, Mapping):
            raise PowdrrExecutionError("behavior scenario has no dimensions")
        if not all(item in BEHAVIOR_DIMENSIONS for item in unresolved):
            raise PowdrrExecutionError(
                "behavior scenario names an unsupported unresolved dimension"
            )
        if not isinstance(assumptions, list):
            raise PowdrrExecutionError("normative assumptions must be a list")
        not_applicable_dimensions = {
            str(item.get("dimension"))
            for item in assumptions
            if isinstance(item, Mapping)
            and isinstance(item.get("dimension"), str)
            and item.get("dimension") in BEHAVIOR_DIMENSIONS
            and _is_not_applicable_resolution(item.get("resolution"))
        }
        concrete_assumption_dimensions = {
            str(item.get("dimension"))
            for item in assumptions
            if isinstance(item, Mapping)
            and isinstance(item.get("dimension"), str)
            and item.get("dimension") not in not_applicable_dimensions
        }
        not_applicable_dimensions.update(
            dimension
            for dimension in unresolved
            if dimension not in concrete_assumption_dimensions
            and _is_not_applicable_resolution(dimensions.get(dimension))
        )
        effective_unresolved = [
            dimension
            for dimension in unresolved
            if dimension not in not_applicable_dimensions
        ]
        effective_assumptions = [
            item
            for item in assumptions
            if not (
                isinstance(item, Mapping)
                and item.get("dimension") in not_applicable_dimensions
            )
        ]
        try:
            resolved_assumptions = validate_normative_assumptions(
                effective_assumptions, expected_dimensions=effective_unresolved
            )
        except ValueError as error:
            raise PowdrrExecutionError(
                f"normative defaults did not resolve every clarification: {error}"
            ) from error
        resolved_dimensions = dict(dimensions)
        for dimension in not_applicable_dimensions:
            resolved_dimensions[dimension] = "not_applicable"
        for assumption in resolved_assumptions:
            resolved_dimensions[assumption["dimension"]] = (
                "ASSUMED DEFAULT: " + assumption["resolution"]
            )
        raw_scenario["dimensions"] = resolved_dimensions
        raw_scenario["assumptions"] = list(resolved_assumptions)
        if resolved_assumptions:
            raw_scenario["then"] = (
                str(raw_scenario.get("then", ""))
                + " Unspecified behavior was resolved using the recorded "
                "normative defaults."
            ).strip()
        status = "resolved"
    elif status == "needs_clarification" and not allow_clarification:
        raise PowdrrExecutionError(
            "behavior scenario needs clarification before implementation: "
            + ", ".join(str(item) for item in unresolved)
        )
    elif assumptions and not benchmark_mode:
        raise PowdrrExecutionError(
            "behavior scenario contains normative assumptions, but "
            "normative assumptions require benchmark mode"
        )
    elif status == "resolved" and benchmark_mode:
        if not isinstance(assumptions, list):
            raise PowdrrExecutionError("normative assumptions must be a list")
        not_applicable_dimensions = {
            str(item.get("dimension"))
            for item in assumptions
            if isinstance(item, Mapping)
            and isinstance(item.get("dimension"), str)
            and item.get("dimension") in BEHAVIOR_DIMENSIONS
            and _is_not_applicable_resolution(item.get("resolution"))
        }
        effective_assumptions = [
            item
            for item in assumptions
            if not (
                isinstance(item, Mapping)
                and item.get("dimension") in not_applicable_dimensions
            )
        ]
        try:
            resolved_assumptions = validate_normative_assumptions(effective_assumptions)
        except ValueError as error:
            raise PowdrrExecutionError(
                f"normative assumptions are malformed: {error}"
            ) from error
        dimensions = raw_scenario.get("dimensions")
        if not isinstance(dimensions, Mapping):
            raise PowdrrExecutionError("behavior scenario has no dimensions")
        resolved_dimensions = dict(dimensions)
        for dimension in not_applicable_dimensions:
            if dimension in BEHAVIOR_DIMENSIONS:
                resolved_dimensions[dimension] = "not_applicable"
        for assumption in resolved_assumptions:
            expected = "ASSUMED DEFAULT: " + assumption["resolution"]
            if dimensions.get(assumption["dimension"]) != expected:
                raise PowdrrExecutionError(
                    "assumption resolution does not match its behavior dimension"
                )
        raw_scenario["assumptions"] = list(resolved_assumptions)
        raw_scenario["dimensions"] = resolved_dimensions
    if status == "needs_clarification":
        if not all(item in BEHAVIOR_DIMENSIONS for item in unresolved):
            raise PowdrrExecutionError(
                "behavior scenario names an unsupported unresolved dimension"
            )
        draft_scenario = dict(raw_scenario)
        dimensions = draft_scenario.get("dimensions")
        if not isinstance(dimensions, Mapping):
            raise PowdrrExecutionError(
                "provisional behavior scenario has no dimensions"
            )
        draft_dimensions = dict(dimensions)
        for name in unresolved:
            draft_dimensions[name] = (
                f"NEEDS CLARIFICATION: the task specification does not resolve {name}."
            )
        draft_scenario["dimensions"] = draft_dimensions
        draft_scenario["then"] = (
            str(draft_scenario.get("then", ""))
            + " This design is provisional; resolve the marked dimensions "
            "before implementation."
        ).strip()
        raw_scenario = draft_scenario
    clause_id = clause.get("clause_id")
    evidence = design.get("expected_test")
    if not isinstance(clause_id, str) or not clause_id.strip():
        raise PowdrrExecutionError("behavior scenario source clause has no ID")
    if not isinstance(evidence, str) or not evidence.strip():
        raise PowdrrExecutionError("behavior scenario has no executable test evidence")
    scenario = {
        "scenario_id": f"scenario:{clause_id}",
        **dict(raw_scenario),
        "evidence": [evidence.strip()],
        "validator": evidence.strip(),
    }
    partial_contract = design.get("partial_contract")
    routing = (
        partial_contract.get("routing")
        if isinstance(partial_contract, Mapping)
        else "include"
    )
    scenario["routing"] = routing
    validation_group_id = clause.get("validation_group_id")
    validation_relation = clause.get("validation_relation", "independent")
    if isinstance(validation_group_id, str) and validation_group_id.strip():
        scenario["validation_group_id"] = validation_group_id
        scenario["validation_relation"] = validation_relation
    try:
        compiled = compile_behavior_scenarios((scenario,))[0]
    except ValueError as error:
        raise PowdrrExecutionError(
            f"behavior scenario is incomplete: {error}"
        ) from error
    return {**dict(design), "behavior_scenario": compiled.to_data()}


__all__ = ["FeatureCommandRuntime", "feature_command_catalog"]
