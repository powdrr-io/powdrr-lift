from typing import Any

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
from powdrr_lift.core.code_index import (
    BranchState,
    code_index_db_path,
    lookup_code_provenance,
    lookup_code_provenance_span,
    refresh_code_index,
)
from powdrr_lift.core.edit_context import (
    EditContextLine,
    EditContextRange,
    EditContextReport,
    lookup_edit_context,
    parse_line_range,
    parse_line_ranges,
    render_edit_context_report,
)
from powdrr_lift.core.entity_context import (
    EntityReferenceReport,
    EntityRelationshipReport,
    lookup_entity_references,
    lookup_entity_relationships,
    render_entity_reference_report,
    render_entity_relationship_report,
)
from powdrr_lift.core.index import (
    ChangelogDocument,
    EntityGraph,
    EntityOccurrence,
    ProvenanceRecord,
    RelationshipOccurrence,
    SourceIndex,
    build_changelog_index,
    build_changelog_index_at_ref,
)
from powdrr_lift.core.pr_analysis import (
    BranchDiffEntry,
    collect_branch_diff_entries,
    resolve_default_branch,
    resolve_repo_root,
)
from powdrr_lift.core.schemas import (
    Change,
    ChangeLog,
    Decision,
    Entity,
    Intent,
    RelationshipChange,
    Span,
    parse_change_log,
)
from powdrr_lift.core.template import (
    create_change_log_template,
    render_change_log_template,
)

__all__ = [
    "BranchDiffEntry",
    "BranchState",
    "BlameChunk",
    "BlameFileView",
    "BlameLine",
    "BlameProvenance",
    "BlameViewState",
    "EditContextLine",
    "EditContextRange",
    "EditContextReport",
    "EntityReferenceReport",
    "EntityRelationshipReport",
    "ChangelogDocument",
    "Change",
    "ChangeLog",
    "Decision",
    "Entity",
    "EntityGraph",
    "EntityOccurrence",
    "Intent",
    "code_index_db_path",
    "ProvenanceRecord",
    "RelationshipChange",
    "RelationshipOccurrence",
    "RepoTreeNode",
    "Span",
    "SourceIndex",
    "ValidationIssue",
    "ValidationReport",
    "blame_file_view_to_data",
    "blame_tree_node_to_data",
    "blame_view_state_to_data",
    "build_changelog_index",
    "build_changelog_index_at_ref",
    "build_blame_file_view",
    "build_blame_view_state",
    "build_repo_tree",
    "collect_branch_diff_entries",
    "create_change_log_template",
    "lookup_code_provenance",
    "lookup_code_provenance_span",
    "lookup_entity_references",
    "lookup_entity_relationships",
    "lookup_edit_context",
    "parse_change_log",
    "parse_line_range",
    "parse_line_ranges",
    "parse_validation_report",
    "render_change_log_template",
    "render_edit_context_report",
    "render_entity_reference_report",
    "render_entity_relationship_report",
    "resolve_default_branch",
    "resolve_repo_root",
    "refresh_code_index",
    "build_validation_report",
    "validate_change_log_yaml",
]


def __getattr__(name: str) -> Any:
    if name in {
        "ValidationIssue",
        "ValidationReport",
        "build_validation_report",
        "parse_validation_report",
        "validate_change_log_yaml",
    }:
        from powdrr_lift.core.validate import (
            ValidationIssue,
            ValidationReport,
            build_validation_report,
            parse_validation_report,
            validate_change_log_yaml,
        )

        return {
            "ValidationIssue": ValidationIssue,
            "ValidationReport": ValidationReport,
            "build_validation_report": build_validation_report,
            "parse_validation_report": parse_validation_report,
            "validate_change_log_yaml": validate_change_log_yaml,
        }[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
