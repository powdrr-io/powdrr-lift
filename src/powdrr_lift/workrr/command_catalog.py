"""The typed internal command catalog used by the feature Procedrr flow."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

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
from powdrr_lift.core.semantic_contract import (
    BoundSourceExtraction,
    SemanticContractError,
)
from powdrr_lift.core.semantic_decision import SemanticDecision, SemanticDecisionError
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.workrr.semantic_contract_compiler import (
    bind_behavior_family_decision,
    bind_source_extractions,
    bind_source_semantic_decisions,
    compile_source_contract,
    prepare_behavior_family_decision,
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
        "prepare_behavior_family_decision": CommandSpec(
            name="prepare_behavior_family_decision",
            input_schema=object_schema(
                {"clause": {}, "extractions": {}},
                required=("clause", "extractions"),
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
        "compile_canonical_feature_design": CommandSpec(
            name="compile_canonical_feature_design",
            input_schema=object_schema(
                {"work_item_name": {}, "design_decisions": {}},
                required=("work_item_name", "design_decisions"),
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
        if name == "ensure_current_structrr":
            state["baseline_path"] = feature_endpoint._ensure_current_baseline(
                worktree, runner
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
                if set(split) != {"statements"}:
                    raise PowdrrExecutionError(
                        "atomicity split may contain only ordered statements"
                    )
                compiler_decisions[clause_id] = {
                    "multiple": True,
                    "statements": split.get("statements"),
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
            }

        def merge_semantic_design_operation() -> Any:
            """Join the independently elicited semantic fields for one clause."""
            return _merge_semantic_design_values(parameters)

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

        def prepare_source_semantic_decisions_operation() -> Any:
            try:
                return prepare_source_semantic_decisions(semantic_clause())
            except SemanticContractError as exc:
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
                    requests=requests, provider_results=raw_results
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
                return prepare_behavior_family_decision(semantic_clause(), behaviors[0])
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
                family = bind_behavior_family_decision(family_request, family_result)
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
            }

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
            work_item_name = feature_endpoint._require_flow_text(
                parameters, "work_item_name"
            )
            try:
                design = compile_feature_design(
                    ledger,
                    work_item_name,
                    raw_design_decisions,
                )
            except FeatureObligationError as exc:
                raise PowdrrExecutionError(str(exc)) from exc
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
            obligations = [
                {
                    "id": f"sentence-{index}",
                    "description": item.projection.description,
                    "design": item.projection.to_data(),
                }
                for index, item in enumerate(design.obligations, start=1)
            ]
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
                }
                for index, item in enumerate(design.obligations, start=1)
            ]
            required_test_cases = feature_endpoint._compile_required_test_case_edits(
                semantic_cases,
                tuple(state.get("provider_inventory", ())),
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
            canonical_document["verification_contracts"] = verification_contracts
            path = output_root / "canonical-feature-design.json"
            path.write_text(
                json.dumps(canonical_document, indent=2, sort_keys=True) + "\n",
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
            state["canonical_feature_design_path"] = path
            state["feature_obligations_path"] = path
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
                "bind_source_semantic_decisions": bind_handler(
                    bind_source_semantic_decisions_operation
                ),
                "prepare_source_extractions": bind_handler(
                    prepare_source_extractions_operation
                ),
                "bind_source_extractions": bind_handler(
                    bind_source_extractions_operation
                ),
                "prepare_behavior_family_decision": bind_handler(
                    prepare_behavior_family_decision_operation
                ),
                "compile_partial_semantic_contract": bind_handler(
                    compile_partial_semantic_contract_operation
                ),
                "merge_semantic_design": bind_handler(merge_semantic_design_operation),
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
            return handler(
                parameters,
                config=config,
                runner=runner,
                worktree=worktree,
                output_root=output_root,
                branch=branch,
                slug=slug,
                state=state,
            )
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


__all__ = ["FeatureCommandRuntime", "feature_command_catalog"]
