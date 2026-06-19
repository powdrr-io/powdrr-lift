from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.change_log_template import _resolve_default_branch, _resolve_repo_root
from powdrr_lift.core.code_index import (
    BranchState,
    CodeIndexStore,
    _current_branch,
)
from powdrr_lift.core.index import ChangelogDocument, SourceIndex

_DEFAULT_OUTPUT_PATH = Path(".powdrr-lift") / "state" / "codebase-state.yaml"


@dataclass(frozen=True, slots=True)
class CodebaseStateSource:
    pr_number: int | None
    commit_sha: str | None
    commit_timestamp: int | None
    changelog_path: str | None
    title: str | None
    change_id: str | None


@dataclass(frozen=True, slots=True)
class CodebaseStateEntity:
    id: str
    type: str | None
    last_action: str | None
    source: CodebaseStateSource


@dataclass(frozen=True, slots=True)
class CodebaseStateRelationship:
    source: str
    target: str
    relationship: str | None
    rationale: str | None
    last_action: str | None
    source_record: CodebaseStateSource


@dataclass(frozen=True, slots=True)
class CodebaseStateLifecycleItem:
    id: str
    description: str | None
    last_action: str | None
    source: CodebaseStateSource


@dataclass(frozen=True, slots=True)
class CodebaseStateDecision:
    key: str
    decision_id: str | None
    summary: str | None
    status: str | None
    replaces: str | None
    sources: list[CodebaseStateSource] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CodebaseStateIntent:
    problem: str | None
    goal: str | None
    sources: list[CodebaseStateSource] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CodebaseStateReport:
    repo_root: str
    branch_name: str
    parent_branch: str
    branch_head_sha: str
    parent_head_sha: str
    indexed_at: int
    entities: list[CodebaseStateEntity] = field(default_factory=list)
    relationships: list[CodebaseStateRelationship] = field(default_factory=list)
    invariants: list[CodebaseStateLifecycleItem] = field(default_factory=list)
    guidance: list[CodebaseStateLifecycleItem] = field(default_factory=list)
    decisions: list[CodebaseStateDecision] = field(default_factory=list)
    intents: list[CodebaseStateIntent] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CodebaseStateDecisionReport:
    repo_root: str
    branch_name: str
    parent_branch: str
    branch_head_sha: str
    parent_head_sha: str
    indexed_at: int
    decisions: list[CodebaseStateDecision] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CodebaseStateInvariantReport:
    repo_root: str
    branch_name: str
    parent_branch: str
    branch_head_sha: str
    parent_head_sha: str
    indexed_at: int
    invariants: list[CodebaseStateLifecycleItem] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _CodebaseStateSnapshot:
    store: CodeIndexStore
    source_index: SourceIndex
    branch_name: str
    parent_branch: str
    branch_state: BranchState


def build_codebase_state_report(
    branch_name: str | None = None,
    *,
    parent_branch: str | None = None,
    repo_root: str | Path | None = None,
) -> CodebaseStateReport:
    repo_root_path = _resolve_repo_root(repo_root)
    resolved_branch = branch_name or _current_branch(repo_root_path)
    resolved_parent_branch = parent_branch or _resolve_default_branch(repo_root_path)

    state = _load_codebase_state_state(
        repo_root_path=repo_root_path,
        branch_name=resolved_branch,
        parent_branch=resolved_parent_branch,
    )

    return _build_codebase_state_report(state)


def build_current_decisions_report(
    branch_name: str | None = None,
    *,
    parent_branch: str | None = None,
    repo_root: str | Path | None = None,
) -> CodebaseStateDecisionReport:
    repo_root_path = _resolve_repo_root(repo_root)
    resolved_branch = branch_name or _current_branch(repo_root_path)
    resolved_parent_branch = parent_branch or _resolve_default_branch(repo_root_path)
    state = _load_codebase_state_state(
        repo_root_path=repo_root_path,
        branch_name=resolved_branch,
        parent_branch=resolved_parent_branch,
    )

    current_decisions = _collect_current_decisions(state.store, resolved_branch)
    return CodebaseStateDecisionReport(
        repo_root=str(repo_root_path),
        branch_name=resolved_branch,
        parent_branch=resolved_parent_branch,
        branch_head_sha=state.branch_state.branch_head_sha,
        parent_head_sha=state.branch_state.parent_head_sha,
        indexed_at=state.branch_state.indexed_at,
        decisions=current_decisions,
    )


def build_invariants_report(
    branch_name: str | None = None,
    *,
    parent_branch: str | None = None,
    repo_root: str | Path | None = None,
) -> CodebaseStateInvariantReport:
    repo_root_path = _resolve_repo_root(repo_root)
    resolved_branch = branch_name or _current_branch(repo_root_path)
    resolved_parent_branch = parent_branch or _resolve_default_branch(repo_root_path)
    state = _load_codebase_state_state(
        repo_root_path=repo_root_path,
        branch_name=resolved_branch,
        parent_branch=resolved_parent_branch,
    )

    invariants = _collect_current_lifecycle_items(
        state.source_index.documents,
        kind="invariants",
    )
    return CodebaseStateInvariantReport(
        repo_root=str(repo_root_path),
        branch_name=resolved_branch,
        parent_branch=resolved_parent_branch,
        branch_head_sha=state.branch_state.branch_head_sha,
        parent_head_sha=state.branch_state.parent_head_sha,
        indexed_at=state.branch_state.indexed_at,
        invariants=invariants,
    )


def render_codebase_state_report(report: CodebaseStateReport) -> str:
    return yaml.safe_dump(_codebase_state_report_to_data(report), sort_keys=False)


def render_current_decisions_report(report: CodebaseStateDecisionReport) -> str:
    return yaml.safe_dump(_current_decisions_report_to_data(report), sort_keys=False)


def render_invariants_report(report: CodebaseStateInvariantReport) -> str:
    return yaml.safe_dump(_invariants_report_to_data(report), sort_keys=False)


def create_codebase_state(
    branch_name: str | None = None,
    *,
    output_path: str | Path | None = None,
    parent_branch: str | None = None,
    repo_root: str | Path | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    report = build_codebase_state_report(
        branch_name=branch_name,
        parent_branch=parent_branch,
        repo_root=repo_root_path,
    )
    resolved_output_path = _resolve_output_path(repo_root_path, output_path)
    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_output_path.write_text(
        render_codebase_state_report(report),
        encoding="utf-8",
    )
    return resolved_output_path


def codebase_state_default_output_path(repo_root: str | Path | None = None) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    return repo_root_path / _DEFAULT_OUTPUT_PATH


def _build_codebase_state_report(
    state: _CodebaseStateSnapshot,
) -> CodebaseStateReport:
    documents = sorted(
        state.source_index.documents,
        key=_document_sort_key,
    )
    entities = _collect_current_entities(documents)
    relationships = _collect_current_relationships(documents)
    invariants = _collect_current_lifecycle_items(
        documents,
        kind="invariants",
    )
    guidance = _collect_current_lifecycle_items(
        documents,
        kind="guidance",
    )
    decisions = _collect_decisions(documents)
    intents = _collect_intents(documents)

    return CodebaseStateReport(
        repo_root=str(state.store.repo_root),
        branch_name=state.branch_name,
        parent_branch=state.parent_branch,
        branch_head_sha=state.branch_state.branch_head_sha,
        parent_head_sha=state.branch_state.parent_head_sha,
        indexed_at=state.branch_state.indexed_at,
        entities=entities,
        relationships=relationships,
        invariants=invariants,
        guidance=guidance,
        decisions=decisions,
        intents=intents,
    )


def _collect_current_entities(
    documents: list[ChangelogDocument],
) -> list[CodebaseStateEntity]:
    entities_by_id: dict[str, CodebaseStateEntity] = {}
    for document in documents:
        source = _document_source(document)
        for entity_change in document.changelog.entity_changes or []:
            entity_id = _normalize_text(entity_change.id)
            if entity_id is None:
                continue

            action = _normalize_text(entity_change.action)
            if action == "deleted":
                entities_by_id.pop(entity_id, None)
                continue

            existing = entities_by_id.get(entity_id)
            entity_type = _normalize_text(entity_change.type)
            if entity_type is None and existing is not None:
                entity_type = existing.type

            entities_by_id[entity_id] = CodebaseStateEntity(
                id=entity_id,
                type=entity_type,
                last_action=action,
                source=source,
            )

    return [entities_by_id[entity_id] for entity_id in sorted(entities_by_id)]


def _collect_current_relationships(
    documents: list[ChangelogDocument],
) -> list[CodebaseStateRelationship]:
    relationships_by_key: dict[
        tuple[str, str, str | None], CodebaseStateRelationship
    ] = {}
    for document in documents:
        source = _document_source(document)
        for relationship_change in document.changelog.entity_relationship_changes or []:
            relationship_source = _normalize_text(relationship_change.source)
            relationship_target = _normalize_text(relationship_change.target)
            if relationship_source is None or relationship_target is None:
                continue

            key = (
                relationship_source,
                relationship_target,
                _normalize_text(relationship_change.relationship),
            )
            action = _normalize_text(relationship_change.action)
            if action == "removed":
                relationships_by_key.pop(key, None)
                continue

            existing = relationships_by_key.get(key)
            rationale = _normalize_text(relationship_change.rationale)
            if rationale is None and existing is not None:
                rationale = existing.rationale

            relationships_by_key[key] = CodebaseStateRelationship(
                source=relationship_source,
                target=relationship_target,
                relationship=key[2],
                rationale=rationale,
                last_action=action,
                source_record=source,
            )

    return [
        relationships_by_key[key]
        for key in sorted(
            relationships_by_key, key=lambda item: (item[0], item[1], item[2] or "")
        )
    ]


def _collect_current_lifecycle_items(
    documents: list[ChangelogDocument],
    *,
    kind: str,
) -> list[CodebaseStateLifecycleItem]:
    items_by_id: dict[str, CodebaseStateLifecycleItem] = {}
    for document in documents:
        source = _document_source(document)
        if kind == "invariants":
            raw_items: list[Any] = list(document.changelog.invariant_changes or [])
        else:
            raw_items = list(document.changelog.guidance_changes or [])

        for raw_item in raw_items:
            item_id = _normalize_text(raw_item.id)
            if item_id is None:
                continue

            action = _normalize_text(raw_item.action)
            if action == "removed":
                items_by_id.pop(item_id, None)
                continue

            existing = items_by_id.get(item_id)
            description = _normalize_text(raw_item.description)
            if description is None and existing is not None:
                description = existing.description

            items_by_id[item_id] = CodebaseStateLifecycleItem(
                id=item_id,
                description=description,
                last_action=action,
                source=source,
            )

    return [items_by_id[item_id] for item_id in sorted(items_by_id)]


def _collect_decisions(
    documents: list[ChangelogDocument],
) -> list[CodebaseStateDecision]:
    decision_rows: dict[str, CodebaseStateDecision] = {}
    decision_row_key_by_id: dict[str, str] = {}
    for document in documents:
        source = _document_source(document)
        for index, decision in enumerate(document.changelog.decisions or [], start=1):
            decision_id = _normalize_text(decision.id)
            summary = _normalize_text(decision.summary)
            replaces = _normalize_text(decision.replaces)
            key = decision_id or summary or f"pr-{document.pr_number}-decision-{index}"
            if replaces is not None:
                replaced_key = decision_row_key_by_id.get(replaces)
                if replaced_key is not None and replaced_key in decision_rows:
                    replaced_row = decision_rows[replaced_key]
                    decision_rows[replaced_key] = CodebaseStateDecision(
                        key=replaced_row.key,
                        decision_id=replaced_row.decision_id,
                        summary=replaced_row.summary,
                        status="superseded",
                        replaces=replaced_row.replaces,
                        sources=list(replaced_row.sources),
                    )

            existing = decision_rows.get(key)
            if existing is None:
                decision_rows[key] = CodebaseStateDecision(
                    key=key,
                    decision_id=decision_id,
                    summary=summary,
                    status="current",
                    replaces=replaces,
                    sources=[source],
                )
            else:
                sources = list(existing.sources)
                if source not in sources:
                    sources.append(source)
                decision_rows[key] = CodebaseStateDecision(
                    key=key,
                    decision_id=existing.decision_id or decision_id,
                    summary=existing.summary or summary,
                    status=existing.status or "current",
                    replaces=existing.replaces or replaces,
                    sources=sources,
                )

            if decision_id is not None:
                decision_row_key_by_id[decision_id] = key

    return sorted(
        decision_rows.values(),
        key=lambda decision: (decision.sources[0].commit_timestamp or -1, decision.key),
    )


def _collect_intents(
    documents: list[ChangelogDocument],
) -> list[CodebaseStateIntent]:
    intent_rows: dict[tuple[str | None, str | None], CodebaseStateIntent] = {}
    for document in documents:
        source = _document_source(document)
        problem = _normalize_text(document.changelog.intent.problem)
        goal = _normalize_text(document.changelog.intent.goal)
        if problem is None and goal is None:
            continue

        key = (problem, goal)
        if key not in intent_rows:
            intent_rows[key] = CodebaseStateIntent(
                problem=problem,
                goal=goal,
                sources=[source],
            )
            continue

        existing = intent_rows[key]
        sources = list(existing.sources)
        if source not in sources:
            sources.append(source)
        intent_rows[key] = CodebaseStateIntent(
            problem=existing.problem,
            goal=existing.goal,
            sources=sources,
        )

    return sorted(
        intent_rows.values(),
        key=lambda intent: (
            intent.sources[0].commit_timestamp or -1,
            intent.problem or "",
            intent.goal or "",
        ),
    )


def _codebase_state_report_to_data(report: CodebaseStateReport) -> dict[str, Any]:
    return {
        "repo_root": report.repo_root,
        "branch_name": report.branch_name,
        "parent_branch": report.parent_branch,
        "branch_head_sha": report.branch_head_sha,
        "parent_head_sha": report.parent_head_sha,
        "indexed_at": report.indexed_at,
        "entities": [
            {
                "id": entity.id,
                "type": entity.type,
                "last_action": entity.last_action,
                "source": _source_to_data(entity.source),
            }
            for entity in report.entities
        ],
        "relationships": [
            {
                "source": relationship.source,
                "target": relationship.target,
                "relationship": relationship.relationship,
                "rationale": relationship.rationale,
                "last_action": relationship.last_action,
                "source_record": _source_to_data(relationship.source_record),
            }
            for relationship in report.relationships
        ],
        "invariants": [
            {
                "id": item.id,
                "description": item.description,
                "last_action": item.last_action,
                "source": _source_to_data(item.source),
            }
            for item in report.invariants
        ],
        "guidance": [
            {
                "id": item.id,
                "description": item.description,
                "last_action": item.last_action,
                "source": _source_to_data(item.source),
            }
            for item in report.guidance
        ],
        "decisions": [
            {
                "key": decision.key,
                "decision_id": decision.decision_id,
                "summary": decision.summary,
                "status": decision.status,
                "replaces": decision.replaces,
                "sources": [_source_to_data(source) for source in decision.sources],
            }
            for decision in report.decisions
        ],
        "intents": [
            {
                "problem": intent.problem,
                "goal": intent.goal,
                "sources": [_source_to_data(source) for source in intent.sources],
            }
            for intent in report.intents
        ],
    }


def _source_to_data(source: CodebaseStateSource) -> dict[str, Any]:
    return {
        "pr_number": source.pr_number,
        "commit_sha": source.commit_sha,
        "commit_timestamp": source.commit_timestamp,
        "changelog_path": source.changelog_path,
        "title": source.title,
        "change_id": source.change_id,
    }


def _load_codebase_state_state(
    *,
    repo_root_path: Path,
    branch_name: str,
    parent_branch: str,
) -> _CodebaseStateSnapshot:
    store = CodeIndexStore(repo_root_path)
    source_index = store.refresh(branch_name, parent_branch)
    branch_state = store.branch_state_for(branch_name)
    if branch_state is None:
        raise RuntimeError(f"Missing branch state for {branch_name!r}.")

    return _CodebaseStateSnapshot(
        store=store,
        source_index=source_index,
        branch_name=branch_name,
        parent_branch=parent_branch,
        branch_state=branch_state,
    )


def _collect_current_decisions(
    store: CodeIndexStore,
    branch_name: str,
) -> list[CodebaseStateDecision]:
    decision_rows: dict[str, CodebaseStateDecision] = {}
    for current_decision in store.lookup_current_decisions(branch_name):
        key = (
            current_decision.decision_id
            or current_decision.decision_summary
            or (
                f"pr-{current_decision.pr_number}-decision-"
                f"{current_decision.decision_index}"
            )
        )
        source = CodebaseStateSource(
            pr_number=current_decision.pr_number,
            commit_sha=current_decision.commit_sha,
            commit_timestamp=current_decision.commit_timestamp,
            changelog_path=current_decision.changelog_path,
            title=current_decision.title,
            change_id=current_decision.change_id,
        )
        existing = decision_rows.get(key)
        if existing is None:
            decision_rows[key] = CodebaseStateDecision(
                key=key,
                decision_id=current_decision.decision_id,
                summary=current_decision.decision_summary,
                status=current_decision.decision_status,
                replaces=current_decision.replaces_decision_id,
                sources=[source],
            )
            continue

        sources = list(existing.sources)
        if source not in sources:
            sources.append(source)
        decision_rows[key] = CodebaseStateDecision(
            key=key,
            decision_id=existing.decision_id or current_decision.decision_id,
            summary=existing.summary or current_decision.decision_summary,
            status=existing.status or current_decision.decision_status,
            replaces=existing.replaces or current_decision.replaces_decision_id,
            sources=sources,
        )

    return sorted(
        decision_rows.values(),
        key=lambda decision: (decision.sources[0].commit_timestamp or -1, decision.key),
    )


def _current_decisions_report_to_data(
    report: CodebaseStateDecisionReport,
) -> dict[str, Any]:
    return {
        "repo_root": report.repo_root,
        "branch_name": report.branch_name,
        "parent_branch": report.parent_branch,
        "branch_head_sha": report.branch_head_sha,
        "parent_head_sha": report.parent_head_sha,
        "indexed_at": report.indexed_at,
        "decisions": [
            {
                "key": decision.key,
                "decision_id": decision.decision_id,
                "summary": decision.summary,
                "status": decision.status,
                "replaces": decision.replaces,
                "sources": [_source_to_data(source) for source in decision.sources],
            }
            for decision in report.decisions
        ],
    }


def _invariants_report_to_data(
    report: CodebaseStateInvariantReport,
) -> dict[str, Any]:
    return {
        "repo_root": report.repo_root,
        "branch_name": report.branch_name,
        "parent_branch": report.parent_branch,
        "branch_head_sha": report.branch_head_sha,
        "parent_head_sha": report.parent_head_sha,
        "indexed_at": report.indexed_at,
        "invariants": [
            {
                "id": item.id,
                "description": item.description,
                "last_action": item.last_action,
                "source": _source_to_data(item.source),
            }
            for item in report.invariants
        ],
    }


def _document_source(document: ChangelogDocument) -> CodebaseStateSource:
    return CodebaseStateSource(
        pr_number=document.pr_number,
        commit_sha=document.commit_sha,
        commit_timestamp=document.commit_timestamp,
        changelog_path=str(document.changelog_path),
        title=document.changelog.title,
        change_id=document.changelog.change_id,
    )


def _document_sort_key(
    document: ChangelogDocument,
) -> tuple[int, int, int, str]:
    return (
        1 if document.commit_timestamp is None else 0,
        document.commit_timestamp or 0,
        document.pr_number,
        str(document.changelog_path),
    )


def _normalize_text(value: object | None) -> str | None:
    if value is None:
        return None

    normalized_value = str(value).strip()
    return normalized_value or None


def _resolve_output_path(repo_root: Path, output_path: str | Path | None) -> Path:
    if output_path is None:
        return repo_root / _DEFAULT_OUTPUT_PATH

    resolved_output = Path(output_path)
    if not resolved_output.is_absolute():
        resolved_output = repo_root / resolved_output

    return resolved_output
