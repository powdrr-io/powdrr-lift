"""The typed internal command catalog used by the feature Procedrr flow."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from powdrr_lift.core.decision_obligation import content_fingerprint
from powdrr_lift.core.feature_obligation import (
    FeatureObligationError,
    compile_feature_design,
)
from powdrr_lift.core.instruction_ledger import (
    InstructionLedger,
    InstructionLedgerError,
    compile_instruction_ledger,
)
from powdrr_lift.errors import PowdrrExecutionError
from procedrr.command_catalog import CommandCatalog, CommandSpec, object_schema

_OUTPUT: dict[str, Any] = {}


def _spec(
    name: str,
    parameters: Iterable[str],
    *,
    required: bool = True,
    logic: Callable[[Mapping[str, Any]], Any] | None = None,
) -> CommandSpec:
    names = tuple(parameters)
    return CommandSpec(
        name=name,
        input_schema=object_schema(
            {parameter: {} for parameter in names},
            required=names if required else (),
            additional_properties=False,
        ),
        output_schema=_OUTPUT,
        logic=logic,
    )


def feature_command_catalog(
    implementations: Mapping[str, Callable[[Mapping[str, Any]], Any]] | None = None,
) -> CommandCatalog:
    """Return the implement-feature commands and their implementations.

    Static callers omit ``implementations`` and receive the same catalog with
    dispatch slots intentionally empty. Runtime callers provide the bound
    implementations, making each returned ``CommandSpec`` the complete command
    object used for validation and dispatch.
    """
    implementations = implementations or {}
    commands = {
        "ensure_current_structrr": (),
        "discover_validation_profiles": ("baseline",),
        "compile_instruction_ledger": ("work_item_name", "feature_description"),
        "merge_semantic_design": (
            "kind",
            "description",
            "acceptance_criterion",
            "expected_test",
        ),
        "compile_canonical_feature_design": ("work_item_name", "design_decisions"),
        "plan_structrr_diff": (
            "baseline",
            "work_item_name",
            "feature_description",
            "feature_design",
        ),
        "materialize_feature_intents": ("plan", "obligations"),
        "compile_verification_obligations": (
            "baseline",
            "plan",
            "feature_description",
        ),
        "assert_verification_obligations_complete": ("verification_obligations",),
        "prepare_proposal_review": (
            "baseline",
            "plan",
            "feature_description",
            "verification_obligations",
        ),
        "bind_proposal_decision_results": ("worklist", "decisions"),
        "finalize_proposal_review": (
            "proposal_revision_path",
            "worklist_path",
            "decisions",
        ),
        "run_opencode": (
            "baseline",
            "plan",
            "proposal_review_receipt",
            "work_item_name",
            "feature_description",
            "obligations",
            "verification_obligations",
            "repair_issue",
            "repair_request",
            "review_verdict",
        ),
        "run_validation_profile": ("profile", "implementation"),
        "aggregate_validation": ("implementation", "results"),
        "validate_required_test_cases": ("plan",),
        "run_verification_evidence": ("obligations",),
        "reconcile_verification_evidence": (
            "obligations",
            "evidence",
            "candidate_tree",
        ),
        "review_worker_diff": ("implementation", "validation"),
        "prepare_implementation_review": ("implementation", "validation", "review"),
        "compile_obligation_review_packets": ("obligations", "evidence"),
        "bind_obligation_reviews": ("packets", "decisions"),
        "aggregate_obligation_reviews": (
            "packets",
            "reviews",
            "verification_reconciliation",
        ),
        "collect_repair_issues": ("validation", "review", "reconciliation"),
        "open_pull_request": (
            "review",
            "plan",
            "work_item_name",
            "feature_description",
        ),
        "create_pr_changelog": (
            "pull_request",
            "plan",
            "work_item_name",
            "feature_description",
        ),
        "update_pull_request": ("pull_request", "changelog"),
    }
    return CommandCatalog(
        tuple(
            _spec(
                name,
                parameters,
                required=name != "run_opencode",
                logic=implementations.get(name),
            )
            for name, parameters in commands.items()
        )
    )


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
                decisions, inventory=state.get("provider_inventory", ())
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

        def merge_semantic_design_operation() -> Any:
            """Join the independently elicited semantic fields for one clause."""
            required = (
                "kind",
                "description",
                "acceptance_criterion",
                "expected_test",
            )
            values = {name: parameters.get(name) for name in required}
            if any(
                not isinstance(value, str) or not value.strip()
                for value in values.values()
            ):
                raise PowdrrExecutionError(
                    "merge_semantic_design requires non-empty semantic fields"
                )
            if values["kind"] not in {
                "entity",
                "feature",
                "interface",
                "invariant",
                "guidance",
                "non_goal",
            }:
                raise PowdrrExecutionError("merge_semantic_design kind is invalid")
            return values

        def compile_canonical_feature_design_operation() -> Any:
            ledger_path = state.get("instruction_ledger_path")
            if not isinstance(ledger_path, Path):
                raise PowdrrExecutionError("instruction ledger is unavailable")
            try:
                ledger = InstructionLedger.from_data(
                    json.loads(ledger_path.read_text(encoding="utf-8"))
                )
            except (OSError, json.JSONDecodeError, InstructionLedgerError) as exc:
                raise PowdrrExecutionError(
                    f"instruction ledger cannot be loaded: {exc}"
                ) from exc
            raw_design_decisions = feature_endpoint._collected_results(
                parameters.get("design_decisions")
            )
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
            path = output_root / "canonical-feature-design.json"
            path.write_text(
                json.dumps(design.to_data(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            state["canonical_feature_design_path"] = path
            state["feature_obligations_path"] = path
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
            )
            return {
                "path": str(path),
                "fingerprint": content_fingerprint(design.to_data()),
                "obligations": obligations,
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

        handlers: dict[str, Callable[[], Any]] = {
            "extract_proposal_issues": extract_proposal_issues,
            "aggregate_category_edits": aggregate_category_edits,
            "decompose_feature_description": decompose_feature_description,
            "compile_instruction_ledger": compile_instruction_ledger_operation,
            "merge_semantic_design": merge_semantic_design_operation,
            "compile_canonical_feature_design": (
                compile_canonical_feature_design_operation
            ),
            "apply_sentence_design_trace": apply_sentence_design_trace,
            "materialize_feature_intents": materialize_feature_intents,
            "compile_verification_obligations": compile_verification_obligations,
            "assert_verification_obligations_complete": (
                assert_verification_obligations_complete
            ),
        }

        def bind_handler(
            handler: Callable[[], Any],
        ) -> Callable[[Mapping[str, Any]], Any]:
            def dispatch(_parameters: Mapping[str, Any]) -> Any:
                return handler()

            return dispatch

        runtime_catalog = feature_command_catalog(
            implementations={
                name: bind_handler(handler) for name, handler in handlers.items()
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
        if name == "run_opencode":
            return feature_endpoint._run_opencode_phase(
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


__all__ = ["FeatureCommandRuntime", "feature_command_catalog"]
