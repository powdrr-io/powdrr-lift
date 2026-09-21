"""The typed internal command catalog used by the feature Procedrr flow."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

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


__all__ = ["feature_command_catalog"]
