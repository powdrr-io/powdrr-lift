"""One-shot Workrr orchestration for a planned feature handoff."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.change_log_parser import parse_change_log
from powdrr_lift.core.decision_obligation import (
    DecisionOutcome,
    DecisionResult,
    DecisionWorklist,
    content_fingerprint,
)
from powdrr_lift.core.execution_plan import ExecutionPlan, ExecutionUnit
from powdrr_lift.core.spec_context import (
    gather_specification_context,
    render_gather_context_report,
)
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.structrr.active_intent import (
    ActiveIntentReference,
    ActiveIntentResolutionError,
    resolve_active_intent,
)
from powdrr_lift.structrr.bootstrap import (
    bootstrap_structrr,
    validate_bootstrap_sections,
)
from powdrr_lift.structrr.gate_compiler import (
    compile_proposal_worklist,
    evaluate_structural_proposal_gate,
)
from powdrr_lift.structrr.proposal import (
    ProposalRevision,
    compile_proposal_revision,
    load_proposal_revision,
    validate_proposal_revision,
)
from powdrr_lift.structrr.proposal_review import (
    ProposalReviewReceipt,
    load_review_receipt,
    write_review_receipt,
)
from powdrr_lift.structrr.validation import (
    DiscoveredValidationProfile,
)
from powdrr_lift.structrr.verification_obligations import (
    VerificationObligationCompilation,
    compile_verification_obligations,
)
from powdrr_lift.workrr.coding_agent import (
    CodingAgentAttempt,
    CodingAgentAttemptStore,
    CodingAgentRunner,
    CodingAgentStatus,
    ImplementationRequest,
    OpenCodePermissionPolicy,
    OpenCodeProvider,
)
from powdrr_lift.workrr.coding_agent_validation import (
    ValidationProfile,
    ValidationReport,
    ValidationReportStatus,
    ValidationResult,
    ValidationResultStatus,
    ValidationRunner,
)
from powdrr_lift.workrr.evidence_reconciliation import reconcile_verification_evidence
from powdrr_lift.workrr.git import integration_branch_name, slugify_workflow_id
from powdrr_lift.workrr.procedrr import WorkrrProcedrrClient
from powdrr_lift.workrr.protocol import WorkflowLLMClient
from powdrr_lift.workrr.verification_evidence import VerificationEvidenceRunner
from powdrr_lift.workrr.verification_provider import (
    default_verification_provider_registry,
)
from procedrr import parse_and_validate
from procedrr_evaluator import Evaluator
from procedrr_evaluator.evaluator import ValidationGateError

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class FeatureEndpointConfig:
    feature_description: str
    work_item_name: str
    repo_root: Path
    allowed_paths: tuple[str, ...]
    validation_command: tuple[str, ...] = ()
    base_branch: str = "main"
    opencode_executable: str = "opencode"
    opencode_model: str = "deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731"
    output_root: Path | None = None
    open_pr: bool = True
    push_changes: bool = True
    planning_client: WorkflowLLMClient | None = None


@dataclass(frozen=True, slots=True)
class FeatureEndpointResult:
    status: str
    branch: str
    worktree: Path
    baseline_path: Path
    plan_path: Path
    request_path: Path
    attempt: CodingAgentAttempt | None
    validation: ValidationReport | None
    review: dict[str, Any]
    pull_request_url: str | None = None
    changelog_path: Path | None = None
    feature_obligations_path: Path | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "branch": self.branch,
            "worktree": str(self.worktree),
            "baseline_path": str(self.baseline_path),
            "plan_path": str(self.plan_path),
            "request_path": str(self.request_path),
            "attempt": self.attempt.to_data() if self.attempt else None,
            "validation": self.validation.to_data() if self.validation else None,
            "review": self.review,
            "pull_request_url": self.pull_request_url,
            "changelog_path": str(self.changelog_path) if self.changelog_path else None,
            "feature_obligations_path": (
                str(self.feature_obligations_path)
                if self.feature_obligations_path
                else None
            ),
        }


def run_feature_endpoint(
    config: FeatureEndpointConfig,
    *,
    runner: Runner = subprocess.run,
) -> FeatureEndpointResult:
    """Plan, implement, review, and optionally open one feature PR."""
    if not config.feature_description.strip():
        raise ValueError("feature_description must not be empty")
    if not config.allowed_paths:
        raise ValueError("at least one allowed path is required")
    root = config.repo_root.resolve()
    slug = slugify_workflow_id(config.work_item_name)
    branch = integration_branch_name(config.work_item_name)
    worktree = root / ".worktrees" / "powdrr" / slug
    output_root = (
        config.output_root or root / ".powdrr" / "feature-runs" / slug
    ).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    _require_clean_root(root, runner)
    _run(runner, root, ["git", "fetch", "origin", config.base_branch])
    _run(
        runner,
        root,
        [
            "git",
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree),
            f"origin/{config.base_branch}",
        ],
    )
    try:
        return _execute_procedrr_flow(
            config,
            runner=runner,
            worktree=worktree,
            output_root=output_root,
            branch=branch,
        )
    except Exception:
        raise


def run_feature_in_place(
    config: FeatureEndpointConfig,
    *,
    runner: Runner = subprocess.run,
) -> FeatureEndpointResult:
    """Run the shared implement-feature flow in the caller's checkout.

    This is the workspace policy used by sandboxed agent runners such as
    Harbor.  The Procedrr flow remains identical to the pull-request endpoint;
    only the Git lifecycle differs: no fetch, nested worktree, push, or PR.
    The flow still commits the completed implementation to the current
    checkout so the harness can collect it.
    """
    if not config.feature_description.strip():
        raise ValueError("feature_description must not be empty")
    if not config.allowed_paths:
        raise ValueError("at least one allowed path is required")
    root = config.repo_root.resolve()
    slug = slugify_workflow_id(config.work_item_name)
    output_root = (
        config.output_root or root / ".powdrr" / "feature-runs" / slug
    ).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    _require_clean_root(root, runner)
    branch = _git_output(runner, root, ["git", "branch", "--show-current"])
    if not branch:
        branch = "HEAD"
    return _execute_procedrr_flow(
        replace(config, open_pr=False, push_changes=False),
        runner=runner,
        worktree=root,
        output_root=output_root,
        branch=branch,
    )


def _execute_procedrr_flow(
    config: FeatureEndpointConfig,
    *,
    runner: Runner,
    worktree: Path,
    output_root: Path,
    branch: str,
) -> FeatureEndpointResult:
    slug = slugify_workflow_id(config.work_item_name)
    state: dict[str, Any] = {}
    flow_path = _validate_procedrr_flow(worktree)
    flow = parse_and_validate(flow_path.read_text(encoding="utf-8"))
    validation_profiles = _bootstrap_validation_profiles(
        worktree,
        output_root=output_root,
        explicit_command=config.validation_command,
    )
    if not validation_profiles:
        raise PowdrrExecutionError(
            "Could not discover a validation command. Provide "
            "--validation-command or declare project validation tooling."
        )
    state["validation_profiles"] = validation_profiles
    state["validation_profile_names"] = tuple(
        profile.name for profile in validation_profiles
    )
    state["provider_inventory"] = tuple(
        inventory_entry
        for item in default_verification_provider_registry().inventory(
            worktree, validation_profiles
        )
        for inventory_entry in _expand_provider_inventory(item.to_data())
    )

    def execute(tool: str, parameters: Mapping[str, Any]) -> Any:
        if tool == "gather_context":
            types = parameters.get("types")
            if not isinstance(types, list) or not all(
                isinstance(item, str) for item in types
            ):
                raise PowdrrExecutionError("gather_context types are malformed")
            report = gather_specification_context(worktree, types=types)
            rendered = json.loads(render_gather_context_report(report))
            if "required_test_cases" in types:
                rendered["verification_inventory"] = list(
                    state.get("provider_inventory", ())
                )
            return rendered
        if tool == "edit":
            file_path = parameters.get("file_path")
            document = parameters.get("document")
            if not isinstance(file_path, str) or not isinstance(document, Mapping):
                raise PowdrrExecutionError("design interview edit is malformed")
            target = _resolve_flow_path(worktree, file_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
            return dict(document)
        if tool == "yaml_edit":
            return _apply_flow_yaml_edit(worktree, parameters)
        if tool != "internal":
            raise PowdrrExecutionError(
                f"feature flow requested unsupported tool {tool!r}"
            )
        command = parameters.get("command")
        if not isinstance(command, list) or not command:
            raise PowdrrExecutionError("feature flow operation command is malformed")
        name = command[0]
        if name == "ensure_current_structrr":
            state["baseline_path"] = _ensure_current_baseline(worktree, runner)
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
            _run(runner, worktree, command)
            work_item_name = _command_option(command, "--work-item-name")
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
            _run(runner, worktree, command)
            work_item_name = _command_option(command, "--work-item-name")
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
            return _evaluate_proposal_command(runner, worktree, command)
        if name == "extract_proposal_issues":
            evaluation = parameters.get("evaluation")
            return (
                list(evaluation.get("issues", []))
                if isinstance(evaluation, Mapping)
                else []
            )
        if name == "aggregate_category_edits":
            decisions = parameters.get("decisions")
            if not isinstance(decisions, Mapping):
                raise PowdrrExecutionError(
                    "aggregate_category_edits requires category decisions"
                )
            return _aggregate_category_edits(
                decisions, inventory=state.get("provider_inventory", ())
            )
        if name == "decompose_feature_description":
            feature_description = parameters.get("feature_description")
            if (
                not isinstance(feature_description, str)
                or not feature_description.strip()
            ):
                raise PowdrrExecutionError("feature description is empty")
            return _decompose_feature_description(feature_description)
        if name == "apply_sentence_design_trace":
            return _apply_sentence_design_trace(parameters, state=state)
        if len(command) != 1:
            raise PowdrrExecutionError("feature flow operation command is malformed")
        if name == "plan_structrr_diff":
            baseline = _require_flow_text(parameters, "baseline")
            if baseline != str(state["baseline_path"]):
                raise PowdrrExecutionError(
                    "planning baseline does not match ensured baseline"
                )
            plan_config = replace(
                config,
                work_item_name=_require_flow_text(parameters, "work_item_name"),
                feature_description=_require_flow_text(
                    parameters, "feature_description"
                ),
            )
            state["plan_path"] = _write_structrr_plan(
                worktree,
                plan_config,
                interview_input=parameters.get("interview_input"),
            )
            _commit(runner, worktree, "Record Structrr feature diff")
            return {"path": str(state["plan_path"])}
        if name == "prepare_proposal_review":
            return _prepare_proposal_review(
                config,
                runner=runner,
                worktree=worktree,
                output_root=output_root,
                slug=slug,
                state=state,
                parameters=parameters,
            )
        if name == "finalize_proposal_review":
            review = _finalize_proposal_review(
                worktree=worktree,
                output_root=output_root,
                parameters=parameters,
            )
            state["proposal_review_receipt_path"] = review["receipt_path"]
            return review
        if name == "compile_feature_obligations":
            return _compile_feature_obligations(
                parameters,
                worktree=worktree,
                output_root=output_root,
                state=state,
            )
        if name == "materialize_feature_intents":
            return _materialize_feature_intents(parameters, state=state)
        if name == "validate_materialized_intent_contracts":
            return _validate_materialized_intent_contracts(parameters, state=state)
        if name == "compile_verification_obligations":
            return _compile_verification_obligations(
                parameters,
                worktree=worktree,
                output_root=output_root,
                state=state,
                allowed_paths=config.allowed_paths,
            )
        if name == "update_plan_from_sentence_trace":
            return _update_plan_from_sentence_trace(parameters, state=state)
        if name == "run_opencode":
            return _run_opencode_phase(
                config,
                runner=runner,
                worktree=worktree,
                output_root=output_root,
                branch=branch,
                slug=slug,
                state=state,
                parameters=parameters,
            )
        if name == "run_validation_profile":
            return _run_validation_profile(parameters, worktree=worktree, state=state)
        if name == "aggregate_validation":
            return _aggregate_validation(parameters, worktree=worktree, state=state)
        if name == "validate_required_test_cases":
            return _validate_required_test_cases(
                parameters, worktree=worktree, state=state
            )
        if name == "run_verification_evidence":
            return _run_verification_evidence(
                parameters,
                worktree=worktree,
                output_root=output_root,
                state=state,
                runner=runner,
            )
        if name == "reconcile_verification_evidence":
            return _reconcile_verification_evidence(parameters, state=state)
        if name == "review_worker_diff":
            if not isinstance(parameters.get("implementation"), Mapping):
                raise PowdrrExecutionError(
                    "review did not receive implementation state"
                )
            if not isinstance(parameters.get("validation"), Mapping):
                raise PowdrrExecutionError("review did not receive validation state")
            review = review_feature_diff(
                worktree,
                state["request"],
                state["attempt"],
                validation=state["validation"],
                reconciliation=state.get("verification_reconciliation"),
                runner=runner,
            )
            state["review"] = review
            return review
        if name == "prepare_implementation_review":
            return _prepare_implementation_review(
                worktree=worktree,
                output_root=output_root,
                runner=runner,
                state=state,
                parameters=parameters,
            )
        if name == "aggregate_intent_review":
            return _aggregate_intent_review(parameters)
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
            work_item_name = _require_flow_text(parameters, "work_item_name")
            if Path(_require_flow_text(parameters, "plan")) != state["plan_path"]:
                raise PowdrrExecutionError(
                    "PR input plan does not match the planned diff"
                )
            feature_config = replace(
                config,
                work_item_name=work_item_name,
                feature_description=_require_flow_text(
                    parameters, "feature_description"
                ),
            )
            _commit(runner, worktree, f"Implement {work_item_name}")
            if config.push_changes:
                _run(
                    runner,
                    worktree,
                    ["git", "push", "--set-upstream", "origin", branch],
                )
            if not config.open_pr:
                return None
            state["pull_request_url"] = _open_pull_request(
                runner, worktree, feature_config, branch
            )
            return state["pull_request_url"]
        if name == "create_pr_changelog":
            pull_request_value = parameters.get("pull_request")
            if pull_request_value is None:
                return None
            pull_request = _require_flow_text(parameters, "pull_request")
            if Path(_require_flow_text(parameters, "plan")) != state["plan_path"]:
                raise PowdrrExecutionError(
                    "changelog input plan does not match the planned diff"
                )
            feature_config = replace(
                config,
                work_item_name=_require_flow_text(parameters, "work_item_name"),
                feature_description=_require_flow_text(
                    parameters, "feature_description"
                ),
            )
            state["changelog_path"] = _create_pr_changelog(
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
                _update_pull_request_description(
                    runner,
                    worktree,
                    pull_request_value,
                    config,
                    Path(changelog).relative_to(worktree),
                )
            return pull_request_value
        raise PowdrrExecutionError(f"feature flow requested unknown operation {name!r}")

    if config.planning_client is None:
        raise PowdrrExecutionError(
            "implement-feature requires a planning_client for its "
            "design-interview subprocess"
        )
    try:
        flow_directory = flow_path.parent
        evaluator = Evaluator(
            WorkrrProcedrrClient(
                config.planning_client,
                skills_dir=flow_directory,
            ),
            execute,
            process_directory=flow_directory,
            judge_clients={
                "planning": WorkrrProcedrrClient(
                    config.planning_client,
                    skills_dir=flow_directory,
                )
            },
        )
        evaluator.evaluate(
            flow,
            {
                "feature_description": config.feature_description,
                "work_item_name": config.work_item_name,
            },
        )
    except ValidationGateError:
        result = _feature_endpoint_result(state, branch, worktree, "review_failed")
    else:
        result = _feature_endpoint_result(
            state,
            branch,
            worktree,
            "pr_opened" if state.get("pull_request_url") else "completed",
        )
    _write_run_result(output_root, result)
    return result


def _prepare_proposal_review(
    config: FeatureEndpointConfig,
    *,
    runner: Runner,
    worktree: Path,
    output_root: Path,
    slug: str,
    state: dict[str, Any],
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    baseline_path = Path(_require_flow_text(parameters, "baseline"))
    plan_path = Path(_require_flow_text(parameters, "plan"))
    if baseline_path != state["baseline_path"] or plan_path != state["plan_path"]:
        raise PowdrrExecutionError("proposal review inputs do not match plan state")
    feature_description = _require_flow_text(parameters, "feature_description")
    (
        _planned_additions,
        _planned_deletions,
        acceptance_criteria,
        must_preserve,
        non_goals,
    ) = _load_implementation_plan(plan_path, feature_description)
    baseline_document = _load_yaml_mapping(baseline_path)
    plan_document = _load_yaml_mapping(plan_path)
    procedrr_path = (
        worktree / "docs" / "procedrr" / "skill-definitions" / "implement-feature.yaml"
    )
    source_refs: tuple[str, ...] = (
        f"structrr:{baseline_path.relative_to(worktree)}",
        f"structrr-diff:{plan_path.relative_to(worktree)}",
        f"procedrr:{procedrr_path.relative_to(worktree)}",
    )
    feature_obligations_path = state.get("feature_obligations_path")
    if isinstance(feature_obligations_path, Path):
        source_refs = (
            *source_refs,
            f"obligations:{feature_obligations_path.relative_to(worktree)}",
        )
    proposal = compile_proposal_revision(
        slug,
        baseline_document,
        plan_document,
        acceptance_criteria=acceptance_criteria,
        must_preserve=must_preserve,
        non_goals=non_goals,
        allowed_paths=config.allowed_paths,
        source_refs=source_refs,
    )
    proposal_path = plan_path.parent / "proposal-revision.json"
    if proposal_path.exists():
        try:
            validate_proposal_revision(proposal_path, proposal)
        except ValueError as error:
            raise PowdrrExecutionError(
                f"current Structrr inputs no longer match {proposal_path}: {error}"
            ) from error
    else:
        proposal_path.write_text(
            json.dumps(proposal.to_data(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _commit(runner, worktree, "Record proposal revision")
    active_intent_clauses = _resolve_feature_intent(
        worktree,
        baseline_document=baseline_document,
        feature_document=plan_document,
    )
    active_intent_clause_ids = tuple(
        clause.clause_id for clause in active_intent_clauses
    )
    evidence_fingerprints = _proposal_evidence_fingerprints(
        worktree,
        proposal,
        baseline_document=baseline_document,
        plan_document=plan_document,
        active_intent_clauses=active_intent_clauses,
    )
    worklist, structural_failures = evaluate_structural_proposal_gate(
        proposal,
        active_intent_clause_ids=active_intent_clause_ids,
        evidence_fingerprints=evidence_fingerprints,
        verification_compilation=state.get("verification_obligations"),
    )
    if structural_failures:
        raise PowdrrExecutionError(
            "proposal failed deterministic structural review: "
            + "; ".join(structural_failures)
        )
    worklist_path = output_root / "proposal-review-worklist.json"
    worklist_path.write_text(
        json.dumps(worklist.to_data(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    state["proposal_revision"] = proposal
    state["proposal_worklist"] = worklist
    state["active_intent_clause_ids"] = active_intent_clause_ids
    return {
        "proposal_revision_path": str(proposal_path),
        "proposal_fingerprint": proposal.fingerprint,
        "worklist_path": str(worklist_path),
        "worklist": worklist.to_data(),
        "active_intent_clauses": [clause.to_data() for clause in active_intent_clauses],
        "verification_obligations": (
            state["verification_obligations"].to_data()
            if isinstance(
                state.get("verification_obligations"), VerificationObligationCompilation
            )
            else {"obligations": [], "failures": []}
        ),
    }


def _finalize_proposal_review(
    *, worktree: Path, output_root: Path, parameters: Mapping[str, Any]
) -> dict[str, Any]:
    proposal_path = Path(_require_flow_text(parameters, "proposal_revision_path"))
    worklist_path = Path(_require_flow_text(parameters, "worklist_path"))
    raw_decisions = parameters.get("decisions")
    if not isinstance(raw_decisions, list) or not all(
        isinstance(item, Mapping) for item in raw_decisions
    ):
        raise PowdrrExecutionError(
            "proposal review decisions must be a list of objects"
        )
    proposal = load_proposal_revision(proposal_path)
    worklist = DecisionWorklist.from_data(
        json.loads(worklist_path.read_text(encoding="utf-8"))
    )
    _validate_review_evidence_sources(
        worktree,
        proposal,
        worklist,
        baseline_document=_load_yaml_mapping(
            next(
                worktree / ref.partition(":")[2]
                for ref in proposal.source_refs
                if ref.startswith("structrr:")
            )
        ),
    )
    collected_decisions = _collected_results(raw_decisions)
    if collected_decisions is None:
        raise PowdrrExecutionError("proposal review decisions are malformed")
    decisions = tuple(DecisionResult.from_data(item) for item in collected_decisions)
    receipt = ProposalReviewReceipt(
        proposal_fingerprint=proposal.fingerprint,
        worklist_fingerprint=worklist.fingerprint,
        decision_results=decisions,
        accepted=all(item.outcome is DecisionOutcome.PASS for item in decisions)
        and len(decisions) == len(worklist.specifications),
    )
    receipt.assert_current(proposal, worklist)
    receipt_path = output_root / "proposal-review-receipt.json"
    write_review_receipt(receipt_path, receipt)
    return {
        "accepted": receipt.accepted,
        "receipt_path": str(receipt_path),
        "proposal_fingerprint": receipt.proposal_fingerprint,
        "worklist_fingerprint": receipt.worklist_fingerprint,
    }


def _validate_review_evidence_sources(
    worktree: Path,
    proposal: ProposalRevision,
    worklist: DecisionWorklist,
    *,
    baseline_document: Mapping[str, Any],
) -> None:
    feature_document: Mapping[str, Any] | None = None
    for source_ref in proposal.source_refs:
        if source_ref.startswith("structrr-diff:"):
            feature_document = _load_yaml_mapping(
                worktree / source_ref.partition(":")[2]
            )
            break
    active_intent_refs = {
        f"intent:{clause.clause_id}"
        for clause in _resolve_feature_intent(
            worktree,
            baseline_document=baseline_document,
            feature_document=feature_document,
        )
    }
    known_refs = {"proposal", "baseline", "plan", *proposal.source_refs}
    known_refs.update(active_intent_refs)
    required_refs = {
        reference
        for specification in worklist.specifications
        for reference in specification.evidence_requirements
    }
    unknown_refs = sorted(
        reference
        for reference in required_refs
        if reference.split("@", 1)[0] not in known_refs
        and not reference.startswith("sha256:")
    )
    if unknown_refs:
        raise PowdrrExecutionError(
            "proposal review requires unknown evidence references: "
            + ", ".join(unknown_refs)
        )
    for source_ref in proposal.source_refs:
        prefix, separator, relative_path = source_ref.partition(":")
        if prefix not in {"structrr", "structrr-diff", "procedrr"} or not separator:
            continue
        if not (worktree / relative_path).exists():
            raise PowdrrExecutionError(
                f"proposal review evidence source does not exist: {source_ref}"
            )


def _compile_feature_obligations(
    parameters: Mapping[str, Any],
    *,
    worktree: Path,
    output_root: Path,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Turn the approved plan trace into durable worker obligations."""
    plan = _require_flow_text(parameters, "plan")
    if Path(plan) != state.get("plan_path"):
        raise PowdrrExecutionError("obligations plan does not match the planned diff")
    feature_description = _require_flow_text(parameters, "feature_description")
    sentences = parameters.get("sentences")
    design_decisions = _collected_results(parameters.get("design_decisions"))
    requirement_decisions = _collected_results(parameters.get("requirement_decisions"))
    reflection_decisions = _collected_results(parameters.get("reflection_decisions"))
    if not isinstance(sentences, list):
        raise PowdrrExecutionError("feature sentences must be a list")
    if design_decisions is None:
        raise PowdrrExecutionError("sentence design decisions must be a list")
    if not isinstance(requirement_decisions, list) or not isinstance(
        reflection_decisions, list
    ):
        raise PowdrrExecutionError("sentence decisions must be lists")
    if (
        len(sentences) != len(design_decisions)
        or len(sentences) != len(requirement_decisions)
        or len(sentences) != len(reflection_decisions)
    ):
        raise PowdrrExecutionError("sentence decision counts do not match")
    trace_path = output_root / "feature-sentence-trace.json"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(
        json.dumps(
            {
                "feature_description": feature_description,
                "sentences": sentences,
                "design_decisions": design_decisions,
                "requirement_decisions": requirement_decisions,
                "reflection_decisions": reflection_decisions,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    plan_document = _load_yaml_mapping(Path(plan))
    plan_refs = _plan_acceptance_references(plan_document)
    if not plan_refs:
        raise PowdrrExecutionError("plan produced no acceptance criteria obligations")

    obligations: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, (sentence, design, requirement, _reflection) in enumerate(
        zip(
            sentences,
            design_decisions,
            requirement_decisions,
            reflection_decisions,
            strict=True,
        ),
        start=1,
    ):
        if not isinstance(sentence, Mapping):
            raise PowdrrExecutionError("feature sentence is malformed")
        sentence_id = sentence.get("id")
        sentence_text = sentence.get("text")
        if not isinstance(sentence_id, str) or not isinstance(sentence_text, str):
            raise PowdrrExecutionError("feature sentence is missing id or text")
        if not isinstance(design, Mapping):
            raise PowdrrExecutionError("sentence design decision is malformed")
        if not _decision_value(requirement, "required"):
            continue
        identifier = sentence_id or f"sentence-{index}"
        if identifier in seen_ids:
            raise PowdrrExecutionError("feature sentence identifiers are duplicated")
        seen_ids.add(identifier)
        obligations.append(
            {
                "id": identifier,
                "description": sentence_text.strip(),
                "plan_refs": plan_refs,
                "design": dict(design),
            }
        )
    if not obligations:
        required_count = sum(
            _decision_value(item, "required") for item in requirement_decisions
        )
        reflected_count = sum(
            _decision_value(item, "reflected") for item in reflection_decisions
        )
        raise PowdrrExecutionError(
            "feature description produced no obligations; "
            f"sentences={len(sentences)}, required={required_count}, "
            f"reflected={reflected_count}, trace={trace_path}"
        )

    path = output_root / "feature-obligations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "feature_description": feature_description,
                "plan": plan,
                "obligations": obligations,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    state["feature_obligations"] = tuple(item["description"] for item in obligations)
    state["feature_obligations_path"] = path
    return {"path": str(path), "obligations": obligations}


def _materialize_feature_intents(
    parameters: Mapping[str, Any], *, state: Mapping[str, Any]
) -> dict[str, Any]:
    """Persist each repaired feature obligation as an active intent clause."""
    plan = _require_flow_text(parameters, "plan")
    if Path(plan) != state.get("plan_path"):
        raise PowdrrExecutionError(
            "intent materialization plan does not match the plan"
        )
    raw_obligations = parameters.get("obligations")
    if not isinstance(raw_obligations, Mapping):
        raise PowdrrExecutionError(
            "intent materialization requires feature obligations"
        )
    obligations = raw_obligations.get("obligations")
    if not isinstance(obligations, list) or not obligations:
        raise PowdrrExecutionError("feature obligations must be a non-empty list")

    document = dict(_load_yaml_mapping(Path(plan)))
    change_id = document.get("change_id")
    if not isinstance(change_id, str) or not change_id.strip():
        raise PowdrrExecutionError(
            "plan is missing change_id for intent materialization"
        )
    active_intent = list(document.get("active_intent", []))
    if any(not isinstance(item, Mapping) for item in active_intent):
        raise PowdrrExecutionError("plan active_intent must contain objects")
    existing_ids = {
        item.get("clause_id")
        for item in active_intent
        if isinstance(item.get("clause_id"), str)
    }
    intent_clauses: list[dict[str, Any]] = []
    for obligation in obligations:
        if not isinstance(obligation, Mapping):
            raise PowdrrExecutionError("feature obligation is malformed")
        obligation_id = obligation.get("id")
        design = obligation.get("design")
        if not isinstance(obligation_id, str) or not obligation_id.strip():
            raise PowdrrExecutionError("feature obligation is missing id")
        if not isinstance(design, Mapping):
            raise PowdrrExecutionError(
                f"feature obligation {obligation_id!r} is missing design"
            )
        description = design.get("description")
        kind = design.get("kind")
        if not isinstance(description, str) or not description.strip():
            raise PowdrrExecutionError(
                f"feature obligation {obligation_id!r} is missing design description"
            )
        if not isinstance(kind, str) or not kind.strip():
            raise PowdrrExecutionError(
                f"feature obligation {obligation_id!r} is missing design kind"
            )
        clause_id = f"feature-obligation-{obligation_id}"
        clause = {
            "clause_id": clause_id,
            "intent_id": f"feature:{change_id}",
            "kind": _intent_kind_for_design(kind),
            "statement": description.strip(),
            "source_ref": f"feature-obligation:{obligation_id}",
            "version": 1,
            "active": True,
            "action": "added",
            "intent_effect": (
                "makes this feature obligation an explicit active intent clause"
            ),
        }
        verification = design.get("verification")
        if isinstance(verification, Mapping):
            clause["verification"] = dict(verification)
        intent_clauses.append(clause)
        if clause_id not in existing_ids:
            active_intent.append(clause)

    document["active_intent"] = active_intent
    Path(plan).write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    parse_change_log(Path(plan).read_text(encoding="utf-8"))
    return {
        "path": plan,
        "updated": sum(
            clause["clause_id"] not in existing_ids for clause in intent_clauses
        ),
        "intent_clauses": intent_clauses,
    }


def _intent_kind_for_design(kind: str) -> str:
    if kind == "invariant":
        return "invariant"
    if kind in {"guidance", "non_goal"}:
        return "guidance"
    return "decision"


def _validate_materialized_intent_contracts(
    parameters: Mapping[str, Any], *, state: Mapping[str, Any]
) -> dict[str, Any]:
    """Require every materialized intent to be explicitly verifiable.

    Feature obligations become active intent clauses before proposal review.  A
    required test case must name each such clause in ``intent_refs`` unless the
    clause explicitly declares a non-obligating verification classification and
    rationale.  This keeps sentence-derived design from silently becoming an
    unverified implementation obligation.
    """
    plan = _require_flow_text(parameters, "plan")
    if Path(plan) != state.get("plan_path"):
        raise PowdrrExecutionError(
            "intent contract validation plan does not match the plan"
        )
    document = _load_yaml_mapping(Path(plan))
    clauses = document.get("active_intent")
    if not isinstance(clauses, list):
        raise PowdrrExecutionError(
            "plan active_intent must contain materialized clauses"
        )
    contracts = _require_required_test_cases(document)
    contract_refs = {
        reference
        for contract in contracts
        for reference in contract.get("intent_refs", [])
        if isinstance(reference, str)
    }
    failures: list[str] = []
    checked: list[str] = []
    for clause in clauses:
        if not isinstance(clause, Mapping):
            continue
        source_ref = clause.get("source_ref")
        clause_id = clause.get("clause_id")
        if not (
            isinstance(source_ref, str)
            and source_ref.startswith("feature-obligation:")
            and isinstance(clause_id, str)
            and clause_id.strip()
        ):
            continue
        checked.append(clause_id)
        verification = clause.get("verification")
        if (
            isinstance(verification, Mapping)
            and verification.get("mode") == "non_obligating"
        ):
            rationale = verification.get("rationale")
            if isinstance(rationale, str) and rationale.strip():
                continue
            failures.append(
                f"materialized intent {clause_id} declares non_obligating "
                "without a rationale"
            )
            continue
        if clause_id not in contract_refs:
            failures.append(
                f"materialized intent {clause_id} has no verification contract; "
                "add its exact clause_id to required_test_cases.intent_refs or "
                "declare verification.mode=non_obligating with a rationale"
            )
    if failures:
        raise PowdrrExecutionError(
            "intent verification coverage failed: " + "; ".join(failures)
        )
    return {"checked": checked, "contract_refs": sorted(contract_refs)}


def _compile_verification_obligations(
    parameters: Mapping[str, Any],
    *,
    worktree: Path,
    output_root: Path,
    state: dict[str, Any],
    allowed_paths: tuple[str, ...],
) -> dict[str, Any]:
    """Compile the pre-implementation proof packet from current Structrr state."""
    baseline_path = Path(_require_flow_text(parameters, "baseline"))
    plan_path = Path(_require_flow_text(parameters, "plan"))
    if baseline_path != state.get("baseline_path") or plan_path != state.get(
        "plan_path"
    ):
        raise PowdrrExecutionError(
            "verification obligation inputs do not match the current plan state"
        )
    baseline = _load_yaml_mapping(baseline_path)
    plan = _load_yaml_mapping(plan_path)
    active = _resolve_feature_intent(
        worktree, baseline_document=baseline, feature_document=plan
    )
    candidate_contracts = _verification_contracts(
        _merge_contract_mappings(
            _mapping_values(baseline.get("required_test_cases")),
            _mapping_values(plan.get("required_test_cases")),
        )
    )
    previous_contracts = _verification_contracts(
        _mapping_values(baseline.get("required_test_cases"))
    )
    relationships = tuple(
        _mapping_values(baseline.get("entity_relationships"))
        + _mapping_values(plan.get("entity_relationships"))
    )
    proposal = state.get("proposal_revision")
    if not isinstance(proposal, ProposalRevision):
        proposal = compile_proposal_revision(
            slugify_workflow_id(_require_flow_text(parameters, "feature_description")),
            baseline,
            plan,
            acceptance_criteria=(),
            must_preserve=(),
            non_goals=(),
            allowed_paths=allowed_paths,
            source_refs=(
                f"structrr:{baseline_path.relative_to(worktree)}",
                f"structrr-diff:{plan_path.relative_to(worktree)}",
            ),
        )
    compilation = compile_verification_obligations(
        proposal,
        active_intents=tuple(item.to_data() for item in active),
        contracts=candidate_contracts,
        provider_inventory=state.get("provider_inventory", ()),
        anticipated_paths=_planned_paths(plan),
        relationships=relationships,
        previous_contracts=previous_contracts,
    )
    state["verification_obligations"] = compilation
    path = output_root / "verification-obligations.json"
    path.write_text(
        json.dumps(compilation.to_data(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "path": str(path),
        "fingerprint": compilation.fingerprint,
        "closure": compilation.closure.to_data(),
        "obligations": [item.to_data() for item in compilation.obligations],
        "excluded_contracts": list(compilation.excluded_contracts),
        "failures": list(compilation.failures),
    }


def _mapping_values(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _expand_provider_inventory(value: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    selectors = value.get("selectors")
    if not isinstance(selectors, list):
        return ()
    return tuple(
        {
            "inventory_id": ":".join(
                str(value.get(field, "")) for field in ("provider", "profile")
            )
            + ":"
            + str(selector),
            "provider": value.get("provider"),
            "profile": value.get("profile"),
            "selector": selector,
            "fingerprint": value.get("fingerprint"),
            "verifier_fingerprint": value.get("fingerprint"),
            "command": value.get("command", []),
        }
        for selector in selectors
        if isinstance(selector, str) and selector.strip()
    )


def _run_verification_evidence(
    parameters: Mapping[str, Any],
    *,
    worktree: Path,
    output_root: Path,
    state: dict[str, Any],
    runner: Runner,
) -> dict[str, Any]:
    raw_obligations = parameters.get("obligations")
    if not isinstance(raw_obligations, list) or not all(
        isinstance(item, Mapping) for item in raw_obligations
    ):
        raise PowdrrExecutionError("verification obligations must be a list")
    from powdrr_lift.structrr.verification_obligations import VerificationObligation

    obligations = tuple(
        VerificationObligation.from_data(item) for item in raw_obligations
    )
    head = _git_output(runner, worktree, ["git", "rev-parse", "HEAD"])
    diff = _git_output(runner, worktree, ["git", "diff", "--binary"])
    status = _git_output(runner, worktree, ["git", "status", "--porcelain"])
    candidate_tree = content_fingerprint({"head": head, "diff": diff, "status": status})
    progress_events = state.setdefault("verification_progress", [])
    evidence = VerificationEvidenceRunner(
        default_verification_provider_registry(),
        progress=lambda event: progress_events.append(dict(event)),
    ).run(
        obligations,
        root=worktree,
        artifact_root=output_root / "verification-evidence",
        candidate_tree=candidate_tree,
        inventory=state.get("provider_inventory", ()),
    )
    state["verification_evidence"] = evidence
    return {
        "candidate_tree": candidate_tree,
        "evidence": [item.to_data() for item in evidence],
        "progress_events": list(progress_events),
        "passed": all(item.status.value == "passed" for item in evidence),
    }


def _require_required_test_cases(
    document: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    """Load executable required-test contracts from the generated plan."""
    raw_cases = document.get("required_test_cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise PowdrrExecutionError(
            "plan must contain a non-empty required_test_cases list"
        )
    cases: list[Mapping[str, Any]] = []
    for item in raw_cases:
        if not isinstance(item, Mapping):
            raise PowdrrExecutionError(
                "required_test_cases must contain detail mappings"
            )
        if item.get("status") == "superseded":
            continue
        required = (
            "id",
            "description",
            "intent_refs",
            "provider",
            "selector",
            "profile",
            "expectation",
            "applicability",
            "status",
        )
        missing = [
            field
            for field in required
            if field not in item or item[field] in (None, "", [])
        ]
        if missing:
            identifier = item.get("id", "<unnamed>")
            raise PowdrrExecutionError(
                f"required test case {identifier!r} is incomplete: "
                + ", ".join(missing)
            )
        if not isinstance(item["intent_refs"], list) or not all(
            isinstance(value, str) and value.strip() for value in item["intent_refs"]
        ):
            raise PowdrrExecutionError(
                f"required test case {item['id']!r} has invalid intent_refs"
            )
        if not isinstance(item["applicability"], Mapping):
            raise PowdrrExecutionError(
                f"required test case {item['id']!r} has invalid applicability"
            )
        cases.append(item)
    if not cases:
        raise PowdrrExecutionError("plan contains no active required test cases")
    return tuple(cases)


def _evaluate_proposal_command(
    runner: Runner, worktree: Path, command: list[str]
) -> dict[str, Any]:
    """Run proposal evaluation while preserving diagnostics for repair."""
    result = runner(command, cwd=worktree, capture_output=True, text=True, check=False)
    issues: list[Any] = []
    for output in (result.stdout, result.stderr):
        try:
            report = yaml.safe_load(output)
        except yaml.YAMLError:
            continue
        if isinstance(report, Mapping) and isinstance(report.get("issues"), list):
            issues.extend(report["issues"])
    return {
        "returncode": result.returncode,
        "issues": issues,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _validate_required_test_cases(
    parameters: Mapping[str, Any],
    *,
    worktree: Path,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Confirm every active plan contract names a test discovered post-edit."""
    plan_path = Path(_require_flow_text(parameters, "plan"))
    if plan_path != state.get("plan_path"):
        raise PowdrrExecutionError(
            "required test case validation plan does not match the plan state"
        )
    cases = _require_required_test_cases(_load_yaml_mapping(plan_path))
    profiles = state.get("validation_profiles")
    if not isinstance(profiles, tuple):
        raise PowdrrExecutionError("validation profiles are unavailable")
    inventory = tuple(
        entry
        for item in default_verification_provider_registry().inventory(
            worktree, profiles
        )
        for entry in _expand_provider_inventory(item.to_data())
    )
    state["provider_inventory"] = inventory
    available = {
        (item.get("provider"), item.get("profile"), item.get("selector"))
        for item in inventory
    }
    failures: list[str] = []
    checked: list[dict[str, Any]] = []
    for case in cases:
        key = (case["provider"], case["profile"], case["selector"])
        present = key in available
        checked.append(
            {"id": case["id"], "selector": case["selector"], "present": present}
        )
        if not present:
            failures.append(
                f"required test case {case['id']} was not discovered: "
                f"{case['provider']}/{case['profile']}/{case['selector']}"
            )
    return {"passed": not failures, "failures": failures, "cases": checked}


def _reconcile_verification_evidence(
    parameters: Mapping[str, Any], *, state: dict[str, Any]
) -> dict[str, Any]:
    obligations = parameters.get("obligations")
    evidence = parameters.get("evidence")
    candidate_tree = parameters.get("candidate_tree")
    if not isinstance(obligations, list) or not isinstance(evidence, list):
        raise PowdrrExecutionError("reconciliation inputs must be lists")
    if not isinstance(candidate_tree, str) or not candidate_tree:
        raise PowdrrExecutionError("reconciliation candidate tree is missing")
    result = reconcile_verification_evidence(
        tuple(item for item in obligations if isinstance(item, Mapping)),
        tuple(item for item in evidence if isinstance(item, Mapping)),
        candidate_tree=candidate_tree,
    )
    state["verification_reconciliation"] = result
    return result


def _verification_contracts(
    values: Sequence[Mapping[str, Any]],
) -> tuple[Any, ...]:
    from powdrr_lift.core.verification_contract import VerificationContract

    return tuple(VerificationContract.from_mapping(value) for value in values)


def _merge_contract_mappings(
    baseline: Sequence[Mapping[str, Any]],
    candidate: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    merged: dict[str, Mapping[str, Any]] = {
        str(item.get("id")): item
        for item in baseline
        if isinstance(item.get("id"), str) and item.get("id")
    }
    for item in candidate:
        contract_id = item.get("id")
        if isinstance(contract_id, str) and contract_id:
            merged[contract_id] = item
    return tuple(merged[key] for key in sorted(merged))


def _planned_paths(document: Mapping[str, Any]) -> tuple[str, ...]:
    paths: list[str] = []
    for item in _mapping_values(document.get("files")):
        path = item.get("path")
        if isinstance(path, str) and path.strip():
            paths.append(path.strip())
    return tuple(dict.fromkeys(paths))


def _update_plan_from_sentence_trace(
    parameters: Mapping[str, Any], *, state: dict[str, Any]
) -> dict[str, Any]:
    """Make every required but unreflected sentence explicit in the plan."""
    plan = _require_flow_text(parameters, "plan")
    if Path(plan) != state.get("plan_path"):
        raise PowdrrExecutionError("plan repair does not match the planned diff")
    sentences = parameters.get("sentences")
    requirement_decisions = _collected_results(parameters.get("requirement_decisions"))
    reflection_decisions = _collected_results(parameters.get("reflection_decisions"))
    if not isinstance(sentences, list):
        raise PowdrrExecutionError("feature sentences must be a list")
    if not isinstance(requirement_decisions, list) or not isinstance(
        reflection_decisions, list
    ):
        raise PowdrrExecutionError("sentence decisions must be lists")
    if len(sentences) != len(requirement_decisions) or len(sentences) != len(
        reflection_decisions
    ):
        raise PowdrrExecutionError("sentence decision counts do not match")

    path = Path(plan)
    document = _load_yaml_mapping(path)
    raw_criteria = document.get("acceptance_criteria")
    criteria = list(raw_criteria) if isinstance(raw_criteria, list) else []
    existing_ids = {
        str(item.get("id"))
        for item in criteria
        if isinstance(item, Mapping) and item.get("id")
    }
    updated = 0
    for sentence, requirement, reflection in zip(
        sentences, requirement_decisions, reflection_decisions, strict=True
    ):
        if not _decision_value(requirement, "required") or _decision_value(
            reflection, "reflected"
        ):
            continue
        if not isinstance(sentence, Mapping):
            raise PowdrrExecutionError("feature sentence is malformed")
        sentence_id = sentence.get("id")
        sentence_text = sentence.get("text")
        if not isinstance(sentence_id, str) or not isinstance(sentence_text, str):
            raise PowdrrExecutionError("feature sentence is missing id or text")
        criterion_id = f"trace-{sentence_id}"
        if criterion_id in existing_ids:
            continue
        criteria.append(
            {
                "id": criterion_id,
                "description": sentence_text.strip(),
            }
        )
        existing_ids.add(criterion_id)
        updated += 1
    if updated:
        repaired = dict(document)
        repaired["acceptance_criteria"] = criteria
        path.write_text(yaml.safe_dump(repaired, sort_keys=False), encoding="utf-8")
        parse_change_log(path.read_text(encoding="utf-8"))
    return {"path": str(path), "updated": updated}


def _apply_sentence_design_trace(
    parameters: Mapping[str, Any], *, state: dict[str, Any]
) -> dict[str, Any]:
    """Translate each instruction sentence into one typed plan consequence."""
    plan = _require_flow_text(parameters, "plan")
    if Path(plan) != state.get("plan_path"):
        raise PowdrrExecutionError("sentence design plan does not match the plan")
    sentences = parameters.get("sentences")
    decisions = _collected_results(parameters.get("design_decisions"))
    if not isinstance(sentences, list) or decisions is None:
        raise PowdrrExecutionError("sentence design inputs must be lists")
    if len(sentences) != len(decisions):
        raise PowdrrExecutionError("sentence design counts do not match")

    section_by_kind = {
        "entity": "entities",
        "feature": "features",
        "interface": "features",
        "invariant": "invariants",
        "guidance": "guidance",
        "acceptance_criterion": "acceptance_criteria",
        "expected_test": "expected_tests",
        "intent": "features",
        "non_goal": "guidance",
    }
    document = _load_yaml_mapping(Path(plan))
    updated_document = {key: value for key, value in document.items()}
    updated = 0
    for sentence, decision in zip(sentences, decisions, strict=True):
        if not isinstance(sentence, Mapping) or not isinstance(decision, Mapping):
            raise PowdrrExecutionError("sentence design item is malformed")
        sentence_id = sentence.get("id")
        kind = decision.get("kind")
        description = decision.get("description")
        acceptance = decision.get("acceptance_criterion")
        expected_test = decision.get("expected_test")
        if not isinstance(sentence_id, str) or not isinstance(kind, str):
            raise PowdrrExecutionError("sentence design item is missing id or kind")
        if kind not in section_by_kind:
            raise PowdrrExecutionError(f"unknown sentence design kind: {kind}")
        if not isinstance(description, str) or not description.strip():
            raise PowdrrExecutionError("sentence design item is missing description")
        if not isinstance(acceptance, str) or not acceptance.strip():
            raise PowdrrExecutionError(
                "sentence design item is missing acceptance criterion"
            )
        if not isinstance(expected_test, str) or not expected_test.strip():
            raise PowdrrExecutionError("sentence design item is missing expected test")

        design_id = f"design-{sentence_id}"
        section_name = section_by_kind[kind]
        section = list(updated_document.get(section_name, []))
        existing_ids = {
            str(item.get("id"))
            for item in section
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        }
        if design_id not in existing_ids:
            section.append(
                {
                    "id": design_id,
                    "description": (
                        acceptance.strip()
                        if kind == "acceptance_criterion"
                        else description.strip()
                    ),
                    "action": "added",
                    "intent_effect": (
                        "records the concrete design consequence of the requested "
                        "feature sentence"
                    ),
                }
            )
            updated += 1
        if kind != "acceptance_criterion":
            acceptance_section = list(updated_document.get("acceptance_criteria", []))
            acceptance_id = f"{design_id}-acceptance"
            acceptance_ids = {
                str(item.get("id"))
                for item in acceptance_section
                if isinstance(item, Mapping) and isinstance(item.get("id"), str)
            }
            if acceptance_id not in acceptance_ids:
                acceptance_section.append(
                    {
                        "id": acceptance_id,
                        "description": acceptance.strip(),
                        "intent_effect": (
                            "defines how the requested feature intent is verified"
                        ),
                    }
                )
                updated += 1
            updated_document["acceptance_criteria"] = acceptance_section
        test_section = list(updated_document.get("expected_tests", []))
        test_id = f"{design_id}-test"
        test_ids = {
            str(item.get("id"))
            for item in test_section
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        }
        if test_id not in test_ids:
            test_section.append(
                {
                    "id": test_id,
                    "description": expected_test.strip(),
                    "intent_effect": (
                        "defines the test evidence for the requested feature intent"
                    ),
                }
            )
            updated += 1
        updated_document[section_name] = section
        updated_document["expected_tests"] = test_section

    if updated:
        path = Path(plan)
        path.write_text(
            yaml.safe_dump(updated_document, sort_keys=False), encoding="utf-8"
        )
        parse_change_log(path.read_text(encoding="utf-8"))
    return {"path": plan, "updated": updated}


def _decompose_feature_description(feature_description: str) -> list[dict[str, str]]:
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[.!?])\s+|\n+", feature_description)
        if item.strip()
    ]
    return [
        {"id": f"sentence-{index}", "text": sentence}
        for index, sentence in enumerate(sentences, start=1)
    ]


def _decision_value(value: Any, key: str) -> bool:
    return isinstance(value, Mapping) and value.get(key) is True


def _collected_results(value: Any) -> list[Any] | None:
    if not isinstance(value, list):
        return None
    return [
        item["result"] if isinstance(item, Mapping) and "result" in item else item
        for item in value
    ]


def _plan_acceptance_references(document: Mapping[str, Any]) -> list[str]:
    raw_criteria = document.get("acceptance_criteria")
    if not isinstance(raw_criteria, list):
        return []
    references: list[str] = []
    for index, raw in enumerate(raw_criteria, start=1):
        identifier = raw.get("id") if isinstance(raw, Mapping) else None
        if not isinstance(identifier, str) or not identifier.strip():
            identifier = f"acceptance-{index}"
        reference = f"acceptance_criteria.{identifier}"
        if _plan_reference_exists(document, reference):
            references.append(reference)
    return references


def _require_feature_obligations(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        raise PowdrrExecutionError("implementation is missing feature obligations")
    raw_obligations = value.get("obligations")
    if not isinstance(raw_obligations, list) or not raw_obligations:
        raise PowdrrExecutionError("implementation has no feature obligations")
    descriptions: list[str] = []
    for item in raw_obligations:
        if not isinstance(item, Mapping):
            raise PowdrrExecutionError("implementation obligation is malformed")
        description = item.get("description")
        if not isinstance(description, str) or not description.strip():
            raise PowdrrExecutionError("implementation obligation has no description")
        descriptions.append(description.strip())
    return tuple(dict.fromkeys(descriptions))


def _feature_obligation_path(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    path = value.get("path")
    return path if isinstance(path, str) and path.strip() else None


def _plan_reference_exists(document: Mapping[str, Any], reference: str) -> bool:
    section, separator, identifier = reference.partition(".")
    if not separator or not section or not identifier:
        return False
    items = document.get(section)
    if not isinstance(items, list):
        return False
    return any(
        isinstance(item, Mapping) and str(item.get("id", "")) == identifier
        for item in items
    )


def _run_opencode_phase(
    config: FeatureEndpointConfig,
    *,
    runner: Runner,
    worktree: Path,
    output_root: Path,
    branch: str,
    slug: str,
    state: dict[str, Any],
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    del branch
    baseline_path = Path(_require_flow_text(parameters, "baseline"))
    plan_path = Path(_require_flow_text(parameters, "plan"))
    if baseline_path != state["baseline_path"] or plan_path != state["plan_path"]:
        raise PowdrrExecutionError("implementation inputs do not match the plan state")
    feature_description = _require_flow_text(parameters, "feature_description")
    work_item_name = _require_flow_text(parameters, "work_item_name")
    feature_obligations = _require_feature_obligations(parameters.get("obligations"))
    (
        planned_additions,
        planned_deletions,
        acceptance_criteria,
        must_preserve,
        non_goals,
    ) = _load_implementation_plan(plan_path, feature_description)
    baseline_document = _load_yaml_mapping(baseline_path)
    plan_document = _load_yaml_mapping(plan_path)
    required_test_cases = _require_required_test_cases(plan_document)
    procedrr_path = (
        worktree / "docs" / "procedrr" / "skill-definitions" / "implement-feature.yaml"
    )
    source_refs: tuple[str, ...] = (
        f"structrr:{baseline_path.relative_to(worktree)}",
        f"structrr-diff:{plan_path.relative_to(worktree)}",
        f"procedrr:{procedrr_path.relative_to(worktree)}",
    )
    obligation_path = _feature_obligation_path(parameters.get("obligations"))
    if obligation_path is not None:
        source_refs = (
            *source_refs,
            f"obligations:{Path(obligation_path).relative_to(worktree)}",
        )
    proposal_revision = compile_proposal_revision(
        slug,
        baseline_document,
        plan_document,
        acceptance_criteria=acceptance_criteria,
        must_preserve=must_preserve,
        non_goals=non_goals,
        allowed_paths=config.allowed_paths,
        source_refs=source_refs,
    )
    proposal_revision_path = plan_path.parent / "proposal-revision.json"
    if not proposal_revision_path.exists():
        proposal_revision_path.write_text(
            json.dumps(proposal_revision.to_data(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _commit(runner, worktree, "Record proposal revision")
    else:
        try:
            validate_proposal_revision(proposal_revision_path, proposal_revision)
        except ValueError as error:
            raise PowdrrExecutionError(
                "current Structrr inputs no longer match the persisted proposal "
                f"revision at {proposal_revision_path}: {error}"
            ) from error
    review_path_value = parameters.get("proposal_review_receipt") or state.get(
        "proposal_review_receipt_path"
    )
    if not isinstance(review_path_value, str) or not review_path_value.strip():
        raise PowdrrExecutionError(
            "OpenCode is blocked until proposal review produces an accepted receipt"
        )
    try:
        review_receipt = load_review_receipt(Path(review_path_value))
        active_intent_clauses = _resolve_feature_intent(
            worktree,
            baseline_document=baseline_document,
            feature_document=plan_document,
        )
        active_intent_clause_ids = tuple(
            clause.clause_id for clause in active_intent_clauses
        )
        evidence_fingerprints = _proposal_evidence_fingerprints(
            worktree,
            proposal_revision,
            baseline_document=baseline_document,
            plan_document=plan_document,
            active_intent_clauses=active_intent_clauses,
        )
        review_worklist = compile_proposal_worklist(
            proposal_revision,
            active_intent_clause_ids=active_intent_clause_ids,
            evidence_fingerprints=evidence_fingerprints,
            verification_compilation=state.get("verification_obligations"),
        )
        review_receipt.assert_current(proposal_revision, review_worklist)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise PowdrrExecutionError(
            "proposal review receipt is missing, stale, or invalid: "
            f"{review_path_value}: {error}"
        ) from error
    if not review_receipt.accepted:
        raise PowdrrExecutionError(
            "OpenCode is blocked because proposal review did not pass"
        )
    base_commit = _git_output(runner, worktree, ["git", "rev-parse", "HEAD"])
    proposal_source = f"proposal:{proposal_revision_path.relative_to(worktree)}"
    source_context = (*source_refs, proposal_source)
    units = _proposal_execution_units(
        slug=slug,
        feature_description=feature_description,
        proposal_revision=proposal_revision,
        acceptance_criteria=acceptance_criteria,
        planned_additions=planned_additions,
        planned_deletions=planned_deletions,
        must_preserve=must_preserve,
        non_goals=non_goals,
        feature_obligations=feature_obligations,
        required_test_cases=required_test_cases,
        allowed_paths=config.allowed_paths,
        source_refs=source_context,
        validation_profiles=state["validation_profile_names"],
        verification_obligations=tuple(
            item.to_data()
            for item in state.get("verification_obligations", ()).obligations
        )
        if isinstance(
            state.get("verification_obligations"), VerificationObligationCompilation
        )
        else (),
    )
    plan = ExecutionPlan(
        plan_id=f"{slug}-execution",
        proposed_pr_fingerprint=proposal_revision.fingerprint,
        units=units,
        allowed_paths=config.allowed_paths,
    )
    provider = state.get("opencode_provider")
    if not isinstance(provider, OpenCodeProvider):
        provider = OpenCodeProvider(
            executable=config.opencode_executable,
            model=config.opencode_model,
            diagnostics_root=output_root / "opencode",
            permission_policy=OpenCodePermissionPolicy(
                _allowed_validation_commands(state["validation_profiles"])
            ),
        )
        state["opencode_provider"] = provider
    repair_request = parameters.get("repair_request")
    repair_issue = parameters.get("repair_issue")
    attempt_store = CodingAgentAttemptStore(output_root / "artifacts")
    coding_runner = CodingAgentRunner(
        provider=provider,
        store=attempt_store,
    )
    attempts: list[CodingAgentAttempt] = []
    requests: list[ImplementationRequest] = []
    repair_mode = (
        isinstance(repair_request, str) and bool(repair_request.strip())
    ) or isinstance(repair_issue, Mapping)
    request_units = (
        (
            _aggregate_execution_unit(
                units, slug=slug, feature_description=feature_description
            ),
        )
        if repair_mode
        else units
    )
    for index, unit in enumerate(request_units, start=1):
        request = ImplementationRequest.from_execution_unit(
            unit,
            request_id=(
                f"{slug}-repair" if repair_mode else f"{slug}-implementation-{index}"
            ),
            base_commit=base_commit,
            plan_fingerprint=plan.proposed_pr_fingerprint,
            context_refs=source_context,
            allowed_commands=_allowed_validation_commands(state["validation_profiles"]),
        )
        if repair_mode:
            if isinstance(repair_issue, Mapping):
                repair_request = request.repair_prompt(repair_issue)
            fallback_context = ""
            if provider.session_id is None:
                fallback_context = (
                    f"Work item: {work_item_name}\n"
                    f"Feature: {feature_description}\n"
                    "This is a new repair session; use the existing worktree and "
                    "repair only the reported issue.\n\n"
                )
            request = replace(
                request,
                prompt=(
                    fallback_context + f"{repair_request}\n"
                    "Use the current session context and current worktree state; "
                    "do not "
                    "re-plan the feature or revisit unrelated changes."
                ),
                allow_existing_changes=True,
            )
        elif index > 1:
            request = replace(request, allow_existing_changes=True)
        request_path = output_root / "implementation-request.json"
        request_path.write_text(request.to_json(), encoding="utf-8")
        operation_request_path = output_root / "requests" / f"{unit.unit_id}.json"
        operation_request_path.parent.mkdir(parents=True, exist_ok=True)
        operation_request_path.write_text(request.to_json(), encoding="utf-8")
        before_paths = _changed_paths(runner, worktree)
        attempt_number = int(state.get("opencode_attempt_number", 0)) + 1
        state["opencode_attempt_number"] = attempt_number
        if not repair_mode and isinstance(provider, OpenCodeProvider):
            provider.session_id = None
        attempt = coding_runner.run(
            request,
            worktree_root=worktree,
            attempt_id=(
                f"{slug}-repair-attempt-{attempt_number}"
                if repair_mode
                else f"{slug}-{unit.unit_id}-attempt-{attempt_number}"
            ),
        )
        requests.append(request)
        attempts.append(attempt)
        checkpoint = _operation_checkpoint(
            runner=runner,
            worktree=worktree,
            unit=unit,
            request=request,
            attempt=attempt,
            before_paths=before_paths,
        )
        checkpoint_path = _write_operation_checkpoint(
            output_root, attempt.attempt_id, checkpoint
        )
        if repair_mode:
            state["operation_checkpoints"] = [checkpoint]
        else:
            state.setdefault("operation_checkpoints", []).append(checkpoint)
        state.setdefault("operation_checkpoint_paths", []).append(checkpoint_path)
        if not checkpoint["passed"]:
            break
    request = requests[-1]
    attempt = attempts[-1]
    state.update(
        request=request,
        request_path=request_path,
        attempt=attempt,
        attempts=tuple(attempts),
        attempt_store=attempt_store,
    )
    return {
        "request_id": request.request_id,
        "attempt": attempt.to_data(),
        "attempts": [item.to_data() for item in attempts],
    }


def _proposal_evidence_fingerprints(
    worktree: Path,
    proposal: ProposalRevision,
    *,
    baseline_document: Mapping[str, Any],
    plan_document: Mapping[str, Any],
    active_intent_clauses: Sequence[Any],
) -> dict[str, str]:
    fingerprints = {
        "baseline": content_fingerprint(baseline_document),
        "plan": content_fingerprint(plan_document),
        "proposal": proposal.fingerprint,
    }
    for clause in active_intent_clauses:
        fingerprints[f"intent:{clause.clause_id}"] = content_fingerprint(
            clause.to_data()
        )
    for source_ref in proposal.source_refs:
        _prefix, separator, relative_path = source_ref.partition(":")
        if not separator:
            continue
        source_path = worktree / relative_path
        if source_path.is_file():
            fingerprints[source_ref] = content_fingerprint(
                {"path": relative_path, "bytes": source_path.read_bytes().hex()}
            )
    return fingerprints


def _resolve_feature_intent(
    worktree: Path,
    *,
    baseline_document: Mapping[str, Any] | None = None,
    feature_document: Mapping[str, Any] | None = None,
) -> tuple[ActiveIntentReference, ...]:
    try:
        return resolve_active_intent(
            worktree,
            baseline_document=baseline_document,
            feature_document=feature_document,
        )
    except ActiveIntentResolutionError as error:
        raise PowdrrExecutionError(
            f"canonical active-intent resolution failed: {error}"
        ) from error


def _prepare_implementation_review(
    *,
    worktree: Path,
    output_root: Path,
    runner: Runner,
    state: Mapping[str, Any],
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    implementation = parameters.get("implementation")
    validation = parameters.get("validation")
    review = parameters.get("review")
    if not isinstance(implementation, Mapping):
        raise PowdrrExecutionError(
            "implementation review requires implementation state"
        )
    if not isinstance(validation, Mapping):
        raise PowdrrExecutionError("implementation review requires validation state")
    if not isinstance(review, Mapping):
        raise PowdrrExecutionError("implementation review requires worker review state")
    request = state.get("request")
    if not isinstance(request, ImplementationRequest):
        raise PowdrrExecutionError(
            "implementation review requires an implementation request"
        )
    diff = _git_output(
        runner, worktree, ["git", "diff", "--binary", request.base_commit, "--"]
    )
    changed_paths = _git_output(
        runner, worktree, ["git", "diff", "--name-only", request.base_commit, "--"]
    ).splitlines()
    baseline_document = _load_yaml_mapping(Path(state["baseline_path"]))
    active_intent_clauses = _resolve_feature_intent(
        worktree, baseline_document=baseline_document
    )
    evidence_refs = (
        "git-diff@"
        + content_fingerprint(
            {
                "base_commit": request.base_commit,
                "changed_paths": changed_paths,
                "patch": diff,
            }
        ),
        "validation@" + content_fingerprint(validation),
        "worker-review@" + content_fingerprint(review),
        "intent-state@"
        + content_fingerprint([clause.to_data() for clause in active_intent_clauses]),
    )
    evidence = {
        "base_commit": request.base_commit,
        "changed_paths": changed_paths,
        "git_diff": diff,
        "validation": dict(validation),
        "worker_review": dict(review),
        "evidence_refs": list(evidence_refs),
        "active_intent_clauses": [clause.to_data() for clause in active_intent_clauses],
    }
    evidence_path = output_root / "implementation-review-evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "evidence_path": str(evidence_path),
        "evidence_fingerprint": content_fingerprint(evidence),
        "evidence_refs": list(evidence_refs),
        "active_intent_clauses": evidence["active_intent_clauses"],
        "git_diff": diff,
        "changed_paths": changed_paths,
        "validation": dict(validation),
        "worker_review": dict(review),
    }


def _aggregate_intent_review(parameters: Mapping[str, Any]) -> dict[str, Any]:
    decisions = parameters.get("decisions")
    evidence = parameters.get("evidence")
    if not isinstance(decisions, list) or not all(
        isinstance(item, Mapping) for item in decisions
    ):
        raise PowdrrExecutionError("intent review decisions must be a list of objects")
    if not isinstance(evidence, Mapping):
        raise PowdrrExecutionError("intent review requires implementation evidence")
    raw_clauses = evidence.get("active_intent_clauses")
    raw_refs = evidence.get("evidence_refs")
    if not isinstance(raw_clauses, list) or not isinstance(raw_refs, list):
        raise PowdrrExecutionError("implementation evidence is incomplete")
    clause_ids = sorted(
        item["clause_id"]
        for item in raw_clauses
        if isinstance(item, Mapping) and isinstance(item.get("clause_id"), str)
    )
    by_clause = {
        item.get("clause_id"): item
        for item in decisions
        if isinstance(item.get("clause_id"), str)
    }
    failures: list[str] = []
    for clause_id in clause_ids:
        decision = by_clause.get(clause_id)
        if decision is None:
            failures.append(f"missing intent review for {clause_id}")
            continue
        if decision.get("verdict") != "preserved":
            failures.append(f"intent review did not preserve {clause_id}")
        refs = decision.get("evidence_refs")
        if not isinstance(refs, list) or set(refs) != set(raw_refs):
            failures.append(f"intent review evidence mismatch for {clause_id}")
    return {"passed": not failures, "failures": failures}


def _proposal_execution_units(
    *,
    slug: str,
    feature_description: str,
    proposal_revision: ProposalRevision,
    acceptance_criteria: tuple[str, ...],
    planned_additions: tuple[dict[str, Any], ...],
    planned_deletions: tuple[dict[str, Any], ...],
    must_preserve: tuple[str, ...],
    non_goals: tuple[str, ...],
    allowed_paths: tuple[str, ...],
    source_refs: tuple[str, ...],
    validation_profiles: tuple[str, ...] = ("feature-validation",),
    feature_obligations: tuple[str, ...] = (),
    required_test_cases: Sequence[Mapping[str, Any]] = (),
    verification_obligations: Sequence[Mapping[str, Any]] = (),
) -> tuple[ExecutionUnit, ...]:
    """Compile one worker unit per explicit Structrr operation."""
    acceptance_criteria = tuple(
        dict.fromkeys((*feature_obligations, *acceptance_criteria))
    )
    verification_summary = tuple(
        "Prove contract "
        f"{item.get('contract_id')}: {item.get('provider')}/"
        f"{item.get('profile')}/{item.get('selector')} must {item.get('expectation')}."
        for item in verification_obligations
        if isinstance(item, Mapping)
    )
    required_test_summary = tuple(
        "Implement and preserve required test case "
        f"{item.get('id')}: {item.get('description')} "
        f"(provider={item.get('provider')}, profile={item.get('profile')}, "
        f"selector={item.get('selector')}, expectation={item.get('expectation')})."
        for item in required_test_cases
        if isinstance(item, Mapping)
    )
    acceptance_criteria = tuple(
        dict.fromkeys(
            (*acceptance_criteria, *required_test_summary, *verification_summary)
        )
    )
    if not proposal_revision.operations:
        return (
            ExecutionUnit(
                unit_id=f"implement-{slug}",
                objective=feature_description,
                paths=allowed_paths,
                validation_profiles=validation_profiles,
                acceptance_criteria=acceptance_criteria,
                planned_additions=planned_additions,
                planned_deletions=planned_deletions,
                must_preserve=must_preserve,
                non_goals=non_goals,
                source_refs=source_refs,
            ),
        )
    units: list[ExecutionUnit] = []
    for operation in proposal_revision.operations:
        content = dict(operation.content)
        content["section"] = operation.section
        subject = content.get("description") or content.get("summary")
        objective = (
            str(subject)
            if isinstance(subject, str) and subject.strip()
            else (
                f"{operation.action} {operation.section} subject {operation.subject_id}"
            )
        )
        operation_criteria = (
            *acceptance_criteria,
            f"Complete only Structrr operation {operation.operation_id}.",
        )
        operation_ref = f"{source_refs[-1]}#{operation.operation_id}"
        units.append(
            ExecutionUnit(
                unit_id=f"implement-{slug}-{operation.operation_id}",
                objective=objective,
                paths=allowed_paths,
                dependencies=(units[-1].unit_id,) if units else (),
                validation_profiles=validation_profiles,
                acceptance_criteria=operation_criteria,
                planned_additions=(content,) if operation.action == "add" else (),
                planned_deletions=(content,) if operation.action == "remove" else (),
                must_preserve=must_preserve,
                non_goals=non_goals,
                source_refs=(*source_refs, operation_ref),
            )
        )
    return tuple(units)


def _aggregate_execution_unit(
    units: tuple[ExecutionUnit, ...],
    *,
    slug: str,
    feature_description: str,
) -> ExecutionUnit:
    """Create one broad repair unit without changing the original plan."""
    return ExecutionUnit(
        unit_id=f"repair-{slug}",
        objective=feature_description,
        paths=units[0].paths,
        validation_profiles=units[0].validation_profiles,
        acceptance_criteria=tuple(
            dict.fromkeys(
                criterion for unit in units for criterion in unit.acceptance_criteria
            )
        ),
        planned_additions=tuple(
            change for unit in units for change in unit.planned_additions
        ),
        planned_deletions=tuple(
            change for unit in units for change in unit.planned_deletions
        ),
        must_preserve=tuple(
            dict.fromkeys(item for unit in units for item in unit.must_preserve)
        ),
        non_goals=tuple(
            dict.fromkeys(item for unit in units for item in unit.non_goals)
        ),
        source_refs=tuple(
            dict.fromkeys(item for unit in units for item in unit.source_refs)
        ),
    )


def _changed_paths(runner: Runner, worktree: Path) -> set[str]:
    paths: set[str] = set()
    for command in (
        ["git", "diff", "--name-only"],
        ["git", "diff", "--cached", "--name-only"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ):
        paths.update(_git_output(runner, worktree, command).splitlines())
    return {path for path in paths if path}


def _operation_checkpoint(
    *,
    runner: Runner,
    worktree: Path,
    unit: ExecutionUnit,
    request: ImplementationRequest,
    attempt: CodingAgentAttempt,
    before_paths: set[str],
) -> dict[str, Any]:
    after_paths = _changed_paths(runner, worktree)
    task_paths = tuple(sorted(after_paths - before_paths))
    out_of_scope = tuple(
        path
        for path in task_paths
        if not any(
            scope == "."
            or Path(path) == Path(scope)
            or Path(scope) in Path(path).parents
            for scope in request.allowed_paths
        )
    )
    diff_check = runner(
        ["git", "diff", "--check"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    existing_in_scope = bool(after_paths) and all(
        any(
            scope == "."
            or Path(path) == Path(scope)
            or Path(scope) in Path(path).parents
            for scope in request.allowed_paths
        )
        for path in after_paths
    )
    has_changes = bool(task_paths) or (
        request.allow_existing_changes and existing_in_scope
    )
    passed = (
        attempt.status is CodingAgentStatus.COMPLETED
        and has_changes
        and not out_of_scope
        and diff_check.returncode == 0
    )
    reason = None
    if attempt.status is not CodingAgentStatus.COMPLETED:
        reason = f"coding-agent attempt was {attempt.status.value}"
    elif not task_paths and not has_changes:
        reason = "operation produced no new worktree changes"
    elif out_of_scope:
        reason = f"operation changed out-of-scope paths: {list(out_of_scope)}"
    elif diff_check.returncode != 0:
        reason = "operation produced a diff that fails git diff --check"
    return {
        "unit_id": unit.unit_id,
        "request_id": request.request_id,
        "attempt_id": attempt.attempt_id,
        "status": "passed" if passed else "failed",
        "passed": passed,
        "changed_paths": list(task_paths),
        "out_of_scope_paths": list(out_of_scope),
        "diff_check_returncode": diff_check.returncode,
        "error": reason,
    }


def _write_operation_checkpoint(
    output_root: Path, attempt_id: str, checkpoint: Mapping[str, Any]
) -> Path:
    path = output_root / "artifacts" / "checkpoints" / f"{attempt_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(checkpoint), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _load_implementation_plan(
    path: Path, feature_description: str
) -> tuple[
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise PowdrrExecutionError(
            f"could not read Structrr plan {path}: {error}"
        ) from error
    if not isinstance(raw, Mapping):
        raise PowdrrExecutionError(f"Structrr plan {path} must contain a mapping")

    additions: list[dict[str, Any]] = []
    deletions: list[dict[str, Any]] = []
    for section, value in raw.items():
        if not isinstance(section, str) or not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, Mapping):
                continue
            action = item.get("action")
            if action not in {"added", "deleted", "removed"}:
                continue
            planned = {"section": section, **dict(item)}
            if action == "added":
                additions.append(planned)
            else:
                deletions.append(planned)

    criteria: list[str] = []
    raw_criteria = raw.get("acceptance_criteria")
    if isinstance(raw_criteria, list):
        for item in raw_criteria:
            if isinstance(item, str) and item.strip():
                criteria.append(item.strip())
            elif isinstance(item, Mapping):
                description = item.get("description", item.get("text"))
                if isinstance(description, str) and description.strip():
                    criteria.append(description.strip())
    if not criteria:
        criteria.append(
            f"The requested feature behavior is implemented: {feature_description}"
        )
    criteria.append("Only the declared implementation paths are changed.")
    return (
        tuple(additions),
        tuple(deletions),
        tuple(dict.fromkeys(criteria)),
        _plan_subject_text(raw, ("invariants", "guidance")),
        _plan_text_values(raw.get("non_goals")),
    )


def _load_yaml_mapping(path: Path) -> Mapping[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise PowdrrExecutionError(
            f"could not read YAML document {path}: {error}"
        ) from error
    if not isinstance(raw, Mapping):
        raise PowdrrExecutionError(f"YAML document {path} must contain a mapping")
    return raw


def _plan_subject_text(
    document: Mapping[str, Any], sections: tuple[str, ...]
) -> tuple[str, ...]:
    values: list[str] = []
    for section in sections:
        raw_values = document.get(section)
        if not isinstance(raw_values, list):
            continue
        for item in raw_values:
            if not isinstance(item, Mapping):
                continue
            if item.get("action") in {"deleted", "removed"}:
                continue
            identifier = item.get("id")
            description = item.get("description", item.get("text"))
            if isinstance(description, str) and description.strip():
                prefix = (
                    f"{section}.{identifier}: "
                    if isinstance(identifier, str) and identifier.strip()
                    else f"{section}: "
                )
                values.append(prefix + description.strip())
    return tuple(dict.fromkeys(values))


def _plan_text_values(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    values: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            values.append(item.strip())
        elif isinstance(item, Mapping):
            description = item.get("description", item.get("text"))
            if isinstance(description, str) and description.strip():
                values.append(description.strip())
    return tuple(dict.fromkeys(values))


def _allowed_validation_commands(
    profiles: Sequence[DiscoveredValidationProfile],
) -> tuple[str, ...]:
    return tuple(
        f"{' '.join(profile.command)} *" for profile in profiles if profile.command
    )


def _run_validation_profile(
    parameters: Mapping[str, Any], *, worktree: Path, state: dict[str, Any]
) -> dict[str, Any]:
    profile = parameters.get("profile")
    if not isinstance(profile, Mapping):
        raise PowdrrExecutionError("validation profile must be a mapping")
    name = profile.get("name")
    command = profile.get("command")
    if not isinstance(name, str) or not name.strip():
        raise PowdrrExecutionError("validation profile name is malformed")
    if not isinstance(command, list) or not all(
        isinstance(item, str) and item for item in command
    ):
        raise PowdrrExecutionError("validation profile command is malformed")
    # The Procedrr flow runs one profile per loop iteration. Narrow the
    # request as well as the runner registry; ValidationRunner otherwise
    # resolves every declared profile and returns a multi-result report for
    # this single-profile operation.
    request = replace(state["request"], validation_profiles=(name,))
    report = ValidationRunner({name: ValidationProfile(name, tuple(command))}).run(
        request, state["attempt"], worktree_root=worktree
    )
    if len(report.results) != 1:
        raise PowdrrExecutionError(
            f"validation profile {name!r} did not produce one result"
        )
    return report.results[0].to_data()


def _aggregate_validation(
    parameters: Mapping[str, Any], *, worktree: Path, state: dict[str, Any]
) -> dict[str, Any]:
    del worktree
    raw_results = parameters.get("results")
    if not isinstance(raw_results, list) or not all(
        isinstance(item, Mapping) for item in raw_results
    ):
        raise PowdrrExecutionError("validation results must be a list of mappings")
    collected_results = _collected_results(raw_results)
    if collected_results is None:
        raise PowdrrExecutionError("validation results are malformed")
    results = tuple(
        ValidationResult(
            profile=str(item["profile"]),
            command=tuple(item.get("command", [])),
            status=ValidationResultStatus(str(item["status"])),
            returncode=item.get("returncode"),
            stdout=str(item.get("stdout", "")),
            stderr=str(item.get("stderr", "")),
            error=item.get("error"),
        )
        for item in collected_results
    )
    failed = any(
        result.status
        in (ValidationResultStatus.FAILED, ValidationResultStatus.TIMED_OUT)
        for result in results
    )
    blocked = any(result.status is ValidationResultStatus.BLOCKED for result in results)
    validation = ValidationReport(
        attempt_id=state["attempt"].attempt_id,
        request_id=state["request"].request_id,
        status=(
            ValidationReportStatus.BLOCKED
            if blocked
            else ValidationReportStatus.FAILED
            if failed
            else ValidationReportStatus.PASSED
        ),
        results=results,
    )
    failed_checkpoints = [
        checkpoint
        for checkpoint in state.get("operation_checkpoints", [])
        if isinstance(checkpoint, Mapping) and checkpoint.get("passed") is not True
    ]
    if failed_checkpoints:
        first_failure = failed_checkpoints[0]
        validation = ValidationReport(
            attempt_id=validation.attempt_id,
            request_id=validation.request_id,
            status=ValidationReportStatus.FAILED,
            results=(
                *validation.results,
                ValidationResult(
                    profile="operation-checkpoint",
                    command=(),
                    status=ValidationResultStatus.FAILED,
                    returncode=None,
                    error=(
                        "operation checkpoint failed: "
                        f"{first_failure.get('error', 'unknown failure')}"
                    ),
                ),
            ),
            error="one or more operation checkpoints failed",
        )
    state["attempt_store"].save_validation_report(validation)
    state["validation"] = validation
    return validation.to_data()


def _require_flow_text(parameters: Mapping[str, Any], name: str) -> str:
    value = parameters.get(name)
    if not isinstance(value, str) or not value.strip():
        raise PowdrrExecutionError(
            f"feature flow parameter {name!r} must be a non-empty string"
        )
    return value


def _command_option(command: list[Any], option: str) -> str:
    try:
        index = command.index(option)
        value = command[index + 1]
    except (ValueError, IndexError) as error:
        raise PowdrrExecutionError(f"flow command is missing {option}") from error
    if not isinstance(value, str) or not value.strip():
        raise PowdrrExecutionError(f"flow command option {option} is malformed")
    return value


def _feature_endpoint_result(
    state: dict[str, Any], branch: str, worktree: Path, status: str
) -> FeatureEndpointResult:
    return FeatureEndpointResult(
        status,
        branch,
        worktree,
        state["baseline_path"],
        state["plan_path"],
        state["request_path"],
        state.get("attempt"),
        state.get("validation"),
        state.get("review", {"passed": False}),
        state.get("pull_request_url"),
        state.get("changelog_path"),
        state.get("feature_obligations_path"),
    )


def _write_run_result(output_root: Path, result: FeatureEndpointResult) -> Path:
    """Persist one stable summary alongside the detailed run telemetry."""
    path = output_root / "run-result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result.to_data(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def review_feature_diff(
    worktree: Path,
    request: ImplementationRequest,
    attempt: CodingAgentAttempt,
    validation: ValidationReport,
    *,
    reconciliation: Mapping[str, Any] | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Review the worker result before any commit or PR operation."""
    changed = tuple(
        _git_output(runner, worktree, ["git", "diff", "--name-only"]).splitlines()
    )
    allowed = set(request.allowed_paths)
    out_of_scope = tuple(path for path in changed if path not in allowed)
    diff_check = runner(
        ["git", "diff", "--check"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    passed = (
        attempt.status is CodingAgentStatus.COMPLETED
        and validation.status is ValidationReportStatus.PASSED
        and (reconciliation is None or reconciliation.get("passed") is True)
        and bool(changed)
        and not out_of_scope
        and diff_check.returncode == 0
    )
    return {
        "passed": passed,
        "changed_paths": list(changed),
        "out_of_scope_paths": list(out_of_scope),
        "diff_check_returncode": diff_check.returncode,
        "worker_status": attempt.status.value,
        "validation_status": validation.status.value,
        "validation_error": validation.error,
        "verification_reconciliation": dict(reconciliation or {}),
    }


def _write_structrr_plan(
    worktree: Path,
    config: FeatureEndpointConfig,
    *,
    interview_input: Any,
) -> Path:
    slug = slugify_workflow_id(config.work_item_name)
    proposal = worktree / "docs" / "proposals" / slug
    proposal.mkdir(parents=True, exist_ok=True)
    path = proposal / "structrr-diff.yaml"
    interview = dict(interview_input) if isinstance(interview_input, Mapping) else {}
    sections = {
        key: _interview_edits(interview.get(f"{key}_edits"))
        for key in (
            "requirements",
            "approach",
            "entities",
            "entity_relationships",
            "invariants",
            "guidance",
            "features",
            "human_decisions",
            "intent",
            "intents",
            "acceptance_criteria",
            "expected_tests",
            "required_test_cases",
            "expected_outcomes",
            "non_goals",
            "risks",
            "decisions",
            "proposed_prs",
            "modules",
            "tools",
        )
    }
    acceptance_criteria = _plan_text_items(
        sections["acceptance_criteria"],
        fallback=(
            f"The requested feature behavior is implemented: "
            f"{config.feature_description}"
        ),
        prefix=slug,
    )
    acceptance_criteria = _append_sentence_plan_items(
        acceptance_criteria, config.feature_description
    )
    features = _append_sentence_design_items(
        sections["features"]
        or [{"id": slug, "description": config.feature_description, "action": "added"}],
        config.feature_description,
    )
    sections = {
        key: _ensure_intent_effects(items, config.feature_description)
        for key, items in sections.items()
    }
    features = _ensure_intent_effects(features, config.feature_description)
    document = {
        "schema": "https://powdrr.io/schema/changelog-v2",
        "change_id": slug,
        "title": config.work_item_name,
        "intent": {
            "problem": "The requested product behavior is not yet available.",
            "goal": config.feature_description,
        },
        "human-decisions": sections["human_decisions"],
        "files": [],
        "entities": sections["entities"]
        or [{"id": slug, "type": "Feature", "action": "added"}],
        "entity_relationships": sections["entity_relationships"],
        "features": features,
        "invariants": sections["invariants"],
        "guidance": sections["guidance"],
        "acceptance_criteria": acceptance_criteria,
        "required_test_cases": sections["required_test_cases"],
        "proposed_prs": sections["proposed_prs"],
    }
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    parse_change_log(path.read_text(encoding="utf-8"))
    return path


def _interview_edits(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Mapping):
        return []
    edits: list[dict[str, Any]] = []
    for action, items in (
        ("added", value.get("added")),
        ("deleted", value.get("deleted")),
    ):
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, Mapping):
                edits.append({**item, "action": action})
    return edits


def _ensure_intent_effects(
    items: Sequence[Mapping[str, Any]], feature_description: str
) -> list[dict[str, Any]]:
    """Make the intent effect explicit on every generated Structrr operation."""
    result: list[dict[str, Any]] = []
    for item in items:
        updated = dict(item)
        if (
            not isinstance(updated.get("intent_effect"), str)
            or not updated["intent_effect"].strip()
        ):
            action = str(updated.get("action", "added"))
            verb = (
                "preserves and adds to"
                if action in {"add", "added"}
                else "removes from"
            )
            updated["intent_effect"] = (
                f"{verb} the requested feature intent: {feature_description}"
            )
        result.append(updated)
    return result


def _plan_text_items(
    items: Sequence[Mapping[str, Any]], *, fallback: str, prefix: str
) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    for index, item in enumerate(items, start=1):
        description = item.get("description")
        if isinstance(description, str) and description.strip():
            identifier = item.get("id")
            values.append(
                {
                    "id": (
                        identifier.strip()
                        if isinstance(identifier, str) and identifier.strip()
                        else f"{prefix}-acceptance-{index}"
                    ),
                    "description": description.strip(),
                }
            )
            continue
        if isinstance(item.get("text"), str) and item["text"].strip():
            values.append(
                {
                    "id": f"{prefix}-acceptance-{index}",
                    "description": item["text"].strip(),
                }
            )
    return values or [{"id": f"{prefix}-acceptance-1", "description": fallback}]


def _append_sentence_plan_items(
    items: Sequence[Mapping[str, Any]], feature_description: str
) -> list[dict[str, Any]]:
    """Keep every instruction sentence visible as a plan-level criterion."""
    result = [dict(item) for item in items]
    existing_ids = {
        str(item.get("id")) for item in result if isinstance(item.get("id"), str)
    }
    for sentence in _decompose_feature_description(feature_description):
        sentence_id = str(sentence["id"])
        criterion_id = f"trace-{sentence_id}"
        if criterion_id in existing_ids:
            continue
        result.append(
            {
                "id": criterion_id,
                "description": f"The implementation must satisfy: {sentence['text']}",
            }
        )
        existing_ids.add(criterion_id)
    return result


def _append_sentence_design_items(
    items: Sequence[Mapping[str, Any]], feature_description: str
) -> list[dict[str, Any]]:
    """Represent each instruction sentence as a traceable design element."""
    result = [dict(item) for item in items]
    existing_ids = {
        str(item.get("id")) for item in result if isinstance(item.get("id"), str)
    }
    for sentence in _decompose_feature_description(feature_description):
        sentence_id = str(sentence["id"])
        design_id = f"trace-{sentence_id}"
        if design_id in existing_ids:
            continue
        result.append(
            {
                "id": design_id,
                "description": sentence["text"],
                "action": "added",
            }
        )
        existing_ids.add(design_id)
    return result


def _aggregate_category_edits(
    decisions: Mapping[str, Any],
    *,
    inventory: Sequence[Any] = (),
) -> dict[str, Any]:
    aggregated: dict[str, Any] = {}
    for category, decision in decisions.items():
        added: list[dict[str, Any]] = []
        deleted: list[dict[str, Any]] = []
        if isinstance(decision, Mapping):
            action = decision.get("action")
            item = decision.get("item")
            if action == "add" and isinstance(item, Mapping):
                added.append(dict(item))
            elif action == "delete" and isinstance(item, Mapping):
                deleted.append(dict(item))
        aggregated[str(category)] = {"added": added, "deleted": deleted}
    if "required_test_cases" in aggregated:
        aggregated["required_test_cases"] = {
            "added": _compile_required_test_case_edits(
                aggregated["required_test_cases"]["added"], inventory
            ),
            "deleted": aggregated["required_test_cases"]["deleted"],
        }
    return aggregated


def _compile_required_test_case_edits(
    items: Sequence[Any], inventory: Sequence[Any]
) -> list[dict[str, Any]]:
    """Compile semantic test obligations against discovered executable tests."""
    candidates = [item for item in inventory if isinstance(item, Mapping)]
    candidate_by_id = {_verification_inventory_id(item): item for item in candidates}
    pytest_profiles = [
        item
        for item in candidates
        if item.get("provider") == "pytest"
        and isinstance(item.get("profile"), str)
        and str(item.get("profile")).strip()
    ]
    compiled: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        unknown = set(item) - {
            "id",
            "description",
            "intent_refs",
            "expected_outcome",
            "test_selection",
            "existing_test",
        }
        if unknown:
            raise PowdrrExecutionError(
                "required test obligation contains executable fields that Powdrr "
                "must compile: " + ", ".join(sorted(unknown))
            )
        for field in ("id", "description"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise PowdrrExecutionError(
                    f"required test obligation requires non-empty {field}"
                )
        if not isinstance(item.get("intent_refs"), list) or not all(
            isinstance(value, str) and value.strip() for value in item["intent_refs"]
        ):
            raise PowdrrExecutionError(
                f"required test obligation {item['id']!r} has invalid intent_refs"
            )
        selection = item.pop("test_selection", item.pop("existing_test", "new"))
        expected_outcome = item.pop("expected_outcome", None)
        if isinstance(expected_outcome, str) and expected_outcome.strip():
            item["description"] = (
                f"{item['description'].strip()} "
                f"Expected outcome: {expected_outcome.strip()}"
            )
        if not isinstance(selection, str) or not selection.strip():
            raise PowdrrExecutionError(
                "required test case test_selection must be a candidate id or 'new'"
            )
        selection = selection.strip()
        if selection != "new":
            candidate = candidate_by_id.get(selection)
            if candidate is None:
                raise PowdrrExecutionError(
                    f"required test case selected unknown inventory entry {selection!r}"
                )
            item.update(
                {
                    "provider": candidate.get("provider"),
                    "profile": candidate.get("profile"),
                    "selector": candidate.get("selector"),
                }
            )
        else:
            if not pytest_profiles:
                raise PowdrrExecutionError(
                    "new required test cases need a discovered pytest profile"
                )
            profile = pytest_profiles[0]
            identifier = str(item.get("id", "required-test")).strip()
            test_slug = re.sub(r"[^a-z0-9]+", "_", identifier.lower()).strip("_")
            if not test_slug:
                raise PowdrrExecutionError(
                    "new required test case id must produce a test selector"
                )
            item.update(
                {
                    "provider": "pytest",
                    "profile": profile.get("profile"),
                    "selector": (f"tests/test_{test_slug}.py::test_{test_slug}"),
                }
            )
        item.update(
            {
                "expectation": "pass",
                "applicability": {"mode": "affected_closure"},
                "status": "active",
            }
        )
        compiled.append(item)
    return compiled


def _verification_inventory_id(item: Mapping[str, Any]) -> str:
    if isinstance(item.get("inventory_id"), str) and item["inventory_id"].strip():
        return item["inventory_id"].strip()
    return ":".join(
        str(item.get(field, "")) for field in ("provider", "profile", "selector")
    )


def _resolve_flow_path(worktree: Path, relative_path: str) -> Path:
    target = (worktree / relative_path).resolve()
    if target != worktree.resolve() and worktree.resolve() not in target.parents:
        raise PowdrrExecutionError(f"flow path escapes worktree: {relative_path}")
    return target


def _apply_flow_yaml_edit(
    worktree: Path, parameters: Mapping[str, Any]
) -> dict[str, Any]:
    edit = parameters.get("edit")
    if not isinstance(edit, Mapping):
        raise PowdrrExecutionError("yaml_edit requires an edit mapping")
    file_path = edit.get("path", parameters.get("file_path"))
    edits = edit.get("edits")
    if not isinstance(file_path, str) or not isinstance(edits, list):
        raise PowdrrExecutionError("yaml_edit requires path and edits")
    target = _resolve_flow_path(worktree, file_path)
    document = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise PowdrrExecutionError("yaml_edit target must contain a mapping")
    updated = dict(document)
    for item in edits:
        if not isinstance(item, Mapping):
            raise PowdrrExecutionError("yaml_edit entries must be mappings")
        path = item.get("path")
        if (
            not isinstance(path, list)
            or not path
            or not all(isinstance(part, str) for part in path)
        ):
            raise PowdrrExecutionError("yaml_edit entries require a path list")
        cursor: Any = updated
        for part in path[:-1]:
            if not isinstance(cursor, Mapping) or part not in cursor:
                raise PowdrrExecutionError(f"yaml_edit path does not exist: {path}")
            cursor = cursor[part]
        if not isinstance(cursor, dict):
            raise PowdrrExecutionError(f"yaml_edit parent is not a mapping: {path}")
        cursor[path[-1]] = item.get("value")
    target.write_text(yaml.safe_dump(updated, sort_keys=False), encoding="utf-8")
    return {"path": str(target), "edits": len(edits)}


def _validate_procedrr_flow(worktree: Path) -> Path:
    repository_path = (
        worktree / "docs" / "procedrr" / "skill-definitions" / "implement-feature.yaml"
    )
    path = repository_path
    if not path.is_file():
        path = Path(__file__).resolve().parents[2] / "implement-feature.yaml"
    try:
        parse_and_validate(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PowdrrExecutionError(
            f"Shared feature Procedrr definition is invalid: {path}: {error}"
        ) from error
    return path


def _structrr_taxonomy_path(worktree: Path) -> Path:
    repository_path = worktree / "software_development_entity_taxonomy.md"
    if repository_path.is_file():
        return repository_path
    return (
        Path(__file__).resolve().parents[2] / "software_development_entity_taxonomy.md"
    )  # noqa: E501


def _ensure_current_baseline(worktree: Path, runner: Runner) -> Path:
    """Reuse a current baseline only when every bootstrap section is current."""
    relative_paths = _git_output(
        runner,
        worktree,
        ["git", "ls-files", "docs/structrr/current/baseline-*.yaml"],
    ).splitlines()
    if not relative_paths:
        baseline = bootstrap_structrr(
            worktree, taxonomy_path=_structrr_taxonomy_path(worktree)
        )
        if not baseline.validation.successful:
            raise PowdrrExecutionError(
                f"Structrr bootstrap validation failed: {baseline.validation.issues}"
            )
        _commit(runner, worktree, "Bootstrap Structrr baseline")
        return baseline.output_path
    ranked: list[tuple[int, str]] = []
    for relative_path in relative_paths:
        timestamp = _git_output(
            runner,
            worktree,
            ["git", "log", "-1", "--format=%ct", "--", relative_path],
        )
        ranked.append((int(timestamp or "0"), relative_path))
    _, selected = max(ranked)
    selected_path = worktree / selected
    try:
        document = _load_yaml_mapping(selected_path)
    except PowdrrExecutionError:
        document = {}
    section_issues = validate_bootstrap_sections(document)
    if not section_issues:
        return selected_path
    baseline = bootstrap_structrr(
        worktree, taxonomy_path=_structrr_taxonomy_path(worktree)
    )
    if not baseline.validation.successful:
        raise PowdrrExecutionError(
            f"Structrr bootstrap regeneration failed: {baseline.validation.issues}"
        )
    _commit(runner, worktree, "Refresh Structrr baseline sections")
    return baseline.output_path


def _bootstrap_validation_profiles(
    worktree: Path,
    *,
    output_root: Path,
    explicit_command: tuple[str, ...],
) -> tuple[DiscoveredValidationProfile, ...]:
    """Run Structrr bootstrap and adapt its detected tools for Workrr."""
    if explicit_command:
        return (
            DiscoveredValidationProfile(
                "feature-validation", explicit_command, "feature command"
            ),
        )
    bootstrap = bootstrap_structrr(
        worktree,
        output_path=output_root / "validation-bootstrap.yaml",
        taxonomy_path=_structrr_taxonomy_path(worktree),
    )
    if not bootstrap.validation.successful:
        raise PowdrrExecutionError(
            "Structrr bootstrap validation failed while discovering validation "
            f"tools: {bootstrap.validation.issues}"
        )
    profiles: list[DiscoveredValidationProfile] = []
    for tool in bootstrap.document.get("tools", []):
        if not isinstance(tool, Mapping):
            continue
        tool_id = tool.get("id")
        command = tool.get("validation_action")
        if not isinstance(tool_id, str) or not tool_id.startswith("validation:"):
            continue
        if not isinstance(command, list) or not all(
            isinstance(item, str) and item for item in command
        ):
            continue
        profiles.append(
            DiscoveredValidationProfile(
                tool_id.removeprefix("validation:"),
                tuple(command),
                str(tool.get("source", "Structrr bootstrap")),
            )
        )
    if not profiles:
        profiles.append(
            DiscoveredValidationProfile(
                "repository-validation",
                ("true",),
                "no repository-declared validation command",
            )
        )
    unique: dict[str, DiscoveredValidationProfile] = {}
    for profile in profiles:
        unique.setdefault(profile.name, profile)
    return tuple(unique.values())


def _require_clean_root(root: Path, runner: Runner) -> None:
    result = _run(runner, root, ["git", "status", "--porcelain"])
    if result.stdout.strip():
        raise PowdrrExecutionError("Feature endpoint requires a clean caller worktree.")


def _commit(runner: Runner, worktree: Path, message: str) -> None:
    _run(runner, worktree, ["git", "add", "-A"])
    _run(runner, worktree, ["git", "commit", "-am", message])


def _create_pr_changelog(
    runner: Runner,
    worktree: Path,
    branch: str,
    pull_request_url: str,
    config: FeatureEndpointConfig,
) -> Path:
    match = re.search(r"/pull/(\d+)", pull_request_url)
    if match is None:
        raise PowdrrExecutionError(
            f"Could not determine the pull request number from {pull_request_url!r}."
        )
    pr_number = match.group(1)
    proposal_root = (
        worktree / "docs" / "proposals" / slugify_workflow_id(config.work_item_name)
    )
    plan_path = proposal_root / "structrr-diff.yaml"
    try:
        plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise PowdrrExecutionError(
            f"could not read Structrr plan for changelog: {plan_path}: {error}"
        ) from error
    if not isinstance(plan, Mapping):
        raise PowdrrExecutionError(f"Structrr plan is not a mapping: {plan_path}")
    path = worktree / "docs" / "changelogs" / f"PR-{pr_number}-changelog.yaml"
    proposal_prefix = f"docs/proposals/{proposal_root.name}/"
    changed_paths = [
        changed_path
        for changed_path in _git_output(
            runner,
            worktree,
            ["git", "diff", "--name-only", f"origin/{config.base_branch}...HEAD"],
        ).splitlines()
        if not changed_path.startswith(proposal_prefix)
    ]
    descriptor = {
        "schema": "https://powdrr.io/schema/changelog-v2",
        "change_id": f"PR-{pr_number}",
        "title": config.work_item_name,
        "intent": plan.get(
            "intent",
            {
                "problem": "The requested product behavior is not yet available.",
                "goal": config.feature_description,
            },
        ),
        "human-decisions": plan.get("human-decisions", []),
        "files": [
            {
                "path": changed_path,
                "type": _changelog_file_type(changed_path),
                "span": {"start_line": 1, "end_line": 1},
                "summary": f"Changed as part of {config.work_item_name}.",
                "rationale": config.feature_description,
            }
            for changed_path in changed_paths
        ],
        "entities": plan.get("entities", []),
        "entity_relationships": plan.get("entity_relationships", []),
        "invariants": plan.get("invariants", []),
        "guidance": plan.get("guidance", []),
        "features": plan.get("features", []),
        "acceptance_criteria": plan.get("acceptance_criteria", []),
        "proposed_prs": plan.get("proposed_prs", []),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(descriptor, sort_keys=False), encoding="utf-8")
    parse_change_log(path.read_text(encoding="utf-8"))
    _commit(runner, worktree, f"Add PR-{pr_number} changelog descriptor")
    _run(runner, worktree, ["git", "push", "origin", branch])
    if proposal_root.exists():
        shutil.rmtree(proposal_root)
        _commit(runner, worktree, "Remove temporary feature planning artifacts")
        _run(runner, worktree, ["git", "push", "origin", branch])
    return path


def _changelog_file_type(path: str) -> str:
    if path.startswith("tests/"):
        return "Test file"
    if path.endswith((".yaml", ".yml", ".json", ".toml")):
        return "Configuration file"
    if path.startswith("docs/"):
        return "Documentation"
    return "Source file"


def _update_pull_request_description(
    runner: Runner,
    worktree: Path,
    pull_request_url: str,
    config: FeatureEndpointConfig,
    changelog_relative_path: Path,
) -> None:
    body = (
        f"## Feature\n\n{config.feature_description}\n\n"
        f"PR changelog descriptor: `{changelog_relative_path}`\n"
        "Implemented by the bounded Workrr/OpenCode handoff and reviewed before commit."
    )
    _run(
        runner,
        worktree,
        ["gh", "pr", "edit", pull_request_url, "--body", body],
    )


def _open_pull_request(
    runner: Runner,
    worktree: Path,
    config: FeatureEndpointConfig,
    branch: str,
) -> str:
    body = (
        f"## Feature\n\n{config.feature_description}\n\n"
        "Implemented by the bounded Workrr/OpenCode handoff and reviewed before commit."
    )
    result = _run(
        runner,
        worktree,
        [
            "gh",
            "pr",
            "create",
            "--base",
            config.base_branch,
            "--head",
            branch,
            "--title",
            config.work_item_name,
            "--body",
            body,
        ],
    )
    return result.stdout.strip().splitlines()[-1]


def _run(
    runner: Runner, cwd: Path, command: list[str]
) -> subprocess.CompletedProcess[str]:
    result = runner(command, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise PowdrrExecutionError(
            f"Command failed ({result.returncode}): {' '.join(command)}\n"
            f"{result.stderr.strip()}"
        )
    return result


def _git_output(runner: Runner, cwd: Path, command: list[str]) -> str:
    return _run(runner, cwd, command).stdout.strip()


__all__ = [
    "FeatureEndpointConfig",
    "FeatureEndpointResult",
    "review_feature_diff",
    "run_feature_in_place",
    "run_feature_endpoint",
]
