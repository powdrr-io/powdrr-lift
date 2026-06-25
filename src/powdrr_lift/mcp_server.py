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


def _load_fastmcp() -> Any:
    try:
        from mcp.server.fastmcp import FastMCP as fastmcp
    except ImportError:  # pragma: no cover
        return None

    return fastmcp


FastMCP = _load_fastmcp()


def build_server() -> Any:
    if FastMCP is None:
        raise RuntimeError(
            "The 'mcp' package is required to run the powdrr-lift MCP server."
        )

    server: Any = FastMCP("powdrr-lift")

    @server.tool()
    def init_change_log_template(
        branch_name: str,
        output_path: str | None = None,
        repo_root: str | None = None,
        default_branch: str | None = None,
    ) -> str:
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
        branch_name: str,
        plan_diff_path: str,
        output_path: str | None = None,
        repo_root: str | None = None,
        default_branch: str | None = None,
    ) -> str:
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
        proposed_change_log_yaml: str,
        branch_name: str,
        repo_root: str | None = None,
        default_branch: str | None = None,
    ) -> str:
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
        file_path: str,
        line_ranges: list[str],
        parent_branch: str,
        branch_name: str | None = None,
        repo_root: str | None = None,
    ) -> str:
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
        entity_name: str,
        parent_branch: str,
        branch_name: str | None = None,
        repo_root: str | None = None,
    ) -> str:
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
        entity_name: str,
        parent_branch: str,
        branch_name: str | None = None,
        repo_root: str | None = None,
    ) -> str:
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
        entity_name: str,
        parent_branch: str,
        branch_name: str | None = None,
        repo_root: str | None = None,
    ) -> str:
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
        parent_branch: str,
        branch_name: str | None = None,
        repo_root: str | None = None,
    ) -> str:
        repo_root_path = resolve_repo_root(repo_root)
        report = build_invariants_report(
            branch_name=branch_name,
            parent_branch=parent_branch,
            repo_root=repo_root_path,
        )
        return render_invariants_report(report)

    @server.tool()
    def get_current_decisions(
        parent_branch: str,
        branch_name: str | None = None,
        repo_root: str | None = None,
    ) -> str:
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
    ) -> str:
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
    ) -> str:
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
        entity_types: list[str],
        work_item_name: str,
        output_path: str | None = None,
        title: str | None = None,
        repo_root: str | None = None,
    ) -> str:
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
        work_item_name: str,
        architecture_specification_path: str | None = None,
        output_path: str | None = None,
        title: str | None = None,
        repo_root: str | None = None,
    ) -> str:
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
        work_item_name: str,
        output_path: str | None = None,
        title: str | None = None,
        repo_root: str | None = None,
    ) -> str:
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
        work_item_name: str,
        output_path: str | None = None,
        title: str | None = None,
        repo_root: str | None = None,
    ) -> str:
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
        work_item_name: str,
        output_path: str | None = None,
        title: str | None = None,
        repo_root: str | None = None,
    ) -> str:
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
        work_item_name: str,
        repo_root: str | None = None,
    ) -> str:
        repo_root_path = resolve_repo_root(repo_root)
        return _start_planning_feature(
            work_item_name=work_item_name,
            repo_root=repo_root_path,
        )

    @server.tool()
    def create_pr_specification(
        work_item_name: str,
        output_path: str | None = None,
        repo_root: str | None = None,
    ) -> str:
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
        feature_plan_specification_path: str,
        changelog_paths: list[str],
        output_path: str | None = None,
        repo_root: str | None = None,
    ) -> str:
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
        query: str,
        limit: int = 10,
        repo_root: str | None = None,
    ) -> str:
        repo_root_path = resolve_repo_root(repo_root)
        report = search_proposed_pr_specifications(
            query,
            repo_root=repo_root_path,
            limit=limit,
        )
        return render_proposed_pr_search_report(report)

    @server.tool()
    def show_proposed_pr(
        pr_number: int,
        repo_root: str | None = None,
    ) -> str:
        repo_root_path = resolve_repo_root(repo_root)
        return show_proposed_pr_specification(pr_number, repo_root=repo_root_path)

    @server.tool()
    def validate_architecture_specification(
        architecture_specification_yaml: str,
        entity_types: list[str],
        work_item_name: str,
        repo_root: str | None = None,
    ) -> str:
        repo_root_path = resolve_repo_root(repo_root)
        return validate_architecture_specification_yaml(
            architecture_specification_yaml,
            entity_types=entity_types,
            work_item_name=work_item_name,
            repo_root=repo_root_path,
        )

    @server.tool()
    def validate_implementation_specification(
        implementation_specification_yaml: str,
        work_item_name: str,
        architecture_specification_path: str | None = None,
        repo_root: str | None = None,
    ) -> str:
        repo_root_path = resolve_repo_root(repo_root)
        return validate_implementation_specification_yaml(
            implementation_specification_yaml,
            architecture_specification_path=architecture_specification_path,
            work_item_name=work_item_name,
            repo_root=repo_root_path,
        )

    @server.tool()
    def validate_system_specification(
        system_specification_yaml: str,
        work_item_name: str,
        repo_root: str | None = None,
    ) -> str:
        repo_root_path = resolve_repo_root(repo_root)
        return validate_system_specification_yaml(
            system_specification_yaml,
            work_item_name=work_item_name,
            repo_root=repo_root_path,
        )

    @server.tool()
    def validate_pr_specification(
        pr_specification_yaml: str,
        work_item_name: str,
        repo_root: str | None = None,
    ) -> str:
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
    ) -> str:
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
