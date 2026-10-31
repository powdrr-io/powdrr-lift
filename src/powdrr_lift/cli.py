from __future__ import annotations

import argparse
import contextlib
import difflib
import hashlib
import io
import json
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml
from powdrr_contracts.models import (
    Artifact,
    OperationRequest,
    RepositorySnapshot,
    artifact_manifest_digest,
)
from powdrr_local import JsonServiceBridge, LocalPowdrrService
from powdrr_local.planning.checkpoints import (
    GitHubCommentCheckpointStore,
    LocalCheckpointStore,
)
from powdrr_local.planning.repair_publication import (
    RepairPublicationError,
    publish_repair_pull_request,
)
from powdrr_local.planning.sessions import GitHubPlanningIssueStore
from powdrr_local.planning.source import read_pinned_source
from powdrr_service.planning.assembly import PathFact
from powdrr_service.planning.compiler import PlanningCompilationError
from powdrr_service.planning.driver import SourceLedgerQuestionMachine
from powdrr_service.planning.plan_sizing import PATH_RUNTIME_SUFFIXES
from powdrr_service.planning.service import (
    PlanningOptions,
    PlanningServiceError,
    PlanningSession,
    run_planning_procedrr,
)
from powdrr_service.planning.source import segment_markdown_source
from powdrr_service.planning.workflow_ids import slugify_workflow_id
from powdrr_service.structrr.rebase import rebase_structrr_snapshot
from powdrr_service.structrr.verification_health import (
    VerificationPolicyMode,
    audit_verification_health,
)

from powdrr_lift.agent_bootstrap import (
    BootstrapTaskConfig,
    _run_output_root,
    run_bootstrap_task,
)
from powdrr_lift.agent_feature import (
    DEFAULT_FEATURE_TIMEOUT_SECONDS,
    run_agent_feature_task,
)
from powdrr_lift.artifact_state import ArtifactStateError, build_artifact_state
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
    create_design_interview_input_template,
    create_feature_pr_specification_template,
    create_implementation_specification_template,
    create_plan_diff_specification,
    create_pr_specification_template,
    create_system_map_specification_template,
    create_system_specification_template,
    current_state_specification_default_output_path,
    feature_pr_specification_default_output_path,
    implementation_specification_default_output_path,
    load_delivery_profile,
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
    validate_system_specification_yaml,
)
from powdrr_lift.core.capability_exception import CapabilityExceptionAuthority
from powdrr_lift.core.delivery_profile import PhaseType
from powdrr_lift.core.effective_contract import resolve_effective_contract
from powdrr_lift.core.entity_taxonomy import load_entity_taxonomy
from powdrr_lift.core.execution_plan import ExecutionPlan
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
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.execution.acceptance import (
    audit_capability_surface,
    run_final_acceptance,
)
from powdrr_lift.execution.builtin_tools import builtin_tool_registry
from powdrr_lift.execution.capabilities import (
    FileCapabilityExceptionStore,
)
from powdrr_lift.execution.checkpoints import ContentAddressedCheckpointStore
from powdrr_lift.execution.compaction import compatibility_report
from powdrr_lift.execution.runtime import ExecutionRuntime
from powdrr_lift.openai_proxy import (
    OpenAIProxyConfig,
    default_openai_proxy_log_dir,
)
from powdrr_lift.openai_proxy import (
    serve as serve_openai_proxy,
)
from powdrr_lift.process.tasks import (
    HumanRole,
    validate_workflow_task_directory,
)
from powdrr_lift.process.templates import (
    instantiate_procedrr_process_workflow,
    instantiate_workflow_template,
    instantiated_workflow_relationships,
    load_workflow_template,
)
from powdrr_lift.pull_request_description import (
    find_existing_pull_request,
    render_pull_request_description_template,
)
from powdrr_lift.repository_state import render_repository_state
from powdrr_lift.structrr.bootstrap import bootstrap_structrr
from powdrr_lift.structrr.intent import (
    IntentClause,
    IntentContract,
    IntentKind,
    IntentStore,
    IntentTrigger,
    make_intent_source,
)
from powdrr_lift.structrr.intent_lineage import audit_intent_lineage
from powdrr_lift.workrr.chat_config import WorkflowChatConfig
from powdrr_lift.workrr.chat_tui import run_workflow_chat_tui
from powdrr_lift.workrr.coding_agent import (
    CodingAgentAttemptStore,
    CodingAgentRunner,
    CodingAgentStatus,
    ImplementationRequest,
    OpenCodePermissionPolicy,
    build_coding_agent_provider,
)
from powdrr_lift.workrr.coding_agent_validation import (
    ValidationReportStatus,
    ValidationRunner,
    parse_validation_profile,
)
from powdrr_lift.workrr.credential_check import (
    check_coding_executable,
    check_jev_access,
    check_model_access,
    configured_model_mapping,
)
from powdrr_lift.workrr.deepswe_design_evaluation import (
    DEFAULT_STATE_DATA_RUBRIC,
    DeepSWEEvaluationError,
    evaluate_deepswe_design,
    evaluate_deepswe_worker_prompt,
    evaluate_prompt_case,
)
from powdrr_lift.workrr.deepswe_probe_evaluation import (
    DeepSWEProbeEvaluationError,
    evaluate_deepswe_probes,
)
from powdrr_lift.workrr.differential_verification import (
    classify_differential_result,
    detect_verifier_changes,
)
from powdrr_lift.workrr.feature_endpoint import (
    FeatureEndpointConfig,
    _structrr_taxonomy_path,
    run_feature_endpoint,
    run_feature_in_place,
)
from powdrr_lift.workrr.feature_run import (
    DEFAULT_FEATURE_NAME,
    DEFAULT_FEATURE_REQUEST,
    AgentFeatureRunConfig,
    run_agent_feature_e2e,
)
from powdrr_lift.workrr.git import (
    WorkflowGitState,
    cleanup_workflow_run,
    commit_and_push_workflow_initialization,
    create_workflow_worktree,
    inspect_workflow_run,
    integration_branch_name,
    load_workflow_git_state,
    new_publication_id,
    resolve_git_repository_root,
    save_workflow_git_state,
    synchronize_workflow_initialization,
)
from powdrr_lift.workrr.github_issue_plan_generation import (
    PlanGenerationError,
    load_plan_workflow_instance,
    publish_execution_plan_pull_request,
)
from powdrr_lift.workrr.github_issue_queue import (
    GitHubIssueClient,
    GitHubIssueQueueError,
    ReviewedExecutionPlan,
    TaskCheckpointCallback,
    continue_manual_prerequisite_issue,
    find_plan_issues,
    fulfill_issue_without_pr,
    load_reviewed_plan_at_commit,
    publish_plan_issues,
    run_worker_once,
)
from powdrr_lift.workrr.human_task import (
    HumanTaskRunnerConfig,
    run_human_task,
)
from powdrr_lift.workrr.local_model import download_local_qwen_model
from powdrr_lift.workrr.planning.evidence import (
    read_evidence_excerpt,
    search_requirements_evidence,
)
from powdrr_lift.workrr.procedrr_chat import run_procedrr_workflow_chat
from powdrr_lift.workrr.prompt_benchmark_comparison import (
    PromptBenchmarkComparisonError,
    compare_prompt_benchmark,
)
from powdrr_lift.workrr.provider_config import (
    ALL_PROVIDERS,
    default_llm_mappings,
    provider_definition,
)
from powdrr_lift.workrr.provider_runtime import resolve_workflow_provider
from powdrr_lift.workrr.providers import (
    build_workflow_client,
    resolve_provider_credentials,
)
from powdrr_lift.workrr.repository_subject_binding import (
    replay_subject_binding_events,
)
from powdrr_lift.workrr.run_artifacts import write_json_artifact, write_run_report
from powdrr_lift.workrr.scenario import (
    WorkflowScenarioError,
    extract_scripted_responses,
    extract_scripted_responses_from_report,
    load_workflow_scenario,
    run_workflow_scenario,
)
from powdrr_lift.workrr.task_agent import (
    WorkflowTaskAgentConfig,
    run_workflow_task,
)
from powdrr_lift.workrr.workflow_provider_selection import choose_workflow_provider

run_workflow_chat = run_procedrr_workflow_chat

_WORKFLOW_FILE_ADDED_EVENT_PREFIX = "[powdrr-file-added] "


def _positive_finite_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number of seconds") from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number of seconds")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="powdrr-lift")
    subparsers = parser.add_subparsers(dest="command", required=True)

    credentials_parser = subparsers.add_parser(
        "check-credentials",
        help=(
            "Check configured planning and coding model access, plus GitHub "
            "repository write access, before a run."
        ),
    )
    credentials_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Git repository root (defaults to the current Git repository).",
    )
    credentials_parser.add_argument(
        "--planning-provider", default="deepinfra-cheap", choices=ALL_PROVIDERS
    )
    credentials_parser.add_argument("--planning-model")
    credentials_parser.add_argument("--planning-api-key")
    credentials_parser.add_argument("--planning-base-url")
    credentials_parser.add_argument(
        "--coding-provider", default="deepinfra", choices=ALL_PROVIDERS
    )
    credentials_parser.add_argument(
        "--coding-model", default="deepseek-ai/DeepSeek-V4-Flash-0731"
    )
    credentials_parser.add_argument(
        "--code-agent", choices=("opencode", "minisweagent"), default="minisweagent"
    )
    credentials_parser.add_argument("--opencode-executable", default="opencode")
    credentials_parser.add_argument("--minisweagent-executable", default="mini")
    credentials_parser.add_argument("--json", action="store_true")
    credentials_parser.set_defaults(func=_run_check_credentials)

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

    publish_plan_issues_parser = subparsers.add_parser(
        "publish-plan-issues",
        help="Publish one minimal GitHub issue per item in a merged plan revision.",
    )
    publish_plan_issues_parser.add_argument("--plan", type=Path, required=True)
    publish_plan_issues_parser.add_argument("--plan-commit", required=True)
    publish_plan_issues_parser.add_argument("--repository", required=True)
    publish_plan_issues_parser.add_argument(
        "--repo-root", type=Path, help="Checkout containing the merged plan revision."
    )
    publish_plan_issues_parser.set_defaults(func=_run_publish_plan_issues)

    plan_parser = subparsers.add_parser(
        "plan",
        help=(
            "Analyze a Markdown plan against the current checkout, generate "
            "per-item PR workflows, and open a review pull request."
        ),
    )
    plan_parser.add_argument("--source", type=Path, required=True)
    plan_parser.add_argument("--plan-id", required=True)
    plan_parser.add_argument(
        "--verification-command",
        required=True,
        help="Repository validation command embedded in each generated PR workflow.",
    )
    plan_parser.add_argument("--output", type=Path)
    plan_parser.add_argument("--repo-root", type=Path)
    plan_parser.add_argument(
        "--planning-provider", default="deepinfra-cheap", choices=ALL_PROVIDERS
    )
    plan_parser.add_argument("--planning-model")
    plan_parser.add_argument(
        "--planning-concurrency",
        type=int,
        default=4,
        help="Maximum ready planning questions to ask concurrently (default: 4).",
    )
    plan_parser.add_argument("--planning-api-key")
    plan_parser.add_argument("--planning-base-url")
    plan_parser.add_argument("--json", action="store_true")
    plan_parser.set_defaults(func=_run_plan)

    plan_resume_parser = subparsers.add_parser(
        "plan-resume",
        help="Resume a durable bounded planning session by its GitHub issue number.",
    )
    plan_resume_parser.add_argument("--repository", required=True)
    plan_resume_parser.add_argument("--session-issue", type=int, required=True)
    plan_resume_parser.add_argument("--repo-root", type=Path)
    plan_resume_parser.add_argument(
        "--planning-provider", default="deepinfra-cheap", choices=ALL_PROVIDERS
    )
    plan_resume_parser.add_argument("--planning-model")
    plan_resume_parser.add_argument(
        "--planning-concurrency",
        type=int,
        default=4,
        help="Maximum ready planning questions to ask concurrently (default: 4).",
    )
    plan_resume_parser.add_argument("--planning-api-key")
    plan_resume_parser.add_argument("--planning-base-url")
    plan_resume_parser.add_argument("--json", action="store_true")
    plan_resume_parser.set_defaults(func=_run_plan_resume)

    plan_repair_parser = subparsers.add_parser(
        "plan-repair",
        help="Apply a typed repair to a reviewed plan and open an amendment PR.",
    )
    plan_repair_parser.add_argument("--plan", type=Path, required=True)
    plan_repair_parser.add_argument(
        "--request",
        type=Path,
        required=True,
        help=(
            "JSON containing one discovery, bounded impact, and typed replacement "
            "records."
        ),
    )
    plan_repair_parser.add_argument("--repo-root", type=Path)
    plan_repair_parser.add_argument("--json", action="store_true")
    plan_repair_parser.set_defaults(func=_run_plan_repair)

    list_plan_issues_parser = subparsers.add_parser(
        "list-plan-issues",
        help="Read the GitHub issue status for every item in a pinned plan revision.",
    )
    list_plan_issues_parser.add_argument("--plan", type=Path, required=True)
    list_plan_issues_parser.add_argument("--plan-commit", required=True)
    list_plan_issues_parser.add_argument("--repository", required=True)
    list_plan_issues_parser.add_argument(
        "--repo-root", type=Path, help="Checkout containing the pinned plan revision."
    )
    list_plan_issues_parser.set_defaults(func=_run_list_plan_issues)

    fulfill_plan_issue_parser = subparsers.add_parser(
        "fulfill-plan-issue-without-pr",
        help=(
            "Mark a planned issue fulfilled by existing work without an "
            "implementation PR."
        ),
    )
    fulfill_plan_issue_parser.add_argument("--repository", required=True)
    fulfill_plan_issue_parser.add_argument("--issue-number", type=int, required=True)
    fulfill_plan_issue_parser.add_argument(
        "--reason", required=True, help="Why the planned work is already fulfilled."
    )
    fulfill_plan_issue_parser.set_defaults(func=_run_fulfill_plan_issue_without_pr)

    github_issue_worker_parser = subparsers.add_parser(
        "github-issue-worker",
        help="Claim and execute one eligible GitHub issue work item.",
    )
    github_issue_worker_parser.add_argument("--repository", required=True)
    github_issue_worker_parser.add_argument(
        "--repo-root", type=Path, help="Checkout containing plan and workflow files."
    )
    github_issue_worker_parser.add_argument(
        "--worker-id", default=f"local-{uuid.uuid4().hex[:8]}"
    )
    github_issue_worker_parser.add_argument(
        "--provider", choices=["auto", *ALL_PROVIDERS], default="auto"
    )
    github_issue_worker_parser.add_argument("--api-key")
    github_issue_worker_parser.add_argument("--base-url")
    github_issue_worker_parser.add_argument(
        "--code-agent", choices=("opencode", "minisweagent"), default="minisweagent"
    )
    github_issue_worker_parser.add_argument("--opencode-executable", default="opencode")
    github_issue_worker_parser.add_argument("--opencode-model")
    github_issue_worker_parser.add_argument("--minisweagent-executable", default="mini")
    github_issue_worker_parser.add_argument(
        "--minisweagent-model",
        default="deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731",
    )
    github_issue_worker_parser.add_argument(
        "--coding-agent-timeout-seconds", type=float, default=1800.0
    )
    github_issue_worker_parser.add_argument(
        "--coding-agent-validation-timeout-seconds", type=float, default=1800.0
    )
    github_issue_worker_parser.add_argument("--max-roundtrips", type=int, default=128)
    github_issue_worker_parser.add_argument(
        "--context-compaction-threshold", type=float, default=0.75
    )
    github_issue_worker_parser.add_argument("--verbose", action="store_true")
    github_issue_worker_parser.set_defaults(func=_run_github_issue_worker)

    continue_issue_worker_parser = subparsers.add_parser(
        "github-issue-worker-continue",
        help=(
            "Retry CI for a staged manual prerequisite PR and resume its issue "
            "workflow."
        ),
    )
    continue_issue_worker_parser.add_argument("--repository", required=True)
    continue_issue_worker_parser.add_argument("--issue-number", type=int, required=True)
    continue_issue_worker_parser.add_argument(
        "--repo-root", type=Path, help="Checkout containing the issue plan."
    )
    continue_issue_worker_parser.add_argument(
        "--provider", choices=["auto", *ALL_PROVIDERS], default="auto"
    )
    continue_issue_worker_parser.add_argument("--api-key")
    continue_issue_worker_parser.add_argument("--base-url")
    continue_issue_worker_parser.add_argument(
        "--code-agent", choices=("opencode", "minisweagent"), default="minisweagent"
    )
    continue_issue_worker_parser.add_argument(
        "--opencode-executable", default="opencode"
    )
    continue_issue_worker_parser.add_argument("--opencode-model")
    continue_issue_worker_parser.add_argument(
        "--minisweagent-executable", default="mini"
    )
    continue_issue_worker_parser.add_argument(
        "--minisweagent-model",
        default="deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731",
    )
    continue_issue_worker_parser.add_argument(
        "--coding-agent-timeout-seconds", type=float, default=1800.0
    )
    continue_issue_worker_parser.add_argument(
        "--coding-agent-validation-timeout-seconds", type=float, default=1800.0
    )
    continue_issue_worker_parser.add_argument("--max-roundtrips", type=int, default=128)
    continue_issue_worker_parser.add_argument(
        "--context-compaction-threshold", type=float, default=0.75
    )
    continue_issue_worker_parser.add_argument("--verbose", action="store_true")
    continue_issue_worker_parser.set_defaults(func=_run_github_issue_worker_continue)

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

    bootstrap_structrr_parser = subparsers.add_parser(
        "bootstrap-structrr",
        aliases=["bootstrap_structrr"],
        help="Generate and validate a Structrr snapshot from repository evidence.",
    )
    bootstrap_structrr_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to inspect; defaults to the current worktree.",
    )
    bootstrap_structrr_parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Output YAML path (default: "
            "docs/structrr/current/baseline-<short-git-hash>.yaml)."
        ),
    )
    bootstrap_structrr_parser.add_argument(
        "--change-id",
        default="bootstrap",
        help="Snapshot change id (default: bootstrap).",
    )
    bootstrap_structrr_parser.add_argument(
        "--title",
        help="Snapshot title; defaults to a title derived from the repository name.",
    )
    bootstrap_structrr_parser.add_argument(
        "--taxonomy",
        type=Path,
        default=Path("software_development_entity_taxonomy.md"),
        help="Repository-relative taxonomy path.",
    )
    bootstrap_structrr_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the validated bootstrap summary as JSON.",
    )
    bootstrap_structrr_parser.set_defaults(func=_run_bootstrap_structrr)

    bootstrap_parser = subparsers.add_parser(
        "bootstrap",
        help="Create a validated project context snapshot in an agent worktree.",
    )
    bootstrap_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Git repository root (defaults to the current Git repository).",
    )
    bootstrap_parser.add_argument("--work-item-name", default="repository-bootstrap")
    bootstrap_parser.add_argument("--base-ref", default="HEAD")
    bootstrap_parser.add_argument("--base-branch")
    bootstrap_parser.add_argument("--remote", default="origin")
    bootstrap_parser.add_argument("--output-root", type=Path)
    bootstrap_parser.add_argument(
        "--validation-timeout-seconds",
        type=float,
        default=600.0,
        help="Maximum time for each discovered validation command (default: 600).",
    )
    bootstrap_parser.add_argument(
        "--taxonomy",
        type=Path,
        default=Path("software_development_entity_taxonomy.md"),
    )
    bootstrap_parser.add_argument(
        "--open-pr",
        dest="open_pr",
        action="store_true",
        default=True,
        help="Open a pull request for the bootstrap branch (default).",
    )
    bootstrap_parser.add_argument(
        "--no-open-pr",
        dest="open_pr",
        action="store_false",
        help="Keep the bootstrap branch local instead of opening a pull request.",
    )
    bootstrap_parser.add_argument("--json", action="store_true")
    bootstrap_parser.set_defaults(func=_run_bootstrap)

    implement_parser = subparsers.add_parser(
        "implement",
        help="Implement a feature headlessly in an isolated agent worktree.",
    )
    implement_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Git repository root (defaults to the current Git repository).",
    )
    feature_input = implement_parser.add_mutually_exclusive_group(required=True)
    feature_input.add_argument("--request-file", type=Path)
    feature_input.add_argument("--feature-description")
    implement_parser.add_argument("--work-item-name", required=True)
    implement_parser.add_argument("--headless", action="store_true", required=True)
    implement_parser.add_argument(
        "--allowed-path", action="append", default=[], dest="allowed_paths"
    )
    implement_parser.add_argument("--validation-command")
    implement_parser.add_argument("--base-ref", default="HEAD")
    implement_parser.add_argument("--base-branch")
    implement_parser.add_argument("--remote", default="origin")
    implement_parser.add_argument("--open-pr", action="store_true")
    implement_parser.add_argument(
        "--timeout-seconds",
        type=_positive_finite_seconds,
        default=DEFAULT_FEATURE_TIMEOUT_SECONDS,
        help=(
            "Overall feature execution wall-clock limit after credential preflight "
            f"(default: {DEFAULT_FEATURE_TIMEOUT_SECONDS} seconds)."
        ),
    )
    implement_parser.add_argument(
        "--max-repair-attempts",
        type=int,
        choices=range(1, 6),
        default=2,
        help="Maximum focused candidate repair attempts (default: 2; range: 1-5).",
    )
    implement_parser.add_argument("--run-id")
    implement_parser.add_argument("--output-root", type=Path)
    implement_parser.add_argument(
        "--planning-provider", default="deepinfra-cheap", choices=ALL_PROVIDERS
    )
    implement_parser.add_argument("--planning-model")
    implement_parser.add_argument("--planning-api-key")
    implement_parser.add_argument("--planning-base-url")
    implement_parser.add_argument(
        "--coding-provider", default="deepinfra", choices=ALL_PROVIDERS
    )
    implement_parser.add_argument(
        "--coding-model", default="deepseek-ai/DeepSeek-V4-Flash-0731"
    )
    implement_parser.add_argument(
        "--code-agent", choices=("opencode", "minisweagent"), default="minisweagent"
    )
    implement_parser.add_argument("--opencode-executable", default="opencode")
    implement_parser.add_argument(
        "--opencode-model", default="deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731"
    )
    implement_parser.add_argument("--minisweagent-executable", default="mini")
    implement_parser.add_argument("--minisweagent-model")
    implement_parser.add_argument("--code-agent-prompt-prefix", default="")
    implement_parser.add_argument("--code-agent-prompt-suffix", default="")
    implement_parser.add_argument("--json", action="store_true")
    implement_parser.set_defaults(func=_run_implement)

    verification_health_parser = subparsers.add_parser(
        "verification-health",
        aliases=["verification_health"],
        help="Audit intent-linked verification coverage and evidence freshness.",
    )
    verification_health_parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help=(
            "JSON or YAML object containing intents, contracts, inventory, "
            "and evidence."
        ),
    )
    verification_health_parser.add_argument(
        "--mode",
        choices=[mode.value for mode in VerificationPolicyMode],
        default=VerificationPolicyMode.REQUIRED_FOR_NEW.value,
    )
    verification_health_parser.add_argument("--candidate-tree")
    verification_health_parser.add_argument("--today")
    verification_health_parser.add_argument("--output", type=Path)
    verification_health_parser.set_defaults(func=_run_verification_health)

    differential_parser = subparsers.add_parser(
        "verification-diff",
        aliases=["verification_diff"],
        help="Classify base/candidate evidence and identify verifier changes.",
    )
    differential_parser.add_argument("--base", type=Path, required=True)
    differential_parser.add_argument("--candidate", type=Path, required=True)
    differential_parser.add_argument("--not-comparable", action="store_true")
    differential_parser.add_argument("--output", type=Path)
    differential_parser.set_defaults(func=_run_verification_diff)

    rebase_structrr_parser = subparsers.add_parser(
        "rebase-structrr",
        aliases=["rebase_structrr"],
        help="Compare Structrr snapshots and report proposal rebase impact.",
    )
    rebase_structrr_parser.add_argument(
        "--baseline", type=Path, required=True, help="Proposal baseline snapshot YAML."
    )
    rebase_structrr_parser.add_argument(
        "--current", type=Path, required=True, help="Current Structrr snapshot YAML."
    )
    rebase_structrr_parser.add_argument(
        "--entity-id",
        action="append",
        default=[],
        help="Entity referenced by the proposal; may be repeated.",
    )
    rebase_structrr_parser.add_argument(
        "--source-subject-key",
        action="append",
        default=[],
        help="Source stable_key referenced by the proposal; may be repeated.",
    )
    rebase_structrr_parser.add_argument(
        "--binding-id",
        action="append",
        default=[],
        help="Source binding referenced by the proposal; may be repeated.",
    )
    rebase_structrr_parser.add_argument(
        "--output", type=Path, help="Write the JSON report to this path."
    )
    rebase_structrr_parser.set_defaults(func=_run_rebase_structrr)

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
            ".powdrr/state/codebase-state.yaml."
        ),
    )
    codebase_state_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    codebase_state_parser.set_defaults(func=_run_codebase_state)

    artifact_state_parser = subparsers.add_parser(
        "artifact-state",
        aliases=["artifact_state"],
        help="Report Powdrr plans, workflows, entities, and changelogs at a commit.",
    )
    artifact_state_parser.add_argument(
        "--branch", required=True, help="Branch that contains the requested commit."
    )
    artifact_state_parser.add_argument(
        "--sha", required=True, help="Commit whose repository state should be read."
    )
    artifact_state_parser.add_argument(
        "--repository",
        help="GitHub repository in owner/name form; inferred from origin when omitted.",
    )
    artifact_state_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Local clone to inspect; defaults to the current Git repository.",
    )
    artifact_state_parser.add_argument(
        "--no-github",
        action="store_true",
        help="Skip live GitHub pull request lookup.",
    )
    for area in (
        "bootstrap",
        "entities",
        "relationships",
        "workflows",
        "plans",
        "changelogs",
    ):
        artifact_state_parser.add_argument(
            f"--{area.replace('_', '-')}",
            action="store_true",
            help=f"Include the {area.replace('_', ' ')} area.",
        )
    artifact_state_parser.add_argument(
        "--pull-requests",
        action="store_true",
        help="Include live GitHub pull request data.",
    )
    artifact_state_parser.set_defaults(func=_run_artifact_state)

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
            ".powdrr/state/current-state.yaml."
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
    feature_pr_specification_parser.add_argument(
        "--interview-input",
        type=Path,
        help="JSON design-interview input used to seed the proposal template.",
    )
    feature_pr_specification_parser.set_defaults(func=_run_feature_pr_specification)

    design_interview_input_parser = subparsers.add_parser(
        "design-interview-input",
        aliases=["design_interview_input"],
        help="Create a JSON input document for the design interview.",
    )
    design_interview_input_parser.add_argument("--work-item-name", required=True)
    design_interview_input_parser.add_argument("--output", type=Path)
    design_interview_input_parser.add_argument("--repo-root", type=Path)
    design_interview_input_parser.set_defaults(func=_run_design_interview_input)

    start_planning_feature_parser = subparsers.add_parser(
        "start-planning-feature",
        aliases=["start_planning_feature"],
        help="Generate the instructions for starting feature planning.",
    )
    start_planning_feature_parser.add_argument(
        "--work-item-name",
        required=True,
        help="Work item name used to fill the planning instructions.",
    )
    start_planning_feature_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when reading the planning instructions.",
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

    workflow_scenario_parser = subparsers.add_parser(
        "workflow-scenario",
        aliases=["workflow_scenario"],
        help=(
            "Run a scripted Procedrr process or durable-task scenario in an "
            "isolated temporary "
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
        "--extract-responses",
        type=Path,
        help="Write parsed model outputs as a replayable YAML/JSON response fixture.",
    )
    workflow_scenario_parser.add_argument(
        "--verify-extracted",
        action="store_true",
        help="Replay extracted responses immediately and fail if replay does not pass.",
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

    scenario_suite_parser = subparsers.add_parser(
        "workflow-scenario-suite",
        help="Replay a checked-in manifest of deterministic workflow scenarios.",
    )
    scenario_suite_parser.add_argument("--manifest", required=True, type=Path)
    scenario_suite_parser.add_argument("--repo-root", type=Path)
    scenario_suite_parser.add_argument("--report", type=Path)
    scenario_suite_parser.add_argument("--json", action="store_true")
    scenario_suite_parser.set_defaults(func=_run_workflow_scenario_suite)

    agent_feature_parser = subparsers.add_parser(
        "agent-feature-e2e",
        help=(
            "Prompt the live Powdrr agent to specify and implement a feature, "
            "run its generated workflows, and verify the resulting artifacts."
        ),
    )
    agent_feature_parser.add_argument("--repo-root", type=Path)
    agent_feature_parser.add_argument("--feature-name", default=DEFAULT_FEATURE_NAME)
    agent_feature_parser.add_argument(
        "--feature-request", default=DEFAULT_FEATURE_REQUEST
    )
    agent_feature_parser.add_argument(
        "--answer", action="append", default=[], help="Answer a live agent follow-up."
    )
    agent_feature_parser.add_argument(
        "--provider",
        choices=["auto", *ALL_PROVIDERS],
        default="deepinfra-cheap",
        help="LLM provider for every live phase (default: deepinfra-cheap).",
    )
    agent_feature_parser.add_argument("--max-turns", type=int, default=40)
    agent_feature_parser.add_argument("--workflow-roundtrips", type=int, default=128)
    agent_feature_parser.add_argument("--start-iterations", type=int, default=10)
    agent_feature_parser.add_argument(
        "--phase-timeout",
        type=float,
        default=None,
        help=(
            "Optional maximum seconds without semantic phase progress before "
            "stopping it (default: no timeout; resets on progress)."
        ),
    )
    agent_feature_parser.add_argument("--report", type=Path)
    agent_feature_parser.add_argument("--transcript-dir", type=Path)
    agent_feature_parser.add_argument("--json", action="store_true")
    agent_feature_parser.set_defaults(func=_run_agent_feature_e2e)

    workrr_feature_parser = subparsers.add_parser(
        "workrr-feature",
        aliases=["workrr_feature"],
        help=(
            "Plan a feature in Structrr, implement it with the selected code agent, "
            "validate and "
            "review the diff, then open a pull request."
        ),
    )
    workrr_feature_parser.add_argument("--feature-description", required=True)
    workrr_feature_parser.add_argument("--work-item-name", required=True)
    workrr_feature_parser.add_argument("--repo-root", type=Path)
    workrr_feature_parser.add_argument(
        "--allowed-path", action="append", required=True, dest="allowed_paths"
    )
    workrr_feature_parser.add_argument(
        "--validation-command",
        help=(
            "Optional feature-specific validation command as one shell-style "
            "argument. Repository checks are discovered automatically."
        ),
    )
    workrr_feature_parser.add_argument("--base-branch", default="main")
    workrr_feature_parser.add_argument(
        "--code-agent", choices=("opencode", "minisweagent"), default="minisweagent"
    )
    workrr_feature_parser.add_argument("--opencode-executable", default="opencode")
    workrr_feature_parser.add_argument(
        "--opencode-model",
        default="deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731",
    )
    workrr_feature_parser.add_argument("--minisweagent-executable", default="mini")
    workrr_feature_parser.add_argument(
        "--minisweagent-model",
        default="deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731",
    )
    workrr_feature_parser.add_argument("--code-agent-prompt-prefix", default="")
    workrr_feature_parser.add_argument("--code-agent-prompt-suffix", default="")
    workrr_feature_parser.add_argument(
        "--planning-provider",
        default="deepinfra-cheap",
        choices=ALL_PROVIDERS,
        help="Provider used for the design-interview planning subprocess.",
    )
    workrr_feature_parser.add_argument(
        "--planning-model",
        help="Override the planning provider's standard-reasoning model.",
    )
    workrr_feature_parser.add_argument("--planning-api-key")
    workrr_feature_parser.add_argument("--planning-base-url")
    workrr_feature_parser.add_argument("--task-id")
    workrr_feature_parser.add_argument("--output-root", type=Path)
    workrr_feature_parser.add_argument(
        "--no-open-pr",
        action="store_true",
        help="Run all local phases but stop before creating the pull request.",
    )
    workrr_feature_parser.add_argument("--json", action="store_true")
    workrr_feature_parser.set_defaults(func=_run_workrr_feature)

    harbor_feature_parser = subparsers.add_parser(
        "harbor-feature",
        aliases=["harbor_feature"],
        description="Run implement-feature in benchmark mode for Harbor.",
        help=(
            "Run implement-feature in benchmark mode in the current sandbox "
            "checkout and commit the result for Harbor to collect."
        ),
    )
    harbor_feature_parser.add_argument("--feature-description", required=True)
    harbor_feature_parser.add_argument("--work-item-name", required=True)
    harbor_feature_parser.add_argument(
        "--task-id", help="Stable benchmark/task identifier propagated to run metadata."
    )
    harbor_feature_parser.add_argument("--repo-root", type=Path)
    harbor_feature_parser.add_argument(
        "--allowed-path", action="append", dest="allowed_paths", default=["."]
    )
    harbor_feature_parser.add_argument("--validation-command")
    harbor_feature_parser.add_argument("--opencode-executable", default="opencode")
    harbor_feature_parser.add_argument(
        "--opencode-model",
        default="deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731",
    )
    harbor_feature_parser.add_argument(
        "--code-agent", choices=("opencode", "minisweagent"), default="minisweagent"
    )
    harbor_feature_parser.add_argument("--minisweagent-executable", default="mini")
    harbor_feature_parser.add_argument(
        "--minisweagent-model",
        default="deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731",
    )
    harbor_feature_parser.add_argument("--code-agent-prompt-prefix", default="")
    harbor_feature_parser.add_argument("--code-agent-prompt-suffix", default="")
    harbor_feature_parser.add_argument(
        "--planning-provider",
        default="deepinfra",
        choices=ALL_PROVIDERS,
    )
    harbor_feature_parser.add_argument("--planning-model")
    harbor_feature_parser.add_argument("--planning-api-key")
    harbor_feature_parser.add_argument("--planning-base-url")
    harbor_feature_parser.add_argument("--output-root", type=Path)
    harbor_run_mode = harbor_feature_parser.add_mutually_exclusive_group()
    harbor_run_mode.add_argument(
        "--design-only",
        action="store_true",
        help=(
            "Run instruction classification and prompt evaluation "
            "without a coding agent."
        ),
    )
    harbor_run_mode.add_argument(
        "--capture-worker-prompts-only",
        action="store_true",
        help=(
            "Run implement-feature through accepted code-task preconditions, "
            "persist provider-ready worker prompts, and stop before coding."
        ),
    )
    harbor_feature_parser.add_argument("--json", action="store_true")
    harbor_feature_parser.set_defaults(func=_run_harbor_feature)

    deepswe_design_eval_parser = subparsers.add_parser(
        "evaluate-deepswe-design",
        help=(
            "Evaluate a design-only run against a DeepSWE reference solution "
            "and verifier tests."
        ),
    )
    deepswe_design_eval_parser.add_argument("--task-dir", required=True, type=Path)
    deepswe_design_eval_parser.add_argument("--run-dir", required=True, type=Path)
    deepswe_design_eval_parser.add_argument(
        "--rubric", type=Path, default=DEFAULT_STATE_DATA_RUBRIC
    )
    deepswe_design_eval_parser.add_argument("--report", type=Path)
    deepswe_design_eval_parser.add_argument(
        "--judge-provider", default="deepinfra-cheap", choices=ALL_PROVIDERS
    )
    deepswe_design_eval_parser.add_argument("--judge-model")
    deepswe_design_eval_parser.add_argument("--judge-api-key")
    deepswe_design_eval_parser.add_argument("--judge-base-url")
    deepswe_design_eval_parser.add_argument("--json", action="store_true")
    deepswe_design_eval_parser.set_defaults(func=_run_deepswe_design_evaluation)

    deepswe_probe_eval_parser = subparsers.add_parser(
        "evaluate-deepswe-probes",
        help=("Compare a frozen generated probe suite with DeepSWE verifier tests."),
    )
    deepswe_probe_eval_parser.add_argument("--task-dir", required=True, type=Path)
    deepswe_probe_eval_parser.add_argument("--run-dir", required=True, type=Path)
    deepswe_probe_eval_parser.add_argument("--probe-suite", type=Path)
    deepswe_probe_eval_parser.add_argument("--report", type=Path)
    deepswe_probe_eval_parser.add_argument(
        "--judge-provider", default="deepinfra-cheap", choices=ALL_PROVIDERS
    )
    deepswe_probe_eval_parser.add_argument("--judge-model")
    deepswe_probe_eval_parser.add_argument("--judge-api-key")
    deepswe_probe_eval_parser.add_argument("--judge-base-url")
    deepswe_probe_eval_parser.add_argument("--json", action="store_true")
    deepswe_probe_eval_parser.set_defaults(func=_run_deepswe_probe_evaluation)

    deepswe_prompt_eval_parser = subparsers.add_parser(
        "evaluate-deepswe-prompt",
        help=(
            "Evaluate captured worker prompts against a DeepSWE reference "
            "solution and verifier tests."
        ),
    )
    deepswe_prompt_eval_parser.add_argument("--task-dir", required=True, type=Path)
    deepswe_prompt_eval_parser.add_argument("--run-dir", required=True, type=Path)
    deepswe_prompt_eval_parser.add_argument(
        "--rubric", type=Path, default=DEFAULT_STATE_DATA_RUBRIC
    )
    deepswe_prompt_eval_parser.add_argument("--report", type=Path)
    deepswe_prompt_eval_parser.add_argument(
        "--judge-provider", default="deepinfra-cheap", choices=ALL_PROVIDERS
    )
    deepswe_prompt_eval_parser.add_argument("--judge-model")
    deepswe_prompt_eval_parser.add_argument("--judge-api-key")
    deepswe_prompt_eval_parser.add_argument("--judge-base-url")
    deepswe_prompt_eval_parser.add_argument("--json", action="store_true")
    deepswe_prompt_eval_parser.set_defaults(func=_run_deepswe_prompt_evaluation)

    prompt_eval_parser = subparsers.add_parser(
        "evaluate-prompt",
        help=(
            "Evaluate captured worker prompts with a versioned prompt case and rubric."
        ),
    )
    prompt_eval_parser.add_argument("--case", required=True, type=Path)
    prompt_eval_parser.add_argument("--run-dir", required=True, type=Path)
    prompt_eval_parser.add_argument("--rubric", required=True, type=Path)
    prompt_eval_parser.add_argument("--report", type=Path)
    prompt_eval_parser.add_argument(
        "--judge-provider", default="deepinfra-cheap", choices=ALL_PROVIDERS
    )
    prompt_eval_parser.add_argument("--judge-model")
    prompt_eval_parser.add_argument("--judge-api-key")
    prompt_eval_parser.add_argument("--judge-base-url")
    prompt_eval_parser.add_argument("--json", action="store_true")
    prompt_eval_parser.set_defaults(func=_run_generic_prompt_evaluation)

    prompt_benchmark_parser = subparsers.add_parser(
        "compare-prompt-benchmark",
        help="Compare a prompt evaluation with final benchmark validation results.",
    )
    prompt_benchmark_parser.add_argument("--prompt-report", required=True, type=Path)
    prompt_benchmark_parser.add_argument("--benchmark-run", required=True, type=Path)
    prompt_benchmark_parser.add_argument("--report", type=Path)
    prompt_benchmark_parser.add_argument("--json", action="store_true")
    prompt_benchmark_parser.set_defaults(func=_run_prompt_benchmark_comparison)

    replay_subject_bindings_parser = subparsers.add_parser(
        "replay-subject-bindings",
        help=(
            "Replay deterministic repository subject binding from a saved "
            "Procedrr event log without model calls."
        ),
    )
    replay_subject_bindings_parser.add_argument("--events", required=True, type=Path)
    replay_subject_bindings_parser.add_argument("--output", type=Path)
    replay_subject_bindings_parser.set_defaults(func=_run_replay_subject_bindings)

    extract_responses_parser = subparsers.add_parser(
        "extract-workflow-responses",
        help=(
            "Convert a live workflow scenario report into a scripted response fixture."
        ),
    )
    extract_responses_parser.add_argument("--report", required=True, type=Path)
    extract_responses_parser.add_argument("--output", required=True, type=Path)
    extract_responses_parser.set_defaults(func=_extract_workflow_responses)

    procedrr_validation_parser = subparsers.add_parser(
        "validate-procedrr-processes",
        help="Validate every Procedrr process in one or more process directories.",
    )
    procedrr_validation_parser.add_argument("paths", nargs="+", type=Path)
    procedrr_validation_parser.add_argument("--json", action="store_true")
    procedrr_validation_parser.set_defaults(func=_run_validate_procedrr_processes)

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
        help="Provider used for nested adversarial review processes.",
    )
    workflow_chat_parser.add_argument(
        "--processes-dir",
        dest="processes_dir",
        metavar="PROCESSES_DIR",
        type=Path,
        default=Path("docs/procedrr/processes"),
        help="Directory containing validated Procedrr process definitions.",
    )
    workflow_chat_parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory to write generated workflow task JSON files to.",
    )
    workflow_chat_parser.add_argument(
        "--model",
        default="glm-5.2",
        help="Model to use for Procedrr process selection and execution.",
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
        help="Repository root to resolve the default process directory.",
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
        help="Legacy workflow template YAML or JSON to instantiate.",
    )
    instantiate_workflow_parser.add_argument(
        "--process",
        help="Procedrr process to run as a durable workflow task.",
    )
    instantiate_workflow_parser.add_argument(
        "--process-input",
        action="append",
        default=[],
        metavar="NAME=JSON",
        help="JSON value for a process task input-state field; may be repeated.",
    )
    instantiate_workflow_parser.add_argument(
        "--execution-plan",
        type=Path,
        help="Typed execution plan to compile instead of using a template.",
    )
    instantiate_workflow_parser.add_argument(
        "--delivery-profile",
        type=Path,
        help="Delivery profile used with --execution-plan.",
    )
    instantiate_workflow_parser.add_argument(
        "--action-contract",
        type=Path,
        help="Phase-to-actions JSON/YAML used with --execution-plan.",
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
    instantiate_workflow_parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Reset a stale dedicated integration branch to the generated source "
            "commit and push it with lease protection."
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
        default=128,
        help=("Maximum LLM action roundtrips (default: 128)."),
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

    exceptions_parser = subparsers.add_parser(
        "execution-exceptions",
        aliases=["execution_exceptions"],
        help="Inspect or decide pending capability exception requests.",
    )
    exceptions_parser.add_argument("--workflow-dir", type=Path, required=True)
    exceptions_parser.add_argument("--exception-id")
    exceptions_parser.add_argument(
        "--decision", choices=["approve", "deny"], help="Decision to record."
    )
    exceptions_parser.add_argument("--decided-by", default="human")
    exceptions_parser.add_argument("--json", action="store_true")
    exceptions_parser.set_defaults(func=_run_execution_exceptions)

    checkpoints_parser = subparsers.add_parser(
        "execution-checkpoints",
        aliases=["execution_checkpoints"],
        help="Inspect or restore typed execution checkpoints.",
    )
    checkpoints_parser.add_argument("--workflow-dir", type=Path, required=True)
    checkpoints_parser.add_argument("--execution-id", required=True)
    checkpoints_parser.add_argument("--checkpoint-id")
    checkpoints_parser.add_argument("--repo-root", type=Path)
    checkpoints_parser.add_argument(
        "--revert", action="store_true", help="Restore the selected checkpoint."
    )
    checkpoints_parser.set_defaults(func=_run_execution_checkpoints)

    compatibility_parser = subparsers.add_parser(
        "execution-compatibility",
        aliases=["execution_compatibility"],
        help="Inspect persisted execution JSON for migration compatibility.",
    )
    compatibility_parser.add_argument("--workflow-dir", type=Path, required=True)
    compatibility_parser.set_defaults(func=_run_execution_compatibility)

    acceptance_parser = subparsers.add_parser(
        "final-acceptance",
        aliases=["final_acceptance"],
        help="Run the deterministic typed execution acceptance scenario.",
    )
    acceptance_parser.add_argument("--repo-root", type=Path)
    acceptance_parser.add_argument("--workflow-dir", type=Path, required=True)
    acceptance_parser.set_defaults(func=_run_final_acceptance)

    audit_parser = subparsers.add_parser(
        "audit-capabilities",
        aliases=["audit_capabilities"],
        help="Audit the runtime capability registry for complete manifests.",
    )
    audit_parser.set_defaults(func=_run_audit_capabilities)

    intent_audit_parser = subparsers.add_parser(
        "audit-intent-lineage",
        aliases=["audit_intent_lineage"],
        help="Replay a fingerprinted history manifest and report intent drift.",
    )
    intent_audit_parser.add_argument("--input", type=Path, required=True)
    intent_audit_parser.add_argument("--output", type=Path)
    intent_audit_parser.set_defaults(func=_run_audit_intent_lineage)

    remember_intent_parser = subparsers.add_parser(
        "remember-intent",
        aliases=["remember_intent"],
        help="Persist one explicit, scoped user instruction for future executions.",
    )
    remember_intent_parser.add_argument("--workflow-dir", type=Path, required=True)
    remember_intent_parser.add_argument("--intent-id", required=True)
    remember_intent_parser.add_argument("--clause-id", required=True)
    remember_intent_parser.add_argument("--text", required=True)
    remember_intent_parser.add_argument("--source-ref", required=True)
    remember_intent_parser.add_argument("--supplied-by", default="user")
    remember_intent_parser.add_argument(
        "--kind", choices=[kind.value for kind in IntentKind], required=True
    )
    remember_intent_parser.add_argument(
        "--selector", action="append", default=[], metavar="KEY=VALUE"
    )
    remember_intent_parser.add_argument(
        "--trigger",
        choices=[item.value for item in IntentTrigger],
        default="before_action",
    )
    remember_intent_parser.add_argument("--trigger-action")
    remember_intent_parser.add_argument(
        "--require", dest="requirements", action="append", default=[]
    )
    remember_intent_parser.add_argument("--completion-gate")
    remember_intent_parser.add_argument("--precedence", type=int, default=0)
    remember_intent_parser.set_defaults(func=_run_remember_intent)

    list_intent_parser = subparsers.add_parser(
        "list-intent", aliases=["list_intent"], help="List remembered intent clauses."
    )
    list_intent_parser.add_argument("--workflow-dir", type=Path, required=True)
    list_intent_parser.add_argument("--include-inactive", action="store_true")
    list_intent_parser.set_defaults(func=_run_list_intent)

    explain_intent_parser = subparsers.add_parser(
        "explain-effective-contract",
        aliases=["explain_effective_contract"],
        help="Explain exactly which remembered intent applies to a context.",
    )
    explain_intent_parser.add_argument("--workflow-dir", type=Path, required=True)
    explain_intent_parser.add_argument(
        "--context", action="append", default=[], metavar="KEY=VALUE"
    )
    explain_intent_parser.set_defaults(func=_run_explain_effective_contract)

    revoke_intent_parser = subparsers.add_parser(
        "revoke-intent",
        aliases=["revoke_intent"],
        help="Revoke a remembered intent clause.",
    )
    revoke_intent_parser.add_argument("--workflow-dir", type=Path, required=True)
    revoke_intent_parser.add_argument("--clause-id", required=True)
    revoke_intent_parser.add_argument("--expected-version", type=int, required=True)
    revoke_intent_parser.set_defaults(func=_run_revoke_intent)

    supersede_intent_parser = subparsers.add_parser(
        "supersede-intent",
        aliases=["supersede_intent"],
        help="Install an explicit replacement for a remembered intent clause.",
    )
    supersede_intent_parser.add_argument("--workflow-dir", type=Path, required=True)
    supersede_intent_parser.add_argument("--clause-id", required=True)
    supersede_intent_parser.add_argument("--replacement-clause-id", required=True)
    supersede_intent_parser.add_argument("--expected-version", type=int, required=True)
    supersede_intent_parser.add_argument(
        "--kind", choices=[kind.value for kind in IntentKind]
    )
    supersede_intent_parser.add_argument(
        "--selector", action="append", default=[], metavar="KEY=VALUE"
    )
    supersede_intent_parser.add_argument(
        "--trigger", choices=[item.value for item in IntentTrigger]
    )
    supersede_intent_parser.add_argument("--trigger-action")
    supersede_intent_parser.add_argument(
        "--require", dest="requirements", action="append"
    )
    supersede_intent_parser.add_argument("--completion-gate")
    supersede_intent_parser.add_argument("--precedence", type=int)
    supersede_intent_parser.set_defaults(func=_run_supersede_intent)

    compile_workflow_parser = subparsers.add_parser(
        "compile-execution-plan",
        aliases=["compile_execution_plan"],
        help=(
            "Compile a typed execution plan and delivery profile into a durable "
            "workflow task graph."
        ),
    )
    compile_workflow_parser.add_argument(
        "--plan", type=Path, required=True, help="Execution plan JSON file."
    )
    compile_workflow_parser.add_argument(
        "--profile", type=Path, required=True, help="Delivery profile YAML file."
    )
    compile_workflow_parser.add_argument(
        "--actions",
        type=Path,
        required=True,
        help="JSON or YAML mapping of phase names to allowed action names.",
    )
    compile_workflow_parser.add_argument(
        "--workflow-dir",
        type=Path,
        required=True,
        help="Directory in which to persist the compiled workflow and runtime state.",
    )
    compile_workflow_parser.add_argument(
        "--execution-id",
        help="Runtime execution id; defaults to the execution plan id.",
    )
    compile_workflow_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root used by the execution runtime.",
    )
    compile_workflow_parser.set_defaults(func=_run_compile_execution_plan)

    request_parser = subparsers.add_parser(
        "compile-implementation-request",
        aliases=["compile_implementation_request"],
        help="Compile one execution-plan unit into a coding-agent request artifact.",
    )
    request_parser.add_argument("--plan", type=Path, required=True)
    request_parser.add_argument("--unit-id", required=True)
    request_parser.add_argument("--request-id", required=True)
    request_parser.add_argument("--base-commit", required=True)
    request_parser.add_argument("--output", type=Path, required=True)
    request_parser.add_argument(
        "--context-ref", action="append", default=[], metavar="REFERENCE"
    )
    request_parser.add_argument(
        "--allowed-command", action="append", default=[], metavar="PREFIX"
    )
    request_parser.set_defaults(func=_run_compile_implementation_request)

    coding_agent_parser = subparsers.add_parser(
        "run-coding-agent",
        aliases=["run_coding_agent"],
        help="Run one bounded external coding-agent implementation attempt.",
    )
    coding_agent_parser.add_argument(
        "--request",
        type=Path,
        required=True,
        help="Implementation request JSON produced from a validated execution unit.",
    )
    coding_agent_parser.add_argument(
        "--worktree",
        type=Path,
        required=True,
        help="Clean dedicated worktree in which the worker may edit.",
    )
    coding_agent_parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory for persisted request and attempt artifacts.",
    )
    coding_agent_parser.add_argument("--attempt-id", required=True)
    coding_agent_parser.add_argument(
        "--validation-profile",
        action="append",
        default=[],
        metavar="NAME=COMMAND",
        help="Registered validation profile, repeatable; COMMAND is parsed as argv.",
    )
    coding_agent_parser.add_argument(
        "--opencode-executable",
        default="opencode",
        help="OpenCode executable name or path.",
    )
    coding_agent_parser.add_argument(
        "--opencode-model",
        help=(
            "OpenCode model reference in provider/model form, for example "
            "deepinfra/deepseek-flash."
        ),
    )
    coding_agent_parser.add_argument(
        "--code-agent", choices=("opencode", "minisweagent"), default="minisweagent"
    )
    coding_agent_parser.add_argument("--minisweagent-executable", default="mini")
    coding_agent_parser.add_argument(
        "--minisweagent-model",
        default="deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731",
    )
    coding_agent_parser.add_argument("--code-agent-prompt-prefix", default="")
    coding_agent_parser.add_argument("--code-agent-prompt-suffix", default="")
    coding_agent_parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=300.0,
        help="Maximum seconds without Procedrr or code-agent activity (default: 300).",
    )
    coding_agent_parser.add_argument(
        "--validation-timeout-seconds", type=float, default=600.0
    )
    coding_agent_parser.set_defaults(func=_run_coding_agent)

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
        "--integration-branch",
        help="Specific workflow attempt branch to inspect or clean up.",
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
            "reset committed workflow files to their initial generated state, "
            "and preserve the integration branch."
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
        raise PowdrrExecutionError(
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


def _run_plan(args: argparse.Namespace) -> int:
    return _run_plan_session(args)


def _run_plan_resume(args: argparse.Namespace) -> int:
    return _run_plan_session(args, resume=True)


def _run_plan_repair(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root).resolve()
    plan_path = _repository_file_path(args.plan, repo_root, "Reviewed plan")
    request_path = args.request.expanduser()
    if not request_path.is_absolute():
        request_path = repo_root / request_path
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        if not isinstance(request, Mapping):
            raise ValueError("repair request root must be an object")
        plan_relative = plan_path.relative_to(repo_root).as_posix()
        operation_request = _collect_plan_repair_operation_request(
            repo_root=repo_root,
            plan_path=plan_path,
            plan_relative=plan_relative,
            repair_request=request,
        )
        update = JsonServiceBridge(LocalPowdrrService()).start(operation_request)
        if update.status != "succeeded":
            message = (
                update.failure.message
                if update.failure is not None
                else "Service did not prepare the plan repair."
            )
            raise ValueError(message)
        prepared_artifact = next(
            (
                artifact
                for artifact in update.output_artifacts
                if artifact.artifact_id == "plan-repair/prepared-publication.json"
            ),
            None,
        )
        if prepared_artifact is None:
            raise ValueError("Service response omitted prepared plan repair data.")
        publication = json.loads(prepared_artifact.data)
        if not isinstance(publication, Mapping):
            raise ValueError("Service returned malformed plan repair data.")
        pull_request_url, changed_paths = publish_repair_pull_request(
            repo_root=repo_root, publication=publication
        )
    except (
        OSError,
        json.JSONDecodeError,
        ValueError,
        RepairPublicationError,
    ) as error:
        raise PowdrrExecutionError(f"plan repair failed: {error}") from error
    result = {
        "plan_id": publication["plan_id"],
        "repair_id": publication["repair_id"],
        "pull_request_url": pull_request_url,
        "changed_paths": list(changed_paths),
        "affected_item_ids": publication["affected_item_ids"],
    }
    if args.json:
        sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    else:
        print(f"Plan repair PR: {pull_request_url}")
        print("Changed artifacts: " + ", ".join(changed_paths))
    return 0


def _collect_plan_repair_operation_request(
    *,
    repo_root: Path,
    plan_path: Path,
    plan_relative: str,
    repair_request: Mapping[str, Any],
) -> OperationRequest:
    plan_document = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    if not isinstance(plan_document, Mapping):
        raise ValueError("Reviewed plan must contain a YAML object.")
    planning = plan_document.get("planning")
    structrr = planning.get("structrr") if isinstance(planning, Mapping) else None
    proposal_path = (
        structrr.get("proposal_path") if isinstance(structrr, Mapping) else None
    )
    items = plan_document.get("items")
    if not isinstance(proposal_path, str) or not isinstance(items, list):
        raise ValueError("Reviewed plan is missing its proposal or item catalog.")
    relative_paths = [plan_relative, proposal_path]
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(
            item.get("workflow_file"), str
        ):
            raise ValueError("Reviewed plan contains an invalid workflow reference.")
        relative_paths.append(item["workflow_file"])
    if len(set(relative_paths)) != len(relative_paths):
        relative_paths = list(dict.fromkeys(relative_paths))
    source_artifacts = tuple(
        Artifact(
            artifact_id=relative,
            media_type="application/vnd.powdrr.source-file",
            data=_repository_file_path(
                Path(relative), repo_root, "Plan artifact"
            ).read_bytes(),
        )
        for relative in relative_paths
    )
    request_artifact_id = "plan-repair/request.json"
    request_artifact = Artifact(
        artifact_id=request_artifact_id,
        media_type="application/vnd.powdrr.plan-repair-request+json",
        data=json.dumps(repair_request, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        ),
    )
    manifest_digest = artifact_manifest_digest(source_artifacts)
    head_sha = _git_head(repo_root)
    repository = RepositorySnapshot(
        repository_id=_discover_github_repository(repo_root),
        branch=_current_branch(repo_root),
        base_sha=None,
        head_sha=head_sha,
        manifest_digest=manifest_digest,
        worktree_digest=manifest_digest,
        source_artifact_refs=tuple(item.artifact_id for item in source_artifacts),
    )
    return OperationRequest(
        request_id=f"plan-repair-{uuid.uuid4().hex}",
        operation_kind="replan",
        inputs={
            "plan_path": plan_relative,
            "base_commit": head_sha,
            "repair_request_artifact": request_artifact_id,
        },
        repository=repository,
        artifacts=(*source_artifacts, request_artifact),
        capabilities=({"name": "repository.pinned_file_snapshot", "version": "1"},),
    )


def _planning_session_operation(
    issue_store: GitHubPlanningIssueStore,
    *,
    action: str,
    repository_id: str,
    **parameters: Any,
) -> PlanningSession:
    request = OperationRequest(
        request_id=f"planning-session-{uuid.uuid4().hex}",
        operation_kind="plan",
        inputs={
            "action": action,
            "repository_id": repository_id,
            **parameters,
        },
    )
    update = JsonServiceBridge(
        LocalPowdrrService(planning_issue_store=issue_store)
    ).start(request)
    if update.status != "succeeded":
        message = (
            update.failure.message
            if update.failure is not None
            else "Service did not return a planning session."
        )
        raise PlanningServiceError(message)
    session_artifact = next(
        (
            artifact
            for artifact in update.output_artifacts
            if artifact.artifact_id == "planning/session.json"
            and artifact.media_type == "application/vnd.powdrr.planning-session+json"
        ),
        None,
    )
    if session_artifact is None:
        raise PlanningServiceError("Service response omitted planning session data.")
    try:
        session_data = json.loads(session_artifact.data)
    except json.JSONDecodeError as error:
        raise PlanningServiceError(
            "Service returned malformed planning session data."
        ) from error
    return PlanningSession.from_data(session_data)


def _run_plan_session(args: argparse.Namespace, *, resume: bool = False) -> int:
    repo_root = resolve_repo_root(args.repo_root).resolve()
    github = GitHubIssueClient(args.repository) if resume else None
    session: PlanningSession | None = None
    store: LocalCheckpointStore | None = None
    issue_checkpoint_store: GitHubCommentCheckpointStore | None = None
    try:
        if resume:
            assert github is not None
            session = _planning_session_operation(
                GitHubPlanningIssueStore(github),
                action="load_session",
                repository_id=args.repository,
                issue_number=args.session_issue,
            )
            plan_id = session.plan_id
            source_relative = session.source_path
            verification_command = session.verification_command
        else:
            source_path = _repository_file_path(args.source, repo_root, "Source plan")
            if not source_path.is_file():
                raise ValueError(f"Source plan does not exist: {source_path}")
            source_relative = source_path.relative_to(repo_root).as_posix()
            plan_id = args.plan_id
            verification_command = args.verification_command
            repository_name = _discover_github_repository(repo_root)
            github = GitHubIssueClient(repository_name)

        output_path = (
            _repository_file_path(args.output, repo_root, "Output plan")
            if not resume and args.output is not None
            else repo_root
            / "docs"
            / "plans"
            / f"{slugify_workflow_id(plan_id)}-execution-plan.yaml"
        )
        if output_path == repo_root / source_relative:
            raise ValueError("Output plan must be different from the source plan")
        if output_path.exists():
            raise FileExistsError(
                f"Output plan already exists: {output_path}; choose another path "
                "so the reviewed plan is not overwritten."
            )

        base_commit = (
            session.base_commit if session is not None else _git_head(repo_root)
        )
        _require_clean_planning_checkout(repo_root, base_commit)
        assert github is not None
        pinned_source = read_pinned_source(
            str(repo_root),
            base_commit,
            source_relative,
            repository_id=github.repository,
        )
        if session is None:
            session = _planning_session_operation(
                GitHubPlanningIssueStore(github),
                action="create_session",
                repository_id=github.repository,
                plan_id=plan_id,
                base_commit=base_commit,
                source_path=source_relative,
                source_blob_sha=pinned_source.blob_sha,
                verification_command=verification_command,
            )
        elif pinned_source.blob_sha != session.source_blob_sha:
            raise PlanningServiceError(
                "pinned source blob differs from the planning issue metadata"
            )

        structrr_baseline = _bootstrap_planning_structrr_baseline(
            repo_root, session.session_id
        )

        planning_provider = resolve_workflow_provider(args.planning_provider)
        planning_mapping = configured_model_mapping(
            planning_provider, args.planning_model
        )
        credentials = resolve_provider_credentials(
            planning_mapping.provider,
            args.planning_api_key,
            args.planning_base_url,
        )
        planning_client = build_workflow_client(
            credentials,
            model=planning_mapping.model,
            model_cache_dir=repo_root / ".powdrr" / "models",
            progress_stream=sys.stderr,
        )
        set_output_limit = getattr(
            planning_client, "set_structured_output_token_limit", None
        )
        if callable(set_output_limit):
            set_output_limit(8192)

        source = segment_markdown_source(pinned_source)
        taxonomy = load_entity_taxonomy(repo_root, _structrr_taxonomy_path(repo_root))
        machine = SourceLedgerQuestionMachine(
            source,
            evidence_search=lambda requirements: search_requirements_evidence(
                repo_root,
                base_commit,
                requirements,
            ),
            evidence_excerpt_reader=lambda evidence: read_evidence_excerpt(
                repo_root, evidence
            ),
            verification_command=verification_command,
            repository_paths=_planning_repository_paths(
                repo_root, base_commit, pinned_source.text
            ),
            plan_id=plan_id,
            structrr_baseline=structrr_baseline,
            taxonomy_entity_types=taxonomy.entity_types,
        )
        assert github is not None
        store = LocalCheckpointStore(repo_root, session_id=session.session_id)
        issue_checkpoint_store = GitHubCommentCheckpointStore(
            github, session.issue.number, session_id=session.session_id
        )
        if resume and not store.path.exists():
            store.import_legacy_comments(
                github.list_checkpoint_comments(
                    session.issue.number, session_id=session.session_id
                )
            )
        journal = store.load()
        if journal.events:
            issue_checkpoint_store.publish_archive(journal.events)
        process_directory = (
            Path(__file__).resolve().parents[2] / "docs" / "procedrr" / "processes"
        )
        result = run_planning_procedrr(
            process_directory=process_directory,
            session=session,
            state_machine=machine,
            checkpoint_store=store,
            client=planning_client,
            options=PlanningOptions(
                model=planning_mapping.model,
                max_concurrent_questions=(
                    1
                    if planning_mapping.provider == "local"
                    else args.planning_concurrency
                ),
                use_provider_schema=planning_mapping.provider != "deepinfra-cheap",
            ),
            stderr=sys.stderr,
        )
        if result.terminal_status.value != "succeeded":
            journal = store.load()
            if journal.events:
                issue_checkpoint_store.publish_archive(journal.events)
            raise PlanningServiceError(
                f"Procedrr planning process ended {result.terminal_status.value}"
            )
        journal = store.load()
        if journal.events:
            issue_checkpoint_store.publish_archive(journal.events)
        accepted_answers = _accepted_planning_answers(journal.events)
        relative_output = output_path.relative_to(repo_root).as_posix()
        plan_slug = slugify_workflow_id(plan_id)
        proposal_relative = (
            Path("docs") / "plans" / f"{plan_slug}-structrr-proposal.json"
        ).as_posix()
        compiled = machine.compile_accepted_plan(
            accepted_answers,
            plan_id=plan_id,
            plan_path=relative_output,
            proposal_path=proposal_relative,
        )
        generated_files = _write_compiled_plan_files(repo_root, compiled.files)
        for generated_file in generated_files:
            _stage_generated_file(repo_root, generated_file)
        pull_request = publish_execution_plan_pull_request(
            repo_root=repo_root,
            plan=compiled.plan,
            generated_files=generated_files,
            source_plan_path=source_relative,
        )
        try:
            github.add_issue_comment(
                session.issue.number,
                "Planning completed and the review PR is open: "
                f"{pull_request.pull_request_url}",
            )
        except GitHubIssueQueueError as update_error:
            print(
                f"Could not write the completed planning status to GitHub: "
                f"{update_error}",
                file=sys.stderr,
            )
    except Exception as error:
        if store is not None and issue_checkpoint_store is not None:
            try:
                journal = store.load()
                if journal.events:
                    issue_checkpoint_store.publish_archive(journal.events)
            except Exception as archive_error:
                print(
                    f"Could not publish the planning checkpoint archive: "
                    f"{archive_error}",
                    file=sys.stderr,
                )
        if session is not None and github is not None:
            try:
                github.add_issue_comment(
                    session.issue.number,
                    "Planning stopped after a terminal error. "
                    f"Resume with `powdrr plan-resume --session-issue "
                    f"{session.issue.number}`. Error: {error}",
                )
            except GitHubIssueQueueError as update_error:
                print(
                    f"Could not write the terminal planning status to GitHub: "
                    f"{update_error}",
                    file=sys.stderr,
                )
        print(f"Could not create execution plan: {error}", file=sys.stderr)
        return 2

    if args.json:
        print(
            json.dumps(
                {
                    "plan_id": plan_id,
                    "session_issue_url": session.issue.url,
                    "path": relative_output,
                    "items": [item.item_id for item in compiled.plan.items],
                    "generated_files": [
                        path.relative_to(repo_root).as_posix()
                        for path in generated_files
                    ],
                    "workflow_count": len(compiled.plan.items),
                    "branch": pull_request.branch_name,
                    "base_branch": pull_request.base_branch,
                    "commit": pull_request.commit_sha,
                    "pull_request_url": pull_request.pull_request_url,
                    "status": "pull_request_opened",
                    "published_issues": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"Wrote reviewable execution plan to {relative_output}")
        print(f"Planning session: {session.issue.url}")
        print(f"Proposed items: {len(compiled.plan.items)}")
        print(f"Generated PR workflows: {len(compiled.plan.items)}")
        print(f"Opened plan review PR: {pull_request.pull_request_url}")
        print("No work-item issues were published and no work was executed.")
    return 0


def _accepted_planning_answers(events: Sequence[Any]) -> dict[str, dict[str, Any]]:
    """Replay accepted answers, removing any later-invalidated response."""
    accepted: dict[str, dict[str, Any]] = {}
    for event in events:
        question = event.payload.get("question")
        if not isinstance(question, Mapping):
            continue
        question_id = question.get("question_id")
        if not isinstance(question_id, str):
            continue
        if event.event_type == "answer_accepted":
            answer = event.payload.get("accepted_answer")
            if isinstance(answer, Mapping):
                accepted[question_id] = dict(answer)
        elif event.event_type == "needs_answer":
            accepted.pop(question_id, None)
    return accepted


def _bootstrap_planning_structrr_baseline(
    repo_root: Path, session_id: str
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="powdrr-lift-plan-structrr-") as temp_dir:
        bootstrap = bootstrap_structrr(
            repo_root,
            output_path=Path(temp_dir) / f"{session_id}.yaml",
            taxonomy_path=_structrr_taxonomy_path(repo_root),
        )
        if not bootstrap.validation.successful:
            details = "; ".join(
                f"{issue.code}: {issue.message}"
                for issue in bootstrap.validation.issues
            )
            raise PlanGenerationError(
                f"Structrr bootstrap failed before planning: {details}"
            )
        return bootstrap.document


def _discover_github_repository(repo_root: Path) -> str:
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GitHubIssueQueueError(
            "could not determine the current GitHub repository: "
            + result.stderr.strip()
        )
    value = json.loads(result.stdout)
    repository = value.get("nameWithOwner") if isinstance(value, Mapping) else None
    if not isinstance(repository, str) or not repository:
        raise GitHubIssueQueueError("gh repo view did not return nameWithOwner")
    return repository


def _git_head(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("planning requires a committed base revision")
    return result.stdout.strip()


def _require_clean_planning_checkout(repo_root: Path, base_commit: str) -> None:
    if _git_head(repo_root) != base_commit:
        raise PlanningServiceError(
            "resume requires a checkout at the session's pinned base commit"
        )
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    if result.stdout:
        raise PlanningServiceError(
            "planning requires a clean checkout at the pinned base commit"
        )


def _planning_repository_paths(
    repo_root: Path, base_commit: str, source_text: str
) -> Mapping[str, PathFact]:
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", base_commit],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    tracked = set(listing)
    cited_paths = set(
        re.findall(
            r"(?<![A-Za-z0-9_./-])(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.[A-Za-z0-9]+",
            source_text,
        )
    )
    return {
        path: PathFact(path, PATH_RUNTIME_SUFFIXES.get(Path(path).suffix.lower()))
        for path in sorted(tracked & cited_paths)
    }


def _write_compiled_plan_files(
    repo_root: Path, files: Mapping[str, str]
) -> tuple[Path, ...]:
    destinations = tuple((repo_root / relative).resolve() for relative in files)
    for destination in destinations:
        try:
            destination.relative_to(repo_root.resolve())
        except ValueError as error:
            raise PlanningCompilationError(
                "compiled artifact destination escaped the repository"
            ) from error
        if destination.exists():
            raise FileExistsError(
                "compiled artifact already exists: "
                f"{destination.relative_to(repo_root)}"
            )
    created: list[Path] = []
    try:
        for (_relative, content), destination in zip(
            files.items(), destinations, strict=True
        ):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
            created.append(destination)
    except OSError:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return destinations


def _repository_file_path(path: Path, repo_root: Path, label: str) -> Path:
    candidate = path if path.is_absolute() else repo_root / path
    resolved = candidate.resolve()
    try:
        resolved.relative_to(repo_root)
    except ValueError as error:
        raise ValueError(f"{label} path must be inside the repository") from error
    return resolved


def _run_publish_plan_issues(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    plan_path = args.plan
    if plan_path.is_absolute():
        try:
            plan_path = plan_path.resolve().relative_to(repo_root.resolve())
        except ValueError:
            print("Plan path must be inside the repository checkout.", file=sys.stderr)
            return 2
    normalized_plan_path = plan_path.as_posix()
    try:
        plan = load_reviewed_plan_at_commit(
            normalized_plan_path, args.plan_commit, repo_root=repo_root
        )
        report = publish_plan_issues(
            plan=plan,
            plan_path=normalized_plan_path,
            plan_commit=args.plan_commit,
            client=GitHubIssueClient(args.repository),
        )
    except GitHubIssueQueueError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "plan_id": report.plan_id,
                "plan_commit": report.plan_commit,
                "created_issue_numbers": report.created_issue_numbers,
                "existing_issue_numbers": report.existing_issue_numbers,
            },
            indent=2,
        )
    )
    return 0


def _run_list_plan_issues(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    plan_path = args.plan
    if plan_path.is_absolute():
        try:
            plan_path = plan_path.resolve().relative_to(repo_root.resolve())
        except ValueError:
            print("Plan path must be inside the repository checkout.", file=sys.stderr)
            return 2
    normalized_plan_path = plan_path.as_posix()
    try:
        plan = load_reviewed_plan_at_commit(
            normalized_plan_path, args.plan_commit, repo_root=repo_root
        )
        matches = find_plan_issues(
            plan=plan,
            plan_path=normalized_plan_path,
            plan_commit=args.plan_commit,
            client=GitHubIssueClient(args.repository),
        )
    except GitHubIssueQueueError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "plan_id": plan.plan_id,
                "plan_commit": args.plan_commit,
                "issues": [
                    {
                        "item_id": match.item_id,
                        "issue_number": match.issue_number,
                        "url": match.url,
                        "state": match.state,
                        "labels": match.labels,
                    }
                    for match in matches
                ],
                "missing_item_ids": [
                    match.item_id for match in matches if match.issue_number is None
                ],
            },
            indent=2,
        )
    )
    return 0


def _run_fulfill_plan_issue_without_pr(args: argparse.Namespace) -> int:
    try:
        issue = fulfill_issue_without_pr(
            issue_number=args.issue_number,
            reason=args.reason,
            client=GitHubIssueClient(args.repository),
        )
    except GitHubIssueQueueError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "issue_number": issue.number,
                "state": issue.state,
                "fulfillment": "without_implementation_pr",
                "label": "powdrr:fulfilled-without-pr",
            },
            indent=2,
        )
    )
    return 0


def _run_github_issue_worker(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)

    def run_task(
        workflow_path: Path,
        workflow_id: str,
        worker_id: str,
        item: Any,
        plan: ReviewedExecutionPlan,
        fulfilled_without_pr_dependencies: tuple[str, ...],
        checkpoint_callback: TaskCheckpointCallback,
    ) -> int:
        if workflow_path.is_dir():
            workflow_dir = workflow_path
        else:
            instance = load_plan_workflow_instance(workflow_path)
            if instance.workflow_id != workflow_id:
                raise PlanGenerationError(
                    "workflow definition ID does not match its plan item"
                )
            workflow_inputs = dict(instance.inputs)
            workflow_context = dict(workflow_inputs["proposed_pr_context"])
            workflow_context.update(
                {
                    "title": item.title,
                    "objective": item.objective,
                    "scope": list(item.scope),
                    "acceptance_criteria": list(item.acceptance_criteria),
                    "allowed_paths": list(item.allowed_paths),
                }
            )
            workflow_inputs["feature_description"] = item.objective
            if item.implementation_steps:
                workflow_context["implementation_steps"] = list(
                    item.implementation_steps
                )
            if item.inline_adjustments:
                workflow_context["inline_plan_adjustments"] = [
                    dict(adjustment) for adjustment in item.inline_adjustments
                ]
            workflow_inputs["proposed_pr_context"] = workflow_context
            execution_id = uuid.uuid4().hex[:12]
            workflow_instance_id = _plan_workflow_instance_id(
                workflow_id, workflow_inputs, worker_id, attempt_id=execution_id
            )
            integration_branch = integration_branch_name(
                f"{workflow_id}-attempt-{execution_id}"
            )
            integration_worktree, integration_branch = create_workflow_worktree(
                repo_root,
                workflow_id,
                base_branch="main",
                branch_name=integration_branch,
                worktree_name=integration_branch,
            )
            runtime_relative_directory = (
                Path("docs")
                / "workflows"
                / slugify_workflow_id(plan.plan_id)
                / "runtime"
                / slugify_workflow_id(workflow_instance_id)
            )
            workflow_dir, _tasks = instantiate_procedrr_process_workflow(
                process_id=instance.process_id,
                process_directory=_bundled_procedrr_process_directory(),
                input_state=workflow_inputs,
                work_item_name=workflow_instance_id,
                workflow_instance_name=workflow_instance_id,
                output_root=(integration_worktree / runtime_relative_directory.parent),
            )
            save_workflow_git_state(
                workflow_dir,
                WorkflowGitState(
                    proposed_pr_id=workflow_id,
                    base_branch="main",
                    integration_branch=integration_branch,
                    workflow_relative_directory=(
                        workflow_dir.relative_to(integration_worktree).as_posix()
                    ),
                    publication_id=execution_id,
                    depends_on_workflows=tuple(
                        planned.workflow_id
                        for dependency in item.dependencies
                        for planned in plan.items
                        if planned.item_id == dependency
                    ),
                    fulfilled_without_pr_dependencies=(
                        fulfilled_without_pr_dependencies
                    ),
                ),
            )
            return run_workflow_task(
                WorkflowTaskAgentConfig(
                    workflow_dir=workflow_dir,
                    repo_root=repo_root,
                    provider=args.provider,
                    workflow_id=workflow_id,
                    api_key=args.api_key,
                    base_url=args.base_url,
                    max_roundtrips=args.max_roundtrips,
                    context_compaction_threshold=args.context_compaction_threshold,
                    verbose=args.verbose,
                    code_agent=getattr(args, "code_agent", "minisweagent"),
                    opencode_executable=getattr(
                        args, "opencode_executable", "opencode"
                    ),
                    opencode_model=getattr(args, "opencode_model", None),
                    minisweagent_executable=getattr(
                        args, "minisweagent_executable", "mini"
                    ),
                    minisweagent_model=getattr(
                        args,
                        "minisweagent_model",
                        "deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731",
                    ),
                    coding_agent_timeout_seconds=getattr(
                        args, "coding_agent_timeout_seconds", 1800.0
                    ),
                    coding_agent_validation_timeout_seconds=getattr(
                        args, "coding_agent_validation_timeout_seconds", 1800.0
                    ),
                    task_checkpoint_callback=checkpoint_callback,
                    github_issue_number=item.github_issue_number,
                    github_repository=args.repository,
                ),
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
        return run_workflow_task(
            WorkflowTaskAgentConfig(
                workflow_dir=workflow_dir,
                repo_root=repo_root,
                provider=args.provider,
                workflow_id=workflow_id,
                api_key=args.api_key,
                base_url=args.base_url,
                max_roundtrips=args.max_roundtrips,
                context_compaction_threshold=args.context_compaction_threshold,
                verbose=args.verbose,
                code_agent=getattr(args, "code_agent", "minisweagent"),
                opencode_executable=getattr(args, "opencode_executable", "opencode"),
                opencode_model=getattr(args, "opencode_model", None),
                minisweagent_executable=getattr(
                    args, "minisweagent_executable", "mini"
                ),
                minisweagent_model=getattr(
                    args,
                    "minisweagent_model",
                    "deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731",
                ),
                coding_agent_timeout_seconds=getattr(
                    args, "coding_agent_timeout_seconds", 1800.0
                ),
                coding_agent_validation_timeout_seconds=getattr(
                    args, "coding_agent_validation_timeout_seconds", 1800.0
                ),
                task_checkpoint_callback=checkpoint_callback,
                github_issue_number=item.github_issue_number,
                github_repository=args.repository,
            ),
            stdout=sys.stdout,
            stderr=sys.stderr,
        )

    try:
        report = run_worker_once(
            client=GitHubIssueClient(args.repository),
            repo_root=repo_root,
            worker_id=args.worker_id,
            run_task=run_task,
        )
    except GitHubIssueQueueError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "outcome": report.outcome,
                "issue_number": report.issue_number,
                "item_id": report.item_id,
                "detail": report.detail,
            },
            indent=2,
        )
    )
    return (
        0
        if report.outcome in {"no_eligible_work", "awaiting_review", "awaiting_human"}
        else 1
    )


def _run_github_issue_worker_continue(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    client = GitHubIssueClient(args.repository)

    def resume_workflow(
        checkpoint: Mapping[str, Any], checkpoint_callback: TaskCheckpointCallback
    ) -> int:
        workflow_id = str(checkpoint["workflow_id"])
        branch = str(checkpoint["integration_branch"])
        workflow_relative = Path(str(checkpoint["workflow_directory"]))
        if workflow_relative.is_absolute() or ".." in workflow_relative.parts:
            raise GitHubIssueQueueError(
                "manual prerequisite checkpoint contains an unsafe workflow path"
            )
        integration_worktree, _ = create_workflow_worktree(
            repo_root,
            workflow_id,
            base_branch="main",
            branch_name=branch,
            worktree_name=branch,
        )
        workflow_dir = integration_worktree / workflow_relative
        human_result = run_human_task(
            HumanTaskRunnerConfig(
                workflow_dir=workflow_dir,
                repo_root=integration_worktree,
                task_id=str(checkpoint["human_task_id"]),
                answer=(
                    "The operator confirmed that the manual prerequisite instructions "
                    "were completed. The staged pull request CI checks passed before "
                    "Powdrr resumed this workflow."
                ),
                task_checkpoint_callback=checkpoint_callback,
            ),
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        if human_result != 0:
            return human_result
        return run_workflow_task(
            WorkflowTaskAgentConfig(
                workflow_dir=workflow_dir,
                repo_root=integration_worktree,
                provider=args.provider,
                workflow_id=workflow_id,
                api_key=args.api_key,
                base_url=args.base_url,
                max_roundtrips=args.max_roundtrips,
                context_compaction_threshold=args.context_compaction_threshold,
                verbose=args.verbose,
                code_agent=args.code_agent,
                opencode_executable=args.opencode_executable,
                opencode_model=args.opencode_model,
                minisweagent_executable=args.minisweagent_executable,
                minisweagent_model=args.minisweagent_model,
                coding_agent_timeout_seconds=args.coding_agent_timeout_seconds,
                coding_agent_validation_timeout_seconds=(
                    args.coding_agent_validation_timeout_seconds
                ),
                task_checkpoint_callback=checkpoint_callback,
                github_issue_number=args.issue_number,
                github_repository=args.repository,
            ),
            stdout=sys.stdout,
            stderr=sys.stderr,
        )

    try:
        report = continue_manual_prerequisite_issue(
            client=client,
            issue_number=args.issue_number,
            repo_root=repo_root,
            resume_workflow=resume_workflow,
        )
    except GitHubIssueQueueError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "outcome": report.outcome,
                "issue_number": report.issue_number,
                "detail": report.detail,
            },
            indent=2,
        )
    )
    return 0 if report.outcome in {"awaiting_review", "awaiting_human"} else 1


def _plan_workflow_instance_id(
    workflow_id: str,
    input_state: Mapping[str, Any],
    worker_id: str,
    *,
    attempt_id: str | None = None,
) -> str:
    """Give every issue-worker attempt its own durable Procedrr run directory."""
    serialized = json.dumps(
        input_state,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    fingerprint = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:12]
    suffix = f"-{slugify_workflow_id(worker_id)}"
    if attempt_id:
        suffix += f"-attempt-{slugify_workflow_id(attempt_id)}"
    return f"{workflow_id}-task-{fingerprint}{suffix}"


def _bundled_procedrr_process_directory() -> Path:
    """Locate Powdrr-Lift's process catalog, independent of target repository."""
    return Path(__file__).resolve().parents[2] / "docs" / "procedrr" / "processes"


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


def _parse_process_inputs(values: list[str]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for value in values:
        name, separator, encoded = value.partition("=")
        if not separator or not name.strip():
            raise ValueError(
                "--process-input must use NAME=JSON with a non-empty NAME."
            )
        key = name.strip()
        if key in parsed:
            raise ValueError(f"--process-input repeated input {key!r}.")
        try:
            parsed[key] = json.loads(encoded)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"--process-input {key!r} must contain valid JSON: {error.msg}"
            ) from error
    return parsed


def _run_instantiate_workflow(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    compiled_inputs = (
        args.execution_plan,
        args.delivery_profile,
        args.action_contract,
    )
    if any(value is not None for value in compiled_inputs) and not all(
        value is not None for value in compiled_inputs
    ):
        print(
            "--execution-plan, --delivery-profile, and --action-contract "
            "must be supplied together",
            file=sys.stderr,
        )
        return 2
    if args.template is not None and args.process is not None:
        print("--template and --process cannot be used together", file=sys.stderr)
        return 2
    if args.process is not None and any(value is not None for value in compiled_inputs):
        print(
            "--process cannot be combined with compiled execution-plan options",
            file=sys.stderr,
        )
        return 2
    if (
        args.template is None
        and args.process is None
        and not all(value is not None for value in compiled_inputs)
    ):
        print(
            "--template, --process, or the complete compiled execution-plan "
            "options are required",
            file=sys.stderr,
        )
        return 2
    if args.process is None and args.process_input:
        print("--process-input requires --process", file=sys.stderr)
        return 2
    template_path = _resolve_cli_path(repo_root, args.template)
    output_root = args.output_root
    if not output_root.is_absolute():
        output_root = repo_root / output_root
    template_values = _parse_template_values(args.template_value)
    process_inputs = _parse_process_inputs(args.process_input)
    reused_existing = False
    try:
        project_root = resolve_git_repository_root(repo_root)
        process_task_context = process_inputs.get("task_context")
        process_task_state = (
            process_task_context.get("input_state", {})
            if isinstance(process_task_context, Mapping)
            else process_inputs
        )
        proposed_pr_id = template_values.get(
            "proposed-pr-id",
            (
                str(process_task_state.get("proposed_pr_id"))
                if isinstance(process_task_state, Mapping)
                and process_task_state.get("proposed_pr_id")
                else args.work_item_name
            ),
        )
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
        if all(value is not None for value in compiled_inputs):
            assert args.execution_plan is not None
            assert args.delivery_profile is not None
            assert args.action_contract is not None
            plan_path = _resolve_cli_path(repo_root, args.execution_plan)
            profile_path = _resolve_cli_path(repo_root, args.delivery_profile)
            action_path = _resolve_cli_path(repo_root, args.action_contract)
            assert plan_path is not None
            assert profile_path is not None
            assert action_path is not None
            output_directory = _workflow_output_directory(
                output_root, args.work_item_name
            )
            if output_directory.exists() and any(output_directory.iterdir()):
                raise FileExistsError(
                    f"Workflow output directory is not empty: {output_directory}."
                )
            plan = ExecutionPlan.from_data(_load_structured_mapping(plan_path))
            profile = load_delivery_profile(profile_path)
            actions_by_phase = _parse_action_contract(
                _load_structured_mapping(action_path)
            )
            runtime = ExecutionRuntime(
                plan.plan_id,
                profile_id=profile.profile_id,
                workflow_directory=output_directory,
                repo_root=repo_root,
                profile=profile,
            )
            tasks = runtime.compile_plan_to_workflow(
                profile,
                plan,
                actions_by_phase=actions_by_phase,
                workflow_directory=output_directory,
            ).tasks
            invariants: tuple[Any, ...] = ()
            relationships: tuple[Any, ...] = ()
        elif args.process is not None:
            instance_slug = re.sub(
                r"[^a-z0-9]+",
                "-",
                (args.workflow_instance_name or args.work_item_name).strip().lower(),
            ).strip("-")
            expected_directory = _workflow_output_directory(
                output_root, args.work_item_name
            )
            reused_existing = (
                expected_directory / f"{instance_slug}-task-001.yaml"
            ).is_file()
            output_directory, tasks = instantiate_procedrr_process_workflow(
                process_id=args.process,
                process_directory=repo_root / "docs" / "procedrr" / "processes",
                input_state=process_inputs,
                work_item_name=args.work_item_name,
                output_root=output_root,
                workflow_instance_name=args.workflow_instance_name,
            )
            invariants, relationships = (), ()
        else:
            assert template_path is not None
            output_directory = _workflow_output_directory(
                output_root, args.work_item_name
            )
            instance_slug = re.sub(
                r"[^a-z0-9]+",
                "-",
                (args.workflow_instance_name or args.work_item_name).strip().lower(),
            ).strip("-")
            task_count = len(load_workflow_template(template_path).task_templates)
            expected_tasks = tuple(
                output_directory / f"{instance_slug}-task-{index + 1:03d}.yaml"
                for index in range(task_count)
            )
            reused_existing = bool(expected_tasks) and all(
                path.is_file() for path in expected_tasks
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
        existing_state = load_workflow_git_state(
            output_directory,
            workflow_id=proposed_pr_id,
        )
        state = WorkflowGitState(
            proposed_pr_id=proposed_pr_id,
            base_branch="main",
            integration_branch=integration_branch,
            workflow_relative_directory=str(relative_workflow),
            publication_id=(
                existing_state.publication_id
                if existing_state is not None
                else new_publication_id()
            ),
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
        synchronize_workflow_initialization(
            integration_worktree, repo_root, force=args.force
        )
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        print(f"Could not instantiate workflow: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "workflow_id": proposed_pr_id,
                "workflow_directory": str(output_directory),
                "task_count": len(tasks),
                "first_task": str(output_directory / f"{tasks[0].task_id}.yaml"),
                "first_task_id": tasks[0].task_id,
                "integration_branch": integration_branch,
                "integration_worktree": (
                    str(integration_worktree) if integration_worktree else None
                ),
                "reused": reused_existing,
            },
            indent=2,
        )
    )
    return 0


def _resolve_cli_path(repo_root: Path, path: Path | None) -> Path | None:
    if path is None or path.is_absolute():
        return path
    return repo_root / path


def _workflow_output_directory(output_root: Path, work_item_name: str) -> Path:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", work_item_name.strip().lower()).strip("-")
    if not slug:
        raise ValueError("work-item-name must contain at least one letter or digit")
    return output_root / slug


def _parse_action_contract(
    actions_data: Mapping[str, Any],
) -> dict[PhaseType, tuple[str, ...]]:
    actions_by_phase: dict[PhaseType, tuple[str, ...]] = {}
    for raw_phase, raw_actions in actions_data.items():
        try:
            phase = PhaseType(str(raw_phase))
        except ValueError as error:
            raise PowdrrExecutionError(
                f"Unknown delivery phase in action contract: {raw_phase!r}",
                error_code="compiled_workflow_phase_unknown",
                remediation="Use only phase names declared by the delivery profile.",
            ) from error
        if not isinstance(raw_actions, list | tuple) or not all(
            isinstance(action, str) for action in raw_actions
        ):
            raise PowdrrExecutionError(
                f"Action contract for phase {phase.value!r} must be a list of strings.",
                error_code="compiled_workflow_action_contract_invalid",
                action_kind=phase.value,
                remediation="Provide a list containing each allowed action name.",
            )
        actions_by_phase[phase] = tuple(raw_actions)
    return actions_by_phase


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


def _run_execution_exceptions(args: argparse.Namespace) -> int:
    """Inspect or decide durable capability exception requests."""
    store = FileCapabilityExceptionStore(args.workflow_dir)
    if args.decision and not args.exception_id:
        print("--exception-id is required with --decision", file=sys.stderr)
        return 2
    if args.decision:
        request = store.load_request(args.exception_id)
        if request is None:
            print(f"Unknown pending exception: {args.exception_id}", file=sys.stderr)
            return 1
        secret = os.environ.get("POWDRR_CAPABILITY_SECRET")
        if not secret:
            print(
                "POWDRR_CAPABILITY_SECRET is required to decide exceptions",
                file=sys.stderr,
            )
            return 2
        runtime = ExecutionRuntime(
            request.execution_id,
            profile_id="exception-decision",
            workflow_directory=args.workflow_dir,
            repo_root=args.repo_root or Path.cwd(),
            exception_authority=CapabilityExceptionAuthority(secret.encode("utf-8")),
            exception_store=store,
        )
        decision = runtime.decide_capability_exception(
            request, approved=args.decision == "approve", decided_by=args.decided_by
        )
        payload: object = decision.to_data()
    elif args.exception_id:
        request = store.load_request(args.exception_id)
        if request is None:
            print(f"Unknown exception: {args.exception_id}", file=sys.stderr)
            return 1
        payload = request.decision_packet()
    else:
        payload = [request.decision_packet() for request in store.pending()]
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _run_execution_checkpoints(args: argparse.Namespace) -> int:
    """Inspect or restore checkpoints through the durable execution runtime."""
    store = ContentAddressedCheckpointStore(
        args.workflow_dir / "execution" / "checkpoints"
    )
    if args.revert and not args.checkpoint_id:
        print("--checkpoint-id is required with --revert", file=sys.stderr)
        return 2
    if args.checkpoint_id:
        checkpoint = store.load(args.checkpoint_id)
        if args.revert:
            repo_root = resolve_repo_root(args.repo_root or checkpoint.workspace_root)
            runtime = ExecutionRuntime(
                args.execution_id,
                profile_id="checkpoint-restore",
                workflow_directory=args.workflow_dir,
                repo_root=repo_root,
            )
            print(
                json.dumps(
                    runtime.restore_checkpoint(args.checkpoint_id).to_data(),
                    indent=2,
                )
            )
        else:
            print(json.dumps(checkpoint.to_data(), indent=2))
        return 0
    manifests = sorted(store.manifests.glob("*.json"))
    print(json.dumps([store.load(path.stem).to_data() for path in manifests], indent=2))
    return 0


def _run_execution_compatibility(args: argparse.Namespace) -> int:
    print(json.dumps(compatibility_report(args.workflow_dir), indent=2))
    return 0


def _run_final_acceptance(args: argparse.Namespace) -> int:
    report = run_final_acceptance(resolve_repo_root(args.repo_root), args.workflow_dir)
    print(json.dumps(report.to_data(), indent=2))
    return 0 if report.passed else 1


def _run_audit_capabilities(args: argparse.Namespace) -> int:
    checks = audit_capability_surface(builtin_tool_registry())
    report = {
        "passed": all(item.passed for item in checks),
        "checks": [item.to_data() for item in checks],
    }
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


def _run_audit_intent_lineage(args: argparse.Namespace) -> int:
    try:
        history = json.loads(args.input.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"Could not read intent lineage history: {error}", file=sys.stderr)
        return 2
    if not isinstance(history, Mapping):
        print("Intent lineage history must be a JSON object", file=sys.stderr)
        return 2
    report = audit_intent_lineage(history)
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["passed"] else 1


def _run_remember_intent(args: argparse.Namespace) -> int:
    selectors = _parse_intent_pairs(args.selector)
    source = make_intent_source(
        intent_id=args.intent_id,
        exact_text=args.text,
        source_ref=args.source_ref,
        supplied_by=args.supplied_by,
    )
    clause = IntentClause(
        args.clause_id,
        source.intent_id,
        (0, len(source.exact_text)),
        IntentKind(args.kind),
        IntentContract(
            selectors=selectors,
            trigger=IntentTrigger(args.trigger),
            trigger_action=args.trigger_action,
            requirements=tuple(args.requirements),
            completion_gate=args.completion_gate,
            precedence=args.precedence,
        ),
    )
    saved_source, clauses, created = IntentStore(args.workflow_dir).capture(
        source, (clause,)
    )
    print(
        json.dumps(
            {
                "created": created,
                "acknowledgment": (
                    f"Recorded {clauses[0].kind.value} intent {clauses[0].clause_id!r} "
                    f"for source {saved_source.intent_id!r}."
                ),
                "source": saved_source.to_data(),
                "clauses": [item.to_data() for item in clauses],
            },
            indent=2,
        )
    )
    return 0


def _run_list_intent(args: argparse.Namespace) -> int:
    store = IntentStore(args.workflow_dir)
    clauses = store.list(include_inactive=args.include_inactive)
    print(json.dumps([item.to_data() for item in clauses], indent=2))
    return 0


def _run_explain_effective_contract(args: argparse.Namespace) -> int:
    context = {
        key: values[-1] for key, values in _parse_intent_pairs(args.context).items()
    }
    contract = resolve_effective_contract(IntentStore(args.workflow_dir), context)
    print(json.dumps(contract.explain(), indent=2))
    return 0


def _run_revoke_intent(args: argparse.Namespace) -> int:
    revoked = IntentStore(args.workflow_dir).revoke(
        args.clause_id, expected_version=args.expected_version
    )
    print(json.dumps(revoked.to_data(), indent=2))
    return 0


def _run_supersede_intent(args: argparse.Namespace) -> int:
    store = IntentStore(args.workflow_dir)
    current = next(
        item
        for item in store.list(include_inactive=True)
        if item.clause_id == args.clause_id
    )
    replacement = IntentClause(
        args.replacement_clause_id,
        current.intent_id,
        current.source_span,
        IntentKind(args.kind) if args.kind else current.kind,
        IntentContract(
            selectors=_parse_intent_pairs(args.selector)
            if args.selector
            else current.contract.selectors,
            trigger=IntentTrigger(args.trigger)
            if args.trigger
            else current.contract.trigger,
            trigger_action=args.trigger_action or current.contract.trigger_action,
            requirements=tuple(args.requirements)
            if args.requirements is not None
            else current.contract.requirements,
            completion_gate=args.completion_gate or current.contract.completion_gate,
            precedence=(
                args.precedence
                if args.precedence is not None
                else current.contract.precedence
            ),
        ),
    )
    installed = store.update_clause(
        args.clause_id, replacement, expected_version=args.expected_version
    )
    print(json.dumps(installed.to_data(), indent=2))
    return 0


def _parse_intent_pairs(values: Sequence[str]) -> dict[str, tuple[str, ...]]:
    result: dict[str, list[str]] = {}
    for value in values:
        key, separator, raw_values = value.partition("=")
        if not separator or not key.strip() or not raw_values.strip():
            raise ValueError("intent selectors must use KEY=VALUE")
        result.setdefault(key.strip(), []).extend(
            item.strip() for item in raw_values.split(",") if item.strip()
        )
    return {key: tuple(items) for key, items in result.items()}


def _run_compile_execution_plan(args: argparse.Namespace) -> int:
    """Materialize the user-authored plan through the typed phase controller."""
    assert args.plan is not None
    assert args.profile is not None
    assert args.actions is not None
    plan_data = _load_structured_mapping(args.plan)
    plan = ExecutionPlan.from_data(plan_data)
    profile = load_delivery_profile(args.profile)
    actions_data = _load_structured_mapping(args.actions)
    actions_by_phase = _parse_action_contract(actions_data)
    repo_root = resolve_repo_root(args.repo_root)
    runtime = ExecutionRuntime(
        args.execution_id or plan.plan_id,
        profile_id=profile.profile_id,
        workflow_directory=args.workflow_dir,
        repo_root=repo_root,
        profile=profile,
    )
    workflow = runtime.compile_plan_to_workflow(
        profile,
        plan,
        actions_by_phase=actions_by_phase,
        workflow_directory=args.workflow_dir,
    )
    print(
        json.dumps(
            {
                "execution_id": runtime.execution_id,
                "plan_id": plan.plan_id,
                "profile_id": profile.profile_id,
                "workflow_directory": str(Path(args.workflow_dir).resolve()),
                "task_ids": [task.task_id for task in workflow.tasks],
                "task_count": len(workflow.tasks),
            },
            indent=2,
        )
    )
    return 0


def _run_coding_agent(args: argparse.Namespace) -> int:
    request_data = _load_structured_mapping(args.request)
    request = ImplementationRequest.from_data(request_data)
    try:
        provider = build_coding_agent_provider(
            args.code_agent,
            opencode_executable=args.opencode_executable,
            opencode_model=args.opencode_model,
            minisweagent_executable=args.minisweagent_executable,
            minisweagent_model=args.minisweagent_model,
            timeout_seconds=args.timeout_seconds,
            diagnostics_root=args.output_dir / args.code_agent,
            prompt_prefix=args.code_agent_prompt_prefix,
            prompt_suffix=args.code_agent_prompt_suffix,
            permission_policy=OpenCodePermissionPolicy(request.allowed_commands),
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    runner = CodingAgentRunner(
        provider=provider,
        store=CodingAgentAttemptStore(args.output_dir),
    )
    store = runner.store
    attempt = runner.run(
        request,
        worktree_root=args.worktree,
        attempt_id=args.attempt_id,
    )
    profiles = {}
    for value in args.validation_profile:
        profile = parse_validation_profile(value)
        profiles[profile.name] = profile
    validation = ValidationRunner(
        profiles, timeout_seconds=args.validation_timeout_seconds
    ).run(request, attempt, worktree_root=args.worktree)
    store.save_validation_report(validation)
    output = attempt.to_data()
    output["validation"] = validation.to_data()
    print(json.dumps(output, indent=2, sort_keys=True))
    return int(
        attempt.status is not CodingAgentStatus.COMPLETED
        or validation.status is not ValidationReportStatus.PASSED
    )


def _run_compile_implementation_request(args: argparse.Namespace) -> int:
    plan = ExecutionPlan.from_data(_load_structured_mapping(args.plan))
    request = ImplementationRequest.from_execution_plan(
        plan,
        unit_id=args.unit_id,
        request_id=args.request_id,
        base_commit=args.base_commit,
        context_refs=tuple(args.context_ref),
        allowed_commands=tuple(args.allowed_command),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(request.to_json(), encoding="utf-8")
    print(json.dumps(request.to_data(), indent=2, sort_keys=True))
    return 0


def _load_structured_mapping(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise PowdrrExecutionError(
            f"Could not read structured input {path}: {error}",
            error_code="compiled_workflow_input_invalid",
            remediation="Provide a readable JSON or YAML input file.",
        ) from error
    if not isinstance(loaded, dict):
        raise PowdrrExecutionError(
            f"Structured input {path} must contain an object.",
            error_code="compiled_workflow_input_invalid",
            remediation=(
                "Provide a top-level object in the plan or action contract file."
            ),
        )
    return loaded


def _run_workflow_recovery(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    try:
        integration_branch = getattr(args, "integration_branch", None)
        report = inspect_workflow_run(
            repo_root,
            args.proposed_pr_id,
            integration_branch=integration_branch,
            worktree_name=integration_branch,
        )
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


def _run_bootstrap_structrr(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    requested_output = _repository_relative_option(repo_root, args.output, None)
    requested_taxonomy = _repository_relative_option(
        repo_root, args.taxonomy, "software_development_entity_taxonomy.md"
    )
    if (args.output is not None and requested_output is None) or (
        requested_taxonomy is None
    ):
        return _run_legacy_bootstrap_structrr(args, repo_root)
    from powdrr_agent.repository import (
        LocalActionError,
        collect_tracked_worktree,
        digest_at_path,
        materialize_artifact,
    )
    from powdrr_local import JsonServiceBridge, LocalPowdrrService

    try:
        output_path = _repository_relative_option(
            repo_root, args.output, default_name=None
        )
        if output_path is None:
            short_sha = subprocess.run(
                ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            output_path = f"docs/structrr/current/baseline-{short_sha}.yaml"
        taxonomy_path = _repository_relative_option(
            repo_root,
            args.taxonomy,
            default_name="software_development_entity_taxonomy.md",
        )
        if taxonomy_path is None:
            raise LocalActionError(
                "forbidden_path",
                "The taxonomy must be inside the repository for service analysis.",
            )
        report_path = str(Path(output_path).with_suffix(".md").as_posix())
        expected = {
            output_path: digest_at_path(repo_root, output_path),
            report_path: digest_at_path(repo_root, report_path),
        }
        request = collect_tracked_worktree(
            repo_root,
            request_id=str(uuid.uuid4()),
            operation_kind="bootstrap",
            inputs={
                "output_path": output_path,
                "taxonomy_path": taxonomy_path,
                "change_id": args.change_id,
                "title": args.title,
                "repository_name": repo_root.name,
            },
        )
        update = JsonServiceBridge(LocalPowdrrService()).start(request)
        if update.status != "succeeded":
            message = (
                update.failure.message
                if update.failure
                else "service returned an incomplete Structrr bootstrap"
            )
            print(f"Structrr bootstrap validation failed: {message}", file=sys.stderr)
            return 1
        outputs = {item.artifact_id: item for item in update.output_artifacts}
        if set(outputs) != set(expected):
            raise LocalActionError(
                "invalid_service_output",
                "Service returned unexpected Structrr artifact paths.",
            )
        for relative_path, prior_digest in expected.items():
            if digest_at_path(repo_root, relative_path) != prior_digest:
                raise LocalActionError(
                    "stale_precondition",
                    f"{relative_path} changed while Structrr was being analyzed.",
                )
        for relative_path, prior_digest in expected.items():
            materialize_artifact(
                repo_root, outputs[relative_path], expected_digest=prior_digest
            )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Could not build Structrr bootstrap: {error}", file=sys.stderr)
        return 1
    document = yaml.safe_load(outputs[output_path].data)
    summary = {
        "output_path": str(repo_root / output_path),
        "report_path": str(repo_root / report_path),
        "baseline_status": "not_run",
        "entity_count": len(document.get("entities", [])),
        "relationship_count": len(document.get("entity_relationships", [])),
        "source_anchor_count": len(document.get("files", [])),
        "source_subject_count": len(document.get("source_subjects", [])),
        "source_binding_count": len(document.get("source_bindings", [])),
    }
    if args.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        print(repo_root / output_path)
        print(repo_root / report_path)
        print(
            f"Validated {summary['entity_count']} entities, "
            f"{summary['relationship_count']} relationships, and "
            f"{summary['source_anchor_count']} source anchors.",
        )
        print("Repository validation commands were discovered but not run.")
    return 0


def _repository_relative_option(
    repo_root: Path, value: Path | None, default_name: str | None
) -> str | None:
    if value is None:
        return default_name
    target = value if value.is_absolute() else repo_root / value
    try:
        return target.resolve(strict=False).relative_to(repo_root).as_posix()
    except ValueError:
        return None


def _run_legacy_bootstrap_structrr(args: argparse.Namespace, repo_root: Path) -> int:
    """Preserve explicit out-of-repository output and taxonomy paths."""
    result = bootstrap_structrr(
        repo_root,
        output_path=args.output,
        change_id=args.change_id,
        title=args.title,
        taxonomy_path=args.taxonomy,
    )
    if not result.validation.successful:
        print("Structrr bootstrap validation failed:", file=sys.stderr)
        for issue in result.validation.issues:
            location = f" ({issue.path})" if issue.path else ""
            print(f"- {issue.code}{location}: {issue.message}", file=sys.stderr)
        return 1
    summary = {
        "output_path": str(result.output_path),
        "report_path": str(result.report_path) if result.report_path else None,
        "baseline_status": "not_run",
        "entity_count": len(result.document["entities"]),
        "relationship_count": len(result.document["entity_relationships"]),
        "source_anchor_count": len(result.document["files"]),
        "source_subject_count": len(result.document["source_subjects"]),
        "source_binding_count": len(result.document["source_bindings"]),
    }
    if args.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        print(result.output_path)
        if result.report_path:
            print(result.report_path)
        print(
            f"Validated {summary['entity_count']} entities, "
            f"{summary['relationship_count']} relationships, and "
            f"{summary['source_anchor_count']} source anchors."
        )
        print("Repository validation commands were discovered but not run.")
    return 0


def _run_bootstrap(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)

    def report_progress(message: str) -> None:
        destination = sys.stderr if args.json else sys.stdout
        print(f"Bootstrap: {message}", file=destination, flush=True)

    result = run_bootstrap_task(
        BootstrapTaskConfig(
            repo_root=repo_root,
            task_name=args.work_item_name,
            base_ref=args.base_ref,
            remote=args.remote,
            base_branch=args.base_branch,
            open_pr=args.open_pr,
            output_root=args.output_root,
            taxonomy_path=args.taxonomy,
            validation_timeout_seconds=args.validation_timeout_seconds,
        ),
        progress=report_progress,
    )
    data = result.to_data()
    if args.json:
        print(json.dumps(data, sort_keys=True))
    else:
        print(f"Bootstrap status: {result.status}")
        if result.validation_status:
            print(f"Validation status: {result.validation_status}")
        if result.task is not None:
            print(f"Branch: {result.task.branch}")
            print(f"Worktree: {result.task.worktree}")
        if result.bootstrap is not None:
            print(f"Snapshot: {result.bootstrap.output_path}")
        if result.pull_request_url:
            print(f"Pull request: {result.pull_request_url}")
        if result.report_markdown_path:
            print(f"Report: {result.report_markdown_path}")
        if result.error:
            print(f"Error: {result.error}", file=sys.stderr)
    return 0 if result.status in {"completed_local", "pr_opened", "no_op"} else 1


def _run_check_credentials(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    results = []
    route_checks = (
        (
            "planning",
            args.planning_provider,
            args.planning_model,
            args.planning_api_key,
            args.planning_base_url,
        ),
        (
            "coding",
            args.coding_provider,
            args.coding_model,
            None,
            None,
        ),
    )
    checked_credentials: dict[tuple[str, str], dict[str, Any]] = {}
    checked_missing_credentials: dict[tuple[str, ...], dict[str, Any]] = {}
    for role, provider, model, api_key, base_url in route_checks:
        definition = provider_definition(provider)
        missing_identity = (
            *definition.api_key_env_names,
            base_url or "",
            api_key or "",
        )
        if missing_identity in checked_missing_credentials:
            checked_missing_credentials[missing_identity].setdefault(
                "roles", [role]
            ).append(role)
            continue
        try:
            mapping = configured_model_mapping(provider, model)
            credentials = resolve_provider_credentials(provider, api_key, base_url)
        except PowdrrExecutionError as error:
            result = {
                "role": role,
                "provider": provider,
                "model": model or "<unspecified>",
                "status": "configuration_error",
                "message": str(error),
                "roles": [role],
            }
            checked_missing_credentials[missing_identity] = result
            results.append(result)
            continue
        credential_identity = (credentials.base_url, credentials.api_key)
        if credential_identity in checked_credentials:
            checked_credentials[credential_identity].setdefault("roles", [role]).append(
                role
            )
            continue
        check_result: dict[str, Any] = check_model_access(
            role=role,
            provider=provider,
            mapping=mapping,
            repo_root=repo_root,
            api_key=api_key,
            base_url=base_url,
        ).to_data()
        check_result["roles"] = [role]
        checked_credentials[credential_identity] = check_result
        results.append(check_result)

    executable = (
        args.opencode_executable
        if args.code_agent == "opencode"
        else args.minisweagent_executable
    )
    results.append(check_coding_executable(executable, role="coding-agent").to_data())
    results.append(check_jev_access().to_data())

    github_result: dict[str, str] = {
        "role": "github-access",
        "provider": "gh",
        "model": "GitHub CLI and repository permissions",
        "status": "unavailable",
    }
    try:
        auth = subprocess.run(
            ["gh", "auth", "status"],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        if auth.returncode == 0:
            repository = subprocess.run(
                ["gh", "repo", "view", "--json", "nameWithOwner,viewerPermission"],
                cwd=repo_root,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            if repository.returncode == 0:
                details = json.loads(repository.stdout)
                permission = details.get("viewerPermission")
                github_result.update(
                    status=(
                        "passed"
                        if permission in {"WRITE", "MAINTAIN", "ADMIN"}
                        else "insufficient_repository_permission"
                    ),
                    repository=details.get("nameWithOwner", "unknown"),
                    permission=str(permission or "unknown"),
                )
            else:
                github_result["message"] = (
                    repository.stderr.strip()[-500:]
                    or "Could not identify the GitHub repository."
                )
        else:
            github_result["message"] = (
                auth.stderr.strip()[-500:] or "GitHub authentication failed."
            )
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        github_result["message"] = str(error)[:500]
    results.append(github_result)

    success = all(item["status"] == "passed" for item in results)
    report = {"passed": success, "checks": results}
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for item in results:
            roles = item.get("roles")
            role_label = "/".join(roles) if roles else item["role"]
            print(
                f"{role_label}: {item['status']} ({item['provider']} / {item['model']})"
            )
            if item.get("message"):
                print(f"  {item['message']}")
            if item.get("credential_source"):
                print(f"  Credential source: {item['credential_source']}")
    return 0 if success else 1


def _run_verification_health(args: argparse.Namespace) -> int:
    document = _read_yaml_mapping(args.input)
    report = audit_verification_health(
        intents=tuple(_mapping_list(document.get("intents"))),
        contracts=tuple(_mapping_list(document.get("contracts"))),
        inventory=tuple(_mapping_list(document.get("inventory"))),
        evidence=tuple(_mapping_list(document.get("evidence"))),
        verifier_changes=tuple(_mapping_list(document.get("verifier_changes"))),
        waivers=tuple(_mapping_list(document.get("waivers"))),
        candidate_tree=args.candidate_tree,
        today=args.today,
        mode=VerificationPolicyMode(args.mode),
    )
    rendered = json.dumps(report.to_data(), indent=2, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(args.output)
    return 0 if report.passed else 1


def _run_verification_diff(args: argparse.Namespace) -> int:
    base = _read_yaml_mapping(args.base)
    candidate = _read_yaml_mapping(args.candidate)
    result = classify_differential_result(
        str(candidate.get("obligation_id") or base.get("obligation_id")),
        base,
        candidate,
        comparable=not args.not_comparable,
    )
    report = {
        "result": result.to_data(),
        "verifier_changes": [
            change.to_data() for change in detect_verifier_changes(base, candidate)
        ],
    }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(args.output)
    return (
        0 if result.status.value in {"preserved_pass", "fixed_existing_failure"} else 1
    )


def _mapping_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _run_rebase_structrr(args: argparse.Namespace) -> int:
    baseline = _read_yaml_mapping(args.baseline)
    current = _read_yaml_mapping(args.current)
    report = rebase_structrr_snapshot(
        baseline,
        current,
        referenced_entity_ids=args.entity_id,
        referenced_source_subject_keys=args.source_subject_key,
        referenced_binding_ids=args.binding_id,
    )
    rendered = json.dumps(report.to_data(), indent=2, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(args.output)
    return 0


def _read_yaml_mapping(path: Path) -> Mapping[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"Structrr snapshot must be a mapping: {path}")
    return value


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
        if (
            relative_path
            in {
                Path("templates/execute-proposed-pr.yaml"),
                Path("templates/fully-review-pr.yaml"),
            }
            and not path.exists()
        ):
            results.append(
                {
                    "validator": "validate_legacy_template_removal",
                    "files": [str(relative_path)],
                    "validation_successful": True,
                    "report": {
                        "message": "Legacy template was replaced by a Procedrr process."
                    },
                }
            )
            continue
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

        if relative_path.parts[:3] == ("docs", "procedrr", "processes"):
            from procedrr import ProcessCatalog
            from procedrr.catalog import ProcessCatalogError

            try:
                catalog = ProcessCatalog.load(path.parent)
            except (OSError, ProcessCatalogError, ValueError) as error:
                report_text = json.dumps(
                    {"validation_successful": False, "error": str(error)}
                )
            else:
                report_text = json.dumps(
                    {
                        "validation_successful": True,
                        "process_count": len(catalog.list()),
                    }
                )
            add_result(
                validator="validate_procedrr_processes",
                paths=[path],
                report_text=report_text,
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
            raise PowdrrExecutionError(
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


def _run_artifact_state(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    requested_areas = [
        area
        for area in (
            "bootstrap",
            "entities",
            "relationships",
            "workflows",
            "plans",
            "changelogs",
        )
        if getattr(args, area)
    ]
    if args.pull_requests:
        requested_areas.append("pull_requests")
    if requested_areas == ["workflows"]:
        from powdrr_agent.artifact_state import collect_workflow_snapshot
        from powdrr_local import JsonServiceBridge, LocalPowdrrService

        try:
            request = collect_workflow_snapshot(
                repo_root,
                branch=args.branch,
                sha=args.sha,
                repository_id=args.repository or "",
                request_id=str(uuid.uuid4()),
            )
            update = JsonServiceBridge(LocalPowdrrService()).start(request)
        except (OSError, ValueError) as error:
            print(f"Could not build artifact state: {error}", file=sys.stderr)
            return 1
        if update.status != "succeeded" or not update.output_artifacts:
            message = (
                update.failure.message
                if update.failure
                else "service returned no projection"
            )
            print(f"Could not build artifact state: {message}", file=sys.stderr)
            return 1
        print(update.output_artifacts[0].data.decode("utf-8"))
        return 0
    try:
        report = build_artifact_state(
            repo_root,
            branch=args.branch,
            sha=args.sha,
            repository=args.repository,
            include_github=not args.no_github,
            areas=requested_areas or None,
        )
    except ArtifactStateError as error:
        print(f"Could not build artifact state: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
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
        interview_input=args.interview_input,
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


def _run_design_interview_input(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    output_path = create_design_interview_input_template(
        work_item_name=args.work_item_name,
        output_path=args.output,
        repo_root=repo_root,
    )
    _stage_generated_file(repo_root, output_path)
    print(f"Wrote design interview input to {output_path}")
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
    "feature-pr-specification.yaml": "pr",
    "feature-pr-specification.yml": "pr",
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
    if args.extract_responses is not None:
        fixture_path = (
            args.extract_responses
            if args.extract_responses.is_absolute()
            else repo_root / args.extract_responses
        )
        fixture_path.parent.mkdir(parents=True, exist_ok=True)
        fixture_path.write_text(
            yaml.safe_dump(
                extract_scripted_responses_from_report(data), sort_keys=False
            ),
            encoding="utf-8",
        )
        print(f"Wrote scripted responses to {fixture_path}")
        if args.verify_extracted:
            if scenario.get("execution_mode") == "workflow_chain":
                print(
                    "--verify-extracted currently supports single-phase "
                    "scenarios only.",
                    file=sys.stderr,
                )
                return 1
            replay = dict(scenario)
            replay["provider"] = {
                "mode": "scripted",
                "responses": extract_scripted_responses_from_report(data),
            }
            replay_result = run_workflow_scenario(
                replay,
                scenario_path=scenario_path,
                repo_root=repo_root,
                keep_failed=args.keep_failed,
            )
            if replay_result.status != "passed":
                print("Extracted response replay failed.", file=sys.stderr)
                return 1
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


def _run_workflow_scenario_suite(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    manifest_path = (
        args.manifest if args.manifest.is_absolute() else repo_root / args.manifest
    )
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 1:
            raise WorkflowScenarioError(
                "Scenario suite manifest schema_version must be 1."
            )
        entries = manifest.get("scenarios")
        if not isinstance(entries, list) or not entries:
            raise WorkflowScenarioError("Scenario suite manifest requires scenarios.")
        reports: list[dict[str, Any]] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                raise WorkflowScenarioError(
                    f"Scenario suite entry {index} must be an object."
                )
            scenario_path = Path(str(entry.get("path", "")))
            if not scenario_path.is_absolute():
                scenario_path = manifest_path.parent / scenario_path
            scenario = load_workflow_scenario(scenario_path)
            if scenario.get("provider", {}).get("mode") == "live":
                raise WorkflowScenarioError(
                    f"Scenario suite cannot include live scenario: {scenario_path}"
                )
            result = run_workflow_scenario(
                scenario, scenario_path=scenario_path, repo_root=repo_root
            )
            expected = str(entry.get("expected_status", "passed"))
            reports.append(
                {
                    "path": str(scenario_path),
                    "status": result.status,
                    "expected_status": expected,
                }
            )
        failed = [item for item in reports if item["status"] != item["expected_status"]]
    except (OSError, TypeError, ValueError, WorkflowScenarioError) as exc:
        print(f"Workflow scenario suite failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(reports, indent=2))
    else:
        for report in reports:
            print(f"{report['status']}: {report['path']}")
    if args.report is not None:
        report_path = (
            args.report if args.report.is_absolute() else repo_root / args.report
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(reports, indent=2) + "\n", encoding="utf-8")
    return 1 if failed else 0


def _run_agent_feature_e2e(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    result = run_agent_feature_e2e(
        AgentFeatureRunConfig(
            repo_root=repo_root,
            feature_request=args.feature_request,
            feature_name=args.feature_name,
            answers=tuple(args.answer),
            max_turns=args.max_turns,
            workflow_roundtrips=args.workflow_roundtrips,
            start_iterations=args.start_iterations,
            phase_timeout=args.phase_timeout,
            provider=args.provider,
            report_path=args.report or Path(".powdrr/agent-feature-run/report.json"),
            transcript_dir=args.transcript_dir
            or Path(".powdrr/agent-feature-run/transcripts"),
        )
    )
    if args.json:
        print(json.dumps(result.to_data(), indent=2, ensure_ascii=False))
    else:
        print(f"Agent feature run {result.status}; report: {result.report_path}")
        for phase in result.phases:
            print(f"{phase['name']}: returncode={phase['returncode']}")
    return 0 if result.status == "passed" else 1


def _run_implement(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    run_id = args.run_id or uuid.uuid4().hex[:12]
    try:
        feature_description = (
            args.request_file.read_text(encoding="utf-8")
            if args.request_file is not None
            else args.feature_description
        )
    except OSError as error:
        print(f"Could not read feature request: {error}", file=sys.stderr)
        return 2
    if not isinstance(feature_description, str) or not feature_description.strip():
        print("Feature request must not be empty.", file=sys.stderr)
        return 2

    check_args = argparse.Namespace(
        repo_root=repo_root,
        planning_provider=args.planning_provider,
        planning_model=args.planning_model,
        planning_api_key=args.planning_api_key,
        planning_base_url=args.planning_base_url,
        coding_provider=args.coding_provider,
        coding_model=args.coding_model,
        code_agent=args.code_agent,
        opencode_executable=args.opencode_executable,
        minisweagent_executable=args.minisweagent_executable,
        open_pr=args.open_pr,
        json=True,
    )
    preflight_output = io.StringIO()
    with contextlib.redirect_stdout(preflight_output):
        preflight_status = _run_check_credentials(check_args)
    try:
        preflight = json.loads(preflight_output.getvalue())
    except json.JSONDecodeError:
        preflight = {
            "passed": False,
            "checks": [],
            "error": "Invalid preflight output.",
        }

    work_item_name = args.work_item_name
    output_root = (
        args.output_root
        or _run_output_root(
            subprocess.run,
            repo_root,
            run_id,
        )
    ).resolve()
    if preflight_status != 0:
        output_root.mkdir(parents=True, exist_ok=True)
        preflight_result = {
            "status": "failed",
            "task_id": work_item_name,
            "request": feature_description,
            "allowed_paths": list(args.allowed_paths or ["."]),
            "effective_profile": {
                "planning_provider": args.planning_provider,
                "planning_model": args.planning_model,
                "coding_provider": args.coding_provider,
                "coding_model": args.coding_model,
                "code_agent": args.code_agent,
            },
            "preflight": preflight,
            "operational_failures": [
                {"stage": "preflight", "message": "Credential or access checks failed."}
            ],
            "uncertainty_decisions": [],
            "publication": {"status": "not_reached"},
            "limits": {
                "timeout_seconds": args.timeout_seconds,
                "max_repair_attempts": args.max_repair_attempts,
            },
        }
        metadata = {
            "request": feature_description,
            "allowed_paths": list(args.allowed_paths or ["."]),
            "effective_profile": preflight_result["effective_profile"],
            "publication_requested": args.open_pr,
            "limits": {
                "timeout_seconds": args.timeout_seconds,
                "max_repair_attempts": args.max_repair_attempts,
            },
        }
        write_json_artifact(output_root, "run-metadata.json", metadata)
        write_json_artifact(output_root, "preflight.json", preflight)
        report_json, report_markdown = write_run_report(
            output_root, result=preflight_result, metadata=metadata
        )
        preflight_result["report_json_path"] = str(report_json)
        preflight_result["report_markdown_path"] = str(report_markdown)
        if args.json:
            print(json.dumps(preflight_result, indent=2, sort_keys=True))
        else:
            print("Headless feature preflight failed.")
            print(f"Report: {report_markdown}")
        return 1

    planning_provider = resolve_workflow_provider(args.planning_provider)
    planning_mapping = configured_model_mapping(planning_provider, args.planning_model)
    planning_credentials = resolve_provider_credentials(
        planning_mapping.provider,
        args.planning_api_key,
        args.planning_base_url,
    )
    planning_client = build_workflow_client(
        planning_credentials,
        model=planning_mapping.model,
        model_cache_dir=repo_root / ".powdrr" / "models",
        progress_stream=sys.stderr,
    )
    coding_model = args.minisweagent_model or (
        f"{args.coding_provider}/{args.coding_model}"
    )
    if args.code_agent == "opencode":
        coding_model = args.opencode_model
    endpoint_config = FeatureEndpointConfig(
        feature_description=feature_description,
        work_item_name=work_item_name,
        repo_root=repo_root,
        allowed_paths=tuple(args.allowed_paths or ["."]),
        validation_command=(
            tuple(shlex.split(args.validation_command))
            if args.validation_command
            else ()
        ),
        base_branch=args.base_branch or "main",
        code_agent=args.code_agent,
        opencode_executable=args.opencode_executable,
        opencode_model=args.opencode_model,
        minisweagent_executable=args.minisweagent_executable,
        minisweagent_model=coding_model,
        code_agent_prompt_prefix=args.code_agent_prompt_prefix,
        code_agent_prompt_suffix=args.code_agent_prompt_suffix,
        output_root=output_root,
        open_pr=False,
        push_changes=False,
        planning_client=planning_client,
        task_id=work_item_name,
        uncertainty_policy="normative_default",
        max_repair_attempts=args.max_repair_attempts,
    )
    result = run_agent_feature_task(
        endpoint_config,
        base_ref=args.base_ref,
        remote=args.remote,
        base_branch=args.base_branch,
        open_pr=args.open_pr,
        run_id=run_id,
        output_root=output_root,
        preflight=preflight,
        timeout_seconds=args.timeout_seconds,
    )
    if args.json:
        print(json.dumps(result.to_data(), indent=2, sort_keys=True))
    else:
        print(f"Headless feature run {result.status}.")
        if result.task:
            print(f"Branch: {result.task.branch}")
            print(f"Worktree: {result.task.worktree}")
        if result.pull_request_url:
            print(f"Pull request: {result.pull_request_url}")
        print(f"Report JSON: {result.report_json_path}")
        print(f"Report Markdown: {result.report_markdown_path}")
        if result.error:
            print(f"Error: {result.error}", file=sys.stderr)
    return 0 if result.status in {"completed_local", "pr_opened", "no_op"} else 1


def _run_workrr_feature(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    planning_provider = resolve_workflow_provider(args.planning_provider)
    planning_mapping = default_llm_mappings(planning_provider)["standard_reasoning"]
    try:
        planning_credentials = resolve_provider_credentials(
            planning_mapping.provider,
            args.planning_api_key,
            args.planning_base_url,
        )
    except PowdrrExecutionError:
        # Keep endpoint construction side-effect free when callers only need to
        # inspect or replace the runner (for example, CLI tests). The endpoint
        # itself still rejects a missing planning client before it can execute
        # the planning flow, while real invocations with credentials continue
        # to fail early during CLI setup.
        if args.planning_api_key or args.planning_base_url:
            raise
        planning_client = None
    else:
        planning_client = build_workflow_client(
            planning_credentials,
            model=args.planning_model or planning_mapping.model,
            model_cache_dir=repo_root / ".powdrr" / "models",
            progress_stream=sys.stderr,
        )
    output_root = (
        args.output_root
        or repo_root
        / ".powdrr"
        / "feature-runs"
        / slugify_workflow_id(args.work_item_name)
    ).resolve()
    try:
        result = run_feature_endpoint(
            FeatureEndpointConfig(
                feature_description=args.feature_description,
                work_item_name=args.work_item_name,
                repo_root=repo_root,
                allowed_paths=tuple(args.allowed_paths),
                validation_command=(
                    tuple(shlex.split(args.validation_command))
                    if args.validation_command
                    else ()
                ),
                base_branch=args.base_branch,
                code_agent=args.code_agent,
                opencode_executable=args.opencode_executable,
                opencode_model=args.opencode_model,
                minisweagent_executable=args.minisweagent_executable,
                minisweagent_model=args.minisweagent_model,
                code_agent_prompt_prefix=args.code_agent_prompt_prefix,
                code_agent_prompt_suffix=args.code_agent_prompt_suffix,
                output_root=args.output_root,
                open_pr=not args.no_open_pr,
                planning_client=planning_client,
                task_id=args.task_id or args.work_item_name,
            )
        )
    except Exception:
        print(f"Run report JSON: {output_root / 'report.json'}", file=sys.stderr)
        print(f"Run report Markdown: {output_root / 'report.md'}", file=sys.stderr)
        raise
    if args.json:
        print(json.dumps(result.to_data(), indent=2, sort_keys=True))
    else:
        print(f"Workrr feature run {result.status}")
        print(f"Branch: {result.branch}")
        print(f"Worktree: {result.worktree}")
        print(f"Review passed: {result.review['passed']}")
        if result.pull_request_url:
            print(f"Pull request: {result.pull_request_url}")
        if result.changelog_path:
            print(f"Changelog: {result.changelog_path}")
        if result.report_json_path:
            print(f"Run report JSON: {result.report_json_path}")
        if result.report_markdown_path:
            print(f"Run report Markdown: {result.report_markdown_path}")
    return 0 if result.status in {"completed", "pr_opened"} else 1


def _run_harbor_feature(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    console = sys.stdout
    if args.json:
        result = _run_harbor_feature_with_streams(
            args, repo_root, console=console, progress_stream=sys.stderr
        )
        return (
            0
            if result.status
            in {
                "completed",
                "completed_with_issues",
                "design_generated",
                "prompt_captured",
            }
            else 1
        )

    work_item = slugify_workflow_id(args.work_item_name)
    output_root = (
        args.output_root or repo_root / ".powdrr" / "feature-runs" / work_item
    ).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    log_path = output_root / "console.log"
    print(
        f"Starting benchmark task: {args.task_id or args.work_item_name}", file=console
    )
    try:
        with log_path.open("w", encoding="utf-8", buffering=1) as log_stream:
            with (
                contextlib.redirect_stdout(log_stream),
                contextlib.redirect_stderr(log_stream),
            ):
                result = _run_harbor_feature_with_streams(
                    args,
                    repo_root,
                    console=console,
                    progress_stream=log_stream,
                )
    except Exception:
        print(f"Benchmark run failed. Details: {log_path}", file=console)
        raise

    print(f"Benchmark task {result.status}.", file=console)
    if args.design_only:
        print(f"Design artifact: {result.plan_path}", file=console)
    elif args.capture_worker_prompts_only:
        print(
            "Worker prompt compilation completed; coding agent was not run.",
            file=console,
        )
        print(f"Latest request: {result.request_path}", file=console)
    else:
        print(f"Review passed: {result.review['passed']}", file=console)
    print(f"Run logs: {output_root}", file=console)
    report_json_path = getattr(result, "report_json_path", None)
    report_markdown_path = getattr(result, "report_markdown_path", None)
    if report_json_path:
        print(f"Run report JSON: {report_json_path}", file=console)
    if report_markdown_path:
        print(f"Run report Markdown: {report_markdown_path}", file=console)
    return (
        0
        if result.status
        in {
            "completed",
            "completed_with_issues",
            "design_generated",
            "prompt_captured",
        }
        else 1
    )


def _run_harbor_feature_with_streams(
    args: argparse.Namespace,
    repo_root: Path,
    *,
    console: Any,
    progress_stream: Any,
) -> Any:
    planning_provider = resolve_workflow_provider(args.planning_provider)
    planning_mapping = default_llm_mappings(planning_provider)["standard_reasoning"]
    try:
        planning_credentials = resolve_provider_credentials(
            planning_mapping.provider,
            args.planning_api_key,
            args.planning_base_url,
        )
    except PowdrrExecutionError:
        if args.planning_api_key or args.planning_base_url:
            raise
        planning_client = None
    else:
        planning_client = build_workflow_client(
            planning_credentials,
            model=args.planning_model or planning_mapping.model,
            model_cache_dir=repo_root / ".powdrr" / "models",
            progress_stream=progress_stream,
        )
    result = run_feature_in_place(
        FeatureEndpointConfig(
            feature_description=args.feature_description,
            work_item_name=args.work_item_name,
            repo_root=repo_root,
            allowed_paths=tuple(args.allowed_paths),
            validation_command=(
                tuple(shlex.split(args.validation_command))
                if args.validation_command
                else ()
            ),
            opencode_executable=args.opencode_executable,
            opencode_model=args.opencode_model,
            code_agent=args.code_agent,
            minisweagent_executable=args.minisweagent_executable,
            minisweagent_model=args.minisweagent_model,
            code_agent_prompt_prefix=args.code_agent_prompt_prefix,
            code_agent_prompt_suffix=args.code_agent_prompt_suffix,
            output_root=args.output_root,
            open_pr=False,
            push_changes=False,
            cleanup_temporary_artifacts=True,
            planning_client=planning_client,
            progress_callback=(
                None
                if args.json
                else lambda message: print(message, file=console, flush=True)
            ),
            task_id=args.task_id or args.work_item_name,
            design_only=args.design_only,
            capture_worker_prompts_only=args.capture_worker_prompts_only,
        )
    )
    if args.json:
        print(json.dumps(result.to_data(), indent=2, sort_keys=True))
    return result


def _run_replay_subject_bindings(args: argparse.Namespace) -> int:
    try:
        report = replay_subject_binding_events(args.events)
    except (OSError, ValueError) as error:
        print(f"Subject binding replay failed: {error}", file=sys.stderr)
        return 1
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"Subject binding replay written to {args.output}")
    return 0


def _run_deepswe_design_evaluation(args: argparse.Namespace) -> int:
    provider = resolve_workflow_provider(args.judge_provider)
    mapping = default_llm_mappings(provider)["standard_reasoning"]
    try:
        credentials = resolve_provider_credentials(
            mapping.provider,
            args.judge_api_key,
            args.judge_base_url,
        )
        model = args.judge_model or mapping.model
        judge = build_workflow_client(
            credentials,
            model=model,
            model_cache_dir=args.run_dir / ".models",
            progress_stream=sys.stderr,
        )
        report = evaluate_deepswe_design(
            task_dir=args.task_dir,
            run_dir=args.run_dir,
            judge=judge,
            rubric_path=args.rubric,
            judge_id=f"{mapping.provider}/{model}",
        )
    except (DeepSWEEvaluationError, PowdrrExecutionError) as error:
        print(f"DeepSWE design evaluation failed: {error}", file=sys.stderr)
        return 1
    report_path = args.report or args.run_dir / "design-quality-evaluation.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        summary = report["summary"]
        print(
            f"DeepSWE design evaluation {report['task_id']}: "
            f"{summary['supported_count']}/{summary['criterion_count']} supported; "
            f"weighted coverage {summary['weighted_coverage']:.1%}; "
            f"critical failures {len(summary['critical_failures'])}."
        )
        print(f"Report: {report_path}")
    return 0 if report["summary"]["passed"] else 1


def _run_deepswe_probe_evaluation(args: argparse.Namespace) -> int:
    try:
        default_suite_path = args.run_dir / "postrun-probes" / "probe-suite.json"
        suite_path = args.probe_suite or default_suite_path
        try:
            suite_data = json.loads(suite_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            suite_data = None
        has_probes = (
            isinstance(suite_data, Mapping)
            and isinstance(suite_data.get("probes"), list)
            and bool(suite_data["probes"])
        )
        judge_id = None
        judge = None
        if has_probes:
            provider = resolve_workflow_provider(args.judge_provider)
            mapping = default_llm_mappings(provider)["standard_reasoning"]
            credentials = resolve_provider_credentials(
                mapping.provider,
                args.judge_api_key,
                args.judge_base_url,
            )
            model = args.judge_model or mapping.model
            judge = build_workflow_client(
                credentials,
                model=model,
                model_cache_dir=args.run_dir / ".models",
                progress_stream=sys.stderr,
            )
            judge_id = f"{mapping.provider}/{model}"
        report = evaluate_deepswe_probes(
            task_dir=args.task_dir,
            run_dir=args.run_dir,
            probe_suite_path=args.probe_suite,
            judge=judge,
            judge_id=judge_id,
        )
    except (DeepSWEProbeEvaluationError, PowdrrExecutionError) as error:
        print(f"DeepSWE probe evaluation failed: {error}", file=sys.stderr)
        return 1
    report_path = args.report or args.run_dir / "probe-quality-evaluation.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        summary = report["summary"]
        print(
            f"DeepSWE probe evaluation {report['task_id']}: "
            f"exact case recall {summary['exact_case_recall']}; "
            f"any case recall {summary['any_case_recall']}; "
            f"unsupported probes {summary['unsupported_probe_count']}."
        )
        print(f"Report: {report_path}")
    return 0


def _run_deepswe_prompt_evaluation(args: argparse.Namespace) -> int:
    provider = resolve_workflow_provider(args.judge_provider)
    mapping = default_llm_mappings(provider)["standard_reasoning"]
    try:
        credentials = resolve_provider_credentials(
            mapping.provider,
            args.judge_api_key,
            args.judge_base_url,
        )
        model = args.judge_model or mapping.model
        judge = build_workflow_client(
            credentials,
            model=model,
            model_cache_dir=args.run_dir / ".models",
            progress_stream=sys.stderr,
        )
        report = evaluate_deepswe_worker_prompt(
            task_dir=args.task_dir,
            run_dir=args.run_dir,
            judge=judge,
            rubric_path=args.rubric,
            judge_id=f"{mapping.provider}/{model}",
        )
    except (DeepSWEEvaluationError, PowdrrExecutionError) as error:
        print(f"DeepSWE prompt evaluation failed: {error}", file=sys.stderr)
        return 1
    report_path = args.report or args.run_dir / "prompt-quality-evaluation.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        summary = report["summary"]
        print(
            f"DeepSWE worker-prompt evaluation {report['task_id']}: "
            f"score {summary['prompt_score']}/100; gate {summary['gate']}; "
            f"{summary['supported_count']}/{summary['criterion_count']} supported; "
            f"critical failures {len(summary['critical_failures'])}; "
            f"evaluation errors {len(summary['evaluation_errors'])}."
        )
        print(f"Report: {report_path}")
    return 0 if report["summary"]["passed"] else 1


def _run_generic_prompt_evaluation(args: argparse.Namespace) -> int:
    provider = resolve_workflow_provider(args.judge_provider)
    mapping = default_llm_mappings(provider)["standard_reasoning"]
    try:
        credentials = resolve_provider_credentials(
            mapping.provider,
            args.judge_api_key,
            args.judge_base_url,
        )
        model = args.judge_model or mapping.model
        judge = build_workflow_client(
            credentials,
            model=model,
            model_cache_dir=args.run_dir / ".models",
            progress_stream=sys.stderr,
        )
        report = evaluate_prompt_case(
            case_path=args.case,
            run_dir=args.run_dir,
            judge=judge,
            rubric_path=args.rubric,
            judge_id=f"{mapping.provider}/{model}",
        )
    except (DeepSWEEvaluationError, PowdrrExecutionError) as error:
        print(f"Prompt evaluation failed: {error}", file=sys.stderr)
        return 1
    report_path = args.report or args.run_dir / "prompt-quality-evaluation.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        summary = report["summary"]
        print(
            f"Prompt evaluation {report['task_id']}: "
            f"score {summary['prompt_score']}/100; gate {summary['gate']}; "
            f"{summary['supported_count']}/{summary['criterion_count']} supported; "
            f"critical failures {len(summary['critical_failures'])}; "
            f"evaluation errors {len(summary['evaluation_errors'])}."
        )
        print(f"Report: {report_path}")
    return 0 if report["summary"]["passed"] else 1


def _run_prompt_benchmark_comparison(args: argparse.Namespace) -> int:
    try:
        report = compare_prompt_benchmark(
            prompt_report_path=args.prompt_report,
            benchmark_run_path=args.benchmark_run,
        )
    except PromptBenchmarkComparisonError as error:
        print(f"Prompt/benchmark comparison failed: {error}", file=sys.stderr)
        return 1
    default_parent = (
        args.benchmark_run if args.benchmark_run.is_dir() else args.benchmark_run.parent
    )
    report_path = args.report or default_parent / "prompt-benchmark-comparison.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        summary = report["summary"]
        pass_rate = summary["validation_pass_rate"]
        rate_text = "n/a" if pass_rate is None else f"{pass_rate:.1%}"
        print(
            f"Prompt/benchmark comparison {report['benchmark']['run_id']}: "
            f"prompt score {report['prompt_report']['score']}/100; "
            f"gate {report['prompt_report']['gate']}; "
            f"critical failures "
            f"{report['prompt_report']['critical_failure_count']}; "
            f"validation pass rate {rate_text}; "
            f"unresolved {summary['unresolved_count']}; "
            f"unmapped failures {summary['unmapped_failure_count']}."
        )
        print(f"Report: {report_path}")
    return 0


def _extract_workflow_responses(args: argparse.Namespace) -> int:
    try:
        responses = extract_scripted_responses(args.report)
    except WorkflowScenarioError as exc:
        print(f"Could not extract workflow responses: {exc}", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(yaml.safe_dump(responses, sort_keys=False), encoding="utf-8")
    print(f"Wrote {len(responses)} scripted responses to {args.output}")
    return 0


def _run_validate_procedrr_processes(args: argparse.Namespace) -> int:
    from procedrr import ProcessCatalog
    from procedrr.catalog import ProcessCatalogError

    results: list[dict[str, Any]] = []
    for directory in args.paths:
        try:
            catalog = ProcessCatalog.load(directory)
        except (OSError, ProcessCatalogError, ValueError) as error:
            results.append(
                {
                    "directory": str(directory),
                    "validation_successful": False,
                    "error": str(error),
                }
            )
            continue
        results.append(
            {
                "directory": str(directory),
                "processes": [process.name for process in catalog.list()],
                "validation_successful": True,
            }
        )
    successful = bool(results) and all(
        result["validation_successful"] for result in results
    )
    if args.json:
        print(
            json.dumps(
                {"validation_successful": successful, "results": results}, indent=2
            )
        )
    else:
        for result in results:
            if result["validation_successful"]:
                print(
                    f"Validated {len(result['processes'])} Procedrr processes in "
                    f"{result['directory']}"
                )
            else:
                print(
                    f"Invalid Procedrr process directory {result['directory']}: "
                    f"{result['error']}",
                    file=sys.stderr,
                )
    return 0 if successful else 1


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
        processes_dir=args.processes_dir,
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
