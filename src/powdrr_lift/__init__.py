from typing import Any

from powdrr_lift.change_log_parser import (
    ChangeEntity,
    ChangeEntityRelationship,
    ChangeFeatureState,
    ChangeFile,
    ChangeGuidance,
    ChangeInvariant,
    ChangeLog,
    ChangeProposedPRState,
    Decision,
    Intent,
    RelatedSection,
    Span,
    parse_change_log,
)

__all__ = [
    "ChangeEntity",
    "ChangeEntityRelationship",
    "ChangeFile",
    "ChangeFeatureState",
    "ChangeGuidance",
    "ChangeInvariant",
    "ChangeLog",
    "ChangeProposedPRState",
    "Decision",
    "EntityGraph",
    "EntityOccurrence",
    "Intent",
    "ChangelogDocument",
    "BranchState",
    "BlameChunk",
    "BlameFileView",
    "BlameLine",
    "BlameProvenance",
    "BlameViewState",
    "CodebaseStateDecision",
    "CodebaseStateEntity",
    "CodebaseStateIntent",
    "CodebaseStateLifecycleItem",
    "CodebaseStateRelationship",
    "CodebaseStateReport",
    "CodebaseStateSource",
    "ArchitectureSpecificationValidationIssue",
    "ArchitectureSpecificationValidationReport",
    "ImplementationSpecificationValidationIssue",
    "ImplementationSpecificationValidationReport",
    "SystemSpecificationValidationIssue",
    "SystemSpecificationValidationReport",
    "PRSpecificationValidationIssue",
    "PRSpecificationValidationReport",
    "ProposedPRSearchReport",
    "ProposedPRSearchResult",
    "architecture_specification_default_output_path",
    "implementation_specification_default_output_path",
    "system_specification_default_output_path",
    "system_map_specification_default_output_path",
    "feature_pr_specification_default_output_path",
    "plan_diff_specification_default_output_path",
    "pr_specification_default_output_path",
    "proposed_pr_specification_path",
    "build_architecture_specification_validation_report",
    "build_implementation_specification_validation_report",
    "build_system_specification_validation_report",
    "build_pr_specification_validation_report",
    "build_plan_diff_report",
    "create_architecture_specification_template",
    "create_implementation_specification_template",
    "create_system_specification_template",
    "create_system_map_specification_template",
    "create_feature_pr_specification_template",
    "create_plan_diff_specification",
    "create_pr_specification_template",
    "build_current_decisions_report",
    "EditContextLine",
    "EditContextRange",
    "EditContextReport",
    "EntityDecisionOccurrence",
    "EntityDecisionReport",
    "EntityReferenceReport",
    "EntityRelationshipReport",
    "code_index_db_path",
    "lookup_code_provenance",
    "lookup_code_provenance_span",
    "lookup_entity_decisions",
    "lookup_entity_references",
    "lookup_entity_relationships",
    "lookup_edit_context",
    "ProvenanceRecord",
    "RelationshipOccurrence",
    "RelatedSection",
    "RepoTreeNode",
    "Span",
    "create_change_log_template",
    "build_codebase_state_report",
    "build_current_state_specification_report",
    "build_invariants_report",
    "build_changelog_index",
    "build_changelog_index_at_ref",
    "build_blame_file_view",
    "build_blame_view_state",
    "build_repo_tree",
    "codebase_state_default_output_path",
    "current_state_specification_default_output_path",
    "create_codebase_state",
    "create_current_state_specification",
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
    "render_codebase_state_report",
    "render_current_state_specification_report",
    "render_current_decisions_report",
    "render_entity_decision_report",
    "render_entity_reference_report",
    "render_entity_relationship_report",
    "render_architecture_specification_template",
    "render_implementation_specification_template",
    "render_system_specification_template",
    "render_system_map_specification_template",
    "render_feature_pr_specification_template",
    "render_plan_diff_specification",
    "render_pr_specification_template",
    "render_proposed_pr_search_report",
    "render_invariants_report",
    "validate_architecture_specification_yaml",
    "validate_implementation_specification_yaml",
    "validate_system_specification_yaml",
    "validate_pr_specification_yaml",
    "search_proposed_pr_specifications",
    "show_proposed_pr_specification",
    "validate_change_log_yaml",
    "ValidationIssue",
    "ValidationReport",
]


def __getattr__(name: str) -> Any:
    if name == "create_change_log_template":
        from powdrr_lift.change_log_template import create_change_log_template

        return create_change_log_template

    if name in {
        "ArchitectureSpecificationValidationIssue",
        "ArchitectureSpecificationValidationReport",
        "ImplementationSpecificationValidationIssue",
        "ImplementationSpecificationValidationReport",
        "SystemSpecificationValidationIssue",
        "SystemSpecificationValidationReport",
        "PRSpecificationValidationIssue",
        "PRSpecificationValidationReport",
        "ProposedPRSearchReport",
        "ProposedPRSearchResult",
        "architecture_specification_default_output_path",
        "implementation_specification_default_output_path",
        "system_specification_default_output_path",
        "system_map_specification_default_output_path",
        "feature_pr_specification_default_output_path",
        "plan_diff_specification_default_output_path",
        "pr_specification_default_output_path",
        "proposed_pr_specification_path",
        "build_architecture_specification_validation_report",
        "build_implementation_specification_validation_report",
        "build_system_specification_validation_report",
        "build_pr_specification_validation_report",
        "build_plan_diff_report",
        "create_architecture_specification_template",
        "create_implementation_specification_template",
        "create_system_specification_template",
        "create_system_map_specification_template",
        "create_feature_pr_specification_template",
        "create_plan_diff_specification",
        "create_pr_specification_template",
        "render_architecture_specification_template",
        "render_implementation_specification_template",
        "render_system_specification_template",
        "render_system_map_specification_template",
        "render_feature_pr_specification_template",
        "render_plan_diff_specification",
        "render_pr_specification_template",
        "render_proposed_pr_search_report",
        "validate_architecture_specification_yaml",
        "validate_implementation_specification_yaml",
        "validate_system_specification_yaml",
        "validate_pr_specification_yaml",
        "search_proposed_pr_specifications",
        "show_proposed_pr_specification",
    }:
        from powdrr_lift.core.architecture_specification import (
            ArchitectureSpecificationValidationIssue,
            ArchitectureSpecificationValidationReport,
            architecture_specification_default_output_path,
            build_architecture_specification_validation_report,
            create_architecture_specification_template,
            render_architecture_specification_template,
            validate_architecture_specification_yaml,
        )
        from powdrr_lift.core.feature_planning_specification import (
            create_feature_pr_specification_template,
            create_system_map_specification_template,
            feature_pr_specification_default_output_path,
            render_feature_pr_specification_template,
            render_system_map_specification_template,
            system_map_specification_default_output_path,
        )
        from powdrr_lift.core.implementation_specification import (
            ImplementationSpecificationValidationIssue,
            ImplementationSpecificationValidationReport,
            build_implementation_specification_validation_report,
            create_implementation_specification_template,
            implementation_specification_default_output_path,
            render_implementation_specification_template,
            validate_implementation_specification_yaml,
        )
        from powdrr_lift.core.plan_diff_specification import (
            build_plan_diff_report,
            create_plan_diff_specification,
            plan_diff_specification_default_output_path,
            render_plan_diff_specification,
        )
        from powdrr_lift.core.pr_specification import (
            ProposedPRSearchReport,
            ProposedPRSearchResult,
            PRSpecificationValidationIssue,
            PRSpecificationValidationReport,
            build_pr_specification_validation_report,
            create_pr_specification_template,
            pr_specification_default_output_path,
            proposed_pr_specification_path,
            render_pr_specification_template,
            render_proposed_pr_search_report,
            search_proposed_pr_specifications,
            show_proposed_pr_specification,
            validate_pr_specification_yaml,
        )
        from powdrr_lift.core.system_specification import (
            SystemSpecificationValidationIssue,
            SystemSpecificationValidationReport,
            build_system_specification_validation_report,
            create_system_specification_template,
            render_system_specification_template,
            system_specification_default_output_path,
            validate_system_specification_yaml,
        )

        return {
            "ArchitectureSpecificationValidationIssue": (
                ArchitectureSpecificationValidationIssue
            ),
            "ArchitectureSpecificationValidationReport": (
                ArchitectureSpecificationValidationReport
            ),
            "ImplementationSpecificationValidationIssue": (
                ImplementationSpecificationValidationIssue
            ),
            "ImplementationSpecificationValidationReport": (
                ImplementationSpecificationValidationReport
            ),
            "SystemSpecificationValidationIssue": (SystemSpecificationValidationIssue),
            "SystemSpecificationValidationReport": (
                SystemSpecificationValidationReport
            ),
            "PRSpecificationValidationIssue": PRSpecificationValidationIssue,
            "PRSpecificationValidationReport": PRSpecificationValidationReport,
            "ProposedPRSearchReport": ProposedPRSearchReport,
            "ProposedPRSearchResult": ProposedPRSearchResult,
            "architecture_specification_default_output_path": (
                architecture_specification_default_output_path
            ),
            "implementation_specification_default_output_path": (
                implementation_specification_default_output_path
            ),
            "system_specification_default_output_path": (
                system_specification_default_output_path
            ),
            "system_map_specification_default_output_path": (
                system_map_specification_default_output_path
            ),
            "feature_pr_specification_default_output_path": (
                feature_pr_specification_default_output_path
            ),
            "plan_diff_specification_default_output_path": (
                plan_diff_specification_default_output_path
            ),
            "pr_specification_default_output_path": (
                pr_specification_default_output_path
            ),
            "proposed_pr_specification_path": proposed_pr_specification_path,
            "build_architecture_specification_validation_report": (
                build_architecture_specification_validation_report
            ),
            "build_implementation_specification_validation_report": (
                build_implementation_specification_validation_report
            ),
            "build_system_specification_validation_report": (
                build_system_specification_validation_report
            ),
            "build_pr_specification_validation_report": (
                build_pr_specification_validation_report
            ),
            "build_plan_diff_report": build_plan_diff_report,
            "create_architecture_specification_template": (
                create_architecture_specification_template
            ),
            "create_implementation_specification_template": (
                create_implementation_specification_template
            ),
            "create_system_specification_template": (
                create_system_specification_template
            ),
            "create_system_map_specification_template": (
                create_system_map_specification_template
            ),
            "create_feature_pr_specification_template": (
                create_feature_pr_specification_template
            ),
            "create_plan_diff_specification": create_plan_diff_specification,
            "create_pr_specification_template": create_pr_specification_template,
            "render_architecture_specification_template": (
                render_architecture_specification_template
            ),
            "render_implementation_specification_template": (
                render_implementation_specification_template
            ),
            "render_system_specification_template": (
                render_system_specification_template
            ),
            "render_system_map_specification_template": (
                render_system_map_specification_template
            ),
            "render_feature_pr_specification_template": (
                render_feature_pr_specification_template
            ),
            "render_plan_diff_specification": render_plan_diff_specification,
            "render_pr_specification_template": render_pr_specification_template,
            "render_proposed_pr_search_report": render_proposed_pr_search_report,
            "validate_architecture_specification_yaml": (
                validate_architecture_specification_yaml
            ),
            "validate_implementation_specification_yaml": (
                validate_implementation_specification_yaml
            ),
            "validate_system_specification_yaml": (validate_system_specification_yaml),
            "validate_pr_specification_yaml": validate_pr_specification_yaml,
            "search_proposed_pr_specifications": search_proposed_pr_specifications,
            "show_proposed_pr_specification": show_proposed_pr_specification,
        }[name]

    if name == "build_changelog_index":
        from powdrr_lift.core.index import build_changelog_index

        return build_changelog_index

    if name == "build_changelog_index_at_ref":
        from powdrr_lift.core.index import build_changelog_index_at_ref

        return build_changelog_index_at_ref

    if name in {
        "CodebaseStateDecision",
        "CodebaseStateEntity",
        "CodebaseStateIntent",
        "CodebaseStateLifecycleItem",
        "CodebaseStateRelationship",
        "CodebaseStateReport",
        "CodebaseStateSource",
        "build_current_state_specification_report",
        "build_current_decisions_report",
        "build_codebase_state_report",
        "build_invariants_report",
        "codebase_state_default_output_path",
        "current_state_specification_default_output_path",
        "create_codebase_state",
        "create_current_state_specification",
        "render_current_decisions_report",
        "render_codebase_state_report",
        "render_current_state_specification_report",
        "render_invariants_report",
    }:
        from powdrr_lift.core.codebase_state import (
            CodebaseStateDecision,
            CodebaseStateEntity,
            CodebaseStateIntent,
            CodebaseStateLifecycleItem,
            CodebaseStateRelationship,
            CodebaseStateReport,
            CodebaseStateSource,
            build_codebase_state_report,
            build_current_decisions_report,
            build_current_state_specification_report,
            build_invariants_report,
            codebase_state_default_output_path,
            create_codebase_state,
            create_current_state_specification,
            current_state_specification_default_output_path,
            render_codebase_state_report,
            render_current_decisions_report,
            render_current_state_specification_report,
            render_invariants_report,
        )

        return {
            "CodebaseStateDecision": CodebaseStateDecision,
            "CodebaseStateEntity": CodebaseStateEntity,
            "CodebaseStateIntent": CodebaseStateIntent,
            "CodebaseStateLifecycleItem": CodebaseStateLifecycleItem,
            "CodebaseStateRelationship": CodebaseStateRelationship,
            "CodebaseStateReport": CodebaseStateReport,
            "CodebaseStateSource": CodebaseStateSource,
            "build_current_state_specification_report": (
                build_current_state_specification_report
            ),
            "build_current_decisions_report": build_current_decisions_report,
            "build_codebase_state_report": build_codebase_state_report,
            "build_invariants_report": build_invariants_report,
            "codebase_state_default_output_path": codebase_state_default_output_path,
            "current_state_specification_default_output_path": (
                current_state_specification_default_output_path
            ),
            "create_codebase_state": create_codebase_state,
            "create_current_state_specification": create_current_state_specification,
            "render_current_decisions_report": render_current_decisions_report,
            "render_codebase_state_report": render_codebase_state_report,
            "render_current_state_specification_report": (
                render_current_state_specification_report
            ),
            "render_invariants_report": render_invariants_report,
        }[name]

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
        "EntityGraph",
        "EntityOccurrence",
        "ProvenanceRecord",
        "RelationshipOccurrence",
        "SourceIndex",
        "build_changelog_index_at_ref",
    }:
        from powdrr_lift.core.index import (
            ChangelogDocument,
            EntityGraph,
            EntityOccurrence,
            ProvenanceRecord,
            RelationshipOccurrence,
            SourceIndex,
            build_changelog_index_at_ref,
        )

        return {
            "ChangelogDocument": ChangelogDocument,
            "EntityGraph": EntityGraph,
            "EntityOccurrence": EntityOccurrence,
            "ProvenanceRecord": ProvenanceRecord,
            "RelationshipOccurrence": RelationshipOccurrence,
            "SourceIndex": SourceIndex,
            "build_changelog_index_at_ref": build_changelog_index_at_ref,
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
        "EntityDecisionOccurrence",
        "EntityDecisionReport",
        "EntityReferenceReport",
        "EntityRelationshipReport",
        "lookup_entity_decisions",
        "lookup_entity_references",
        "lookup_entity_relationships",
        "render_entity_decision_report",
        "render_entity_reference_report",
        "render_entity_relationship_report",
    }:
        from powdrr_lift.core.entity_context import (
            EntityDecisionOccurrence,
            EntityDecisionReport,
            EntityReferenceReport,
            EntityRelationshipReport,
            lookup_entity_decisions,
            lookup_entity_references,
            lookup_entity_relationships,
            render_entity_decision_report,
            render_entity_reference_report,
            render_entity_relationship_report,
        )

        return {
            "EntityDecisionOccurrence": EntityDecisionOccurrence,
            "EntityDecisionReport": EntityDecisionReport,
            "EntityReferenceReport": EntityReferenceReport,
            "EntityRelationshipReport": EntityRelationshipReport,
            "lookup_entity_decisions": lookup_entity_decisions,
            "lookup_entity_references": lookup_entity_references,
            "lookup_entity_relationships": lookup_entity_relationships,
            "render_entity_decision_report": render_entity_decision_report,
            "render_entity_reference_report": render_entity_reference_report,
            "render_entity_relationship_report": render_entity_relationship_report,
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
