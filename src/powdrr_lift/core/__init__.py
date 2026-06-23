from __future__ import annotations

from importlib import import_module
from typing import Any

_ARCHITECTURE = "powdrr_lift.core.architecture_specification"
_BLAME_VIEW = "powdrr_lift.core.blame_view"
_CODEBASE_STATE = "powdrr_lift.core.codebase_state"
_CODE_INDEX = "powdrr_lift.core.code_index"
_EDIT_CONTEXT = "powdrr_lift.core.edit_context"
_ENTITY_CONTEXT = "powdrr_lift.core.entity_context"
_IMPLEMENTATION = "powdrr_lift.core.implementation_specification"
_INDEX = "powdrr_lift.core.index"
_PR_ANALYSIS = "powdrr_lift.core.pr_analysis"
_PR_SPECIFICATION = "powdrr_lift.core.pr_specification"
_SCHEMAS = "powdrr_lift.core.schemas"
_SYSTEM = "powdrr_lift.core.system_specification"
_TEMPLATE = "powdrr_lift.core.template"
_VALIDATE = "powdrr_lift.core.validate"

_EXPORTS: dict[str, str] = {
    "BranchDiffEntry": _PR_ANALYSIS,
    "BranchState": _CODE_INDEX,
    "BlameChunk": _BLAME_VIEW,
    "BlameFileView": _BLAME_VIEW,
    "BlameLine": _BLAME_VIEW,
    "BlameProvenance": _BLAME_VIEW,
    "BlameViewState": _BLAME_VIEW,
    "RepoTreeNode": _BLAME_VIEW,
    "ArchitectureSpecificationValidationIssue": _ARCHITECTURE,
    "ArchitectureSpecificationValidationReport": _ARCHITECTURE,
    "ImplementationSpecificationValidationIssue": _IMPLEMENTATION,
    "ImplementationSpecificationValidationReport": _IMPLEMENTATION,
    "SystemSpecificationValidationIssue": _SYSTEM,
    "SystemSpecificationValidationReport": _SYSTEM,
    "PRSpecificationValidationIssue": _PR_SPECIFICATION,
    "PRSpecificationValidationReport": _PR_SPECIFICATION,
    "ProposedPRSearchReport": _PR_SPECIFICATION,
    "ProposedPRSearchResult": _PR_SPECIFICATION,
    "proposed_pr_specification_path": _PR_SPECIFICATION,
    "CodebaseStateDecision": _CODEBASE_STATE,
    "CodebaseStateEntity": _CODEBASE_STATE,
    "CodebaseStateIntent": _CODEBASE_STATE,
    "CodebaseStateLifecycleItem": _CODEBASE_STATE,
    "CodebaseStateRelationship": _CODEBASE_STATE,
    "CodebaseStateReport": _CODEBASE_STATE,
    "CodebaseStateSource": _CODEBASE_STATE,
    "EditContextLine": _EDIT_CONTEXT,
    "EditContextRange": _EDIT_CONTEXT,
    "EditContextReport": _EDIT_CONTEXT,
    "EntityDecisionOccurrence": _ENTITY_CONTEXT,
    "EntityDecisionReport": _ENTITY_CONTEXT,
    "EntityReferenceReport": _ENTITY_CONTEXT,
    "EntityRelationshipReport": _ENTITY_CONTEXT,
    "ChangelogDocument": _INDEX,
    "ChangeEntity": _SCHEMAS,
    "ChangeEntityRelationship": _SCHEMAS,
    "ChangeFeatureState": _SCHEMAS,
    "ChangeFile": _SCHEMAS,
    "ChangeGuidance": _SCHEMAS,
    "ChangeInvariant": _SCHEMAS,
    "ChangeLog": _SCHEMAS,
    "ChangeProposedPRState": _SCHEMAS,
    "Decision": _SCHEMAS,
    "EntityGraph": _INDEX,
    "EntityOccurrence": _INDEX,
    "Intent": _SCHEMAS,
    "RelatedSection": _SCHEMAS,
    "Span": _SCHEMAS,
    "SourceIndex": _INDEX,
    "ProvenanceRecord": _INDEX,
    "RelationshipOccurrence": _INDEX,
    "ValidationIssue": _VALIDATE,
    "ValidationReport": _VALIDATE,
    "architecture_specification_default_output_path": _ARCHITECTURE,
    "blame_file_view_to_data": _BLAME_VIEW,
    "blame_tree_node_to_data": _BLAME_VIEW,
    "blame_view_state_to_data": _BLAME_VIEW,
    "build_architecture_specification_validation_report": _ARCHITECTURE,
    "build_blame_file_view": _BLAME_VIEW,
    "build_blame_view_state": _BLAME_VIEW,
    "build_codebase_state_report": _CODEBASE_STATE,
    "build_current_decisions_report": _CODEBASE_STATE,
    "build_invariants_report": _CODEBASE_STATE,
    "build_implementation_specification_validation_report": _IMPLEMENTATION,
    "build_pr_specification_validation_report": _PR_SPECIFICATION,
    "build_system_specification_validation_report": _SYSTEM,
    "build_changelog_index": _INDEX,
    "build_changelog_index_at_ref": _INDEX,
    "build_repo_tree": _BLAME_VIEW,
    "code_index_db_path": _CODE_INDEX,
    "codebase_state_default_output_path": _CODEBASE_STATE,
    "collect_branch_diff_entries": _PR_ANALYSIS,
    "create_architecture_specification_template": _ARCHITECTURE,
    "create_change_log_template": _TEMPLATE,
    "create_codebase_state": _CODEBASE_STATE,
    "create_implementation_specification_template": _IMPLEMENTATION,
    "create_pr_specification_template": _PR_SPECIFICATION,
    "create_system_specification_template": _SYSTEM,
    "implementation_specification_default_output_path": _IMPLEMENTATION,
    "lookup_code_provenance": _CODE_INDEX,
    "lookup_code_provenance_span": _CODE_INDEX,
    "lookup_edit_context": _EDIT_CONTEXT,
    "lookup_entity_decisions": _ENTITY_CONTEXT,
    "lookup_entity_references": _ENTITY_CONTEXT,
    "lookup_entity_relationships": _ENTITY_CONTEXT,
    "parse_change_log": _SCHEMAS,
    "parse_line_range": _EDIT_CONTEXT,
    "parse_line_ranges": _EDIT_CONTEXT,
    "parse_validation_report": _VALIDATE,
    "pr_specification_default_output_path": _PR_SPECIFICATION,
    "refresh_code_index": _CODE_INDEX,
    "render_architecture_specification_template": _ARCHITECTURE,
    "render_codebase_state_report": _CODEBASE_STATE,
    "render_current_decisions_report": _CODEBASE_STATE,
    "render_edit_context_report": _EDIT_CONTEXT,
    "render_entity_decision_report": _ENTITY_CONTEXT,
    "render_entity_reference_report": _ENTITY_CONTEXT,
    "render_entity_relationship_report": _ENTITY_CONTEXT,
    "render_implementation_specification_template": _IMPLEMENTATION,
    "render_invariants_report": _CODEBASE_STATE,
    "render_pr_specification_template": _PR_SPECIFICATION,
    "render_proposed_pr_search_report": _PR_SPECIFICATION,
    "render_system_specification_template": _SYSTEM,
    "render_change_log_template": _TEMPLATE,
    "resolve_default_branch": _PR_ANALYSIS,
    "resolve_repo_root": _PR_ANALYSIS,
    "search_proposed_pr_specifications": _PR_SPECIFICATION,
    "show_proposed_pr_specification": _PR_SPECIFICATION,
    "system_specification_default_output_path": _SYSTEM,
    "validate_architecture_specification_yaml": _ARCHITECTURE,
    "build_validation_report": _VALIDATE,
    "validate_change_log_yaml": _VALIDATE,
    "validate_implementation_specification_yaml": _IMPLEMENTATION,
    "validate_pr_specification_yaml": _PR_SPECIFICATION,
    "validate_system_specification_yaml": _SYSTEM,
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module = import_module(module_name)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
