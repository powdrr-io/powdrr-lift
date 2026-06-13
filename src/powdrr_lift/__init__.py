from typing import Any

from powdrr_lift.change_log_parser import (
    Change,
    ChangeLog,
    Decision,
    Entity,
    Intent,
    RelationshipChange,
    Span,
    parse_change_log,
)

__all__ = [
    "Change",
    "ChangeLog",
    "Decision",
    "Entity",
    "Intent",
    "ChangelogDocument",
    "BranchState",
    "BlameChunk",
    "BlameFileView",
    "BlameLine",
    "BlameProvenance",
    "BlameViewState",
    "EditContextLine",
    "EditContextRange",
    "EditContextReport",
    "code_index_db_path",
    "lookup_code_provenance",
    "lookup_code_provenance_span",
    "lookup_edit_context",
    "ProvenanceRecord",
    "RelationshipChange",
    "RepoTreeNode",
    "Span",
    "create_change_log_template",
    "build_changelog_index",
    "build_blame_file_view",
    "build_blame_view_state",
    "build_repo_tree",
    "refresh_code_index",
    "parse_change_log",
    "parse_line_range",
    "parse_line_ranges",
    "blame_file_view_to_data",
    "blame_tree_node_to_data",
    "blame_view_state_to_data",
    "build_validation_report",
    "parse_validation_report",
    "render_edit_context_report",
    "validate_change_log_yaml",
    "ValidationIssue",
    "ValidationReport",
]


def __getattr__(name: str) -> Any:
    if name == "create_change_log_template":
        from powdrr_lift.change_log_template import create_change_log_template

        return create_change_log_template

    if name == "build_changelog_index":
        from powdrr_lift.core.index import build_changelog_index

        return build_changelog_index

    if name in {
        "BranchState",
        "code_index_db_path",
        "lookup_code_provenance",
        "lookup_code_provenance_span",
        "refresh_code_index",
    }:
        from powdrr_lift.core.code_index import (
            BranchState,
            code_index_db_path,
            lookup_code_provenance,
            lookup_code_provenance_span,
            refresh_code_index,
        )

        return {
            "BranchState": BranchState,
            "code_index_db_path": code_index_db_path,
            "lookup_code_provenance": lookup_code_provenance,
            "lookup_code_provenance_span": lookup_code_provenance_span,
            "refresh_code_index": refresh_code_index,
        }[name]

    if name in {
        "BlameChunk",
        "BlameFileView",
        "BlameLine",
        "BlameProvenance",
        "BlameViewState",
        "RepoTreeNode",
        "blame_file_view_to_data",
        "blame_tree_node_to_data",
        "blame_view_state_to_data",
        "build_blame_file_view",
        "build_blame_view_state",
        "build_repo_tree",
    }:
        from powdrr_lift.core.blame_view import (
            BlameChunk,
            BlameFileView,
            BlameLine,
            BlameProvenance,
            BlameViewState,
            RepoTreeNode,
            blame_file_view_to_data,
            blame_tree_node_to_data,
            blame_view_state_to_data,
            build_blame_file_view,
            build_blame_view_state,
            build_repo_tree,
        )

        return {
            "BlameChunk": BlameChunk,
            "BlameFileView": BlameFileView,
            "BlameLine": BlameLine,
            "BlameProvenance": BlameProvenance,
            "BlameViewState": BlameViewState,
            "RepoTreeNode": RepoTreeNode,
            "blame_file_view_to_data": blame_file_view_to_data,
            "blame_tree_node_to_data": blame_tree_node_to_data,
            "blame_view_state_to_data": blame_view_state_to_data,
            "build_blame_file_view": build_blame_file_view,
            "build_blame_view_state": build_blame_view_state,
            "build_repo_tree": build_repo_tree,
        }[name]

    if name in {
        "ChangelogDocument",
        "ProvenanceRecord",
        "SourceIndex",
    }:
        from powdrr_lift.core.index import (
            ChangelogDocument,
            ProvenanceRecord,
            SourceIndex,
        )

        return {
            "ChangelogDocument": ChangelogDocument,
            "ProvenanceRecord": ProvenanceRecord,
            "SourceIndex": SourceIndex,
        }[name]

    if name in {
        "EditContextLine",
        "EditContextRange",
        "EditContextReport",
    }:
        from powdrr_lift.core.edit_context import (
            EditContextLine,
            EditContextRange,
            EditContextReport,
        )

        return {
            "EditContextLine": EditContextLine,
            "EditContextRange": EditContextRange,
            "EditContextReport": EditContextReport,
        }[name]

    if name in {
        "lookup_edit_context",
        "parse_line_range",
        "parse_line_ranges",
        "render_edit_context_report",
    }:
        from powdrr_lift.core.edit_context import (
            lookup_edit_context,
            parse_line_range,
            parse_line_ranges,
            render_edit_context_report,
        )

        return {
            "lookup_edit_context": lookup_edit_context,
            "parse_line_range": parse_line_range,
            "parse_line_ranges": parse_line_ranges,
            "render_edit_context_report": render_edit_context_report,
        }[name]

    if name in {
        "build_validation_report",
        "parse_validation_report",
        "validate_change_log_yaml",
        "ValidationIssue",
        "ValidationReport",
    }:
        from powdrr_lift.change_log_validation import (
            ValidationIssue,
            ValidationReport,
            build_validation_report,
            parse_validation_report,
            validate_change_log_yaml,
        )

        return {
            "build_validation_report": build_validation_report,
            "parse_validation_report": parse_validation_report,
            "validate_change_log_yaml": validate_change_log_yaml,
            "ValidationIssue": ValidationIssue,
            "ValidationReport": ValidationReport,
        }[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
