"""Closed names for public operations and agent capabilities."""

from __future__ import annotations

from dataclasses import dataclass

OPERATION_KINDS = frozenset(
    {
        "design",
        "plan",
        "replan",
        "implement",
        "review_feedback",
        "bootstrap",
        "inspect_artifacts",
        "compile_structrr_diff",
        "verification_health",
        "verification_diff",
        "evaluate_pr_against_changelog",
        "evaluate_deepswe_design",
        "evaluate_deepswe_probes",
        "evaluate_deepswe_prompt",
        "evaluate_prompt",
        "compare_prompt_benchmark",
        "validate_delivery_profile",
        "validate_pr_files",
        "validate_project_structure",
        "validate_procedrr_processes",
    }
)


@dataclass(frozen=True)
class CapabilitySpec:
    name: str
    version: str
    argument_schema: str


CAPABILITIES = {
    item.name: item
    for item in (
        CapabilitySpec(
            "repository.pinned_file_snapshot", "1", "repository_snapshot_v1"
        ),
        CapabilitySpec("repository.pinned_file_read", "1", "pinned_file_read_v1"),
        CapabilitySpec("repository.bounded_search", "1", "bounded_search_v1"),
        CapabilitySpec("worktree.observe", "1", "worktree_observation_v1"),
        CapabilitySpec("worktree.create", "1", "worktree_create_v1"),
        CapabilitySpec("file.materialize", "1", "file_materialize_v1"),
        CapabilitySpec("command.execute", "1", "command_execution_v1"),
        CapabilitySpec("git.mutate", "1", "git_mutation_v1"),
        CapabilitySpec("user.interact", "1", "user_interaction_v1"),
    )
}


def registered_operation(kind: str) -> bool:
    return kind in OPERATION_KINDS
