from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from powdrr_lift.core import (
    architecture_specification_default_output_path,
    blame_view_state_to_data,
    build_blame_view_state,
    build_current_decisions_report,
    build_invariants_report,
    codebase_state_default_output_path,
    create_architecture_specification_template,
    create_change_log_template,
    create_change_log_template_from_plan_diff,
    create_codebase_state,
    create_current_state_specification,
    create_feature_pr_specification_template,
    create_implementation_specification_template,
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
    system_map_specification_default_output_path,
    system_specification_default_output_path,
    validate_architecture_specification_yaml,
    validate_change_log_yaml,
    validate_implementation_specification_yaml,
    validate_pr_specification_yaml,
    validate_system_specification_yaml,
)
from powdrr_lift.core import (
    create_plan_diff_specification as _create_plan_diff_specification,
)
from powdrr_lift.core import (
    start_planning_feature as _start_planning_feature,
)

# Help text intentionally keeps example payloads readable on single lines.
# ruff: noqa: E501


def _load_fastmcp() -> Any:
    try:
        from mcp.server.fastmcp import (  # type: ignore[attr-defined]
            FastMCP as fastmcp,
        )
    except ImportError:  # pragma: no cover
        return None

    return fastmcp


FastMCP = _load_fastmcp()


_TOOL_HELP: dict[str, str] = {
    "init_change_log_template": """Create a v2 change-log template from the current branch diff.

Use this first when beginning a change and you need a structured inventory of
changed files, entities, and related context. Example: call with
`{"branch_name": "feature/auth", "repo_root": "/workspace/repo"}`.
The result is the rendered YAML template; fill it in before validation.
""",
    "init_change_log_template_from_plan_diff": """Create a change-log template seeded from a plan-diff specification.

Use this after a feature plan has been reviewed and you want to turn its
planned differences into an implementation change log. Example:
`{"branch_name": "feature/auth", "plan_diff_path": "docs/specs/auth/plan-diff.yaml"}`.
The plan-diff path must point to the relevant YAML document.
""",
    "evaluate_pr_against_changelog": """Validate a proposed change log against the branch and repository state.

Use this before opening a pull request to catch missing files, entities,
relationships, invariants, and test evidence. Example:
`{"proposed_change_log_yaml": "version: 2\\n...", "branch_name": "feature/auth"}`.
Pass the complete YAML document, not a file path; the returned YAML is a
machine-readable validation report.
""",
    "get_edit_context": """Explain the repository context around lines that are about to be edited.

Use this before modifying code when you need nearby decisions, references,
relationships, and branch-diff context. Example:
`{"file_path": "src/auth.py", "line_ranges": ["40:80"], "parent_branch": "main"}`.
Line ranges use inclusive `start:end` notation and the report is read-only.
""",
    "get_entity_references": """Find where a taxonomy entity is referenced in repository specifications.

Use this to discover the impact surface of an entity before changing it.
Example: `{"entity_name": "AuthService", "parent_branch": "main"}`.
Use the returned paths and spans to gather only the relevant source context.
""",
    "get_entity_decisions": """Find decisions recorded for a taxonomy entity.

Use this before proposing a design change so existing rationale is not lost.
Example: `{"entity_name": "AuthService", "parent_branch": "main"}`.
The result summarizes decision text and its source locations.
""",
    "get_entity_relationships": """Show the relationships connected to a taxonomy entity.

Use this to understand dependencies and update related entities consistently.
Example: `{"entity_name": "AuthService", "parent_branch": "main"}`.
The report covers both incoming and outgoing relationships found in indexed
specifications.
""",
    "get_invariants": """Collect invariants currently declared by the repository.

Use this before implementation or review when behavior must preserve explicit
constraints. Example: `{"parent_branch": "main"}`. The report is useful as a
checklist for implementation and validation.
""",
    "get_current_decisions": """Collect the repository's current design decisions.

Use this to orient yourself before planning a change or resolving ambiguity.
Example: `{"parent_branch": "main"}`. Prefer this broad report for initial
orientation, then use `get_entity_decisions` for a focused entity.
""",
    "get_codebase_state": """Generate a durable snapshot of codebase entities, decisions, and relationships.

Use this to establish current state before planning work or comparing later
changes. Example: `{"branch_name": "feature/auth", "parent_branch": "main"}`.
The tool writes a state artifact and returns its rendered contents.
""",
    "synthesize_current_state": """Generate a current-state specification from repository evidence.

Use this when the repository needs a refreshed high-level state document before
planning. Example: `{"branch_name": "feature/auth", "parent_branch": "main"}`.
The tool writes a specification artifact and returns its rendered contents.
""",
    "create_architecture_specification": """Create an architecture-specification template for a work item.

Use this to document component boundaries and architecture decisions before
implementation. Example: `{"entity_types": ["component", "service"], "work_item_name": "auth"}`.
Use the repository taxonomy for `entity_types`; validate the completed YAML
with `validate_architecture_specification`.
""",
    "create_implementation_specification": """Create an implementation-specification template for a work item.

Use this after architecture decisions are available and before coding. Example:
`{"work_item_name": "auth", "architecture_specification_path": "docs/specs/auth/architecture.yaml"}`.
Validate the completed document with `validate_implementation_specification`.
""",
    "create_system_specification": """Create a system-specification template for a work item.

Use this to describe user-visible behavior, boundaries, and system invariants
before implementation. Example: `{"work_item_name": "auth"}`. Validate the
completed YAML with `validate_system_specification`.
""",
    "create_system_map_specification": """Create a system-map specification template for a work item.

Use this to map the system's major entities and relationships during feature
planning. Example: `{"work_item_name": "auth"}`. This is a planning artifact,
not a runtime diagram renderer.
""",
    "create_feature_pr_specification": """Create a feature pull-request specification template.

Use this to define the intended feature scope and acceptance evidence before
implementation. Example: `{"work_item_name": "auth"}`. Pair it with the
feature planning workflow and validate related artifacts before review.
""",
    "start_planning_feature": """Initialize the feature-planning workflow for a work item.

Use this as the entry point when starting a new feature from a clean planning
context. Example: `{"work_item_name": "auth"}`. It creates the planning
artifacts and returns guidance for the next steps.
""",
    "create_pr_specification": """Create a proposed pull-request specification template.

Use this when the implementation is ready to describe its reviewable change
set. Example: `{"work_item_name": "auth"}`. Complete it from the actual
implementation and validate it with `validate_pr_specification`.
""",
    "create_plan_diff_specification": """Create a plan-diff specification by comparing a feature plan with change logs.

Use this to identify planned work that is missing, changed, or already covered
before implementation begins. Example:
`{"feature_plan_specification_path": "docs/specs/auth/feature.yaml", "changelog_paths": ["docs/changelog/auth.yaml"]}`.
""",
    "search_proposed_prs": """Search indexed proposed pull-request specifications.

Use this to find prior or related planned changes before creating a duplicate
design. Example: `{"query": "authentication", "limit": 5}`. Start with a
small limit, then inspect a promising result with `show_proposed_pr`.
""",
    "show_proposed_pr": """Read one proposed pull-request specification by number.

Use this after `search_proposed_prs` identifies a relevant proposal. Example:
`{"pr_number": 42}`. The result contains the full stored specification.
""",
    "validate_architecture_specification": """Validate a completed architecture specification.

Use this before relying on architecture decisions or handing work to an
implementer. Example: `{"architecture_specification_yaml": "...", "entity_types": ["service"], "work_item_name": "auth"}`.
Pass YAML content directly and fix every reported issue.
""",
    "validate_implementation_specification": """Validate a completed implementation specification.

Use this before coding or review to ensure implementation steps trace to the
architecture and work item. Example:
`{"implementation_specification_yaml": "...", "work_item_name": "auth"}`.
Include `architecture_specification_path` when the implementation references
an architecture document.
""",
    "validate_system_specification": """Validate a completed system specification.

Use this before implementation to check behavior, scope, and required system
evidence. Example: `{"system_specification_yaml": "...", "work_item_name": "auth"}`.
Pass the complete YAML content directly.
""",
    "validate_pr_specification": """Validate a completed proposed pull-request specification.

Use this before requesting review to check scope, evidence, and relationships.
Example: `{"pr_specification_yaml": "...", "work_item_name": "auth"}`.
Pass the complete YAML document directly.
""",
    "get_blame_view": """Inspect line-level ownership and provenance for repository files.

Use this when a change needs historical context or you need to locate the
specification/source evidence behind a line. Example:
`{"file_path": "src/auth.py", "branch_name": "feature/auth", "parent_branch": "main"}`.
Omit `file_path` to get the repository tree and select a file afterward.
""",
}


def _tool_help(tool_name: str, requested: bool) -> str | None:
    if not requested:
        return None
    return _TOOL_HELP[tool_name]


def _required_tool_value[ToolValue](value: ToolValue | None, name: str) -> ToolValue:
    if value is None:
        raise ValueError(f"{name} is required unless help=true")
    return value


def build_server() -> Any:
    if FastMCP is None:
        raise RuntimeError(
            "The 'mcp' package is required to run the powdrr-lift MCP server."
        )

    server: Any = FastMCP("powdrr-lift")

    @server.tool()
    def init_change_log_template(
        branch_name: str | None = None,
        output_path: str | None = None,
        repo_root: str | None = None,
        default_branch: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("init_change_log_template", help):
            return help_text
        repo_root_path = resolve_repo_root(repo_root)
        rendered_output_path = create_change_log_template(
            branch_name=branch_name,
            output_path=None if output_path is None else Path(output_path),
            repo_root=repo_root_path,
            default_branch=default_branch,
        )
        return rendered_output_path.read_text(encoding="utf-8")

    @server.tool()
    def init_change_log_template_from_plan_diff(
        branch_name: str | None = None,
        plan_diff_path: str | None = None,
        output_path: str | None = None,
        repo_root: str | None = None,
        default_branch: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("init_change_log_template_from_plan_diff", help):
            return help_text
        branch_name = _required_tool_value(branch_name, "branch_name")
        plan_diff_path = _required_tool_value(plan_diff_path, "plan_diff_path")
        repo_root_path = resolve_repo_root(repo_root)
        rendered_output_path = create_change_log_template_from_plan_diff(
            branch_name=branch_name,
            plan_diff_path=Path(plan_diff_path),
            output_path=None if output_path is None else Path(output_path),
            repo_root=repo_root_path,
            default_branch=default_branch,
        )
        return rendered_output_path.read_text(encoding="utf-8")

    @server.tool()
    def evaluate_pr_against_changelog(
        proposed_change_log_yaml: str | None = None,
        branch_name: str | None = None,
        repo_root: str | None = None,
        default_branch: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("evaluate_pr_against_changelog", help):
            return help_text
        proposed_change_log_yaml = _required_tool_value(
            proposed_change_log_yaml, "proposed_change_log_yaml"
        )
        repo_root_path = resolve_repo_root(repo_root)
        report_yaml = validate_change_log_yaml(
            proposed_change_log_yaml,
            branch_name=branch_name,
            repo_root=repo_root_path,
            default_branch=default_branch,
        )
        parse_validation_report(report_yaml)
        return report_yaml

    @server.tool()
    def get_edit_context(
        file_path: str | None = None,
        line_ranges: list[str] | None = None,
        parent_branch: str | None = None,
        branch_name: str | None = None,
        repo_root: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("get_edit_context", help):
            return help_text
        file_path = _required_tool_value(file_path, "file_path")
        line_ranges = _required_tool_value(line_ranges, "line_ranges")
        parent_branch = _required_tool_value(parent_branch, "parent_branch")
        repo_root_path = resolve_repo_root(repo_root)
        report = lookup_edit_context(
            file_path,
            parse_line_ranges(line_ranges),
            branch_name=branch_name,
            parent_branch=parent_branch,
            repo_root=repo_root_path,
        )
        return render_edit_context_report(report)

    @server.tool()
    def get_entity_references(
        entity_name: str | None = None,
        parent_branch: str | None = None,
        branch_name: str | None = None,
        repo_root: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("get_entity_references", help):
            return help_text
        entity_name = _required_tool_value(entity_name, "entity_name")
        parent_branch = _required_tool_value(parent_branch, "parent_branch")
        repo_root_path = resolve_repo_root(repo_root)
        report = lookup_entity_references(
            entity_name,
            branch_name=branch_name,
            parent_branch=parent_branch,
            repo_root=repo_root_path,
        )
        return render_entity_reference_report(report)

    @server.tool()
    def get_entity_decisions(
        entity_name: str | None = None,
        parent_branch: str | None = None,
        branch_name: str | None = None,
        repo_root: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("get_entity_decisions", help):
            return help_text
        entity_name = _required_tool_value(entity_name, "entity_name")
        parent_branch = _required_tool_value(parent_branch, "parent_branch")
        repo_root_path = resolve_repo_root(repo_root)
        report = lookup_entity_decisions(
            entity_name,
            branch_name=branch_name,
            parent_branch=parent_branch,
            repo_root=repo_root_path,
        )
        return render_entity_decision_report(report)

    @server.tool()
    def get_entity_relationships(
        entity_name: str | None = None,
        parent_branch: str | None = None,
        branch_name: str | None = None,
        repo_root: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("get_entity_relationships", help):
            return help_text
        entity_name = _required_tool_value(entity_name, "entity_name")
        parent_branch = _required_tool_value(parent_branch, "parent_branch")
        repo_root_path = resolve_repo_root(repo_root)
        report = lookup_entity_relationships(
            entity_name,
            branch_name=branch_name,
            parent_branch=parent_branch,
            repo_root=repo_root_path,
        )
        return render_entity_relationship_report(report)

    @server.tool()
    def get_invariants(
        parent_branch: str | None = None,
        branch_name: str | None = None,
        repo_root: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("get_invariants", help):
            return help_text
        parent_branch = _required_tool_value(parent_branch, "parent_branch")
        repo_root_path = resolve_repo_root(repo_root)
        report = build_invariants_report(
            branch_name=branch_name,
            parent_branch=parent_branch,
            repo_root=repo_root_path,
        )
        return render_invariants_report(report)

    @server.tool()
    def get_current_decisions(
        parent_branch: str | None = None,
        branch_name: str | None = None,
        repo_root: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("get_current_decisions", help):
            return help_text
        parent_branch = _required_tool_value(parent_branch, "parent_branch")
        repo_root_path = resolve_repo_root(repo_root)
        report = build_current_decisions_report(
            branch_name=branch_name,
            parent_branch=parent_branch,
            repo_root=repo_root_path,
        )
        return render_current_decisions_report(report)

    @server.tool()
    def get_codebase_state(
        branch_name: str | None = None,
        parent_branch: str | None = None,
        output_path: str | None = None,
        repo_root: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("get_codebase_state", help):
            return help_text
        repo_root_path = resolve_repo_root(repo_root)
        rendered_output_path = create_codebase_state(
            branch_name=branch_name,
            output_path=(
                codebase_state_default_output_path(repo_root_path)
                if output_path is None
                else Path(output_path)
            ),
            parent_branch=parent_branch,
            repo_root=repo_root_path,
        )
        return rendered_output_path.read_text(encoding="utf-8")

    @server.tool()
    def synthesize_current_state(
        branch_name: str | None = None,
        parent_branch: str | None = None,
        output_path: str | None = None,
        repo_root: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("synthesize_current_state", help):
            return help_text
        repo_root_path = resolve_repo_root(repo_root)
        rendered_output_path = create_current_state_specification(
            branch_name=branch_name,
            output_path=(
                current_state_specification_default_output_path(repo_root_path)
                if output_path is None
                else Path(output_path)
            ),
            parent_branch=parent_branch,
            repo_root=repo_root_path,
        )
        return rendered_output_path.read_text(encoding="utf-8")

    @server.tool()
    def create_architecture_specification(
        entity_types: list[str] | None = None,
        work_item_name: str | None = None,
        output_path: str | None = None,
        title: str | None = None,
        repo_root: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("create_architecture_specification", help):
            return help_text
        entity_types = _required_tool_value(entity_types, "entity_types")
        work_item_name = _required_tool_value(work_item_name, "work_item_name")
        repo_root_path = resolve_repo_root(repo_root)
        rendered_output_path = create_architecture_specification_template(
            entity_types,
            work_item_name=work_item_name,
            output_path=(
                architecture_specification_default_output_path(
                    work_item_name,
                    repo_root_path,
                )
                if output_path is None
                else Path(output_path)
            ),
            repo_root=repo_root_path,
            title=title,
        )
        return rendered_output_path.read_text(encoding="utf-8")

    @server.tool()
    def create_implementation_specification(
        work_item_name: str | None = None,
        architecture_specification_path: str | None = None,
        output_path: str | None = None,
        title: str | None = None,
        repo_root: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("create_implementation_specification", help):
            return help_text
        work_item_name = _required_tool_value(work_item_name, "work_item_name")
        repo_root_path = resolve_repo_root(repo_root)
        rendered_output_path = create_implementation_specification_template(
            architecture_specification_path=architecture_specification_path,
            work_item_name=work_item_name,
            output_path=(
                implementation_specification_default_output_path(
                    work_item_name,
                    repo_root_path,
                )
                if output_path is None
                else Path(output_path)
            ),
            repo_root=repo_root_path,
            title=title,
        )
        return rendered_output_path.read_text(encoding="utf-8")

    @server.tool()
    def create_system_specification(
        work_item_name: str | None = None,
        output_path: str | None = None,
        title: str | None = None,
        repo_root: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("create_system_specification", help):
            return help_text
        work_item_name = _required_tool_value(work_item_name, "work_item_name")
        repo_root_path = resolve_repo_root(repo_root)
        rendered_output_path = create_system_specification_template(
            work_item_name=work_item_name,
            output_path=(
                system_specification_default_output_path(
                    work_item_name,
                    repo_root_path,
                )
                if output_path is None
                else Path(output_path)
            ),
            repo_root=repo_root_path,
            title=title,
        )
        return rendered_output_path.read_text(encoding="utf-8")

    @server.tool()
    def create_system_map_specification(
        work_item_name: str | None = None,
        output_path: str | None = None,
        title: str | None = None,
        repo_root: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("create_system_map_specification", help):
            return help_text
        work_item_name = _required_tool_value(work_item_name, "work_item_name")
        repo_root_path = resolve_repo_root(repo_root)
        rendered_output_path = create_system_map_specification_template(
            work_item_name=work_item_name,
            output_path=(
                system_map_specification_default_output_path(
                    work_item_name,
                    repo_root_path,
                )
                if output_path is None
                else Path(output_path)
            ),
            repo_root=repo_root_path,
            title=title,
        )
        return rendered_output_path.read_text(encoding="utf-8")

    @server.tool()
    def create_feature_pr_specification(
        work_item_name: str | None = None,
        output_path: str | None = None,
        title: str | None = None,
        repo_root: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("create_feature_pr_specification", help):
            return help_text
        work_item_name = _required_tool_value(work_item_name, "work_item_name")
        repo_root_path = resolve_repo_root(repo_root)
        rendered_output_path = create_feature_pr_specification_template(
            work_item_name=work_item_name,
            output_path=(
                feature_pr_specification_default_output_path(
                    work_item_name,
                    repo_root_path,
                )
                if output_path is None
                else Path(output_path)
            ),
            repo_root=repo_root_path,
            title=title,
        )
        return rendered_output_path.read_text(encoding="utf-8")

    @server.tool()
    def start_planning_feature(
        work_item_name: str | None = None,
        repo_root: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("start_planning_feature", help):
            return help_text
        work_item_name = _required_tool_value(work_item_name, "work_item_name")
        repo_root_path = resolve_repo_root(repo_root)
        return _start_planning_feature(
            work_item_name=work_item_name,
            repo_root=repo_root_path,
        )

    @server.tool()
    def create_pr_specification(
        work_item_name: str | None = None,
        output_path: str | None = None,
        repo_root: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("create_pr_specification", help):
            return help_text
        work_item_name = _required_tool_value(work_item_name, "work_item_name")
        repo_root_path = resolve_repo_root(repo_root)
        rendered_output_path = create_pr_specification_template(
            work_item_name=work_item_name,
            output_path=(
                pr_specification_default_output_path(
                    work_item_name,
                    repo_root_path,
                )
                if output_path is None
                else Path(output_path)
            ),
            repo_root=repo_root_path,
        )
        return rendered_output_path.read_text(encoding="utf-8")

    @server.tool()
    def create_plan_diff_specification(
        feature_plan_specification_path: str | None = None,
        changelog_paths: list[str] | None = None,
        output_path: str | None = None,
        repo_root: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("create_plan_diff_specification", help):
            return help_text
        feature_plan_specification_path = _required_tool_value(
            feature_plan_specification_path, "feature_plan_specification_path"
        )
        changelog_paths = _required_tool_value(changelog_paths, "changelog_paths")
        repo_root_path = resolve_repo_root(repo_root)
        rendered_output_path = _create_plan_diff_specification(
            feature_plan_specification_path=feature_plan_specification_path,
            changelog_paths=changelog_paths,
            output_path=(
                plan_diff_specification_default_output_path(
                    feature_plan_specification_path,
                    repo_root_path,
                )
                if output_path is None
                else Path(output_path)
            ),
            repo_root=repo_root_path,
        )
        return rendered_output_path.read_text(encoding="utf-8")

    @server.tool()
    def search_proposed_prs(
        query: str | None = None,
        limit: int = 10,
        repo_root: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("search_proposed_prs", help):
            return help_text
        query = _required_tool_value(query, "query")
        repo_root_path = resolve_repo_root(repo_root)
        report = search_proposed_pr_specifications(
            query,
            repo_root=repo_root_path,
            limit=limit,
        )
        return render_proposed_pr_search_report(report)

    @server.tool()
    def show_proposed_pr(
        pr_number: int | None = None,
        repo_root: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("show_proposed_pr", help):
            return help_text
        pr_number = _required_tool_value(pr_number, "pr_number")
        repo_root_path = resolve_repo_root(repo_root)
        return show_proposed_pr_specification(pr_number, repo_root=repo_root_path)

    @server.tool()
    def validate_architecture_specification(
        architecture_specification_yaml: str | None = None,
        entity_types: list[str] | None = None,
        work_item_name: str | None = None,
        repo_root: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("validate_architecture_specification", help):
            return help_text
        architecture_specification_yaml = _required_tool_value(
            architecture_specification_yaml, "architecture_specification_yaml"
        )
        entity_types = _required_tool_value(entity_types, "entity_types")
        work_item_name = _required_tool_value(work_item_name, "work_item_name")
        repo_root_path = resolve_repo_root(repo_root)
        return validate_architecture_specification_yaml(
            architecture_specification_yaml,
            entity_types=entity_types,
            work_item_name=work_item_name,
            repo_root=repo_root_path,
        )

    @server.tool()
    def validate_implementation_specification(
        implementation_specification_yaml: str | None = None,
        work_item_name: str | None = None,
        architecture_specification_path: str | None = None,
        repo_root: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("validate_implementation_specification", help):
            return help_text
        implementation_specification_yaml = _required_tool_value(
            implementation_specification_yaml, "implementation_specification_yaml"
        )
        work_item_name = _required_tool_value(work_item_name, "work_item_name")
        repo_root_path = resolve_repo_root(repo_root)
        return validate_implementation_specification_yaml(
            implementation_specification_yaml,
            architecture_specification_path=architecture_specification_path,
            work_item_name=work_item_name,
            repo_root=repo_root_path,
        )

    @server.tool()
    def validate_system_specification(
        system_specification_yaml: str | None = None,
        work_item_name: str | None = None,
        repo_root: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("validate_system_specification", help):
            return help_text
        system_specification_yaml = _required_tool_value(
            system_specification_yaml, "system_specification_yaml"
        )
        work_item_name = _required_tool_value(work_item_name, "work_item_name")
        repo_root_path = resolve_repo_root(repo_root)
        return validate_system_specification_yaml(
            system_specification_yaml,
            work_item_name=work_item_name,
            repo_root=repo_root_path,
        )

    @server.tool()
    def validate_pr_specification(
        pr_specification_yaml: str | None = None,
        work_item_name: str | None = None,
        repo_root: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("validate_pr_specification", help):
            return help_text
        pr_specification_yaml = _required_tool_value(
            pr_specification_yaml, "pr_specification_yaml"
        )
        work_item_name = _required_tool_value(work_item_name, "work_item_name")
        repo_root_path = resolve_repo_root(repo_root)
        return validate_pr_specification_yaml(
            pr_specification_yaml,
            work_item_name=work_item_name,
            repo_root=repo_root_path,
        )

    @server.tool()
    def get_blame_view(
        file_path: str | None = None,
        branch_name: str | None = None,
        parent_branch: str | None = None,
        repo_root: str | None = None,
        help: bool = False,
    ) -> str:
        if help_text := _tool_help("get_blame_view", help):
            return help_text
        repo_root_path = resolve_repo_root(repo_root)
        state = build_blame_view_state(
            repo_root=repo_root_path,
            branch_name=branch_name,
            parent_branch=parent_branch,
            selected_file=file_path,
        )
        return json.dumps(blame_view_state_to_data(state), ensure_ascii=False)

    return server


def main() -> int:
    server = build_server()
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
