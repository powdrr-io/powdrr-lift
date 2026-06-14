from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.change_log_template import _resolve_repo_root
from powdrr_lift.core.code_index import CodeIndexStore, _current_branch
from powdrr_lift.core.index import (
    EntityOccurrence,
    ProvenanceRecord,
    RelationshipOccurrence,
    _normalize_entity_id,
)


@dataclass(frozen=True, slots=True)
class EntityReferenceReport:
    branch_name: str
    parent_branch: str
    branch_head_sha: str
    parent_head_sha: str
    indexed_at: int
    entity_name: str
    references: list[ProvenanceRecord] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class EntityRelationshipReport:
    branch_name: str
    parent_branch: str
    branch_head_sha: str
    parent_head_sha: str
    indexed_at: int
    entity_name: str
    entity_occurrences: list[EntityOccurrence] = field(default_factory=list)
    relationships: list[RelationshipOccurrence] = field(default_factory=list)


def lookup_entity_references(
    entity_name: str,
    branch_name: str | None = None,
    *,
    parent_branch: str,
    repo_root: str | Path | None = None,
) -> EntityReferenceReport:
    repo_root_path = _resolve_repo_root(repo_root)
    store = CodeIndexStore(repo_root_path)
    resolved_branch = branch_name or _current_branch(repo_root_path)
    store.refresh(resolved_branch, parent_branch)
    branch_state = store.branch_state_for(resolved_branch)
    if branch_state is None:
        raise RuntimeError(f"Missing branch state for {resolved_branch!r}.")

    normalized_entity_name = _normalize_entity_id(entity_name) or entity_name.strip()

    return EntityReferenceReport(
        branch_name=resolved_branch,
        parent_branch=parent_branch,
        branch_head_sha=branch_state.branch_head_sha,
        parent_head_sha=branch_state.parent_head_sha,
        indexed_at=branch_state.indexed_at,
        entity_name=normalized_entity_name,
        references=store.lookup_entity_references(
            resolved_branch,
            normalized_entity_name,
        ),
    )


def lookup_entity_relationships(
    entity_name: str,
    branch_name: str | None = None,
    *,
    parent_branch: str,
    repo_root: str | Path | None = None,
) -> EntityRelationshipReport:
    repo_root_path = _resolve_repo_root(repo_root)
    store = CodeIndexStore(repo_root_path)
    resolved_branch = branch_name or _current_branch(repo_root_path)
    source_index = store.refresh(resolved_branch, parent_branch)
    branch_state = store.branch_state_for(resolved_branch)
    if branch_state is None:
        raise RuntimeError(f"Missing branch state for {resolved_branch!r}.")

    normalized_entity_name = _normalize_entity_id(entity_name) or entity_name.strip()
    entity_occurrences, relationships = store.lookup_entity_relationships(
        resolved_branch,
        normalized_entity_name,
    )
    return EntityRelationshipReport(
        branch_name=resolved_branch,
        parent_branch=parent_branch,
        branch_head_sha=branch_state.branch_head_sha,
        parent_head_sha=branch_state.parent_head_sha,
        indexed_at=branch_state.indexed_at,
        entity_name=normalized_entity_name,
        entity_occurrences=entity_occurrences
        or list(source_index.entity_graph.entities.get(normalized_entity_name, ())),
        relationships=relationships,
    )


def render_entity_reference_report(report: EntityReferenceReport) -> str:
    return yaml.safe_dump(_entity_reference_report_to_data(report), sort_keys=False)


def render_entity_relationship_report(report: EntityRelationshipReport) -> str:
    return yaml.safe_dump(_entity_relationship_report_to_data(report), sort_keys=False)


def _entity_reference_report_to_data(report: EntityReferenceReport) -> dict[str, Any]:
    return {
        "branch_name": report.branch_name,
        "parent_branch": report.parent_branch,
        "branch_head_sha": report.branch_head_sha,
        "parent_head_sha": report.parent_head_sha,
        "indexed_at": report.indexed_at,
        "entity_name": report.entity_name,
        "references": [
            {
                "kind": reference.kind,
                "pr_number": reference.pr_number,
                "commit_sha": reference.commit_sha,
                "commit_timestamp": reference.commit_timestamp,
                "changelog_path": reference.changelog_path,
                "title": reference.title,
                "change_id": reference.change_id,
                "intent_problem": reference.intent_problem,
                "intent_goal": reference.intent_goal,
                "file_path": reference.file,
                "span": (
                    None
                    if reference.span is None
                    else {
                        "start_line": reference.span.start_line,
                        "end_line": reference.span.end_line,
                    }
                ),
                "summary": reference.summary,
                "rationale": reference.rationale,
                "affects": list(reference.affects),
                "change_index": reference.change_index,
            }
            for reference in report.references
        ],
    }


def _entity_relationship_report_to_data(
    report: EntityRelationshipReport,
) -> dict[str, Any]:
    return {
        "branch_name": report.branch_name,
        "parent_branch": report.parent_branch,
        "branch_head_sha": report.branch_head_sha,
        "parent_head_sha": report.parent_head_sha,
        "indexed_at": report.indexed_at,
        "entity_name": report.entity_name,
        "entity_occurrences": [
            {
                "entity_id": occurrence.entity_id,
                "entity_type": occurrence.entity_type,
                "action": occurrence.action,
                "pr_number": occurrence.pr_number,
                "commit_sha": occurrence.commit_sha,
                "commit_timestamp": occurrence.commit_timestamp,
                "changelog_path": occurrence.changelog_path,
            }
            for occurrence in report.entity_occurrences
        ],
        "relationships": [
            {
                "source": relationship.source,
                "target": relationship.target,
                "relationship": relationship.relationship,
                "action": relationship.action,
                "rationale": relationship.rationale,
                "pr_number": relationship.pr_number,
                "commit_sha": relationship.commit_sha,
                "commit_timestamp": relationship.commit_timestamp,
                "changelog_path": relationship.changelog_path,
            }
            for relationship in report.relationships
        ],
    }
