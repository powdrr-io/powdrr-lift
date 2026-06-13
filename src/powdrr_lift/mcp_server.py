from __future__ import annotations

from pathlib import Path
from typing import Any

from powdrr_lift.core import (
    create_change_log_template,
    lookup_edit_context,
    parse_line_ranges,
    parse_validation_report,
    render_edit_context_report,
    resolve_repo_root,
    validate_change_log_yaml,
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

    return server


def main() -> int:
    server = build_server()
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
