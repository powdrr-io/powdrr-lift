from __future__ import annotations

import argparse
import difflib
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.blame_ui import serve as serve_blame_ui
from powdrr_lift.core import (
    architecture_specification_default_output_path,
    build_architecture_specification_validation_report,
    build_current_decisions_report,
    build_delivery_profile_validation_report,
    build_implementation_specification_validation_report,
    build_invariants_report,
    build_pr_specification_validation_report,
    build_system_specification_validation_report,
    codebase_state_default_output_path,
    create_architecture_specification_template,
    create_change_log_template,
    create_change_log_template_from_plan_diff,
    create_codebase_state,
    create_current_state_specification,
    create_feature_pr_specification_template,
    create_implementation_specification_template,
    create_plan_diff_specification,
    create_pr_specification_template,
    create_system_map_specification_template,
    create_system_specification_template,
    current_state_specification_default_output_path,
    feature_pr_specification_default_output_path,
    implementation_specification_default_output_path,
    lookup_edit_context,
    lookup_entity_decisions,
    lookup_entity_references,
    lookup_entity_relationships,
    parse_line_ranges,
    parse_validation_report,
    plan_diff_specification_default_output_path,
    pr_specification_default_output_path,
    render_current_decisions_report,
    render_edit_context_report,
    render_entity_decision_report,
    render_entity_reference_report,
    render_entity_relationship_report,
    render_invariants_report,
    render_proposed_pr_search_report,
    resolve_repo_root,
    search_proposed_pr_specifications,
    show_proposed_pr_specification,
    start_planning_feature,
    system_map_specification_default_output_path,
    system_specification_default_output_path,
    validate_architecture_specification_yaml,
    validate_change_log_yaml,
    validate_implementation_specification_yaml,
    validate_pr_specification_yaml,
    validate_skill_directory,
    validate_system_specification_yaml,
    validate_workflow_task_directory,
    validate_workflow_template_json,
)
from powdrr_lift.core.entity_taxonomy import load_entity_taxonomy
from powdrr_lift.core.pr_specification import load_proposed_pr_dependency_graph
from powdrr_lift.core.project_structure import (
    create_project_structure_template,
    validate_project_structure_yaml,
)
from powdrr_lift.core.specification_deduplication import (
    deduplicate_specification_ids,
    reformat_specification_file,
)
from powdrr_lift.core.specification_v1 import normalize_specification_v1_file
from powdrr_lift.core.workflow_relationships import (
    WorkflowRelationshipValidationIssue,
    WorkflowRelationshipValidationReport,
    validate_workflow_relationships,
)
from powdrr_lift.core.workflow_task_specification import HumanRole
from powdrr_lift.core.workflow_template_specification import (
    instantiate_workflow_template,
    instantiated_workflow_relationships,
    load_workflow_template,
)
from powdrr_lift.openai_proxy import (
    OpenAIProxyConfig,
    default_openai_proxy_log_dir,
)
from powdrr_lift.openai_proxy import (
    serve as serve_openai_proxy,
)
from powdrr_lift.pull_request_description import (
    find_existing_pull_request,
    render_pull_request_description_template,
)
from powdrr_lift.repository_state import render_repository_state
from powdrr_lift.workflow_ambiguity_review import (
    WorkflowAmbiguityReviewError,
    review_workflow_definition_step,
)
from powdrr_lift.workflow_chat_agent import (
    ALL_PROVIDERS,
    WorkflowChatConfig,
    _build_chat_client,
    _default_llm_mappings,
    _resolve_credentials,
    choose_workflow_provider,
    download_local_qwen_model,
    resolve_workflow_provider,
    run_workflow_chat,
)
from powdrr_lift.workflow_chat_tui import run_workflow_chat_tui
from powdrr_lift.workflow_definition_analysis import (
    analyze_workflow_definition,
    render_skill_prompt_snapshots,
)
from powdrr_lift.workflow_definition_comparison import (
    WorkflowComparisonError,
    compare_workflow_definitions,
)
from powdrr_lift.workflow_error_analysis import (
    WorkflowErrorAnalysisError,
    cluster_workflow_errors,
    load_workflow_error_records,
    promote_replay_candidates,
    workflow_error_analysis_data,
)
from powdrr_lift.workflow_git import (
    WorkflowGitState,
    cleanup_workflow_run,
    commit_and_push_workflow_initialization,
    create_workflow_worktree,
    inspect_workflow_run,
    resolve_git_repository_root,
    save_workflow_git_state,
    synchronize_workflow_initialization,
)
from powdrr_lift.workflow_human_task import (
    HumanTaskRunnerConfig,
    run_human_task,
)
from powdrr_lift.workflow_replay import (
    WorkflowReplayError,
    load_error_record,
    load_workflow_replay_bundle,
    render_skill_replay,
    replay_bundle_from_error_record,
    save_workflow_replay_bundle,
)
from powdrr_lift.workflow_scenario import (
    WorkflowScenarioError,
    load_workflow_scenario,
    run_workflow_scenario,
)
from powdrr_lift.workflow_task_agent import (
    WorkflowTaskAgentConfig,
    run_workflow_task,
)
from powdrr_lift.workflow_tuning import (
    WorkflowTuningError,
    save_workflow_tuning_report,
    tune_workflow,
)

_WORKFLOW_FILE_ADDED_EVENT_PREFIX = "[powdrr-file-added] "


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="powdrr-lift")
    subparsers = parser.add_subparsers(dest="command", required=True)

    repository_state_parser = subparsers.add_parser(
        "repository-state",
        aliases=["repository_state"],
        help="Print structured Git branch and worktree state as JSON.",
    )
    repository_state_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to inspect; defaults to the current worktree.",
    )
    repository_state_parser.set_defaults(func=_run_repository_state)

    pull_request_description_parser = subparsers.add_parser(
        "pull-request-description",
        aliases=["pull_request_description", "pr-description"],
        help="Print the instructed pull-request description template.",
    )
    pull_request_description_parser.add_argument(
        "--kind",
        choices=(
            "general",
            "feature",
            "project-structure",
            "ci-fix",
            "merge-conflict",
            "review-comments",
        ),
        default="general",
        help="Workflow-specific section to append to the common template.",
    )
    pull_request_description_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Worktree to inspect for an existing pull request; defaults to cwd.",
    )
    pull_request_description_parser.set_defaults(func=_run_pull_request_description)

    init_parser = subparsers.add_parser(
        "init",
        help="Generate a ChangeLog template for the current branch.",
    )
    init_parser.add_argument(
        "branch_name",
        nargs="?",
        help="Branch name to compare against the default branch.",
    )
    init_parser.add_argument(
        "--output",
        type=Path,
        help="Write the template to this path instead of the default file.",
    )
    init_parser.add_argument(
        "--pr-number",
        type=int,
        help=(
            "Write the template to docs/changelogs/PR-<num>-changelog.yaml and "
            "print the next workflow step."
        ),
    )
    init_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    init_parser.add_argument(
        "--default-branch",
        help="Override the default branch name.",
    )
    init_parser.set_defaults(func=_run_init)

    init_from_plan_diff_parser = subparsers.add_parser(
        "init-from-plan-diff",
        aliases=["init_from_plan_diff"],
        help="Generate a ChangeLog template from a plan diff specification.",
    )
    init_from_plan_diff_parser.add_argument(
        "branch_name",
        nargs="?",
        help="Branch name to compare against the default branch.",
    )
    init_from_plan_diff_parser.add_argument(
        "--plan-diff",
        type=Path,
        required=True,
        help="Plan diff specification to use when pre-filling related sections.",
    )
    init_from_plan_diff_parser.add_argument(
        "--output",
        type=Path,
        help="Write the template to this path instead of the default file.",
    )
    init_from_plan_diff_parser.add_argument(
        "--pr-number",
        type=int,
        help=(
            "Write the template to docs/changelogs/PR-<num>-changelog.yaml and "
            "print the next workflow step."
        ),
    )
    init_from_plan_diff_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    init_from_plan_diff_parser.add_argument(
        "--default-branch",
        help="Override the default branch name.",
    )
    init_from_plan_diff_parser.set_defaults(func=_run_init_from_plan_diff)

    evaluate_parser = subparsers.add_parser(
        "evaluate-pr-against-changelog",
        aliases=["evaluate_pr_against_changelog"],
        help="Validate a proposed ChangeLog against the branch diff.",
    )
    evaluate_parser.add_argument(
        "branch_name",
        nargs="?",
        help="Branch name to compare against the default branch.",
    )
    evaluate_parser.add_argument(
        "--input",
        type=Path,
        help="Read the proposed ChangeLog YAML from this file instead of stdin.",
    )
    evaluate_parser.add_argument(
        "--pr-number",
        type=int,
        help=(
            "Read docs/changelogs/PR-<num>-changelog.yaml and print the final "
            "workflow step."
        ),
    )
    evaluate_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    evaluate_parser.add_argument(
        "--default-branch",
        help="Override the default branch name.",
    )
    evaluate_parser.set_defaults(func=_run_evaluate)

    validate_pr_files_parser = subparsers.add_parser(
        "validate-pr-files",
        aliases=["validate_pr_files"],
        help=(
            "Run repository format validators for every recognized changed file "
            "in a PR or the checked-out branch."
        ),
    )
    validate_pr_files_parser.add_argument(
        "--pr-number",
        type=int,
        help=(
            "GitHub pull request number whose changed files should be validated. "
            "If omitted, validate files changed on the checked-out branch."
        ),
    )
    validate_pr_files_parser.add_argument(
        "--base-branch",
        default="main",
        help=("Branch to compare with when --pr-number is omitted (default: main)."),
    )
    validate_pr_files_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root and checked-out PR worktree to validate.",
    )
    validate_pr_files_parser.set_defaults(func=_run_validate_pr_files)

    delivery_profile_parser = subparsers.add_parser(
        "validate-delivery-profile",
        aliases=["validate_delivery_profile"],
        help="Validate a typed delivery profile.",
    )
    delivery_profile_parser.add_argument(
        "profile",
        type=Path,
        help="Delivery profile YAML file to validate.",
    )
    delivery_profile_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the machine-readable validation report.",
    )
    delivery_profile_parser.set_defaults(func=_run_validate_delivery_profile)

    edit_context_parser = subparsers.add_parser(
        "edit-context",
        aliases=["edit_context"],
        help="Report changelog-backed context for a file and line ranges.",
    )
    edit_context_parser.add_argument(
        "branch_name",
        nargs="?",
        help="Branch name to inspect. Defaults to the current branch.",
    )
    edit_context_parser.add_argument(
        "--file",
        required=True,
        help="Repository-relative file path to inspect.",
    )
    edit_context_parser.add_argument(
        "--range",
        dest="line_ranges",
        action="append",
        required=True,
        metavar="START:END",
        help="Line range to inspect. May be repeated.",
    )
    edit_context_parser.add_argument(
        "--parent-branch",
        required=True,
        help="Reference parent branch used to build the index.",
    )
    edit_context_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    edit_context_parser.set_defaults(func=_run_edit_context)

    entity_decisions_parser = subparsers.add_parser(
        "entity-decisions",
        aliases=["entity_decisions"],
        help="Report changelog decisions for PRs that mention an entity.",
    )
    entity_decisions_parser.add_argument(
        "branch_name",
        nargs="?",
        help="Branch name to inspect. Defaults to the current branch.",
    )
    entity_decisions_parser.add_argument(
        "--entity",
        required=True,
        help="Canonical entity name to inspect.",
    )
    entity_decisions_parser.add_argument(
        "--parent-branch",
        required=True,
        help="Reference parent branch used to build the index.",
    )
    entity_decisions_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    entity_decisions_parser.set_defaults(func=_run_entity_decisions)

    entity_references_parser = subparsers.add_parser(
        "entity-references",
        aliases=["entity_references"],
        help="Report changelog-backed references for a named entity.",
    )
    entity_references_parser.add_argument(
        "branch_name",
        nargs="?",
        help="Branch name to inspect. Defaults to the current branch.",
    )
    entity_references_parser.add_argument(
        "--entity",
        required=True,
        help="Canonical entity name to inspect.",
    )
    entity_references_parser.add_argument(
        "--parent-branch",
        required=True,
        help="Reference parent branch used to build the index.",
    )
    entity_references_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    entity_references_parser.set_defaults(func=_run_entity_references)

    entity_relationships_parser = subparsers.add_parser(
        "entity-relationships",
        aliases=["entity_relationships"],
        help="Report graph relationships for a named entity.",
    )
    entity_relationships_parser.add_argument(
        "branch_name",
        nargs="?",
        help="Branch name to inspect. Defaults to the current branch.",
    )
    entity_relationships_parser.add_argument(
        "--entity",
        required=True,
        help="Canonical entity name to inspect.",
    )
    entity_relationships_parser.add_argument(
        "--parent-branch",
        required=True,
        help="Reference parent branch used to build the index.",
    )
    entity_relationships_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    entity_relationships_parser.set_defaults(func=_run_entity_relationships)

    invariants_parser = subparsers.add_parser(
        "invariants",
        aliases=["invariants_report"],
        help="Report current invariants for the branch.",
    )
    invariants_parser.add_argument(
        "branch_name",
        nargs="?",
        help="Branch name to inspect. Defaults to the current branch.",
    )
    invariants_parser.add_argument(
        "--parent-branch",
        required=True,
        help="Reference parent branch used to build the index.",
    )
    invariants_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    invariants_parser.set_defaults(func=_run_invariants)

    current_decisions_parser = subparsers.add_parser(
        "current-decisions",
        aliases=["current_decisions"],
        help="Report the current decisions for the branch.",
    )
    current_decisions_parser.add_argument(
        "branch_name",
        nargs="?",
        help="Branch name to inspect. Defaults to the current branch.",
    )
    current_decisions_parser.add_argument(
        "--parent-branch",
        required=True,
        help="Reference parent branch used to build the index.",
    )
    current_decisions_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    current_decisions_parser.set_defaults(func=_run_current_decisions)

    codebase_state_parser = subparsers.add_parser(
        "codebase-state",
        aliases=["codebase_state"],
        help="Generate a changelog-derived snapshot of the current codebase.",
    )
    codebase_state_parser.add_argument(
        "branch_name",
        nargs="?",
        help="Branch name to inspect. Defaults to the current branch.",
    )
    codebase_state_parser.add_argument(
        "--parent-branch",
        help="Reference parent branch used to build the index.",
    )
    codebase_state_parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Write the snapshot to this path instead of "
            ".powdrr-lift/state/codebase-state.yaml."
        ),
    )
    codebase_state_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    codebase_state_parser.set_defaults(func=_run_codebase_state)

    current_state_parser = subparsers.add_parser(
        "synthesize-current-state",
        aliases=["synthesize_current_state"],
        help="Synthesize the current specification state from indexed files.",
    )
    current_state_parser.add_argument(
        "branch_name",
        nargs="?",
        help="Branch name to inspect. Defaults to the current branch.",
    )
    current_state_parser.add_argument(
        "--parent-branch",
        help="Reference parent branch used to build the index.",
    )
    current_state_parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Write the synthesized state to this path instead of "
            ".powdrr-lift/state/current-state.yaml."
        ),
    )
    current_state_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    current_state_parser.set_defaults(func=_run_current_state)

    architecture_specification_parser = subparsers.add_parser(
        "architecture-specification",
        aliases=["architecture_specification"],
        help="Generate an architecture specification template.",
    )
    architecture_specification_parser.add_argument(
        "--work-item-name",
        required=True,
        help="Work item name used as the docs/proposals subfolder for the spec.",
    )
    architecture_specification_parser.add_argument(
        "--entity-type",
        dest="entity_types",
        action="append",
        help="Allowed entity type. May be repeated.",
    )
    architecture_specification_parser.add_argument(
        "--all-entity-types",
        action="store_true",
        help="Allow every entity type from the repository taxonomy.",
    )
    architecture_specification_parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Write the template to this path instead of "
            "docs/proposals/<work-item-name>/architecture-specification.yaml."
        ),
    )
    architecture_specification_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    architecture_specification_parser.add_argument(
        "--title",
        help="Optional title to embed in the template.",
    )
    architecture_specification_parser.set_defaults(func=_run_architecture_specification)

    implementation_specification_parser = subparsers.add_parser(
        "implementation-specification",
        aliases=["implementation_specification"],
        help="Generate an implementation specification template.",
    )
    implementation_specification_parser.add_argument(
        "--work-item-name",
        required=True,
        help="Work item name used as the docs/proposals subfolder for the spec.",
    )
    implementation_specification_parser.add_argument(
        "--architecture-specification",
        type=Path,
        help=(
            "Read the source architecture specification from this path instead "
            "of docs/proposals/<work-item-name>/architecture-specification.yaml."
        ),
    )
    implementation_specification_parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Write the template to this path instead of "
            "docs/proposals/<work-item-name>/implementation-specification.yaml."
        ),
    )
    implementation_specification_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    implementation_specification_parser.add_argument(
        "--title",
        help="Optional title to embed in the template.",
    )
    implementation_specification_parser.set_defaults(
        func=_run_implementation_specification
    )

    system_specification_parser = subparsers.add_parser(
        "system-specification",
        aliases=["system_specification"],
        help="Generate a system specification template.",
    )
    system_specification_parser.add_argument(
        "--work-item-name",
        required=True,
        help="Work item name used as the docs/proposals subfolder for the spec.",
    )
    system_specification_parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Write the template to this path instead of "
            "docs/proposals/<work-item-name>/system-specification.yaml."
        ),
    )
    system_specification_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    system_specification_parser.add_argument(
        "--title",
        help="Optional title to embed in the template.",
    )
    system_specification_parser.set_defaults(func=_run_system_specification)

    project_structure_parser = subparsers.add_parser(
        "project-structure",
        aliases=["project_structure"],
        help="Create the project structure template.",
    )
    project_structure_parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Write the template to this path instead of "
            "docs/project_structure/project-structure.yaml."
        ),
    )
    project_structure_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when creating the template.",
    )
    project_structure_parser.set_defaults(func=_run_project_structure)

    validate_project_structure_parser = subparsers.add_parser(
        "validate-project-structure",
        aliases=["validate_project_structure"],
        help="Validate a project-structure specification-v1 YAML file.",
    )
    validate_project_structure_parser.add_argument(
        "--input",
        type=Path,
        default=Path("docs/project_structure/project-structure.yaml"),
        help="Project-structure YAML file to validate.",
    )
    validate_project_structure_parser.set_defaults(
        func=_run_validate_project_structure,
    )

    system_map_specification_parser = subparsers.add_parser(
        "system-map-specification",
        aliases=["system_map_specification"],
        help="Generate a system map specification template.",
    )
    system_map_specification_parser.add_argument(
        "--work-item-name",
        required=True,
        help="Work item name used as the docs/current subfolder for the spec.",
    )
    system_map_specification_parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Write the template to this path instead of "
            "docs/current/<work-item-name>/system-map-specification.yaml."
        ),
    )
    system_map_specification_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    system_map_specification_parser.add_argument(
        "--title",
        help="Optional title to embed in the template.",
    )
    system_map_specification_parser.set_defaults(func=_run_system_map_specification)

    feature_pr_specification_parser = subparsers.add_parser(
        "feature-pr-specification",
        aliases=["feature_pr_specification"],
        help="Generate a feature and PR specification template.",
    )
    feature_pr_specification_parser.add_argument(
        "--work-item-name",
        required=True,
        help="Work item name used as the docs/proposals subfolder for the spec.",
    )
    feature_pr_specification_parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Write the template to this path instead of "
            "docs/proposals/<work-item-name>/feature-pr-specification.yaml."
        ),
    )
    feature_pr_specification_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    feature_pr_specification_parser.add_argument(
        "--title",
        help="Optional title to embed in the template.",
    )
    feature_pr_specification_parser.set_defaults(func=_run_feature_pr_specification)

    start_planning_feature_parser = subparsers.add_parser(
        "start-planning-feature",
        aliases=["start_planning_feature"],
        help="Generate the instructions for starting feature planning.",
    )
    start_planning_feature_parser.add_argument(
        "--work-item-name",
        required=True,
        help="Work item name used to fill the skill instructions.",
    )
    start_planning_feature_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when reading the planning skill.",
    )
    start_planning_feature_parser.set_defaults(func=_run_start_planning_feature)

    plan_diff_specification_parser = subparsers.add_parser(
        "plan-diff",
        aliases=["plan_diff"],
        help="Generate a plan diff specification from a feature plan and changelogs.",
    )
    plan_diff_specification_parser.add_argument(
        "--feature-plan-specification",
        type=Path,
        required=True,
        help=(
            "Read the feature plan specification from this path and derive the "
            "default output location from its work-item folder."
        ),
    )
    plan_diff_specification_parser.add_argument(
        "--changelog",
        dest="changelog_paths",
        type=Path,
        action="append",
        required=True,
        help="ChangeLog file to compare against the feature plan. May be repeated.",
    )
    plan_diff_specification_parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Write the diff to this path instead of "
            "docs/plan-diffs/<work-item-name>/plan-diff.yaml."
        ),
    )
    plan_diff_specification_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when resolving relative paths.",
    )
    plan_diff_specification_parser.set_defaults(func=_run_plan_diff_specification)

    pr_specification_parser = subparsers.add_parser(
        "pr-specification",
        aliases=["pr_specification"],
        help="Generate a proposed PR specification v1 template.",
    )
    pr_specification_parser.add_argument(
        "--work-item-name",
        required=True,
        help="Work item name used as the docs/proposals subfolder for the spec.",
    )
    pr_specification_parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Write the template to this path instead of "
            "docs/proposals/<work-item-name>/proposed-pr-specification.yaml."
        ),
    )
    pr_specification_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    pr_specification_parser.set_defaults(func=_run_pr_specification)

    search_proposed_prs_parser = subparsers.add_parser(
        "search-proposed-prs",
        aliases=["search_proposed_prs"],
        help="Fuzzy-search proposed PR specifications.",
    )
    search_proposed_prs_parser.add_argument(
        "query",
        help="Search query to match against proposed PR ids, features, and intent.",
    )
    search_proposed_prs_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of results to return.",
    )
    search_proposed_prs_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    search_proposed_prs_parser.set_defaults(func=_run_search_proposed_prs)

    show_proposed_pr_parser = subparsers.add_parser(
        "show-proposed-pr",
        aliases=["show_proposed_pr"],
        help="Print a proposed PR specification by PR number.",
    )
    show_proposed_pr_parser.add_argument(
        "pr_number",
        type=int,
        help="Proposed PR number to print.",
    )
    show_proposed_pr_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    show_proposed_pr_parser.set_defaults(func=_run_show_proposed_pr)

    evaluate_specification_parser = subparsers.add_parser(
        "evaluate",
        help="Validate one specification-v1 YAML file or a directory of them.",
    )
    evaluate_specification_parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        help=(
            "Optional specification-v1 YAML file or directory. When omitted, "
            "evaluate inspects workflow documents changed on the current branch."
        ),
    )
    evaluate_specification_parser.add_argument(
        "--work-item-name",
        help="Work item name used when validating the specification files.",
    )
    evaluate_specification_parser.add_argument(
        "--architecture-specification",
        type=Path,
        help="Architecture specification path for implementation validation.",
    )
    evaluate_specification_parser.add_argument(
        "--entity-type",
        dest="entity_types",
        action="append",
        help="Allowed architecture entity type. May be repeated.",
    )
    evaluate_specification_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running validation.",
    )
    evaluate_specification_parser.add_argument(
        "--base-branch",
        default="main",
        help="Base branch used to discover changed workflow documents (default: main).",
    )
    evaluate_specification_parser.set_defaults(func=_run_evaluate_specification)

    openai_proxy_parser = subparsers.add_parser(
        "openai-proxy",
        aliases=["openai_proxy"],
        help="Start a local OpenAI reverse proxy that records exchanges.",
    )
    openai_proxy_parser.add_argument(
        "--upstream-base-url",
        default="https://api.openai.com",
        help="Upstream OpenAI base URL to forward requests to.",
    )
    openai_proxy_parser.add_argument(
        "--log-dir",
        type=Path,
        help="Directory to write recorded exchanges to.",
    )
    openai_proxy_parser.add_argument(
        "--client-path-prefix",
        default="/v1",
        help="Incoming path prefix to strip before forwarding to the upstream.",
    )
    openai_proxy_parser.add_argument(
        "--upstream-path-prefix",
        default="/v1",
        help="Upstream path prefix to add before forwarding the request.",
    )
    openai_proxy_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind the proxy server to.",
    )
    openai_proxy_parser.add_argument(
        "--port",
        type=int,
        default=8787,
        help="Port to bind the proxy server to.",
    )
    openai_proxy_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root used to resolve the default log directory.",
    )
    openai_proxy_parser.set_defaults(func=_run_openai_proxy)

    llm_diff_parser = subparsers.add_parser(
        "llm-diff",
        aliases=["llm_diff"],
        help="Show the differences between two recorded llm-*.json exchanges.",
    )
    llm_diff_parser.add_argument(
        "first_file",
        type=Path,
        help="Earlier or reference llm-*.json exchange file.",
    )
    llm_diff_parser.add_argument(
        "second_file",
        type=Path,
        help="Later or changed llm-*.json exchange file.",
    )
    llm_diff_parser.set_defaults(func=_run_llm_diff)

    workflow_replay_parser = subparsers.add_parser(
        "workflow-replay",
        aliases=["workflow_replay"],
        help=(
            "Export one workflow LLM error as a replay bundle or render and "
            "validate a bundle without invoking tools or an LLM."
        ),
    )
    replay_source = workflow_replay_parser.add_mutually_exclusive_group(required=True)
    replay_source.add_argument(
        "--bundle",
        type=Path,
        help="Existing YAML or JSON replay bundle to render and validate.",
    )
    replay_source.add_argument(
        "--error-log",
        type=Path,
        help="Workflow LLM JSONL error log containing the record to export.",
    )
    workflow_replay_parser.add_argument(
        "--record-id",
        help="Stable error record id to export; required with --error-log.",
    )
    workflow_replay_parser.add_argument(
        "--output",
        type=Path,
        help="Replay bundle output path; required with --error-log.",
    )
    workflow_replay_parser.add_argument(
        "--definition",
        type=Path,
        help="Optional candidate skill definition used when rendering a bundle.",
    )
    workflow_replay_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root used to resolve bundle and definition paths.",
    )
    workflow_replay_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the complete replay result as JSON.",
    )
    workflow_replay_parser.set_defaults(func=_run_workflow_replay)

    workflow_scenario_parser = subparsers.add_parser(
        "workflow-scenario",
        aliases=["workflow_scenario"],
        help=(
            "Run a scripted workflow skill scenario in an isolated temporary "
            "Git repository."
        ),
    )
    workflow_scenario_parser.add_argument(
        "--scenario",
        required=True,
        type=Path,
        help="Versioned scenario YAML or JSON file.",
    )
    workflow_scenario_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root containing the candidate definition.",
    )
    workflow_scenario_parser.add_argument(
        "--keep-failed",
        action="store_true",
        help="Retain the isolated repository for a failed scenario and print its path.",
    )
    workflow_scenario_parser.add_argument(
        "--report",
        type=Path,
        help=(
            "Write the complete result, including live LLM exchanges and repair "
            "output, to this JSON file."
        ),
    )
    workflow_scenario_parser.add_argument(
        "--max-roundtrips",
        type=int,
        help="Override the scenario roundtrip limit for an investigative run.",
    )
    workflow_scenario_parser.add_argument(
        "--max-stalled-roundtrips",
        type=int,
        help="Override the scenario stalled-action threshold for an investigative run.",
    )
    workflow_scenario_parser.add_argument(
        "--stream-live",
        action="store_true",
        help="Stream live scenario workflow progress while also saving the report.",
    )
    workflow_scenario_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the complete scenario result as JSON.",
    )
    workflow_scenario_parser.set_defaults(func=_run_workflow_scenario)

    definition_validation_parser = subparsers.add_parser(
        "validate-workflow-definition",
        aliases=["validate_workflow_definition"],
        help=(
            "Run schema and deterministic confusion checks for a skill or "
            "workflow template."
        ),
    )
    definition_validation_parser.add_argument("definition", type=Path)
    definition_validation_parser.add_argument("--json", action="store_true")
    definition_validation_parser.set_defaults(func=_run_validate_workflow_definition)

    prompt_snapshot_parser = subparsers.add_parser(
        "render-workflow-prompts",
        aliases=["render_workflow_prompts"],
        help="Render normalized production prompt snapshots for every skill step.",
    )
    prompt_snapshot_parser.add_argument("--definition", required=True, type=Path)
    prompt_snapshot_parser.add_argument("--output-dir", required=True, type=Path)
    prompt_snapshot_parser.add_argument("--repo-root", type=Path)
    prompt_snapshot_parser.set_defaults(func=_run_render_workflow_prompts)

    error_analysis_parser = subparsers.add_parser(
        "analyze-workflow-errors",
        aliases=["analyze_workflow_errors"],
        help="Cluster workflow LLM errors and optionally promote replay candidates.",
    )
    error_analysis_parser.add_argument(
        "--error-log",
        type=Path,
        action="append",
        required=True,
        help="Workflow LLM error JSONL file; repeat to analyze multiple logs.",
    )
    error_analysis_parser.add_argument("--repo-root", type=Path)
    error_analysis_parser.add_argument(
        "--replay-output-dir",
        type=Path,
        help="Optional destination for representative draft replay bundles.",
    )
    error_analysis_parser.add_argument(
        "--limit", type=int, help="Maximum ranked clusters to promote."
    )
    error_analysis_parser.add_argument("--json", action="store_true")
    error_analysis_parser.set_defaults(func=_run_analyze_workflow_errors)

    ambiguity_review_parser = subparsers.add_parser(
        "review-workflow-ambiguity",
        aliases=["review_workflow_ambiguity"],
        help=(
            "Request an advisory high-reasoning ambiguity review of one "
            "definition step."
        ),
    )
    ambiguity_review_parser.add_argument("--definition", required=True, type=Path)
    step_selector = ambiguity_review_parser.add_mutually_exclusive_group(required=True)
    step_selector.add_argument("--step-id")
    step_selector.add_argument("--step-index", type=int)
    ambiguity_review_parser.add_argument("--repo-root", type=Path)
    ambiguity_review_parser.add_argument(
        "--provider", default="auto", choices=ALL_PROVIDERS + ("auto",)
    )
    ambiguity_review_parser.add_argument("--model")
    ambiguity_review_parser.add_argument("--api-key")
    ambiguity_review_parser.add_argument("--base-url")
    ambiguity_review_parser.add_argument("--json", action="store_true")
    ambiguity_review_parser.set_defaults(func=_run_review_workflow_ambiguity)

    comparison_parser = subparsers.add_parser(
        "compare-workflow-definitions",
        aliases=["compare_workflow_definitions"],
        help=(
            "Compare explicit deterministic replay and scenario cases against a "
            "baseline Git ref."
        ),
    )
    comparison_parser.add_argument(
        "--baseline-ref", required=True, help="Git ref containing the baseline."
    )
    comparison_parser.add_argument(
        "--replay",
        type=Path,
        action="append",
        help="Replay bundle to run on both baseline and candidate; repeatable.",
    )
    comparison_parser.add_argument(
        "--scenario",
        type=Path,
        action="append",
        help="Scripted scenario to run on both baseline and candidate; repeatable.",
    )
    comparison_parser.add_argument("--repo-root", type=Path)
    comparison_parser.add_argument("--max-roundtrip-increase", type=int, default=0)
    comparison_parser.add_argument("--max-prompt-user-increase", type=int, default=0)
    comparison_parser.add_argument(
        "--max-repeated-action-increase", type=int, default=0
    )
    comparison_parser.add_argument("--json", action="store_true")
    comparison_parser.set_defaults(func=_run_compare_workflow_definitions)

    tune_parser = subparsers.add_parser(
        "tune-workflow",
        aliases=["tune_workflow"],
        help="Run deterministic workflow validation, comparison, and reporting.",
    )
    tune_parser.add_argument("--definition", required=True, type=Path)
    tune_parser.add_argument(
        "--baseline-ref", help="Optional baseline Git ref; defaults to merge-base."
    )
    tune_parser.add_argument("--replay", type=Path, action="append")
    tune_parser.add_argument("--scenario", type=Path, action="append")
    tune_parser.add_argument("--report", required=True, type=Path)
    tune_parser.add_argument("--snapshot-output-dir", type=Path)
    tune_parser.add_argument("--repo-root", type=Path)
    tune_parser.add_argument("--max-roundtrip-increase", type=int, default=0)
    tune_parser.add_argument("--max-prompt-user-increase", type=int, default=0)
    tune_parser.add_argument("--max-repeated-action-increase", type=int, default=0)
    tune_parser.add_argument("--json", action="store_true")
    tune_parser.set_defaults(func=_run_tune_workflow)

    download_qwen_parser = subparsers.add_parser(
        "download-qwen-model",
        aliases=["download_qwen_model"],
        help="Download and cache the local Qwen model used by workflow-chat.",
    )
    download_qwen_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root used to resolve the default model cache directory.",
    )
    download_qwen_parser.set_defaults(func=_run_download_qwen_model)

    workflow_chat_parser = subparsers.add_parser(
        "workflow-chat",
        aliases=["workflow_chat"],
        help="Start an interactive workflow chat agent in the terminal.",
    )
    workflow_chat_parser.add_argument(
        "--provider",
        choices=["auto", *ALL_PROVIDERS],
        default="auto",
        help=(
            "Normal LLM provider to use. Auto assigns the highest-priority "
            "configured provider to normal and the next one to adversarial."
        ),
    )
    workflow_chat_parser.add_argument(
        "--adversarial-provider",
        choices=ALL_PROVIDERS,
        help="Provider used for nested adversarial review skills.",
    )
    workflow_chat_parser.add_argument(
        "--skills-dir",
        "--templates-dir",
        dest="skills_dir",
        type=Path,
        default=Path("skill-definitions"),
        help="Directory containing skill JSON files.",
    )
    workflow_chat_parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory to write generated workflow task JSON files to.",
    )
    workflow_chat_parser.add_argument(
        "--model",
        default="glm-5.2",
        help="Model to use for template matching and task generation.",
    )
    workflow_chat_parser.add_argument(
        "--api-key",
        help=(
            "API key. Defaults to the provider-specific environment variable "
            "or Codex auth when supported."
        ),
    )
    workflow_chat_parser.add_argument(
        "--base-url",
        help=(
            "Base URL. Defaults to the provider-specific environment variable "
            "or the public API."
        ),
    )
    workflow_chat_parser.add_argument(
        "--max-turns",
        type=int,
        default=8,
        help="Maximum number of follow-up turns before the chat agent stops.",
    )
    workflow_chat_parser.add_argument(
        "--max-stalled-roundtrips",
        type=int,
        default=3,
        help=(
            "Maximum consecutive workflow roundtrips without progress before "
            "the agent stops."
        ),
    )
    workflow_chat_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print additional progress details to stderr.",
    )
    workflow_chat_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to resolve the default templates directory.",
    )
    workflow_chat_parser.set_defaults(func=_run_workflow_chat)

    instantiate_workflow_parser = subparsers.add_parser(
        "instantiate-workflow",
        aliases=["instantiate_workflow"],
        help="Instantiate a workflow template as durable task documents.",
    )
    instantiate_workflow_parser.add_argument(
        "--work-item-name",
        required=True,
        help="Feature or work-item name used for the shared workflow directory.",
    )
    instantiate_workflow_parser.add_argument(
        "--workflow-instance-name",
        help=(
            "Optional unique workflow instance name used to namespace task "
            "files within the work-item directory."
        ),
    )
    instantiate_workflow_parser.add_argument(
        "--template",
        type=Path,
        required=True,
        help="Workflow template YAML or JSON to instantiate.",
    )
    instantiate_workflow_parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("docs") / "workflows",
        help="Directory under which the work-item workflow directory is created.",
    )
    instantiate_workflow_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root used to resolve relative paths.",
    )
    instantiate_workflow_parser.add_argument(
        "--template-value",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help=("Value for a template input-state placeholder; may be repeated."),
    )
    instantiate_workflow_parser.add_argument(
        "--depends-on-workflow",
        action="append",
        default=[],
        metavar="WORKFLOW-ID",
        help=(
            "Workflow id whose integration pull request must be merged before "
            "this workflow can start; may be repeated."
        ),
    )
    instantiate_workflow_parser.set_defaults(func=_run_instantiate_workflow)

    process_workflow_task_parser = subparsers.add_parser(
        "process-workflow-task",
        aliases=["process_workflow_task"],
        help=(
            "Process ready agent tasks from a durable workflow on its integration "
            "branch until human work or completion."
        ),
    )
    process_workflow_task_parser.add_argument(
        "--workflow-dir",
        type=Path,
        required=True,
        help="Directory containing the durable workflow task JSON files.",
    )
    process_workflow_task_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root used as the working directory for task tools.",
    )
    process_workflow_task_parser.add_argument(
        "--workflow-id",
        help=(
            "Workflow/proposed-PR id to select when the directory contains "
            "multiple workflows."
        ),
    )
    process_workflow_task_parser.add_argument(
        "--provider",
        choices=["auto", *ALL_PROVIDERS],
        default="auto",
        help=(
            "Normal LLM provider to use. Auto applies the same configured "
            "provider-priority lookup as workflow-chat."
        ),
    )
    process_workflow_task_parser.add_argument(
        "--task-id",
        help="Process this task instead of the first ready agent task.",
    )
    process_workflow_task_parser.add_argument("--api-key")
    process_workflow_task_parser.add_argument(
        "--base-url",
        help="Optional z.ai-compatible base URL override.",
    )
    process_workflow_task_parser.add_argument(
        "--max-roundtrips",
        type=int,
        default=None,
        help=(
            "Optional maximum LLM action roundtrips; by default progress is unlimited."
        ),
    )
    process_workflow_task_parser.add_argument(
        "--context-compaction-threshold",
        type=float,
        default=0.75,
        help=(
            "Compact durable-task context proactively at this fraction of the "
            "model context window (default: 0.75)."
        ),
    )
    process_workflow_task_parser.add_argument("--verbose", action="store_true")
    process_workflow_task_parser.set_defaults(func=_run_process_workflow_task)

    process_human_task_parser = subparsers.add_parser(
        "process-human-task",
        aliases=["process_human_task"],
        help="Find, present, and complete one ready human workflow task.",
    )
    process_human_task_parser.add_argument(
        "--workflow-dir",
        type=Path,
        required=True,
        help="Directory containing the durable workflow task JSON files.",
    )
    process_human_task_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root used for workflow Git coordination.",
    )
    process_human_task_parser.add_argument(
        "--task-id",
        help="Process this task instead of the first ready human task.",
    )
    process_human_task_parser.add_argument(
        "--role",
        choices=[role.value for role in HumanRole],
        help="Only select a ready human task assigned to this role.",
    )
    answer_group = process_human_task_parser.add_mutually_exclusive_group()
    answer_group.add_argument(
        "--answer",
        help="Answer non-interactively instead of prompting on stdin.",
    )
    answer_group.add_argument(
        "--answer-file",
        type=Path,
        help="Read the human answer from a UTF-8 text file.",
    )
    process_human_task_parser.set_defaults(func=_run_process_human_task)

    workflow_recovery_parser = subparsers.add_parser(
        "workflow-recovery",
        aliases=["workflow_recovery"],
        help="Inspect or clean up Git state for one durable workflow run.",
    )
    workflow_recovery_parser.add_argument(
        "--proposed-pr-id",
        required=True,
        help="Work-item proposed PR id used to name the workflow branches.",
    )
    workflow_recovery_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to inspect; defaults to the current worktree.",
    )
    workflow_recovery_parser.add_argument(
        "--cleanup",
        action="store_true",
        help=(
            "Remove dangling task branches, worktrees, claims, and task PRs; "
            "preserve the integration branch as the last checkpoint."
        ),
    )
    workflow_recovery_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the complete inspection or cleanup report as JSON.",
    )
    workflow_recovery_parser.set_defaults(func=_run_workflow_recovery)

    blame_ui_parser = subparsers.add_parser(
        "blame-ui",
        aliases=["blame_ui"],
        help="Start a local GitHub-style blame viewer powered by the index.",
    )
    blame_ui_parser.add_argument(
        "branch_name",
        nargs="?",
        help="Branch to inspect. Defaults to the current branch.",
    )
    blame_ui_parser.add_argument(
        "--branch-name",
        dest="branch_name_flag",
        help="Branch to inspect. Defaults to the current branch.",
    )
    blame_ui_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    blame_ui_parser.add_argument(
        "--parent-branch",
        help="Reference parent branch used to build the index.",
    )
    blame_ui_parser.add_argument(
        "--file",
        dest="selected_file",
        help="Initial file to show in the blame UI.",
    )
    blame_ui_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind the UI server to.",
    )
    blame_ui_parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind the UI server to.",
    )
    blame_ui_parser.set_defaults(func=_run_blame_ui)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


def _stage_generated_file(repo_root: Path, output_path: Path) -> None:
    """Stage a generated file when the endpoint runs inside a Git checkout."""
    try:
        relative_path = output_path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return

    repository_check = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if repository_check.returncode != 0:
        return

    staged = subprocess.run(
        ["git", "add", "--", str(relative_path)],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if staged.returncode != 0:
        raise RuntimeError(
            f"Could not stage generated file {relative_path}: {staged.stderr.strip()}"
        )
    if os.environ.get("POWDRR_FILE_ADDED_EVENTS") == "1":
        resolved_output_path = output_path.resolve()
        if resolved_output_path.is_dir():
            added_paths = sorted(
                path.relative_to(repo_root.resolve())
                for path in resolved_output_path.rglob("*")
                if path.is_file()
            )
        else:
            added_paths = [relative_path]
        for added_path in added_paths:
            print(
                f"{_WORKFLOW_FILE_ADDED_EVENT_PREFIX}{added_path}",
                file=sys.stderr,
            )


def _run_repository_state(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    sys.stdout.write(render_repository_state(repo_root))
    return 0


def _run_pull_request_description(args: argparse.Namespace) -> int:
    existing_pull_request = find_existing_pull_request(args.repo_root)
    sys.stdout.write(
        render_pull_request_description_template(
            args.kind,
            existing_pull_request=existing_pull_request,
        )
    )
    return 0


def _run_init(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    output_path = _resolve_template_output_path(repo_root, args.output, args.pr_number)
    output_path = create_change_log_template(
        branch_name=args.branch_name,
        output_path=output_path,
        repo_root=repo_root,
        default_branch=args.default_branch,
    )
    _stage_generated_file(repo_root, output_path)
    print(output_path)
    if args.pr_number is not None:
        print("Next: fill out the template according to the instructions in the file.")
        print(
            "Then validate it with: "
            f"powdrr-lift evaluate-pr-against-changelog --pr-number {args.pr_number}"
        )
        print(
            "When it passes, include it in the PR as "
            f"docs/changelogs/PR-{args.pr_number}-changelog.yaml"
        )
    return 0


def _parse_template_values(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        name, separator, replacement = value.partition("=")
        if not separator or not name.strip():
            raise ValueError(
                "--template-value must use NAME=VALUE with a non-empty NAME."
            )
        parsed[name.strip()] = replacement
    return parsed


def _run_instantiate_workflow(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    template_path = args.template
    if not template_path.is_absolute():
        template_path = repo_root / template_path
    output_root = args.output_root
    if not output_root.is_absolute():
        output_root = repo_root / output_root
    template_values = _parse_template_values(args.template_value)
    try:
        project_root = resolve_git_repository_root(repo_root)
        proposed_pr_id = template_values.get("proposed-pr-id", args.work_item_name)
        dependency_graph = load_proposed_pr_dependency_graph(repo_root)
        if proposed_pr_id not in dependency_graph:
            raise ValueError(
                f"No proposed PR specification exists for {proposed_pr_id!r}."
            )
        derived_dependencies = dependency_graph[proposed_pr_id]
        integration_worktree, integration_branch = create_workflow_worktree(
            project_root,
            proposed_pr_id,
        )
        output_directory, tasks = instantiate_workflow_template(
            template_path=template_path,
            work_item_name=args.work_item_name,
            output_root=output_root,
            workflow_instance_name=args.workflow_instance_name,
            template_values=template_values,
        )
        invariants, relationships = instantiated_workflow_relationships(
            template_path,
            work_item_name=args.work_item_name,
            workflow_instance_name=args.workflow_instance_name,
            template_values=template_values,
        )
        relative_workflow = output_directory.relative_to(repo_root)
        state = WorkflowGitState(
            proposed_pr_id=template_values.get("proposed-pr-id", args.work_item_name),
            base_branch="main",
            integration_branch=integration_branch,
            workflow_relative_directory=str(relative_workflow),
            depends_on_workflows=derived_dependencies,
            invariants=invariants,
            relationships=relationships,
        )
        save_workflow_git_state(output_directory, state)
        commit_and_push_workflow_initialization(
            repo_root,
            output_directory,
            push=False,
        )
        synchronize_workflow_initialization(integration_worktree, repo_root)
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        print(f"Could not instantiate workflow: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "workflow_directory": str(output_directory),
                "task_count": len(tasks),
                "first_task": str(output_directory / f"{tasks[0].task_id}.yaml"),
                "first_task_id": tasks[0].task_id,
                "integration_branch": integration_branch,
                "integration_worktree": (
                    str(integration_worktree) if integration_worktree else None
                ),
            },
            indent=2,
        )
    )
    return 0


def _run_process_workflow_task(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    workflow_dir = args.workflow_dir
    if not workflow_dir.is_absolute():
        workflow_dir = repo_root / workflow_dir
    return run_workflow_task(
        WorkflowTaskAgentConfig(
            workflow_dir=workflow_dir,
            repo_root=repo_root,
            provider=args.provider,
            workflow_id=args.workflow_id,
            task_id=args.task_id,
            api_key=args.api_key,
            base_url=args.base_url,
            max_roundtrips=args.max_roundtrips,
            context_compaction_threshold=args.context_compaction_threshold,
            verbose=args.verbose,
        ),
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


def _run_process_human_task(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    workflow_dir = args.workflow_dir
    if not workflow_dir.is_absolute():
        workflow_dir = repo_root / workflow_dir
    return run_human_task(
        HumanTaskRunnerConfig(
            workflow_dir=workflow_dir,
            repo_root=repo_root,
            task_id=args.task_id,
            assignee_role=HumanRole(args.role) if args.role else None,
            answer=args.answer,
            answer_file=args.answer_file,
        ),
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


def _run_workflow_recovery(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    try:
        report = inspect_workflow_run(repo_root, args.proposed_pr_id)
        if args.cleanup:
            report = cleanup_workflow_run(
                repo_root,
                args.proposed_pr_id,
                report=report,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Workflow recovery failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        _print_workflow_recovery_report(report, cleanup=args.cleanup)
    return 1 if report.get("errors") else 0


def _print_workflow_recovery_report(
    report: dict[str, Any],
    *,
    cleanup: bool,
) -> None:
    action = "Cleanup" if cleanup else "Inspection"
    print(f"{action} for proposed PR id: {report['proposed_pr_id']}")
    print(f"Integration branch: {report['integration_branch']}")
    print(
        "Integration checkpoint: "
        f"branch_exists={report['integration_branch_exists']} "
        f"worktree_exists={report['integration_worktree_exists']}"
    )
    inconsistencies = report.get("inconsistencies", [])
    if inconsistencies:
        print("Inconsistencies:")
        for item in inconsistencies:
            print(f"  - {item}")
    for key in ("task_branches", "claim_refs", "worktrees", "pull_requests"):
        values = report.get(key, [])
        if values:
            print(f"{key.replace('_', ' ').title()}:")
            for value in values:
                print(f"  - {value}")
    if cleanup:
        for item in report.get("removed", []):
            print(f"Removed: {item}")
    for error in report.get("errors", []):
        print(f"Error: {error}", file=sys.stderr)


def _run_init_from_plan_diff(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    branch_name = args.branch_name or _current_branch(repo_root)
    output_path = _resolve_template_output_path(repo_root, args.output, args.pr_number)
    output_path = create_change_log_template_from_plan_diff(
        branch_name=branch_name,
        plan_diff_path=args.plan_diff,
        output_path=output_path,
        repo_root=repo_root,
        default_branch=args.default_branch,
    )
    _stage_generated_file(repo_root, output_path)
    print(output_path)
    if args.pr_number is not None:
        print("Next: fill out the template according to the instructions in the file.")
        print(
            "Then validate it with: "
            f"powdrr-lift evaluate-pr-against-changelog --pr-number {args.pr_number}"
        )
        print(
            "When it passes, include it in the PR as "
            f"docs/changelogs/PR-{args.pr_number}-changelog.yaml"
        )
    return 0


def _run_evaluate(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    input_path = _resolve_template_input_path(repo_root, args.input, args.pr_number)
    proposed_yaml = _read_input(input_path)
    report_yaml = validate_change_log_yaml(
        proposed_yaml,
        branch_name=args.branch_name,
        repo_root=repo_root,
        default_branch=args.default_branch,
    )
    report = parse_validation_report(report_yaml)
    sys.stdout.write(report_yaml)
    if not report_yaml.endswith("\n"):
        sys.stdout.write("\n")
    if args.pr_number is not None:
        if report.validation_successful:
            print(
                "Next: include docs/changelogs/PR-"
                f"{args.pr_number}-changelog.yaml in the PR.",
                file=sys.stderr,
            )
        else:
            print(
                "Next: fix docs/changelogs/PR-"
                f"{args.pr_number}-changelog.yaml and rerun the validate command.",
                file=sys.stderr,
            )
    return 0 if report.validation_successful else 1


def _run_validate_pr_files(args: argparse.Namespace) -> int:
    """Validate recognized repository-format files in the current scope."""
    repo_root = resolve_repo_root(args.repo_root)
    changed_file_names: list[str] = []
    if args.pr_number is not None:
        changed = subprocess.run(
            ["gh", "pr", "diff", str(args.pr_number), "--name-only"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if changed.returncode != 0:
            print(
                changed.stderr.strip()
                or f"Could not read files changed by PR #{args.pr_number}.",
                file=sys.stderr,
            )
            return 1
        changed_file_names.extend(changed.stdout.splitlines())
        scope = "pull_request"
    else:
        scope = "checked_out_branch"
        branch_commands = (
            ["git", "diff", "--name-only", f"{args.base_branch}...HEAD"],
            ["git", "diff", "--name-only", "HEAD"],
            ["git", "diff", "--cached", "--name-only"],
        )
        for command in branch_commands:
            changed = subprocess.run(
                command,
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if changed.returncode != 0:
                print(
                    changed.stderr.strip()
                    or (
                        "Could not determine files changed on the checked-out "
                        f"branch relative to {args.base_branch}."
                    ),
                    file=sys.stderr,
                )
                return 1
            changed_file_names.extend(changed.stdout.splitlines())

    # A file can occur in both the committed and working-tree diffs. Preserve
    # the first occurrence so each validator runs once per file/group.
    changed_file_names = list(
        dict.fromkeys(
            relative_path.strip()
            for relative_path in changed_file_names
            if relative_path.strip()
        )
    )
    changed_paths = [repo_root / relative_path for relative_path in changed_file_names]
    results: list[dict[str, Any]] = []
    overall_success = True
    validated_groups: set[Path] = set()

    def add_result(
        *,
        validator: str,
        paths: list[Path],
        report_text: str,
        guidance: str | None = None,
    ) -> None:
        nonlocal overall_success
        try:
            parsed_report = json.loads(report_text)
        except json.JSONDecodeError:
            try:
                parsed_report = yaml.safe_load(report_text)
            except yaml.YAMLError:
                parsed_report = {"raw_report": report_text}
        if guidance is not None and isinstance(parsed_report, dict):
            parsed_report["llm_guidance"] = guidance
        successful = (
            isinstance(parsed_report, dict)
            and parsed_report.get("validation_successful", False) is True
        )
        results.append(
            {
                "validator": validator,
                "files": [str(path.relative_to(repo_root)) for path in paths],
                "validation_successful": successful,
                "report": parsed_report,
            }
        )
        overall_success = overall_success and successful

    for path in changed_paths:
        relative_path = path.relative_to(repo_root)
        if not path.exists():
            results.append(
                {
                    "files": [str(relative_path)],
                    "validation_successful": False,
                    "error": (
                        "Changed file is not present in the checked-out PR worktree."
                    ),
                }
            )
            overall_success = False
            continue

        if relative_path.parts[:1] == ("skill-definitions",) and path.suffix in {
            ".yaml",
            ".yml",
            ".json",
        }:
            skill_directory = repo_root / "skill-definitions"
            if skill_directory not in validated_groups:
                add_result(
                    validator="validate_skill_directory",
                    paths=[skill_directory],
                    report_text=validate_skill_directory(skill_directory),
                )
                validated_groups.add(skill_directory)
            continue

        if (
            relative_path.parts[:2] == ("docs", "workflows")
            and path.name.endswith((".yaml", ".yml", ".json"))
            and "-task-" in path.name
        ):
            workflow_directory = path.parent
            if workflow_directory not in validated_groups:
                add_result(
                    validator="validate_workflow_task_directory",
                    paths=[workflow_directory],
                    report_text=validate_workflow_task_directory(workflow_directory),
                )
                validated_groups.add(workflow_directory)
            continue

        if relative_path == Path("docs/project_structure/project-structure.yaml"):
            project_structure_report = validate_project_structure_yaml(path)
            add_result(
                validator="validate_project_structure_yaml",
                paths=[path],
                report_text=json.dumps(
                    {
                        "validation_successful": (
                            project_structure_report.validation_successful
                        ),
                        "issues": [
                            {
                                "code": issue.code,
                                "message": issue.instructional_message(),
                                **(
                                    {"path": issue.path}
                                    if issue.path is not None
                                    else {}
                                ),
                            }
                            for issue in project_structure_report.issues
                        ],
                    }
                ),
            )
            continue

        if relative_path.parts[:1] == ("templates",) and path.name.endswith(
            "execute-proposed-pr.yaml"
        ):
            add_result(
                validator="validate_workflow_template_json",
                paths=[path],
                report_text=validate_workflow_template_json(
                    path.read_text(encoding="utf-8")
                ),
            )
            continue

        kind = _specification_kind_for_filename(path.name)
        if kind is not None:
            try:
                report_text, automatic_repairs = _validate_pr_file_specification(
                    path, kind, repo_root
                )
            except (OSError, ValueError) as exc:
                results.append(
                    {
                        "validator": f"validate_{kind}_specification_yaml",
                        "files": [str(relative_path)],
                        "validation_successful": False,
                        "error": str(exc),
                    }
                )
                overall_success = False
            else:
                add_result(
                    validator=f"validate_{kind}_specification_yaml",
                    paths=[path],
                    report_text=report_text,
                    guidance=_automatic_repair_guidance(path, automatic_repairs),
                )

    final_report = {
        "scope": scope,
        "pr_number": args.pr_number,
        "base_branch": None if args.pr_number is not None else args.base_branch,
        "changed_file_count": len(changed_paths),
        "validation_successful": overall_success,
        "results": results,
    }
    print(json.dumps(final_report, indent=2, ensure_ascii=False))
    return 0 if overall_success else 1


def _changed_branch_paths(repo_root: Path, base_branch: str) -> list[Path]:
    names: list[str] = []
    for command in (
        ["git", "diff", "--name-only", f"{base_branch}...HEAD"],
        ["git", "diff", "--name-only", "HEAD"],
        ["git", "diff", "--cached", "--name-only"],
    ):
        result = subprocess.run(
            command, cwd=repo_root, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip()
                or f"Could not determine changed files relative to {base_branch}."
            )
        names.extend(line.strip() for line in result.stdout.splitlines())
    return [
        repo_root / name
        for name in dict.fromkeys(name for name in names if name)
        if (repo_root / name).is_file()
    ]


def _repository_yaml_documents(repo_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    documents: list[tuple[Path, dict[str, Any]]] = []
    ignored = {".git", ".venv", ".worktrees", "node_modules", "__pycache__"}
    for path in repo_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".yaml", ".yml"}:
            continue
        if ignored.intersection(path.relative_to(repo_root).parts):
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict):
            documents.append((path, data))
    return documents


def _evaluate_workflow_changed_files(*, repo_root: Path, base_branch: str) -> bool:
    try:
        changed_paths = _changed_branch_paths(repo_root, base_branch)
    except RuntimeError as exc:
        print(json.dumps({"validation_successful": False, "issues": [str(exc)]}))
        return False

    task_paths: list[Path] = []
    state_paths_by_workflow: dict[str, list[Path]] = {}
    task_requirements: dict[str, set[str]] = {}
    issues: list[dict[str, str]] = []
    for path in changed_paths:
        if path.suffix.lower() not in {".yaml", ".yml"}:
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            issues.append({"path": str(path), "message": str(exc)})
            continue
        if not isinstance(data, dict):
            continue
        if "task_id" in data:
            task_paths.append(path)
            template_id = data.get("workflow_template")
            if not isinstance(template_id, str) or not template_id.strip():
                issues.append(
                    {
                        "path": str(path),
                        "message": (
                            "Every workflow task must declare a non-empty "
                            "workflow_template identity."
                        ),
                    }
                )
            else:
                input_state = data.get("input_state")
                workflow_id = (
                    input_state.get("proposed_pr")
                    if isinstance(input_state, dict)
                    else None
                )
                workflow_key = (
                    workflow_id.strip()
                    if isinstance(workflow_id, str) and workflow_id.strip()
                    else "__unscoped__"
                )
                task_requirements.setdefault(workflow_key, set()).add(
                    template_id.strip()
                )
            continue
        if "relationships" in data and "invariants" in data:
            workflow_id = data.get("proposed_pr_id")
            workflow_key = (
                workflow_id.strip()
                if isinstance(workflow_id, str) and workflow_id.strip()
                else "__unscoped__"
            )
            state_paths_by_workflow.setdefault(workflow_key, []).append(path)

    if not task_paths:
        print(
            json.dumps(
                {
                    "scope": "changed_branch_files",
                    "task_count": 0,
                    "validation_successful": True,
                    "message": "No changed workflow task documents were found.",
                },
                indent=2,
            )
        )
        return True

    template_paths: dict[str, Path] = {}
    for template_path, data in _repository_yaml_documents(repo_root):
        if "task_templates" not in data:
            continue
        template_id = data.get("id") or template_path.stem
        if isinstance(template_id, str):
            template_paths.setdefault(template_id, template_path)
    relationship_issues: list[WorkflowRelationshipValidationIssue] = []
    relationships_checked = 0
    proposed_pr_dependencies = load_proposed_pr_dependency_graph(repo_root)
    for workflow_key, template_ids in sorted(task_requirements.items()):
        required_invariants: list[dict[str, Any]] = []
        for template_id in sorted(template_ids):
            template_candidate = template_paths.get(template_id)
            if template_candidate is None:
                issues.append(
                    {
                        "path": "workflow_template",
                        "message": (
                            "Workflow template identity does not resolve: "
                            f"{template_id}"
                        ),
                    }
                )
                continue
            try:
                template = load_workflow_template(template_candidate)
            except (OSError, ValueError) as exc:
                issues.append({"path": str(template_candidate), "message": str(exc)})
                continue
            required_invariants.extend(template.invariants)
        scoped_states = state_paths_by_workflow.get(workflow_key, [])
        if not scoped_states and workflow_key == "__unscoped__":
            scoped_states = [
                path for paths in state_paths_by_workflow.values() for path in paths
            ]
        if workflow_key != "__unscoped__":
            expected_dependencies = set(proposed_pr_dependencies.get(workflow_key, ()))
            if workflow_key not in proposed_pr_dependencies:
                issues.append(
                    {
                        "path": workflow_key,
                        "message": (
                            "Workflow has no matching proposed PR specification: "
                            f"{workflow_key}"
                        ),
                    }
                )
            for state_path in scoped_states:
                try:
                    state_data = yaml.safe_load(state_path.read_text(encoding="utf-8"))
                except (OSError, yaml.YAMLError) as exc:
                    issues.append({"path": str(state_path), "message": str(exc)})
                    continue
                actual_raw = (
                    state_data.get("depends_on_workflows", [])
                    if isinstance(state_data, dict)
                    else []
                )
                actual_dependencies = (
                    set(actual_raw)
                    if isinstance(actual_raw, list)
                    and all(isinstance(item, str) for item in actual_raw)
                    else None
                )
                if actual_dependencies is None:
                    issues.append(
                        {
                            "path": str(state_path),
                            "message": (
                                "depends_on_workflows must be a list of workflow "
                                "or proposed PR ids."
                            ),
                        }
                    )
                elif actual_dependencies != expected_dependencies:
                    issues.append(
                        {
                            "path": str(state_path),
                            "message": (
                                "Workflow dependencies do not match proposed PR "
                                f"dependent_prs for {workflow_key!r}: expected "
                                f"{sorted(expected_dependencies)!r}, found "
                                f"{sorted(actual_dependencies)!r}."
                            ),
                        }
                    )
        report = validate_workflow_relationships(
            scoped_states,
            required_invariant_ids=[
                invariant["id"]
                for invariant in required_invariants
                if isinstance(invariant.get("id"), str)
            ],
            required_invariants=required_invariants,
            repository_root=repo_root,
        )
        relationships_checked += report.relationships_checked
        relationship_issues.extend(report.issues)
    relationship_report = WorkflowRelationshipValidationReport(
        validation_successful=not relationship_issues,
        relationships_checked=relationships_checked,
        issues=relationship_issues,
    )
    print(
        json.dumps(
            {
                "scope": "changed_branch_files",
                "changed_file_count": len(changed_paths),
                "task_count": len(task_paths),
                "template_count": len(
                    {
                        template_id
                        for ids in task_requirements.values()
                        for template_id in ids
                    }
                ),
                "task_template_references_valid": not issues,
                "relationship_validation": relationship_report.to_data(),
                "issues": issues,
                "validation_successful": not issues
                and relationship_report.validation_successful,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return not issues and relationship_report.validation_successful


def _validate_pr_file_specification(
    path: Path,
    kind: str,
    repo_root: Path,
) -> tuple[str, tuple[str, ...]]:
    normalize_specification_v1_file(path)
    automatic_repairs = deduplicate_specification_ids(path)
    proposed_yaml = _read_input(path)
    work_item_name = path.parent.name
    if kind == "system":
        report = validate_system_specification_yaml(
            proposed_yaml,
            work_item_name=work_item_name,
            repo_root=repo_root,
            file_path=path,
        )
    elif kind == "architecture":
        entity_types = tuple(load_entity_taxonomy(repo_root).entity_types)
        report = validate_architecture_specification_yaml(
            proposed_yaml,
            entity_types=entity_types,
            work_item_name=work_item_name,
            repo_root=repo_root,
            file_path=path,
        )
    elif kind == "implementation":
        report = validate_implementation_specification_yaml(
            proposed_yaml,
            architecture_specification_path=path.parent
            / "architecture-specification.yaml",
            work_item_name=work_item_name,
            repo_root=repo_root,
            file_path=path,
        )
    else:
        report = validate_pr_specification_yaml(
            proposed_yaml,
            work_item_name=work_item_name,
            repo_root=repo_root,
            file_path=path,
        )
    return report, automatic_repairs


def _automatic_repair_guidance(path: Path, repairs: tuple[str, ...]) -> str | None:
    if not repairs:
        return None
    changes = "; ".join(repairs)
    return (
        f"The evaluator automatically repaired {path.name}: {changes}. "
        "Re-read the rewritten file before taking another action and use its "
        "current IDs and formatting; do not continue from the previous file "
        "contents."
    )


def _add_llm_guidance_to_report(report_text: str, guidance: str | None) -> str:
    if guidance is None:
        return report_text
    try:
        report = json.loads(report_text)
        format_as_json = True
    except json.JSONDecodeError:
        try:
            report = yaml.safe_load(report_text)
        except yaml.YAMLError:
            return f"llm_guidance: {guidance}\n{report_text}"
        format_as_json = False
    if not isinstance(report, dict):
        return f"llm_guidance: {guidance}\n{report_text}"
    report["llm_guidance"] = guidance
    if format_as_json:
        return json.dumps(report, indent=2, ensure_ascii=False)
    return yaml.safe_dump(report, sort_keys=False)


def _run_edit_context(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    branch_name = args.branch_name or _current_branch(repo_root)
    report = lookup_edit_context(
        args.file,
        parse_line_ranges(args.line_ranges),
        branch_name=branch_name,
        parent_branch=args.parent_branch,
        repo_root=repo_root,
    )
    sys.stdout.write(render_edit_context_report(report))
    return 0


def _run_entity_references(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    branch_name = args.branch_name or _current_branch(repo_root)
    report = lookup_entity_references(
        args.entity,
        branch_name=branch_name,
        parent_branch=args.parent_branch,
        repo_root=repo_root,
    )
    sys.stdout.write(render_entity_reference_report(report))
    return 0


def _run_entity_relationships(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    branch_name = args.branch_name or _current_branch(repo_root)
    report = lookup_entity_relationships(
        args.entity,
        branch_name=branch_name,
        parent_branch=args.parent_branch,
        repo_root=repo_root,
    )
    sys.stdout.write(render_entity_relationship_report(report))
    return 0


def _run_invariants(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    branch_name = args.branch_name or _current_branch(repo_root)
    report = build_invariants_report(
        branch_name=branch_name,
        parent_branch=args.parent_branch,
        repo_root=repo_root,
    )
    sys.stdout.write(render_invariants_report(report))
    return 0


def _run_current_decisions(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    branch_name = args.branch_name or _current_branch(repo_root)
    report = build_current_decisions_report(
        branch_name=branch_name,
        parent_branch=args.parent_branch,
        repo_root=repo_root,
    )
    sys.stdout.write(render_current_decisions_report(report))
    return 0


def _run_codebase_state(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    output_path = create_codebase_state(
        branch_name=args.branch_name,
        output_path=args.output,
        parent_branch=args.parent_branch,
        repo_root=repo_root,
    )
    _stage_generated_file(repo_root, output_path)
    if args.output is None:
        default_output = codebase_state_default_output_path(repo_root)
        print(f"Wrote codebase state to {default_output}")
    else:
        print(f"Wrote codebase state to {output_path}")

    return 0


def _run_current_state(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    output_path = create_current_state_specification(
        branch_name=args.branch_name,
        output_path=args.output,
        parent_branch=args.parent_branch,
        repo_root=repo_root,
    )
    _stage_generated_file(repo_root, output_path)
    if args.output is None:
        default_output = current_state_specification_default_output_path(repo_root)
        print(f"Wrote current state report to {default_output}")
    else:
        print(f"Wrote current state report to {output_path}")

    return 0


def _run_architecture_specification(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    entity_types = args.entity_types
    if args.all_entity_types:
        try:
            entity_types = tuple(load_entity_taxonomy(repo_root).entity_types)
        except (OSError, ValueError) as exc:
            print(f"Unable to load the entity taxonomy: {exc}", file=sys.stderr)
            return 1
    if not entity_types:
        print(
            "Provide --entity-type at least once or use --all-entity-types.",
            file=sys.stderr,
        )
        return 2
    output_path = create_architecture_specification_template(
        entity_types,
        work_item_name=args.work_item_name,
        output_path=args.output,
        repo_root=repo_root,
        title=args.title,
    )
    _stage_generated_file(repo_root, output_path)
    if args.output is None:
        default_output = architecture_specification_default_output_path(
            args.work_item_name,
            repo_root,
        )
        print(f"Wrote architecture specification template to {default_output}")
    else:
        print(f"Wrote architecture specification template to {output_path}")

    return 0


def _run_implementation_specification(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    output_path = create_implementation_specification_template(
        architecture_specification_path=args.architecture_specification,
        work_item_name=args.work_item_name,
        output_path=args.output,
        repo_root=repo_root,
        title=args.title,
    )
    _stage_generated_file(repo_root, output_path)
    if args.output is None:
        default_output = implementation_specification_default_output_path(
            args.work_item_name,
            repo_root,
        )
        print(f"Wrote implementation specification template to {default_output}")
    else:
        print(f"Wrote implementation specification template to {output_path}")

    return 0


def _run_system_specification(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    output_path = create_system_specification_template(
        work_item_name=args.work_item_name,
        output_path=args.output,
        repo_root=repo_root,
        title=args.title,
    )
    _stage_generated_file(repo_root, output_path)
    if args.output is None:
        default_output = system_specification_default_output_path(
            args.work_item_name,
            repo_root,
        )
        print(f"Wrote system specification template to {default_output}")
    else:
        print(f"Wrote system specification template to {output_path}")

    return 0


def _run_project_structure(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    output_path = create_project_structure_template(
        output_path=(
            args.output
            if args.output is not None
            else "docs/project_structure/project-structure.yaml"
        ),
        repo_root=repo_root,
    )
    _stage_generated_file(repo_root, output_path)
    print(f"Wrote project structure template to {output_path}")
    return 0


def _run_validate_project_structure(args: argparse.Namespace) -> int:
    report = validate_project_structure_yaml(args.input)
    report_data = {
        "validation_successful": report.validation_successful,
        "issues": [
            {
                "code": issue.code,
                "message": issue.instructional_message(),
                "corrective_action": issue.corrective_action,
                **({"path": issue.path} if issue.path is not None else {}),
            }
            for issue in report.issues
        ],
    }
    sys.stdout.write(json.dumps(report_data, indent=2) + "\n")
    return 0 if report.validation_successful else 1


def _run_system_map_specification(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    output_path = create_system_map_specification_template(
        work_item_name=args.work_item_name,
        output_path=args.output,
        repo_root=repo_root,
        title=args.title,
    )
    _stage_generated_file(repo_root, output_path)
    if args.output is None:
        default_output = system_map_specification_default_output_path(
            args.work_item_name,
            repo_root,
        )
        print(f"Wrote system map specification template to {default_output}")
    else:
        print(f"Wrote system map specification template to {output_path}")

    return 0


def _run_feature_pr_specification(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    output_path = create_feature_pr_specification_template(
        work_item_name=args.work_item_name,
        output_path=args.output,
        repo_root=repo_root,
        title=args.title,
    )
    _stage_generated_file(repo_root, output_path)
    if args.output is None:
        default_output = feature_pr_specification_default_output_path(
            args.work_item_name,
            repo_root,
        )
        print(f"Wrote feature and PR specification template to {default_output}")
    else:
        print(f"Wrote feature and PR specification template to {output_path}")

    return 0


def _run_start_planning_feature(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    instructions = start_planning_feature(
        work_item_name=args.work_item_name,
        repo_root=repo_root,
    )
    sys.stdout.write(instructions)
    if not instructions.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _run_plan_diff_specification(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    output_path = create_plan_diff_specification(
        feature_plan_specification_path=args.feature_plan_specification,
        changelog_paths=args.changelog_paths,
        output_path=args.output,
        repo_root=repo_root,
    )
    _stage_generated_file(repo_root, output_path)
    if args.output is None:
        default_output = plan_diff_specification_default_output_path(
            args.feature_plan_specification,
            repo_root,
        )
        print(f"Wrote plan diff specification to {default_output}")
    else:
        print(f"Wrote plan diff specification to {output_path}")

    return 0


def _run_pr_specification(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    output_path = create_pr_specification_template(
        work_item_name=args.work_item_name,
        output_path=args.output,
        repo_root=repo_root,
    )
    _stage_generated_file(repo_root, output_path)
    if args.output is None:
        default_output = pr_specification_default_output_path(
            args.work_item_name,
            repo_root,
        )
        print(f"Wrote PR specification template to {default_output}")
    else:
        print(f"Wrote PR specification template to {output_path}")

    return 0


def _run_search_proposed_prs(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    report = search_proposed_pr_specifications(
        args.query,
        repo_root=repo_root,
        limit=args.limit,
    )
    rendered_report = render_proposed_pr_search_report(report)
    sys.stdout.write(rendered_report)
    if not rendered_report.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _run_show_proposed_pr(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    proposed_pr_specification = show_proposed_pr_specification(
        args.pr_number,
        repo_root=repo_root,
    )
    sys.stdout.write(proposed_pr_specification)
    if not proposed_pr_specification.endswith("\n"):
        sys.stdout.write("\n")
    return 0


_SPECIFICATION_FILENAMES = {
    "system-specification.yaml": "system",
    "system-specification.yml": "system",
    "architecture-specification.yaml": "architecture",
    "architecture-specification.yml": "architecture",
    "implementation-specification.yaml": "implementation",
    "implementation-specification.yml": "implementation",
    "proposed-pr-specification.yaml": "pr",
    "proposed-pr-specification.yml": "pr",
}
_SPECIFICATION_FILENAME_SUFFIXES = tuple(
    (f"-{filename}", kind) for filename, kind in _SPECIFICATION_FILENAMES.items()
)


def _specification_kind_for_filename(filename: str) -> str | None:
    kind = _SPECIFICATION_FILENAMES.get(filename)
    if kind is not None:
        return kind
    for suffix, suffix_kind in _SPECIFICATION_FILENAME_SUFFIXES:
        if filename.endswith(suffix):
            return suffix_kind
    return None


def _run_evaluate_specification(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    specification_paths: list[Path] = []
    if args.path is not None:
        input_path = args.path if args.path.is_absolute() else repo_root / args.path
        if not input_path.exists():
            print(f"Specification path does not exist: {input_path}", file=sys.stderr)
            return 1
        specification_paths = (
            [input_path]
            if input_path.is_file()
            else sorted(
                path
                for path in input_path.rglob("*")
                if path.is_file() and _specification_kind_for_filename(path.name)
            )
        )
    if args.path is not None and not specification_paths:
        print(
            f"No recognized specification-v1 YAML files found under {input_path}.",
            file=sys.stderr,
        )
        return 1

    if args.entity_types:
        entity_types = tuple(args.entity_types)
    else:
        try:
            entity_types = tuple(load_entity_taxonomy(repo_root).entity_types)
        except OSError:
            entity_types = ()
    overall_success = True
    for specification_path in specification_paths:
        kind = _specification_kind_for_filename(specification_path.name)
        if kind is None:
            print(
                f"Unsupported specification-v1 filename: {specification_path.name}",
                file=sys.stderr,
            )
            overall_success = False
            continue

        work_item_name = args.work_item_name or specification_path.parent.name
        try:
            normalize_specification_v1_file(specification_path)
            automatic_repairs = deduplicate_specification_ids(
                specification_path, reformat=False
            )
            proposed_yaml = _read_input(specification_path)
            if kind == "system":
                validation_successful = build_system_specification_validation_report(
                    proposed_yaml,
                    work_item_name=work_item_name,
                    repo_root=repo_root,
                ).validation_successful
                report_yaml = validate_system_specification_yaml(
                    proposed_yaml,
                    work_item_name=work_item_name,
                    repo_root=repo_root,
                    file_path=specification_path,
                )
            elif kind == "architecture":
                validation_successful = (
                    build_architecture_specification_validation_report(
                        proposed_yaml,
                        entity_types=entity_types,
                        work_item_name=work_item_name,
                        repo_root=repo_root,
                    ).validation_successful
                )
                report_yaml = validate_architecture_specification_yaml(
                    proposed_yaml,
                    entity_types=entity_types,
                    work_item_name=work_item_name,
                    repo_root=repo_root,
                    file_path=specification_path,
                )
            elif kind == "implementation":
                architecture_path = args.architecture_specification or (
                    specification_path.parent / "architecture-specification.yaml"
                )
                validation_successful = (
                    build_implementation_specification_validation_report(
                        proposed_yaml,
                        architecture_specification_path=architecture_path,
                        work_item_name=work_item_name,
                        repo_root=repo_root,
                    ).validation_successful
                )
                report_yaml = validate_implementation_specification_yaml(
                    proposed_yaml,
                    architecture_specification_path=architecture_path,
                    work_item_name=work_item_name,
                    repo_root=repo_root,
                    file_path=specification_path,
                )
            else:
                validation_successful = build_pr_specification_validation_report(
                    proposed_yaml,
                    work_item_name=work_item_name,
                    repo_root=repo_root,
                    file_path=specification_path,
                ).validation_successful
                report_yaml = validate_pr_specification_yaml(
                    proposed_yaml,
                    work_item_name=work_item_name,
                    repo_root=repo_root,
                    file_path=specification_path,
                )
        except (OSError, ValueError) as exc:
            print(f"{specification_path}: {exc}", file=sys.stderr)
            overall_success = False
            continue

        reformat_specification_file(specification_path)
        report_yaml = _add_llm_guidance_to_report(
            report_yaml,
            _automatic_repair_guidance(specification_path, automatic_repairs),
        )
        print(f"File: {specification_path}")
        sys.stdout.write(report_yaml)
        if not report_yaml.endswith("\n"):
            sys.stdout.write("\n")
        overall_success = overall_success and validation_successful

    if args.path is None:
        workflow_success = _evaluate_workflow_changed_files(
            repo_root=repo_root,
            base_branch=args.base_branch,
        )
        overall_success = overall_success and workflow_success

    return 0 if overall_success else 1


def _run_openai_proxy(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    log_dir = (
        args.log_dir
        if args.log_dir is not None
        else default_openai_proxy_log_dir(repo_root)
    )
    serve_openai_proxy(
        OpenAIProxyConfig(
            upstream_base_url=args.upstream_base_url,
            log_dir=log_dir,
            host=args.host,
            port=args.port,
            client_path_prefix=args.client_path_prefix,
            upstream_path_prefix=args.upstream_path_prefix,
        )
    )
    return 0


def _run_llm_diff(args: argparse.Namespace) -> int:
    try:
        first = _read_llm_exchange(args.first_file)
        second = _read_llm_exchange(args.second_file)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"llm-diff: {exc}", file=sys.stderr)
        return 2

    first_text = json.dumps(first, ensure_ascii=False, indent=2, sort_keys=True)
    second_text = json.dumps(second, ensure_ascii=False, indent=2, sort_keys=True)
    diff = difflib.unified_diff(
        first_text.splitlines(),
        second_text.splitlines(),
        fromfile=str(args.first_file),
        tofile=str(args.second_file),
        lineterm="",
    )
    output = "\n".join(diff)
    if output:
        sys.stdout.write(output + "\n")
    else:
        print("No differences.")
    return 0


def _run_workflow_replay(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    try:
        if args.error_log is not None:
            if not args.record_id:
                raise WorkflowReplayError("--record-id is required with --error-log.")
            if args.output is None:
                raise WorkflowReplayError("--output is required with --error-log.")
            error_log_path = (
                args.error_log
                if args.error_log.is_absolute()
                else repo_root / args.error_log
            )
            record = load_error_record(error_log_path, args.record_id)
            bundle = replay_bundle_from_error_record(record, repo_root=repo_root)
            output_path = (
                args.output if args.output.is_absolute() else repo_root / args.output
            )
            saved_path = save_workflow_replay_bundle(output_path, bundle)
            result: dict[str, Any] = {
                "status": "exported",
                "bundle": str(saved_path),
                "bundle_id": bundle["id"],
            }
        else:
            assert args.bundle is not None
            bundle_path = (
                args.bundle if args.bundle.is_absolute() else repo_root / args.bundle
            )
            bundle = load_workflow_replay_bundle(bundle_path)
            result = render_skill_replay(
                bundle,
                repo_root=repo_root,
                definition_path=args.definition,
            )
            result["status"] = "rendered"
    except WorkflowReplayError as exc:
        print(f"Workflow replay failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif result["status"] == "exported":
        print(f"Exported workflow replay bundle: {result['bundle']}")
    else:
        validation = result["response_validation"]
        step_label = result["step"]["id"] or result["step"]["index"]
        print(f"Rendered workflow replay {result['bundle_id']} for step {step_label}.")
        if validation["valid"]:
            print(f"Recorded response is valid: {validation['action']}")
        else:
            print(f"Recorded response is invalid: {validation['error']}")
    return 0


def _run_workflow_scenario(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    scenario_path = (
        args.scenario if args.scenario.is_absolute() else repo_root / args.scenario
    )
    try:
        scenario = load_workflow_scenario(scenario_path)
        result = run_workflow_scenario(
            scenario,
            scenario_path=scenario_path,
            repo_root=repo_root,
            keep_failed=args.keep_failed,
            max_roundtrips_override=args.max_roundtrips,
            max_stalled_roundtrips_override=args.max_stalled_roundtrips,
            stream_live=args.stream_live,
        )
    except WorkflowScenarioError as exc:
        print(f"Workflow scenario failed: {exc}", file=sys.stderr)
        return 1
    data = result.to_data()
    if args.report is not None:
        report_path = (
            args.report if args.report.is_absolute() else repo_root / args.report
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(f"Workflow scenario {result.scenario_id}: {result.status}")
        for assertion in result.assertions:
            if not assertion["passed"]:
                print(
                    f"Failed {assertion['name']}: expected {assertion['expected']!r}, "
                    f"got {assertion['actual']!r}",
                    file=sys.stderr,
                )
        if result.worktree_root is not None:
            print(f"Retained failed scenario repository: {result.worktree_root}")
    return 0 if result.status == "passed" else 1


def _run_validate_workflow_definition(args: argparse.Namespace) -> int:
    report = analyze_workflow_definition(args.definition)
    data = report.to_data()
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    elif report.validation_successful:
        print(f"Workflow definition valid: {report.definition}")
    else:
        print(f"Workflow definition invalid: {report.definition}", file=sys.stderr)
        for issue in report.issues:
            print(f"{issue.path}: {issue.code}: {issue.message}", file=sys.stderr)
    return 0 if report.validation_successful else 1


def _run_validate_delivery_profile(args: argparse.Namespace) -> int:
    profile_path = args.profile
    content = profile_path.read_text(encoding="utf-8")
    report = build_delivery_profile_validation_report(
        content,
        source_path=profile_path,
    )
    if args.json:
        print(json.dumps(report.to_data(), indent=2, ensure_ascii=False))
    elif report.validation_successful:
        print(f"Delivery profile valid: {profile_path}")
    else:
        print(f"Delivery profile invalid: {profile_path}", file=sys.stderr)
        for issue in report.issues:
            print(f"{issue.path}: {issue.code}: {issue.message}", file=sys.stderr)
    return 0 if report.validation_successful else 1


def _run_render_workflow_prompts(args: argparse.Namespace) -> int:
    paths = render_skill_prompt_snapshots(
        args.definition,
        output_dir=args.output_dir,
        repo_root=args.repo_root,
    )
    for path in paths:
        print(path)
    return 0


def _run_analyze_workflow_errors(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    error_paths = tuple(
        path if path.is_absolute() else repo_root / path for path in args.error_log
    )
    try:
        records = load_workflow_error_records(error_paths)
        clusters = cluster_workflow_errors(records)
        candidates: Sequence[Mapping[str, Any]] = ()
        if args.replay_output_dir is not None:
            output_dir = (
                args.replay_output_dir
                if args.replay_output_dir.is_absolute()
                else repo_root / args.replay_output_dir
            )
            candidates = promote_replay_candidates(
                clusters,
                repo_root=repo_root,
                output_dir=output_dir,
                limit=args.limit,
            )
    except WorkflowErrorAnalysisError as exc:
        print(f"Workflow error analysis failed: {exc}", file=sys.stderr)
        return 1
    data = workflow_error_analysis_data(
        clusters, record_count=len(records), candidates=candidates
    )
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(f"Workflow errors: {len(records)} records in {len(clusters)} clusters")
        for cluster in clusters:
            location = " / ".join(
                value
                for value in (cluster.skill_or_task, cluster.step, cluster.action)
                if value
            )
            print(
                f"{cluster.count}x (rank {cluster.rank}) {location or '<unknown>'}: "
                f"{cluster.error_summary or cluster.error_type or 'unknown error'}"
            )
    return 0


def _run_review_workflow_ambiguity(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    definition_path = (
        args.definition
        if args.definition.is_absolute()
        else repo_root / args.definition
    )
    try:
        requested_provider = resolve_workflow_provider(args.provider)
        mapping = _default_llm_mappings(requested_provider)["high_reasoning"]
        provider = mapping.provider
        model = args.model or mapping.model
        credentials = _resolve_credentials(provider, args.api_key, args.base_url)
        client = _build_chat_client(
            credentials,
            model=model,
            model_cache_dir=repo_root / ".powdrr" / "models",
            progress_stream=sys.stderr,
        )
        review = review_workflow_definition_step(
            client,
            definition_path,
            step_id=args.step_id,
            step_index=args.step_index,
        )
    except (KeyError, RuntimeError, WorkflowAmbiguityReviewError) as exc:
        print(f"Workflow ambiguity review failed: {exc}", file=sys.stderr)
        return 1
    data = review.to_data()
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(
            f"Ambiguity review: {review.definition} step "
            f"{review.step_id or review.step_index} "
            f"(confidence {review.confidence:.2f})"
        )
        for field in ("missing_information", "conflicts", "ambiguous_phrases"):
            values = data[field]
            if values:
                print(f"{field}: {', '.join(values)}")
    return 0


def _run_compare_workflow_definitions(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    thresholds = {
        "roundtrips": args.max_roundtrip_increase,
        "prompt_user_actions": args.max_prompt_user_increase,
        "repeated_actions": args.max_repeated_action_increase,
    }
    try:
        report = compare_workflow_definitions(
            repo_root=repo_root,
            baseline_ref=args.baseline_ref,
            replay_paths=args.replay or (),
            scenario_paths=args.scenario or (),
            thresholds=thresholds,
        )
    except WorkflowComparisonError as exc:
        print(f"Workflow comparison failed: {exc}", file=sys.stderr)
        return 1
    data = report.to_data()
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(
            f"Workflow comparison against {report.baseline_ref}: "
            f"{'passed' if report.passed else 'regressed'}"
        )
        for item in report.regressions:
            print(f"Regression: {item}", file=sys.stderr)
        for item in report.improvements:
            print(f"Improvement: {item}")
    return 0 if report.passed else 1


def _run_tune_workflow(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    report_path = args.report if args.report.is_absolute() else repo_root / args.report
    try:
        report = tune_workflow(
            definition=args.definition,
            repo_root=repo_root,
            baseline_ref=args.baseline_ref,
            replay_paths=args.replay or (),
            scenario_paths=args.scenario or (),
            thresholds={
                "roundtrips": args.max_roundtrip_increase,
                "prompt_user_actions": args.max_prompt_user_increase,
                "repeated_actions": args.max_repeated_action_increase,
            },
            snapshot_output_dir=args.snapshot_output_dir,
        )
        save_workflow_tuning_report(report_path, report)
    except WorkflowTuningError as exc:
        print(f"Workflow tuning failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"Workflow tuning {report['status']}: {report['definition']}")
        print(f"Report: {report_path}")
    return 0 if report["status"] == "passed" else 1


def _read_llm_exchange(path: Path) -> object:
    try:
        exchange = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc.msg}") from exc

    if not isinstance(exchange, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return exchange


def _run_download_qwen_model(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    cache_dir = repo_root / ".powdrr" / "models"
    model_path = download_local_qwen_model(cache_dir)
    print(f"Qwen model cached at {model_path}")
    return 0


def _run_workflow_chat(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    config = WorkflowChatConfig(
        skills_dir=args.skills_dir,
        repo_root=repo_root,
        output_dir=args.output_dir,
        provider=args.provider,
        adversarial_provider=args.adversarial_provider,
        model=args.model,
        api_key=args.api_key,
        base_url=args.base_url,
        max_turns=args.max_turns,
        max_stalled_roundtrips=args.max_stalled_roundtrips,
        verbose=args.verbose,
    )
    if args.provider == "auto" and sys.stdin.isatty():
        try:
            selected_provider = choose_workflow_provider()
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        config = replace(config, normal_provider=selected_provider)
    while True:
        if sys.stdin.isatty() and sys.stdout.isatty():
            exit_code = run_workflow_chat_tui(config)
        else:
            exit_code = run_workflow_chat(config)
        if exit_code != 0 or not sys.stdin.isatty():
            return exit_code
        try:
            next_action = input(
                "Press Enter for another request or type 'exit' to quit: "
            )
        except EOFError:
            return 0
        if next_action.strip().lower() in {"exit", "quit"}:
            return 0


def _run_entity_decisions(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    branch_name = args.branch_name or _current_branch(repo_root)
    report = lookup_entity_decisions(
        args.entity,
        branch_name=branch_name,
        parent_branch=args.parent_branch,
        repo_root=repo_root,
    )
    sys.stdout.write(render_entity_decision_report(report))
    return 0


def _run_blame_ui(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    branch_name = (
        args.branch_name_flag or args.branch_name or _current_branch(repo_root)
    )
    serve_blame_ui(
        repo_root=repo_root,
        branch_name=branch_name,
        parent_branch=args.parent_branch,
        selected_file=args.selected_file,
        host=args.host,
        port=args.port,
    )
    return 0


def _current_branch(repo_root: Path) -> str:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "rev-parse",
            "--abbrev-ref",
            "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    branch_name = result.stdout.strip()
    if not branch_name:
        raise ValueError("Could not determine the current branch name.")

    return branch_name


def _read_input(input_path: Path | None) -> str:
    if input_path is None:
        return sys.stdin.read()

    return input_path.read_text(encoding="utf-8")


def _resolve_template_output_path(
    repo_root: Path,
    explicit_output_path: Path | None,
    pr_number: int | None,
) -> Path | None:
    if explicit_output_path is not None:
        return explicit_output_path

    if pr_number is None:
        return None

    return repo_root / "docs" / "changelogs" / f"PR-{pr_number}-changelog.yaml"


def _resolve_template_input_path(
    repo_root: Path,
    explicit_input_path: Path | None,
    pr_number: int | None,
) -> Path | None:
    if explicit_input_path is not None:
        return explicit_input_path

    if pr_number is None:
        return None

    return repo_root / "docs" / "changelogs" / f"PR-{pr_number}-changelog.yaml"


if __name__ == "__main__":
    raise SystemExit(main())
