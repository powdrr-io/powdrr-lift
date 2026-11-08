"""Deterministic source-ledger question ordering for initial planning."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from powdrr_service.planning.assembly import (
    MAX_WORK_UNIT_PROPOSALS_PER_PAGE,
    MAX_WORK_UNIT_REQUIREMENTS_PER_QUESTION,
    AllocatedWorkUnits,
    AllocationGroup,
    ItemDescription,
    PathFact,
    PendingUnitOutput,
    WorkUnit,
    WorkUnitContext,
    WorkUnitProposal,
    allocate_work_unit_groups,
    allocate_work_units,
    build_item_description_request,
    build_work_unit_proposal_request,
)
from powdrr_service.planning.compiler import (
    CompiledPlan,
    PlanningCompilationError,
    PlanningManifest,
    compile_plan,
    validate_compiled_plan,
)
from powdrr_service.planning.contracts import PlanningContractError
from powdrr_service.planning.coverage import (
    RequirementCoverage,
    RequirementEvidenceSearch,
    build_requirement_coverage_request,
    create_requirement_coverage,
)
from powdrr_service.planning.dependencies import (
    DependencyCandidateDiscovery,
    DependencyGraph,
    HumanSetupNeed,
    ManualSetup,
    UnitDependencyEdge,
    Validation,
    bind_manual_setup,
    build_confirmed_dependency_details_request,
    build_dependency_candidate_review_request,
    build_human_setup_description_request,
    combine_manual_prerequisite_data,
    compile_dependency_graph,
    discover_dependency_candidates,
    implementation_phase_dependency_edges,
)
from powdrr_service.planning.evidence import Evidence
from powdrr_service.planning.multipr_structrr import (
    MultiPRStructrrProposal,
    project_structural_operations,
)
from powdrr_service.planning.plan_models import PlannedWorkItem, ReviewedExecutionPlan
from powdrr_service.planning.questions import (
    QuestionRequest,
    SourceExcerpt,
    batch_question_requests,
)
from powdrr_service.planning.source import (
    BlockDisposition,
    Requirement,
    SegmentedSource,
    allocate_requirements,
    build_block_disposition_request,
    build_extract_requirements_request,
    build_fidelity_request,
    deduplicate_requirement_records,
    extract_implementation_phases,
)
from powdrr_service.planning.structrr import (
    BoundStructuralSubject,
    StructuralBaselineIndex,
    StructuralChange,
    StructuralSubjectEnumerationScope,
    StructuralSubjectPage,
    _merge_code_evidence,
    bind_structural_operation,
    build_structural_candidate_review_request,
    build_structural_change_request,
    build_subject_enumeration_request,
    coalesce_structural_subjects,
    compile_structrr_plan,
    complete_subject_enumeration,
    index_structural_baseline,
    partition_subject_enumeration_units,
    reject_unordered_subject_conflicts,
)
from powdrr_service.planning.test_coverage import build_test_coverage_request
from powdrr_service.planning.workflow_ids import slugify_workflow_id
from powdrr_service.structrr.code_symbols import CodeSymbolSearchResult
from powdrr_service.structrr.snapshot import snapshot_digest


def _planned_file_subjects(
    item_id: str,
    units: Sequence[WorkUnit],
    *,
    requirement_by_id: Mapping[str, Requirement],
    projected_baseline: Mapping[str, Any],
    fallback_source_refs: Sequence[str],
) -> tuple[BoundStructuralSubject, ...]:
    """Bind planned file paths deterministically instead of asking an LLM."""
    owners_by_path: dict[str, dict[str, set[str]]] = {}
    for unit in units:
        for write in unit.planned_writes:
            if not write.path:
                continue
            owner = owners_by_path.setdefault(
                write.path, {"requirements": set(), "units": set(), "sources": set()}
            )
            owner["units"].add(unit.id)
            owner["requirements"].update(unit.requirement_ids)
            for requirement_id in unit.requirement_ids:
                requirement = requirement_by_id.get(requirement_id)
                if requirement is not None:
                    owner["sources"].update(requirement.source_refs)

    source_subjects = projected_baseline.get("source_subjects", [])
    entities = projected_baseline.get("entities", [])
    files = projected_baseline.get("files", [])
    subjects: list[BoundStructuralSubject] = []
    for path, owner in sorted(owners_by_path.items()):
        existing_record = (
            next(
                (
                    record
                    for record in source_subjects
                    if isinstance(record, Mapping)
                    and (
                        record.get("path") == path
                        or record.get("id") in {path, f"file:{path}", f"path::{path}"}
                    )
                ),
                None,
            )
            if isinstance(source_subjects, list)
            else None
        )
        if existing_record is None and isinstance(entities, list):
            existing_record = next(
                (
                    record
                    for record in entities
                    if isinstance(record, Mapping)
                    and (
                        record.get("path") == path
                        or record.get("id") in {path, f"file:{path}", f"path::{path}"}
                    )
                ),
                None,
            )
        if existing_record is None and isinstance(files, list):
            existing_record = next(
                (
                    record
                    for record in files
                    if isinstance(record, Mapping) and record.get("path") == path
                ),
                None,
            )
        stable_key = (
            existing_record.get("stable_key")
            if isinstance(existing_record, Mapping)
            else None
        )
        subject_id = (
            stable_key
            if isinstance(stable_key, str) and stable_key
            else f"path::{path}"
        )
        source_refs = tuple(sorted(owner["sources"])) or tuple(fallback_source_refs)
        subjects.append(
            BoundStructuralSubject(
                subject_id,
                "files",
                item_id,
                "changed" if existing_record is not None else "added",
                tuple(sorted(owner["requirements"])),
                tuple(sorted(owner["units"])),
                source_refs,
                existing_record is None,
                path,
            )
        )
    return tuple(subjects)


def _planned_reference_path_pairs(
    planned_paths: Sequence[str],
    references_by_path: Mapping[str, Sequence[str]],
) -> tuple[tuple[str, str], ...]:
    """Keep reference evidence local to the paths owned by one plan item."""
    path_set = set(planned_paths)
    return tuple(
        sorted(
            (source_path, target_path)
            for source_path in path_set
            for target_path in references_by_path.get(source_path, ())
            if target_path in path_set
        )
    )


def _planned_existing_structural_subjects(
    item_id: str,
    units: Sequence[WorkUnit],
    *,
    requirement_by_id: Mapping[str, Requirement],
    projected_baseline: Mapping[str, Any],
    fallback_source_refs: Sequence[str],
    basedpyright_symbols_by_path: Mapping[str, Sequence[Mapping[str, Any]]]
    | None = None,
) -> tuple[BoundStructuralSubject, ...]:
    """Bind path-related baseline entities and relationships as candidates."""
    owners_by_path: dict[str, dict[str, set[str]]] = {}
    for unit in units:
        for write in unit.planned_writes:
            if not write.path:
                continue
            owner = owners_by_path.setdefault(
                write.path, {"requirements": set(), "units": set(), "sources": set()}
            )
            owner["units"].add(unit.id)
            owner["requirements"].update(unit.requirement_ids)
            for requirement_id in unit.requirement_ids:
                requirement = requirement_by_id.get(requirement_id)
                if requirement is not None:
                    owner["sources"].update(requirement.source_refs)

    path_owner = {path: owner for path, owner in owners_by_path.items()}
    entity_records = projected_baseline.get("entities", [])
    entities = (
        [record for record in entity_records if isinstance(record, Mapping)]
        if isinstance(entity_records, list)
        else []
    )
    source_subjects = projected_baseline.get("source_subjects", [])
    entity_paths_by_id: dict[str, set[str]] = {}
    if isinstance(source_subjects, list):
        for source_subject in source_subjects:
            if not isinstance(source_subject, Mapping):
                continue
            path = source_subject.get("path")
            entity_id = source_subject.get("file_entity_id")
            if (
                isinstance(path, str)
                and path in owners_by_path
                and isinstance(entity_id, str)
            ):
                entity_paths_by_id.setdefault(entity_id, set()).add(path)
    source_bindings = projected_baseline.get("source_bindings", [])
    if isinstance(source_bindings, list):
        for binding in source_bindings:
            if not isinstance(binding, Mapping):
                continue
            path = binding.get("path")
            entity_id = binding.get("entity_id")
            if (
                isinstance(path, str)
                and path in owners_by_path
                and isinstance(entity_id, str)
            ):
                entity_paths_by_id.setdefault(entity_id, set()).add(path)
    candidates: dict[str, dict[str, set[str]]] = {}
    symbol_evidence_by_entity: dict[str, tuple[Mapping[str, Any], ...]] = {}
    symbol_entity_ids_by_path: dict[str, set[str]] = {}
    if basedpyright_symbols_by_path is not None:
        for path, symbols in basedpyright_symbols_by_path.items():
            evidence_by_entity = _bound_code_evidence_for_basedpyright_symbols(
                path,
                symbols,
                projected_baseline=projected_baseline,
            )
            symbol_entity_ids_by_path[path] = set(evidence_by_entity)
            for entity_id, evidence in evidence_by_entity.items():
                symbol_evidence_by_entity[entity_id] = _merge_code_evidence(
                    symbol_evidence_by_entity.get(entity_id, ()), evidence
                )
    for entity in entities:
        entity_id = entity.get("id")
        if not isinstance(entity_id, str):
            continue
        path = entity.get("path")
        entity_paths = (
            {path}
            if isinstance(path, str)
            else set(entity_paths_by_id.get(entity_id, ()))
        )
        if not entity_paths and entity_id.startswith("file:"):
            entity_paths.add(entity_id.removeprefix("file:"))
        related_owners = [
            path_owner[candidate_path]
            for candidate_path in sorted(entity_paths)
            if candidate_path in path_owner
            and (
                isinstance(path, str)
                or candidate_path not in symbol_entity_ids_by_path
                or entity_id in symbol_entity_ids_by_path[candidate_path]
            )
        ]
        if not related_owners:
            continue
        owner = {
            key: set().union(*(candidate[key] for candidate in related_owners))
            for key in ("requirements", "units", "sources")
        }
        candidates[entity_id] = owner

    result: list[BoundStructuralSubject] = []
    for entity_id, owner in sorted(candidates.items()):
        result.append(
            BoundStructuralSubject(
                entity_id,
                "entities",
                item_id,
                "changed",
                tuple(sorted(owner["requirements"])),
                tuple(sorted(owner["units"])),
                tuple(sorted(owner["sources"])) or tuple(fallback_source_refs),
                False,
                None,
                symbol_evidence_by_entity.get(entity_id, ()),
            )
        )

    relationships = projected_baseline.get("entity_relationships", [])
    if isinstance(relationships, list):
        for relationship in relationships:
            if not isinstance(relationship, Mapping):
                continue
            relationship_id = relationship.get("id")
            if not isinstance(relationship_id, str):
                continue
            related_owners = [
                candidates[endpoint]
                for endpoint in (relationship.get("source"), relationship.get("target"))
                if isinstance(endpoint, str) and endpoint in candidates
            ]
            if not related_owners:
                continue
            owner = {
                key: set().union(*(candidate[key] for candidate in related_owners))
                for key in ("requirements", "units", "sources")
            }
            result.append(
                BoundStructuralSubject(
                    relationship_id,
                    "entity_relationships",
                    item_id,
                    "changed",
                    tuple(sorted(owner["requirements"])),
                    tuple(sorted(owner["units"])),
                    tuple(sorted(owner["sources"])) or tuple(fallback_source_refs),
                    False,
                )
            )
    return tuple(result)


def _bound_code_evidence_for_basedpyright_symbols(
    path: str,
    symbols: Sequence[Mapping[str, Any]],
    *,
    projected_baseline: Mapping[str, Any],
) -> dict[str, tuple[Mapping[str, Any], ...]]:
    """Bind each indexed declaration and its query provenance to Structrr."""
    source_subjects = projected_baseline.get("source_subjects", [])
    if not isinstance(source_subjects, list):
        return {}
    module_names = {
        str(subject["qualified_name"])
        for subject in source_subjects
        if isinstance(subject, Mapping)
        and subject.get("path") == path
        and subject.get("kind") == "module"
        and isinstance(subject.get("qualified_name"), str)
    }
    matched_keys: set[tuple[str, str]] = {("module", name) for name in module_names}
    evidence_by_key: dict[tuple[str, str], list[Mapping[str, Any]]] = {}

    def visit(items: Sequence[Mapping[str, Any]], prefix: str) -> None:
        for symbol in items:
            name = symbol.get("name")
            kind = symbol.get("kind")
            if not isinstance(name, str) or not name or not isinstance(kind, str):
                continue
            qualified_name = f"{prefix}.{name}" if prefix else name
            matched_keys.add((kind, qualified_name))
            evidence = symbol.get("code_evidence")
            if isinstance(evidence, Mapping):
                evidence_by_key.setdefault((kind, qualified_name), []).append(
                    dict(evidence)
                )
            children = symbol.get("children")
            if isinstance(children, list):
                visit(
                    [child for child in children if isinstance(child, Mapping)],
                    qualified_name,
                )

    for module_name in sorted(module_names):
        visit(symbols, module_name)
    matched_subjects = {
        str(subject["id"]): (subject.get("kind"), subject.get("qualified_name"))
        for subject in source_subjects
        if isinstance(subject, Mapping)
        and subject.get("path") == path
        and isinstance(subject.get("id"), str)
        and (subject.get("kind"), subject.get("qualified_name")) in matched_keys
    }
    bindings = projected_baseline.get("source_bindings", [])
    if not isinstance(bindings, list):
        return {}
    output: dict[str, tuple[Mapping[str, Any], ...]] = {}
    for binding in bindings:
        if not isinstance(binding, Mapping) or binding.get("path") != path:
            continue
        subject_id = binding.get("subject_id")
        entity_id = binding.get("entity_id")
        if (
            not isinstance(subject_id, str)
            or subject_id not in matched_subjects
            or not isinstance(entity_id, str)
        ):
            continue
        kind, qualified_name = matched_subjects[subject_id]
        if not isinstance(kind, str) or not isinstance(qualified_name, str):
            continue
        key = (kind, qualified_name)
        output[entity_id] = _merge_code_evidence(
            output.get(entity_id, ()), evidence_by_key.get(key, ())
        )
    return output


@dataclass(frozen=True, slots=True)
class SourceLedger:
    """Accepted source obligations and one disposition for every source block."""

    requirements: tuple[Requirement, ...]
    dispositions: tuple[BlockDisposition, ...]


@dataclass(frozen=True, slots=True)
class PlanningAssembly:
    """Deterministically grouped work items with an explicit dependency graph."""

    plan: ReviewedExecutionPlan
    units: tuple[WorkUnit, ...]
    item_unit_ids: Mapping[str, tuple[str, ...]]
    dependency_graph: DependencyGraph


class SourceLedgerQuestionMachine:
    """Ask source questions in stable order and rebuild state from journal answers."""

    def __init__(
        self,
        source: SegmentedSource,
        *,
        evidence_by_requirement: Mapping[str, tuple[Evidence, ...]] | None = None,
        evidence_search: Callable[
            [Sequence[Requirement]], Mapping[str, RequirementEvidenceSearch]
        ]
        | None = None,
        evidence_excerpt_reader: Callable[[Evidence], str] | None = None,
        dependency_reference_search: Callable[
            [Sequence[WorkUnit]], Mapping[tuple[str, str], Sequence[str]]
        ]
        | None = None,
        structural_symbol_search: Callable[
            [Sequence[str]],
            Mapping[str, Sequence[Mapping[str, Any]]] | CodeSymbolSearchResult,
        ]
        | None = None,
        structural_reference_search: Callable[
            [Sequence[str]], Mapping[str, Sequence[str]]
        ]
        | None = None,
        verification_command: str = "make test",
        repository_paths: Mapping[str, PathFact] | None = None,
        preferred_runtimes: Sequence[str] = (),
        plan_id: str = "planning-session",
        structrr_baseline: Mapping[str, Any] | None = None,
        taxonomy_entity_types: tuple[str, ...] = (),
    ) -> None:
        source.validate()
        self.source = source
        self._implementation_phases = extract_implementation_phases(source)
        self.evidence_by_requirement = dict(evidence_by_requirement or {})
        self.evidence_search = evidence_search
        self.evidence_excerpt_reader = evidence_excerpt_reader
        self.dependency_reference_search = dependency_reference_search
        self.structural_symbol_search = structural_symbol_search
        self.structural_reference_search = structural_reference_search
        self._structural_symbol_cache_key: tuple[str, ...] | None = None
        self._structural_symbol_cache: Mapping[str, Sequence[Mapping[str, Any]]] = {}
        self._structural_symbol_coverage: Mapping[str, Any] = {}
        self._structural_reference_cache_key: tuple[str, ...] | None = None
        self._structural_reference_cache: Mapping[str, Sequence[str]] = {}
        self._dependency_reference_cache_key: tuple[Any, ...] | None = None
        self._dependency_reference_cache: Mapping[tuple[str, str], Sequence[str]] = {}
        self._evidence_search_cache: dict[str, RequirementEvidenceSearch] = {}
        self.verification_command = verification_command
        self.repository_paths = dict(repository_paths or {})
        self.preferred_runtimes = tuple(dict.fromkeys(preferred_runtimes))
        self.plan_id = plan_id
        self.structrr_baseline = (
            None if structrr_baseline is None else dict(structrr_baseline)
        )
        self.taxonomy_entity_types = taxonomy_entity_types
        self._invalid_accepted_question_ids: set[str] = set()
        self._accepted_question_input_digests: dict[str, str] | None = None
        self._oversized_work_unit_groups: set[tuple[str, ...]] = set()
        self._max_question_frontier: int | None = None
        self._single_proposal_requirements: set[str] = set()
        self._supplemental_work_unit_requirements: set[str] = set()
        self._pending_deterministic_answers: dict[str, Mapping[str, Any]] = {}
        self._accepted_ledger_cache_key: str | None = None
        self._accepted_ledger_cache_value: SourceLedger | None = None
        self._fidelity_request_cache: dict[str, QuestionRequest[Any]] = {}
        self._validated_fidelity_answers: dict[str, str] = {}
        self._coverage_request_cache: dict[str, QuestionRequest[Any]] = {}
        self._accepted_coverage_cache: dict[str, tuple[str, RequirementCoverage]] = {}
        self._accepted_work_unit_cache_key: str | None = None
        self._accepted_work_unit_cache_value: (
            tuple[WorkUnitContext, tuple[WorkUnitProposal, ...]] | None
        ) = None
        self._structrr_baseline_index_cache: dict[
            tuple[str, ...], StructuralBaselineIndex
        ] = {}
        self._structural_change_request_cache: dict[
            tuple[tuple[str, ...], str], QuestionRequest[Any]
        ] = {}
        self._structural_subject_scope_cache: dict[
            str, tuple[StructuralSubjectEnumerationScope, ...]
        ] = {}
        self._structural_subject_context_cache: dict[
            str,
            tuple[
                tuple[BoundStructuralSubject, ...],
                tuple[tuple[str, str], ...],
                tuple[tuple[str, str], ...],
            ],
        ] = {}
        self._accepted_assembly_cache_key: tuple[Any, ...] | None = None
        self._accepted_assembly_cache_value: PlanningAssembly | None = None

    @property
    def invalid_accepted_question_ids(self) -> frozenset[str]:
        """Accepted answers that no longer satisfy the current parser contract."""
        return frozenset(self._invalid_accepted_question_ids)

    def set_oversized_work_unit_groups(self, groups: Sequence[Sequence[str]]) -> None:
        """Split only requirement groups whose previous response exceeded the cap."""
        self._oversized_work_unit_groups = {
            tuple(group) for group in groups if len(group) > 1
        }
        self._single_proposal_requirements = {
            group[0] for group in groups if len(group) == 1
        }

    def set_accepted_question_input_digests(
        self, input_digests: Mapping[str, str]
    ) -> None:
        """Set digests recorded with answers so changed question inputs are reasked."""
        self._accepted_question_input_digests = dict(input_digests)

    def set_question_frontier_limit(self, limit: int) -> None:
        """Bound frontier construction to one concurrent execution window."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("question frontier limit must be a positive integer")
        self._max_question_frontier = limit

    def _batch_requests(
        self, requests: Sequence[QuestionRequest[Any]]
    ) -> tuple[QuestionRequest[Any], ...]:
        if self._max_question_frontier is not None:
            requests = requests[: self._max_question_frontier]
        if requests and all(
            request.question.kind
            in {"review_structural_candidate", "review_dependency_candidate"}
            for request in requests
        ):
            return tuple(requests)
        return batch_question_requests(requests)

    def repository_search_metrics(self) -> tuple[tuple[str, Mapping[str, Any]], ...]:
        """Return counts from completed searches without exposing evidence text."""
        return tuple(
            (
                requirement_id,
                {
                    "matching_line_count": search.matching_line_count,
                    "matching_path_count": search.matching_path_count,
                    "evidence_count": len(search.evidence),
                    "basedpyright_query_count": search.basedpyright_query_count,
                    "basedpyright_match_count": search.basedpyright_match_count,
                    "rg_match_count": search.rg_match_count,
                    "rg_search_status": search.rg_search_status,
                    "basedpyright_available": search.basedpyright_available,
                    "base_commit": search.base_commit,
                    **(
                        {"code_symbol_coverage": dict(search.code_symbol_coverage)}
                        if search.code_symbol_coverage is not None
                        else {}
                    ),
                },
            )
            for requirement_id, search in sorted(self._evidence_search_cache.items())
        )

    def deterministic_answers(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> tuple[Mapping[str, Any], ...]:
        """Derive source and coverage answers proven by pinned facts."""
        self._pending_deterministic_answers.clear()
        requests = self.ready_questions(accepted_answers)
        derived = list(self._pending_deterministic_answers.values())
        for request in requests:
            for member in request.batch_members or (request,):
                if member.question.kind == "check_requirement_fidelity":
                    question_id = member.question.question_id
                    if question_id in accepted_answers:
                        continue
                    answer = _exact_source_text_fidelity_answer(member)
                    if answer is None:
                        continue
                    member.parse_answer(answer)
                    derived.append(
                        {
                            "question": member.question,
                            "answer": answer,
                            "rule_id": "exact-source-text-fidelity-v1",
                            "reason": (
                                "The requirement statement matches its single pinned "
                                "source anchor after whitespace normalization."
                            ),
                        }
                    )
                    continue
                if member.question.kind != "assess_requirement_coverage":
                    continue
                question_id = member.question.question_id
                if question_id in accepted_answers:
                    continue
                requirement_id = member.question.subject_ids[0]
                search = self._evidence_search_cache.get(requirement_id)
                if not _is_completed_zero_hit_search(
                    search, pinned_commit=self.source.document.commit_sha
                ):
                    continue
                assert search is not None
                answer = {
                    "status": "missing",
                    "evidence_ids": [],
                    "reason": (
                        "The completed BasedPyright and ripgrep search on pinned "
                        f"base {search.base_commit} found zero matching lines, "
                        "paths, or evidence records."
                    ),
                }
                member.parse_answer(answer)
                derived.append(
                    {
                        "question": member.question,
                        "answer": answer,
                        "rule_id": "completed-zero-hit-search-v1",
                        "reason": (
                            "The pinned repository search completed with zero line "
                            "and path matches and returned no evidence."
                        ),
                    }
                )
        return tuple(derived)

    def _next_source_question(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> QuestionRequest[Any] | None:
        requests = self._next_source_questions(accepted_answers)
        return requests[0] if requests else None

    def _next_source_questions(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> tuple[QuestionRequest[Any], ...]:
        self._pending_deterministic_answers.clear()
        raw_records: list[dict[str, Any]] = []
        anchor_by_id = {anchor.id: anchor for anchor in self.source.anchors}
        pending_extractions: list[QuestionRequest[Any]] = []
        frontier_limit = self._max_question_frontier

        for block in self.source.blocks:
            if (
                frontier_limit is not None
                and len(pending_extractions) >= frontier_limit
            ):
                break
            after_anchor: str | None = None
            page_records: list[dict[str, Any]] = []
            block_incomplete = False
            while True:
                extract_request = build_extract_requirements_request(
                    block,
                    anchors=anchor_by_id,
                    after_anchor=after_anchor,
                )
                answer = accepted_answers.get(extract_request.question.question_id)
                if answer is None:
                    pending_extractions.append(extract_request)
                    block_incomplete = True
                    break
                page = extract_request.parse_answer(answer)
                page_records.extend(dict(record) for record in page.records)
                if not page.has_more:
                    break
                after_anchor = page.next_anchor
            if not block_incomplete:
                raw_records.extend(page_records)

        if pending_extractions:
            return self._batch_requests(pending_extractions)

        requirements, drafts = allocate_requirements(
            deduplicate_requirement_records(raw_records),
            first_ordinal=1,
            anchors=anchor_by_id,
            ledger=self.source.instruction_ledger,
        )
        req_by_source: dict[str, list[str]] = {}
        req_by_related_source: dict[str, list[str]] = {}
        for requirement, draft in zip(requirements, drafts, strict=True):
            for source_ref in requirement.source_refs:
                req_by_source.setdefault(source_ref, []).append(requirement.id)
            for source_ref in draft.related_source_refs:
                req_by_related_source.setdefault(source_ref, []).append(requirement.id)
        related_ids: dict[str, list[str]] = {item.id: [] for item in requirements}
        for requirement, draft in zip(requirements, drafts, strict=True):
            related = {
                candidate
                for source_ref in draft.related_source_refs
                for candidate in req_by_source.get(source_ref, [])
                if candidate != requirement.id
            }
            related_ids[requirement.id].extend(sorted(related))
        requirements = tuple(
            replace(item, related_requirement_ids=tuple(related_ids[item.id]))
            for item in requirements
        )

        pending_source_questions: list[QuestionRequest[Any]] = []
        prior_direct_by_fingerprint: dict[str, list[tuple[str, ...]]] = {}
        for block in self.source.blocks:
            direct_ids = tuple(
                dict.fromkeys(
                    requirement_id
                    for anchor_id in block.anchor_ids
                    for requirement_id in req_by_source.get(anchor_id, [])
                )
            )
            related_ids_for_block = tuple(
                dict.fromkeys(
                    requirement_id
                    for anchor_id in block.anchor_ids
                    for requirement_id in req_by_related_source.get(anchor_id, [])
                )
            )
            known_ids = tuple(dict.fromkeys((*direct_ids, *related_ids_for_block)))
            disposition_request = build_block_disposition_request(
                block, requirement_ids=known_ids
            )
            fingerprint = _normalized_block_fingerprint(block.text)
            if disposition_request.question.question_id not in accepted_answers:
                automatic = _deterministic_block_disposition(
                    block_id=block.id,
                    direct_ids=direct_ids,
                    related_ids=related_ids_for_block,
                    prior_direct_ids=prior_direct_by_fingerprint.get(fingerprint, ()),
                    has_exact_duplicate=fingerprint in prior_direct_by_fingerprint,
                )
                if automatic is None:
                    pending_source_questions.append(disposition_request)
                else:
                    answer, rule_id, reason = automatic
                    disposition_request.parse_answer(answer)
                    self._pending_deterministic_answers[
                        disposition_request.question.question_id
                    ] = {
                        "question": disposition_request.question,
                        "answer": answer,
                        "rule_id": rule_id,
                        "reason": reason,
                    }
            else:
                disposition_request.parse_answer(
                    accepted_answers[disposition_request.question.question_id]
                )
            prior_direct_by_fingerprint.setdefault(fingerprint, []).append(direct_ids)
            if (
                frontier_limit is not None
                and len(pending_source_questions) >= frontier_limit
            ):
                break

        if frontier_limit is None or len(pending_source_questions) < frontier_limit:
            for requirement in requirements:
                fidelity_request = self._fidelity_request_cache.get(requirement.id)
                if fidelity_request is None:
                    fidelity_request = build_fidelity_request(
                        requirement, segmented=self.source
                    )
                    self._fidelity_request_cache[requirement.id] = fidelity_request
                answer = accepted_answers.get(fidelity_request.question.question_id)
                if answer is None:
                    pending_source_questions.append(fidelity_request)
                    if (
                        frontier_limit is not None
                        and len(pending_source_questions) >= frontier_limit
                    ):
                        break
                else:
                    answer_digest = hashlib.sha256(
                        json.dumps(
                            answer,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ).encode("utf-8")
                    ).hexdigest()
                    if (
                        self._validated_fidelity_answers.get(
                            fidelity_request.question.question_id
                        )
                        != answer_digest
                    ):
                        fidelity_request.parse_answer(answer)
                        self._validated_fidelity_answers[
                            fidelity_request.question.question_id
                        ] = answer_digest

        disposition_requests = tuple(
            request
            for request in pending_source_questions
            if request.question.kind == "classify_source_block"
        )
        fidelity_requests = tuple(
            request
            for request in pending_source_questions
            if request.question.kind != "classify_source_block"
        )
        return (
            *self._batch_requests(disposition_requests),
            *self._batch_requests(fidelity_requests),
        )

    def next_question(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> QuestionRequest[Any] | None:
        requests = self.ready_questions(accepted_answers)
        if not requests:
            return None
        first = requests[0]
        return first.batch_members[0] if first.batch_members else first

    def ready_questions(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> tuple[QuestionRequest[Any], ...]:
        """Return all independent questions ready from this accepted snapshot."""
        self._invalid_accepted_question_ids.clear()
        source_requests = self._next_source_questions(accepted_answers)
        if source_requests:
            return source_requests
        ledger = self.accepted_ledger(accepted_answers)
        setup_requests = self._explicit_human_setup_requests(ledger, accepted_answers)
        if setup_requests:
            return self._batch_requests(setup_requests)
        self._search_requirements_evidence(ledger.requirements)
        coverage_requests: list[QuestionRequest[Any]] = []
        for requirement in ledger.requirements:
            search = self._evidence_search_cache.get(requirement.id)
            evidence = self._evidence_for_requirement(requirement.id)
            coverage_request = self._coverage_request_cache.get(requirement.id)
            if coverage_request is None:
                coverage_request = self._build_coverage_request(
                    requirement, evidence, search
                )
                self._coverage_request_cache[requirement.id] = coverage_request
            if coverage_request.question.question_id not in accepted_answers:
                coverage_requests.append(coverage_request)
        if coverage_requests:
            return self._batch_requests(coverage_requests)

        work_unit_requests = tuple(
            request
            for request in self._work_unit_requests(accepted_answers)
            if request is not None
        )
        if work_unit_requests:
            return self._batch_requests(work_unit_requests)

        downstream_requests: list[QuestionRequest[Any]] = []
        for request in self._item_description_requests(accepted_answers):
            if request.question.question_id not in accepted_answers:
                downstream_requests.append(request)

        for request in self._dependency_requests(accepted_answers):
            if self._request_is_pending(request, accepted_answers):
                downstream_requests.append(request)
        if downstream_requests:
            return self._batch_requests(downstream_requests)

        pending_test_designs, _accepted_test_designs = self._accepted_test_coverage(
            accepted_answers
        )
        if pending_test_designs:
            return self._batch_requests(pending_test_designs)

        return self._batch_requests(
            tuple(
                request
                for request in self._structrr_requests(accepted_answers)
                if self._request_is_pending(request, accepted_answers)
            )
        )

    def _request_is_pending(
        self,
        request: QuestionRequest[Any],
        accepted_answers: Mapping[str, Mapping[str, Any]],
    ) -> bool:
        question_id = request.question.question_id
        return (
            question_id not in accepted_answers
            or question_id in self._invalid_accepted_question_ids
        )

    def _explicit_human_setup_requests(
        self,
        ledger: SourceLedger,
        accepted_answers: Mapping[str, Mapping[str, Any]],
    ) -> tuple[QuestionRequest[Any], ...]:
        requests: list[QuestionRequest[Any]] = []
        for requirement in ledger.requirements:
            if requirement.kind != "human_setup":
                continue
            validation = Validation(
                "validation-0001",
                "Run the configured repository verification command.",
                "local_command",
                self.verification_command,
                (requirement.id,),
                requirement.source_refs,
                (),
            )
            need = HumanSetupNeed(
                True,
                requirement.statement,
                requirement.source_refs,
                (),
            )
            request = build_human_setup_description_request(
                validation,
                need,
                source=self.source,
                evidence={},
                setup_id=requirement.id,
            )
            question_id = request.question.question_id
            answer = accepted_answers.get(question_id)
            if answer is None:
                requests.append(request)
                continue
            if (
                self._accepted_question_input_digests is not None
                and self._accepted_question_input_digests.get(question_id)
                != request.question.input_digest
            ):
                self._invalid_accepted_question_ids.add(question_id)
                requests.append(request)
                continue
            try:
                request.parse_answer(answer)
            except PlanningContractError:
                self._invalid_accepted_question_ids.add(question_id)
                requests.append(request)
        return tuple(requests)

    def _search_requirements_evidence(
        self, requirements: Sequence[Requirement]
    ) -> None:
        if self.evidence_search is None:
            return
        missing = tuple(
            item for item in requirements if item.id not in self._evidence_search_cache
        )
        if not missing:
            return
        results = self.evidence_search(missing)
        missing_ids = {item.id for item in missing}
        if set(results) != missing_ids:
            raise ValueError("repository evidence search omitted or added requirements")
        for requirement_id, result in results.items():
            if result.base_commit != self.source.document.commit_sha:
                raise ValueError(
                    "repository evidence search used the wrong base commit"
                )
            self._evidence_search_cache[requirement_id] = result

    def _evidence_for_requirement(self, requirement_id: str) -> tuple[Evidence, ...]:
        """Return explicit evidence or the pinned search result for one requirement."""
        supplied = self.evidence_by_requirement.get(requirement_id)
        if supplied is not None:
            return supplied
        search = self._evidence_search_cache.get(requirement_id)
        return () if search is None else search.evidence

    def _evidence_catalog(self) -> dict[str, Evidence]:
        """Collect every evidence record available to this planning session."""
        evidence: dict[str, Evidence] = {}
        sources = [*self.evidence_by_requirement.values()]
        sources.extend(
            search.evidence for search in self._evidence_search_cache.values()
        )
        for records in sources:
            for item in records:
                existing = evidence.setdefault(item.id, item)
                if existing != item:
                    raise ValueError(
                        f"planning evidence ID {item.id} has conflicting records"
                    )
        return evidence

    def _work_unit_requests(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> tuple[QuestionRequest[Any] | None, ...]:
        self._supplemental_work_unit_requirements.clear()
        ledger = self.accepted_ledger(accepted_answers)
        coverage = self.accepted_coverage(accepted_answers)
        unresolved = tuple(
            item.requirement_id for item in coverage if item.answer.status != "complete"
        )
        if not unresolved:
            return ()
        context = self._work_unit_context(ledger, unresolved)
        requests: list[QuestionRequest[Any] | None] = []
        for requirement_ids in self._work_unit_requirement_groups(unresolved):
            cursor: str | None = None
            remaining_requirement_ids = requirement_ids
            covered_requirement_ids: set[str] = set()
            group_complete = True
            last_request: QuestionRequest[Any] | None = None
            while True:
                request = build_work_unit_proposal_request(
                    context,
                    remaining_requirement_ids,
                    valid_example=self._work_unit_example(
                        context, remaining_requirement_ids
                    ),
                    maximum_proposals=(
                        1
                        if any(
                            item in self._single_proposal_requirements
                            for item in remaining_requirement_ids
                        )
                        else MAX_WORK_UNIT_PROPOSALS_PER_PAGE
                    ),
                )
                last_request = request
                answer = accepted_answers.get(request.question.question_id)
                if answer is None:
                    requests.append(request)
                    group_complete = False
                    break
                try:
                    page = request.parse_answer(answer)
                except PlanningContractError:
                    self._invalid_accepted_question_ids.add(
                        request.question.question_id
                    )
                    requests.append(request)
                    group_complete = False
                    break
                covered_requirement_ids.update(
                    requirement_id
                    for proposal in page.records
                    for requirement_id in proposal.requirement_ids
                )
                if not page.has_more:
                    break
                cursor = page.next_anchor
                assert cursor is not None
                cursor_index = remaining_requirement_ids.index(cursor)
                remaining_requirement_ids = remaining_requirement_ids[
                    cursor_index + 1 :
                ]
            if not group_complete:
                continue
            missing_requirement_ids = set(requirement_ids) - covered_requirement_ids
            if not missing_requirement_ids:
                requests.append(None)
                continue
            if len(requirement_ids) == 1:
                assert last_request is not None
                self._invalid_accepted_question_ids.add(
                    last_request.question.question_id
                )
                requests.append(last_request)
                continue
            for requirement_id in sorted(missing_requirement_ids):
                self._supplemental_work_unit_requirements.add(requirement_id)
                single_ids = (requirement_id,)
                request = build_work_unit_proposal_request(
                    context,
                    single_ids,
                    valid_example=self._work_unit_example(context, single_ids),
                    maximum_proposals=(
                        1
                        if requirement_id in self._single_proposal_requirements
                        else MAX_WORK_UNIT_PROPOSALS_PER_PAGE
                    ),
                )
                answer = accepted_answers.get(request.question.question_id)
                if answer is None:
                    requests.append(request)
                    continue
                try:
                    page = request.parse_answer(answer)
                except PlanningContractError:
                    self._invalid_accepted_question_ids.add(
                        request.question.question_id
                    )
                    requests.append(request)
                    continue
                if requirement_id not in {
                    item
                    for proposal in page.records
                    for item in proposal.requirement_ids
                }:
                    self._invalid_accepted_question_ids.add(
                        request.question.question_id
                    )
                    requests.append(request)
                    continue
                requests.append(None)
        return tuple(requests)

    def _work_unit_requirement_groups(
        self, unresolved: tuple[str, ...]
    ) -> tuple[tuple[str, ...], ...]:
        groups: list[tuple[str, ...]] = []
        for offset in range(
            0, len(unresolved), MAX_WORK_UNIT_REQUIREMENTS_PER_QUESTION
        ):
            group = unresolved[
                offset : offset + MAX_WORK_UNIT_REQUIREMENTS_PER_QUESTION
            ]
            if group in self._oversized_work_unit_groups:
                groups.extend((requirement_id,) for requirement_id in group)
            else:
                groups.append(group)
        return tuple(groups)

    def _work_unit_context(
        self, ledger: SourceLedger, unresolved: tuple[str, ...]
    ) -> WorkUnitContext:
        requirements = {item.id: item for item in ledger.requirements}
        evidence_by_requirement = {
            requirement_id: self._evidence_for_requirement(requirement_id)
            for requirement_id in requirements
        }
        evidence = {
            item.id: item
            for items in evidence_by_requirement.values()
            for item in items
        }
        validation_catalog = {
            "validation-0001": {
                "id": "validation-0001",
                "description": "Run the configured repository verification command.",
                "kind": "local_command",
                "target": self.verification_command,
            }
        }
        return WorkUnitContext(
            requirements=requirements,
            unresolved_requirement_ids=unresolved,
            source=self.source,
            evidence=evidence,
            validation_catalog=validation_catalog,
            component_ids=frozenset({"repository"}),
            repository_paths=self.repository_paths,
            ambiguous_runtime_resolutions={},
            preferred_runtimes=self.preferred_runtimes,
            evidence_by_requirement=evidence_by_requirement,
            implementation_phases=self._implementation_phases,
        )

    def _work_unit_example(
        self, context: WorkUnitContext, requirement_ids: tuple[str, ...]
    ) -> Mapping[str, Any]:
        return {
            "records": [
                {
                    "requirement_ids": list(requirement_ids),
                    "implementation_phase_id": (
                        context.implementation_phases[0].id
                        if context.implementation_phases
                        else None
                    ),
                    "deliverable": "One cohesive illustrative deliverable.",
                    "planned_writes": [
                        {
                            "path": "example-output.txt",
                            "role": "documentation",
                            "requirement_ids": list(requirement_ids),
                            "evidence_ids": [],
                        }
                    ],
                    "implementation_step": "Describe one concrete implementation step.",
                    "acceptance_criteria": ["State one observable result."],
                    "required_outputs": [],
                }
            ],
            "has_more": False,
            "next_anchor": None,
        }

    @staticmethod
    def _runtime_fact_records(
        units: Sequence[WorkUnit],
    ) -> list[dict[str, Any]]:
        facts = {
            (unit.id, write.path, write.runtime)
            for unit in units
            for write in unit.planned_writes
        }
        return [
            {"unit_id": unit_id, "path": path, "runtime": runtime}
            for unit_id, path, runtime in sorted(
                facts, key=lambda fact: (fact[0], fact[1], fact[2] or "")
            )
        ]

    def accepted_work_units(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> tuple[WorkUnit, ...]:
        context, proposals = self._accepted_work_unit_proposals(accepted_answers)
        return allocate_work_units(proposals, context=context).units

    def _accepted_work_unit_proposals(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> tuple[WorkUnitContext, tuple[WorkUnitProposal, ...]]:
        proposal_answers = {
            question_id: answer
            for question_id, answer in accepted_answers.items()
            if question_id.startswith("propose-units-v6-")
        }
        cache_key = hashlib.sha256(
            json.dumps(
                {
                    "answers": proposal_answers,
                    "single_proposal_requirements": sorted(
                        self._single_proposal_requirements
                    ),
                    "supplemental_requirements": sorted(
                        self._supplemental_work_unit_requirements
                    ),
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        if (
            cache_key == self._accepted_work_unit_cache_key
            and self._accepted_work_unit_cache_value is not None
        ):
            return self._accepted_work_unit_cache_value
        result = self._compute_accepted_work_unit_proposals(accepted_answers)
        self._accepted_work_unit_cache_key = cache_key
        self._accepted_work_unit_cache_value = result
        return result

    def _compute_accepted_work_unit_proposals(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> tuple[WorkUnitContext, tuple[WorkUnitProposal, ...]]:
        requests = self._work_unit_requests(accepted_answers)
        pending = next((item for item in requests if item is not None), None)
        if pending is not None:
            raise ValueError(
                f"work-unit proposals are incomplete at {pending.question.question_id}"
            )
        ledger = self.accepted_ledger(accepted_answers)
        coverage = self.accepted_coverage(accepted_answers)
        unresolved = tuple(
            item.requirement_id for item in coverage if item.answer.status != "complete"
        )
        if not unresolved:
            return self._work_unit_context(ledger, ()), ()
        context = self._work_unit_context(ledger, unresolved)
        proposals: list[WorkUnitProposal] = []
        for requirement_ids in self._work_unit_requirement_groups(unresolved):
            remaining_requirement_ids = requirement_ids
            while True:
                request = build_work_unit_proposal_request(
                    context,
                    remaining_requirement_ids,
                    valid_example=self._work_unit_example(
                        context, remaining_requirement_ids
                    ),
                    maximum_proposals=(
                        1
                        if any(
                            item in self._single_proposal_requirements
                            for item in remaining_requirement_ids
                        )
                        else MAX_WORK_UNIT_PROPOSALS_PER_PAGE
                    ),
                )
                page = request.parse_answer(
                    accepted_answers[request.question.question_id]
                )
                proposals.extend(page.records)
                if not page.has_more:
                    break
                assert page.next_anchor is not None
                cursor_index = remaining_requirement_ids.index(page.next_anchor)
                remaining_requirement_ids = remaining_requirement_ids[
                    cursor_index + 1 :
                ]
        for requirement_id in sorted(self._supplemental_work_unit_requirements):
            single_ids = (requirement_id,)
            request = build_work_unit_proposal_request(
                context,
                single_ids,
                valid_example=self._work_unit_example(context, single_ids),
                maximum_proposals=(
                    1
                    if requirement_id in self._single_proposal_requirements
                    else MAX_WORK_UNIT_PROPOSALS_PER_PAGE
                ),
            )
            page = request.parse_answer(accepted_answers[request.question.question_id])
            proposals.extend(page.records)
        return context, tuple(proposals)

    def _item_groups(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> tuple[AllocationGroup, ...]:
        units = self.accepted_work_units(accepted_answers)
        if not units:
            return ()
        return allocate_work_unit_groups(units).groups

    def _item_description_requests(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> tuple[QuestionRequest[Any], ...]:
        units = self.accepted_work_units(accepted_answers)
        if not units:
            return ()
        by_id = {unit.id: unit for unit in units}
        phase_by_id = {phase.id: phase for phase in self._implementation_phases}

        def phase_for_group(group: AllocationGroup) -> Any:
            phase_ids = {
                by_id[unit_id].implementation_phase_id
                for unit_id in group.unit_ids
                if by_id[unit_id].implementation_phase_id is not None
            }
            if len(phase_ids) > 1:
                raise PlanningCompilationError(
                    "one allocated item crosses source implementation phases"
                )
            phase_id = next(iter(phase_ids), None)
            return None if phase_id is None else phase_by_id.get(phase_id)

        requests: list[QuestionRequest[Any]] = []
        accepted_titles: list[tuple[str, str]] = []
        for group in allocate_work_unit_groups(units).groups:
            request = build_item_description_request(
                group,
                by_id,
                already_used_titles=accepted_titles,
                implementation_phase=phase_for_group(group),
            )
            answer = accepted_answers.get(request.question.question_id)
            if answer is None:
                requests.append(request)
                continue
            if (
                self._accepted_question_input_digests is not None
                and self._accepted_question_input_digests.get(
                    request.question.question_id
                )
                != request.question.input_digest
            ):
                issue = "the work-unit details or prior item titles changed"
                self._invalid_accepted_question_ids.add(request.question.question_id)
                requests.append(
                    build_item_description_request(
                        group,
                        by_id,
                        already_used_titles=accepted_titles,
                        title_issue=issue,
                        implementation_phase=phase_for_group(group),
                    )
                )
                continue
            try:
                description = request.parse_answer(answer)
            except PlanningContractError as error:
                self._invalid_accepted_question_ids.add(request.question.question_id)
                requests.append(
                    build_item_description_request(
                        group,
                        by_id,
                        already_used_titles=accepted_titles,
                        title_issue=str(error),
                        implementation_phase=phase_for_group(group),
                    )
                )
                continue
            accepted_titles.append((group.id, description.title))
            requests.append(request)
        return tuple(requests)

    def accepted_item_descriptions(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> tuple[tuple[AllocationGroup, ItemDescription], ...]:
        groups = self._item_groups(accepted_answers)
        requests = self._item_description_requests(accepted_answers)
        result = []
        for group, request in zip(groups, requests, strict=True):
            answer = accepted_answers.get(request.question.question_id)
            if answer is None:
                raise ValueError(
                    f"item description is incomplete at {request.question.question_id}"
                )
            result.append((group, request.parse_answer(answer)))
        return tuple(result)

    def _dependency_requests(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> tuple[QuestionRequest[Any], ...]:
        allocation, discovery = self._dependency_discovery(accepted_answers)
        units = {unit.id: unit for unit in allocation.units}
        pairs: list[
            tuple[
                PendingUnitOutput,
                WorkUnit,
                WorkUnit,
                QuestionRequest[bool],
                set[str],
                tuple[str, ...],
            ]
        ] = []
        pending_reviews: list[QuestionRequest[Any]] = []
        for pending_output, consumer in discovery.confirmation_pairs:
            producer = units[pending_output.unit_id]
            allowed_requirement_ids = set(producer.requirement_ids) | set(
                consumer.requirement_ids
            )
            code_reference_paths = tuple(
                discovery.code_reference_paths.get((producer.id, consumer.id), ())
            )
            review = build_dependency_candidate_review_request(
                producer,
                consumer,
                pending_output.description,
                allowed_requirement_ids=allowed_requirement_ids,
                code_reference_paths=code_reference_paths,
            )
            pairs.append(
                (
                    pending_output,
                    consumer,
                    producer,
                    review,
                    allowed_requirement_ids,
                    code_reference_paths,
                )
            )
            answer = accepted_answers.get(review.question.question_id)
            if answer is not None and (
                self._accepted_question_input_digests is not None
                and self._accepted_question_input_digests.get(
                    review.question.question_id
                )
                != review.question.input_digest
            ):
                self._invalid_accepted_question_ids.add(review.question.question_id)
                answer = None
            if answer is None:
                pending_reviews.append(review)
                continue
            try:
                review.parse_answer(answer)
            except PlanningContractError:
                self._invalid_accepted_question_ids.add(review.question.question_id)
                pending_reviews.append(review)
        if pending_reviews:
            return self._batch_requests(pending_reviews)

        requests: list[QuestionRequest[Any]] = []
        for (
            pending_output,
            consumer,
            producer,
            review,
            allowed_ids,
            reference_paths,
        ) in pairs:
            if not review.parse_answer(accepted_answers[review.question.question_id]):
                continue
            details = build_confirmed_dependency_details_request(
                producer,
                consumer,
                pending_output.description,
                allowed_requirement_ids=allowed_ids,
                candidate_review=review,
                code_reference_paths=reference_paths,
            )
            answer = accepted_answers.get(details.question.question_id)
            if answer is not None and (
                self._accepted_question_input_digests is not None
                and self._accepted_question_input_digests.get(
                    details.question.question_id
                )
                != details.question.input_digest
            ):
                self._invalid_accepted_question_ids.add(details.question.question_id)
                answer = None
            if answer is not None:
                try:
                    details.parse_answer(answer)
                except PlanningContractError:
                    self._invalid_accepted_question_ids.add(
                        details.question.question_id
                    )
                    answer = None
            if answer is None:
                requests.append(details)
        return tuple(requests)

    def accepted_dependency_edges(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> tuple[UnitDependencyEdge, ...]:
        allocation, discovery = self._dependency_discovery(accepted_answers)
        units = {unit.id: unit for unit in allocation.units}
        edges = list(discovery.write_order_edges)
        edges.extend(
            implementation_phase_dependency_edges(
                allocation.units, self._implementation_phases
            )
        )
        for pending_output, consumer in discovery.confirmation_pairs:
            producer = units[pending_output.unit_id]
            allowed_requirement_ids = set(producer.requirement_ids) | set(
                consumer.requirement_ids
            )
            code_reference_paths = tuple(
                discovery.code_reference_paths.get((producer.id, consumer.id), ())
            )
            review = build_dependency_candidate_review_request(
                producer,
                consumer,
                pending_output.description,
                allowed_requirement_ids=allowed_requirement_ids,
                code_reference_paths=code_reference_paths,
            )
            if not review.parse_answer(accepted_answers[review.question.question_id]):
                continue
            request = build_confirmed_dependency_details_request(
                producer,
                consumer,
                pending_output.description,
                allowed_requirement_ids=allowed_requirement_ids,
                candidate_review=review,
                code_reference_paths=code_reference_paths,
            )
            answer = request.parse_answer(
                accepted_answers[request.question.question_id]
            )
            edges.append(
                UnitDependencyEdge(
                    producer.id,
                    consumer.id,
                    answer.output or pending_output.description,
                    answer.reason,
                    answer.requirement_ids,
                )
            )
        return tuple(edges)

    def _dependency_discovery(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> tuple[AllocatedWorkUnits, DependencyCandidateDiscovery]:
        context, proposals = self._accepted_work_unit_proposals(accepted_answers)
        allocation = allocate_work_units(proposals, context=context)
        related_requirement_ids = {
            requirement_id: requirement.related_requirement_ids
            for requirement_id, requirement in context.requirements.items()
        }
        paths_key = tuple(
            (unit.id, tuple(write.path for write in unit.planned_writes))
            for unit in allocation.units
        )
        if (
            paths_key != self._dependency_reference_cache_key
            and self.dependency_reference_search is not None
        ):
            self._dependency_reference_cache = self.dependency_reference_search(
                allocation.units
            )
            self._dependency_reference_cache_key = paths_key
        discovery = discover_dependency_candidates(
            allocation.units,
            allocation.pending_outputs,
            related_requirement_ids=related_requirement_ids,
            code_reference_paths=self._dependency_reference_cache,
            implementation_phase_order=(
                {
                    phase.id: index
                    for index, phase in enumerate(self._implementation_phases)
                }
                if self._implementation_phases
                else None
            ),
        )
        return allocation, discovery

    def accepted_assembly(
        self,
        accepted_answers: Mapping[str, Mapping[str, Any]],
        *,
        plan_id: str,
        workflow_directory: str | Path = "docs/workflows",
    ) -> PlanningAssembly:
        """Freeze CP-SAT groups and project confirmed unit edges into plan items."""
        assembly_answers = tuple(
            sorted(
                (question_id, answer)
                for question_id, answer in accepted_answers.items()
                if not question_id.startswith(
                    ("structural-subjects-", "describe-structural-change-")
                )
            )
        )
        assembly_question_ids = {question_id for question_id, _ in assembly_answers}
        accepted_digests = (
            ()
            if self._accepted_question_input_digests is None
            else tuple(
                sorted(
                    (question_id, digest)
                    for question_id, digest in (
                        self._accepted_question_input_digests.items()
                    )
                    if question_id in assembly_question_ids
                )
            )
        )
        cache_key = (
            plan_id,
            str(workflow_directory),
            assembly_answers,
            accepted_digests,
            tuple(sorted(self._oversized_work_unit_groups)),
            tuple(sorted(self._single_proposal_requirements)),
            tuple(sorted(self._supplemental_work_unit_requirements)),
        )
        if (
            cache_key == self._accepted_assembly_cache_key
            and self._accepted_assembly_cache_value is not None
        ):
            return self._accepted_assembly_cache_value
        assembly = self._compile_accepted_assembly(
            accepted_answers,
            plan_id=plan_id,
            workflow_directory=workflow_directory,
        )
        self._accepted_assembly_cache_key = cache_key
        self._accepted_assembly_cache_value = assembly
        return assembly

    def _compile_accepted_assembly(
        self,
        accepted_answers: Mapping[str, Mapping[str, Any]],
        *,
        plan_id: str,
        workflow_directory: str | Path,
    ) -> PlanningAssembly:
        proposals_context, proposals = self._accepted_work_unit_proposals(
            accepted_answers
        )
        allocated = allocate_work_units(proposals, context=proposals_context)
        item_groups = allocate_work_unit_groups(allocated.units).groups
        unit_to_item: dict[str, str] = {}
        item_unit_ids: dict[str, tuple[str, ...]] = {}
        group_by_item: dict[str, AllocationGroup] = {}
        descriptions = dict(self.accepted_item_descriptions(accepted_answers))
        for ordinal, group in enumerate(item_groups, start=1):
            item_id = f"item-{ordinal:04d}"
            unit_ids = group.unit_ids
            item_unit_ids[item_id] = unit_ids
            group_by_item[item_id] = group
            unit_to_item.update({unit_id: item_id for unit_id in unit_ids})
        edges = self.accepted_dependency_edges(accepted_answers)
        item_order_hint = tuple(item_unit_ids)
        graph = compile_dependency_graph(
            allocated.units,
            unit_to_item,
            edges,
            item_order_hint=item_order_hint,
        )
        prerequisites: dict[str, list[str]] = {unit.id: [] for unit in allocated.units}
        for edge in graph.unit_edges:
            prerequisites[edge.after_unit_id].append(edge.before_unit_id)
        finalized_units = tuple(
            replace(unit, prerequisite_unit_ids=tuple(sorted(prerequisites[unit.id])))
            for unit in allocated.units
        )
        dependency_map: dict[str, list[str]] = {
            item_id: [] for item_id in item_order_hint
        }
        for item_edge in graph.item_edges:
            dependency_map[item_edge.after_item_id].append(item_edge.before_item_id)

        units_by_id = {unit.id: unit for unit in finalized_units}
        descriptions_by_group = {
            group.id: description for group, description in descriptions.items()
        }
        workflow_root = Path(workflow_directory)
        workflow_ids = {
            item_id: f"{slugify_workflow_id(plan_id)}-pr-{ordinal:02d}"
            for ordinal, item_id in enumerate(graph.item_order, start=1)
        }
        plan_items: list[PlannedWorkItem] = []
        ordered_allocations: dict[str, tuple[str, ...]] = {}
        for item_id in graph.item_order:
            group = group_by_item[item_id]
            description = descriptions_by_group[group.id]
            item_units = [units_by_id[unit_id] for unit_id in group.unit_ids]
            workflow_id = workflow_ids[item_id]
            plan_items.append(
                PlannedWorkItem(
                    item_id=item_id,
                    title=description.title,
                    objective=description.objective,
                    scope=tuple(item.description for item in description.scope),
                    acceptance_criteria=tuple(
                        dict.fromkeys(
                            criterion
                            for unit in item_units
                            for criterion in unit.acceptance_criteria
                        )
                    ),
                    dependencies=tuple(sorted(dependency_map[item_id])),
                    workflow_directory=None,
                    workflow_id=workflow_id,
                    workflow_file=(
                        workflow_root
                        / slugify_workflow_id(plan_id)
                        / f"{workflow_id}.yaml"
                    ).as_posix(),
                    implementation_steps=tuple(
                        unit.implementation_step for unit in item_units
                    ),
                    allowed_paths=tuple(
                        dict.fromkeys(
                            write.path
                            for unit in item_units
                            for write in unit.planned_writes
                        )
                    ),
                )
            )
            ordered_allocations[item_id] = item_unit_ids[item_id]
        plan = ReviewedExecutionPlan(
            plan_id,
            tuple(plan_items),
            self.source.document.path,
        )
        return PlanningAssembly(
            plan,
            finalized_units,
            ordered_allocations,
            graph,
        )

    def _structrr_item_records(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> tuple[dict[str, Any], ...]:
        assembly = self.accepted_assembly(
            accepted_answers,
            plan_id=self.plan_id,
        )
        pending_test_designs, test_designs = self._accepted_test_coverage(
            accepted_answers
        )
        if pending_test_designs:
            raise PlanningCompilationError(
                "per-PR test design is incomplete at "
                f"{pending_test_designs[0].question.question_id}"
            )
        units = {unit.id: unit for unit in assembly.units}
        records = []
        for item in assembly.plan.items:
            unit_ids = assembly.item_unit_ids[item.item_id]
            records.append(
                {
                    "id": item.item_id,
                    "dependencies": list(item.dependencies),
                    "allowed_paths": list(item.allowed_paths),
                    "acceptance_criteria": list(item.acceptance_criteria),
                    "verification": test_designs[item.item_id],
                    "unit_ids": list(unit_ids),
                    "units": [units[unit_id] for unit_id in unit_ids],
                }
            )
        return tuple(records)

    def _accepted_test_coverage(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> tuple[tuple[QuestionRequest[Any], ...], dict[str, dict[str, Any]]]:
        assembly = self.accepted_assembly(accepted_answers, plan_id=self.plan_id)
        units_by_id = {unit.id: unit for unit in assembly.units}
        ledger = self.accepted_ledger(accepted_answers)
        requirements = {item.id: item for item in ledger.requirements}
        anchors = {anchor.id: anchor for anchor in self.source.anchors}
        pending: list[QuestionRequest[Any]] = []
        accepted: dict[str, dict[str, Any]] = {}
        for item in assembly.plan.items:
            unit_ids = assembly.item_unit_ids[item.item_id]
            item_units = tuple(units_by_id[unit_id] for unit_id in unit_ids)
            source_refs = tuple(
                dict.fromkeys(
                    source_ref
                    for unit in item_units
                    for requirement_id in unit.requirement_ids
                    for source_ref in requirements[requirement_id].source_refs
                )
            )
            excerpt_records: list[SourceExcerpt] = []
            for ref in source_refs:
                anchor = anchors.get(ref)
                if anchor is None:
                    continue
                text = self.source.document.text[
                    anchor.start_offset : anchor.end_offset
                ]
                if len(text) <= 1_500:
                    excerpt_records.append(
                        SourceExcerpt(ref, text, anchor.start_offset)
                    )
                if len(excerpt_records) == 4:
                    break
            excerpts = tuple(excerpt_records)
            test_paths = tuple(
                sorted(
                    path
                    for path in self.repository_paths
                    if _is_test_path(path)
                    and _path_is_in_scope(path, item.allowed_paths)
                )[:40]
            )
            request = build_test_coverage_request(
                {
                    "id": item.item_id,
                    "title": item.title,
                    "objective": item.objective,
                    "scope": item.scope,
                    "implementation_steps": item.implementation_steps,
                    "acceptance_criteria": item.acceptance_criteria,
                    "allowed_paths": item.allowed_paths,
                },
                existing_test_paths=test_paths,
                source_excerpts=excerpts,
            )
            question_id = request.question.question_id
            answer = accepted_answers.get(question_id)
            if (
                answer is not None
                and self._accepted_question_input_digests is not None
                and self._accepted_question_input_digests.get(question_id)
                != request.question.input_digest
            ):
                self._invalid_accepted_question_ids.add(question_id)
                answer = None
            if answer is None:
                pending.append(request)
                continue
            try:
                accepted[item.item_id] = request.parse_answer(answer)
            except PlanningContractError:
                self._invalid_accepted_question_ids.add(question_id)
                pending.append(request)
        return tuple(pending), accepted

    def _structrr_requests(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> tuple[QuestionRequest[Any], ...]:
        if self.structrr_baseline is None:
            return ()
        item_records = self._structrr_item_records(accepted_answers)
        assembly = self.accepted_assembly(accepted_answers, plan_id=self.plan_id)
        ledger = self.accepted_ledger(accepted_answers)
        requirement_by_id = {item.id: item for item in ledger.requirements}
        record_by_id = {str(record["id"]): record for record in item_records}
        planned_python_paths = tuple(
            sorted(
                {
                    write.path
                    for record in item_records
                    for unit in record["units"]
                    for write in unit.planned_writes
                    if write.path.casefold().endswith((".py", ".pyi"))
                }
            )
        )
        if (
            self.structural_symbol_search is not None
            and planned_python_paths
            and planned_python_paths != self._structural_symbol_cache_key
        ):
            symbol_search = self.structural_symbol_search(planned_python_paths)
            if isinstance(symbol_search, CodeSymbolSearchResult):
                self._structural_symbol_cache = symbol_search.symbols_by_path
                self._structural_symbol_coverage = symbol_search.coverage
            else:
                self._structural_symbol_cache = symbol_search
                self._structural_symbol_coverage = {}
            self._structural_symbol_cache_key = planned_python_paths
        if (
            self.structural_reference_search is not None
            and planned_python_paths
            and planned_python_paths != self._structural_reference_cache_key
        ):
            self._structural_reference_cache = self.structural_reference_search(
                planned_python_paths
            )
            self._structural_reference_cache_key = planned_python_paths
        order = assembly.dependency_graph.item_order
        prerequisites: dict[str, set[str]] = {item_id: set() for item_id in order}
        for edge in assembly.dependency_graph.item_edges:
            prerequisites[edge.after_item_id].add(edge.before_item_id)
        completed: set[str] = set()
        prior_operations: list[Mapping[str, Any]] = []
        projected: Mapping[str, Any] = self.structrr_baseline
        while len(completed) < len(order):
            progress_key = tuple(sorted(completed))
            baseline_index = self._structrr_baseline_index_cache.get(progress_key)
            if baseline_index is None:
                baseline_index = index_structural_baseline(projected)
                self._structrr_baseline_index_cache[progress_key] = baseline_index
            baseline_digest = baseline_index.fingerprint
            ready_item_ids = tuple(
                item_id
                for item_id in order
                if item_id not in completed and prerequisites[item_id] <= completed
            )
            if not ready_item_ids:
                raise PlanningCompilationError(
                    "Structrr planning item graph has no ready item"
                )
            candidate_subjects_by_item: dict[
                str,
                tuple[
                    tuple[BoundStructuralSubject, ...],
                    tuple[tuple[str, str], ...],
                ],
            ] = {}
            pending_candidate_reviews: list[QuestionRequest[Any]] = []
            for item_id in ready_item_ids:
                record = record_by_id[item_id]
                units = tuple(record["units"])
                requirement_ids = tuple(
                    dict.fromkeys(
                        requirement_id
                        for unit in units
                        for requirement_id in unit.requirement_ids
                    )
                )
                source_refs = tuple(
                    dict.fromkeys(
                        source_ref
                        for requirement_id in requirement_ids
                        for source_ref in requirement_by_id[requirement_id].source_refs
                    )
                )
                item_python_paths = {
                    write.path
                    for unit in units
                    for write in unit.planned_writes
                    if write.path.casefold().endswith((".py", ".pyi"))
                }
                item_symbols_by_path = {
                    path: self._structural_symbol_cache[path]
                    for path in sorted(item_python_paths)
                    if path in self._structural_symbol_cache
                }
                planned_file_subjects = _planned_file_subjects(
                    item_id,
                    units,
                    requirement_by_id=requirement_by_id,
                    projected_baseline=projected,
                    fallback_source_refs=source_refs,
                )
                deterministic_subjects = _planned_existing_structural_subjects(
                    item_id,
                    units,
                    requirement_by_id=requirement_by_id,
                    projected_baseline=projected,
                    fallback_source_refs=source_refs,
                    basedpyright_symbols_by_path=item_symbols_by_path,
                )
                relevant_candidates: list[BoundStructuralSubject] = []
                excluded_candidates: list[tuple[str, str]] = []
                for candidate in deterministic_subjects:
                    review = build_structural_candidate_review_request(
                        candidate,
                        source=self.source,
                        units=units,
                        projected_baseline=projected,
                        baseline_index=baseline_index,
                    )
                    answer = accepted_answers.get(review.question.question_id)
                    if (
                        answer is not None
                        and self._accepted_question_input_digests is not None
                        and self._accepted_question_input_digests.get(
                            review.question.question_id
                        )
                        != review.question.input_digest
                    ):
                        self._invalid_accepted_question_ids.add(
                            review.question.question_id
                        )
                        answer = None
                    if answer is None:
                        pending_candidate_reviews.append(review)
                        continue
                    try:
                        relevant = review.parse_answer(answer)
                    except PlanningContractError:
                        self._invalid_accepted_question_ids.add(
                            review.question.question_id
                        )
                        pending_candidate_reviews.append(review)
                        continue
                    if relevant:
                        relevant_candidates.append(candidate)
                    else:
                        excluded_candidates.append(
                            (candidate.section, candidate.subject_id)
                        )
                candidate_subjects_by_item[item_id] = (
                    tuple((*planned_file_subjects, *relevant_candidates)),
                    tuple(excluded_candidates),
                )
            if pending_candidate_reviews:
                return self._batch_requests(pending_candidate_reviews)

            subjects_by_item: dict[str, tuple[BoundStructuralSubject, ...]] = {}
            pending_enumerations: list[QuestionRequest[Any]] = []
            for item_id in ready_item_ids:
                record = record_by_id[item_id]
                units = tuple(record["units"])
                requirement_ids = tuple(
                    dict.fromkeys(
                        requirement_id
                        for unit in units
                        for requirement_id in unit.requirement_ids
                    )
                )
                source_refs = tuple(
                    dict.fromkeys(
                        source_ref
                        for requirement_id in requirement_ids
                        for source_ref in requirement_by_id[requirement_id].source_refs
                    )
                )
                item_python_paths = {
                    write.path
                    for unit in units
                    for write in unit.planned_writes
                    if write.path.casefold().endswith((".py", ".pyi"))
                }
                item_reference_path_pairs = _planned_reference_path_pairs(
                    tuple(item_python_paths), self._structural_reference_cache
                )
                item_subjects, excluded_subject_ids = candidate_subjects_by_item[
                    item_id
                ]
                self._structural_subject_context_cache[item_id] = (
                    item_subjects,
                    excluded_subject_ids,
                    item_reference_path_pairs,
                )
                scopes = partition_subject_enumeration_units(
                    plan_id=self.plan_id,
                    item_id=item_id,
                    units=units,
                    requirement_source_refs={
                        requirement_id: requirement_by_id[requirement_id].source_refs
                        for unit in units
                        for requirement_id in unit.requirement_ids
                    },
                    source=self.source,
                    projected_baseline=projected,
                    prior_subjects=item_subjects,
                    excluded_subject_ids=excluded_subject_ids,
                    basedpyright_reference_path_pairs=item_reference_path_pairs,
                    baseline_digest=baseline_digest,
                )
                self._structural_subject_scope_cache[item_id] = scopes
                completed_item_subjects: list[BoundStructuralSubject] = list(
                    item_subjects
                )
                for scope in scopes:
                    scope_id = scope.scope_id
                    scope_units = scope.units
                    scope_source_refs = scope.source_refs
                    scope_paths = {
                        write.path
                        for unit in scope_units
                        for write in unit.planned_writes
                    }
                    scope_reference_pairs = tuple(
                        pair
                        for pair in item_reference_path_pairs
                        if pair[0] in scope_paths and pair[1] in scope_paths
                    )
                    page_ordinal = 1
                    scope_pages: list[StructuralSubjectPage] = []
                    scope_subjects: list[BoundStructuralSubject] = []
                    while True:
                        enumeration_request = build_subject_enumeration_request(
                            plan_id=self.plan_id,
                            item_id=item_id,
                            units=scope_units,
                            source=self.source,
                            source_refs=scope_source_refs,
                            projected_baseline=projected,
                            prior_subjects=(*item_subjects, *scope_subjects),
                            excluded_subject_ids=excluded_subject_ids,
                            basedpyright_reference_path_pairs=scope_reference_pairs,
                            page_ordinal=page_ordinal,
                            baseline_digest=baseline_digest,
                            scope_id=scope_id,
                            baseline_subject_index=scope.baseline_subject_index,
                            code_symbol_coverage=self._structural_symbol_coverage,
                        )
                        question_id = enumeration_request.question.question_id
                        answer = accepted_answers.get(question_id)
                        if (
                            answer is not None
                            and self._accepted_question_input_digests is not None
                            and self._accepted_question_input_digests.get(question_id)
                            != enumeration_request.question.input_digest
                        ):
                            self._invalid_accepted_question_ids.add(question_id)
                            answer = None
                        if answer is None:
                            pending_enumerations.append(enumeration_request)
                            break
                        try:
                            page = enumeration_request.parse_answer(answer)
                        except PlanningContractError:
                            self._invalid_accepted_question_ids.add(question_id)
                            pending_enumerations.append(enumeration_request)
                            break
                        scope_pages.append(page)
                        scope_subjects.extend(page.records)
                        if not page.has_more:
                            completed_item_subjects.extend(
                                complete_subject_enumeration(scope_pages)
                            )
                            break
                        page_ordinal += 1
                if not any(
                    request.question.subject_ids[0] == item_id
                    for request in pending_enumerations
                ):
                    subjects_by_item[item_id] = coalesce_structural_subjects(
                        completed_item_subjects
                    )
            if pending_enumerations:
                return self._batch_requests(pending_enumerations)

            known_entity_ids = {
                str(entity.get("id"))
                for entity in projected.get("entities", [])
                if isinstance(entity, Mapping) and entity.get("id")
            }
            known_entity_ids.update(
                subject.subject_id
                for subjects in subjects_by_item.values()
                for subject in subjects
                if subject.section == "entities"
            )
            pending_changes: list[QuestionRequest[Any]] = []
            batch_operations: list[Mapping[str, Any]] = []
            for item_id in ready_item_ids:
                record = record_by_id[item_id]
                units = tuple(record["units"])
                planned_paths = {
                    write.path for unit in units for write in unit.planned_writes
                }
                for subject in subjects_by_item[item_id]:
                    if subject.section == "files":
                        change = StructuralChange(
                            subject.subject_id,
                            subject.section,
                            subject.item_id,
                            subject.action,
                            "change" if subject.is_new else "unchanged",
                            {"path": subject.planned_path}
                            if subject.is_new and subject.planned_path is not None
                            else None,
                            (
                                "Planned file path is absent from projected baseline."
                                if subject.is_new
                                else "File path already exists in projected baseline."
                            ),
                            subject.requirement_ids,
                            subject.unit_ids,
                            subject.source_refs,
                        )
                        operation = bind_structural_operation(change)
                        if operation is not None:
                            batch_operations.append(operation)
                        continue
                    request_key = (progress_key, subject.subject_id)
                    change_request = self._structural_change_request_cache.get(
                        request_key
                    )
                    if change_request is None:
                        change_request = build_structural_change_request(
                            subject,
                            projected_baseline=projected,
                            taxonomy_entity_types=self.taxonomy_entity_types,
                            planned_paths=planned_paths,
                            known_entity_ids=known_entity_ids,
                            source=self.source,
                            units=units,
                            baseline_digest=baseline_digest,
                            baseline_index=baseline_index,
                            valid_example={
                                "result": "unchanged",
                                "reason": (
                                    "The projected state already satisfies this "
                                    "subject."
                                ),
                            },
                        )
                        self._structural_change_request_cache[request_key] = (
                            change_request
                        )
                    answer = accepted_answers.get(change_request.question.question_id)
                    if answer is None:
                        pending_changes.append(change_request)
                        continue
                    change = change_request.parse_answer(answer)
                    operation = bind_structural_operation(change)
                    if operation is not None:
                        batch_operations.append(operation)
            if pending_changes:
                return self._batch_requests(pending_changes)
            all_operations = (*prior_operations, *batch_operations)
            reject_unordered_subject_conflicts(all_operations, prerequisites)
            projected = project_structural_operations(projected, batch_operations)
            prior_operations.extend(batch_operations)
            completed.update(ready_item_ids)
        return ()

    def _accepted_structural_subjects(
        self,
        item_id: str,
        units: tuple[WorkUnit, ...],
        requirement_source_refs: Mapping[str, Sequence[str]],
        projected_baseline: Mapping[str, Any],
        accepted_answers: Mapping[str, Mapping[str, Any]],
    ) -> tuple[BoundStructuralSubject, ...]:
        scopes = self._structural_subject_scope_cache.get(item_id)
        item_subjects, excluded_subject_ids, reference_path_pairs = (
            self._structural_subject_context_cache.get(item_id, ((), (), ()))
        )
        subjects: list[BoundStructuralSubject] = list(item_subjects)
        if scopes is None:
            scopes = partition_subject_enumeration_units(
                plan_id=self.plan_id,
                item_id=item_id,
                units=units,
                requirement_source_refs=requirement_source_refs,
                source=self.source,
                projected_baseline=projected_baseline,
                prior_subjects=item_subjects,
                excluded_subject_ids=excluded_subject_ids,
                basedpyright_reference_path_pairs=reference_path_pairs,
            )
        for scope in scopes:
            scope_id = scope.scope_id
            scope_units = scope.units
            source_refs = scope.source_refs
            scope_paths = {
                write.path for unit in scope_units for write in unit.planned_writes
            }
            scope_reference_pairs = tuple(
                pair
                for pair in reference_path_pairs
                if pair[0] in scope_paths and pair[1] in scope_paths
            )
            page_ordinal = 1
            prior_subjects: list[BoundStructuralSubject] = []
            pages: list[StructuralSubjectPage] = []
            while True:
                request = build_subject_enumeration_request(
                    plan_id=self.plan_id,
                    item_id=item_id,
                    units=scope_units,
                    source=self.source,
                    source_refs=source_refs,
                    projected_baseline=projected_baseline,
                    prior_subjects=(*item_subjects, *prior_subjects),
                    excluded_subject_ids=excluded_subject_ids,
                    basedpyright_reference_path_pairs=scope_reference_pairs,
                    page_ordinal=page_ordinal,
                    scope_id=scope_id,
                    baseline_subject_index=scope.baseline_subject_index,
                    code_symbol_coverage=self._structural_symbol_coverage,
                )
                answer = accepted_answers.get(request.question.question_id)
                if answer is None:
                    raise ValueError(
                        "Structrr subject enumeration is incomplete at "
                        f"{request.question.question_id}"
                    )
                page = request.parse_answer(answer)
                pages.append(page)
                prior_subjects = list(
                    coalesce_structural_subjects((*prior_subjects, *page.records))
                )
                if not page.has_more:
                    subjects.extend(complete_subject_enumeration(pages))
                    break
                page_ordinal += 1
        return coalesce_structural_subjects(subjects)

    def _structrr_source_ref(self) -> str:
        return (
            f"{self.source.document.path}#sha256:{self.source.document.content_sha256}"
        )

    def accepted_structrr_proposal(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> MultiPRStructrrProposal:
        if self.structrr_baseline is None:
            raise ValueError("Structrr baseline is required to compile the plan")
        # _structrr_requests parses every accepted page and change and therefore
        # deterministically reconstructs the canonical operation list.
        pending = self._structrr_requests(accepted_answers)
        if pending:
            raise ValueError(
                f"Structrr planning is incomplete at {pending[0].question.question_id}"
            )
        item_records = self._structrr_item_records(accepted_answers)
        operations: list[Mapping[str, Any]] = []
        assembly = self.accepted_assembly(accepted_answers, plan_id=self.plan_id)
        ledger = self.accepted_ledger(accepted_answers)
        requirement_by_id = {item.id: item for item in ledger.requirements}
        projected: Mapping[str, Any] = self.structrr_baseline
        for record in item_records:
            item_id = str(record["id"])
            item_operations: list[Mapping[str, Any]] = []
            units = tuple(record["units"])
            subjects = self._accepted_structural_subjects(
                item_id,
                units,
                {
                    requirement_id: requirement.source_refs
                    for requirement_id, requirement in requirement_by_id.items()
                },
                projected,
                accepted_answers,
            )
            for subject in subjects:
                if subject.section == "files":
                    change = StructuralChange(
                        subject.subject_id,
                        subject.section,
                        subject.item_id,
                        subject.action,
                        "change" if subject.is_new else "unchanged",
                        {"path": subject.planned_path}
                        if subject.is_new and subject.planned_path is not None
                        else None,
                        "Planned file path is absent from projected baseline."
                        if subject.is_new
                        else "File path already exists in projected baseline.",
                        subject.requirement_ids,
                        subject.unit_ids,
                        subject.source_refs,
                        subject.code_evidence,
                    )
                    operation = bind_structural_operation(change)
                    if operation is not None:
                        operations.append(operation)
                        item_operations.append(operation)
                    continue
                request = build_structural_change_request(
                    subject,
                    projected_baseline=projected,
                    taxonomy_entity_types=self.taxonomy_entity_types,
                    planned_paths={
                        write.path for unit in units for write in unit.planned_writes
                    },
                    known_entity_ids={
                        str(entity.get("id"))
                        for entity in projected.get("entities", [])
                        if isinstance(entity, Mapping) and entity.get("id")
                    },
                    valid_example={
                        "result": "unchanged",
                        "reason": (
                            "The projected state already satisfies this subject."
                        ),
                    },
                )
                change = request.parse_answer(
                    accepted_answers[request.question.question_id]
                )
                operation = bind_structural_operation(change)
                if operation is not None:
                    operations.append(operation)
                    item_operations.append(operation)
            projected = project_structural_operations(projected, item_operations)
        return compile_structrr_plan(
            plan_id=self.plan_id,
            baseline_snapshot=self.structrr_baseline,
            items=item_records,
            item_order=assembly.dependency_graph.item_order,
            operations=operations,
            source_ref=self._structrr_source_ref(),
        )

    def _accepted_human_setups(
        self,
        ledger: SourceLedger,
        accepted_answers: Mapping[str, Mapping[str, Any]],
        assembly: PlanningAssembly,
        validation: Validation,
    ) -> tuple[tuple[ManualSetup, str], ...]:
        requirements = tuple(
            item for item in ledger.requirements if item.kind == "human_setup"
        )
        if not requirements:
            return ()
        known_requirement_ids = {item.id for item in ledger.requirements}
        unit_by_id = {unit.id: unit for unit in assembly.units}
        setups: list[tuple[ManualSetup, str]] = []
        for ordinal, requirement in enumerate(requirements, start=1):
            need = HumanSetupNeed(
                True,
                requirement.statement,
                requirement.source_refs,
                (),
            )
            setup_validation = Validation(
                validation.id,
                validation.description,
                validation.kind,
                validation.target,
                (requirement.id,),
                requirement.source_refs,
                (),
            )
            request = build_human_setup_description_request(
                setup_validation,
                need,
                source=self.source,
                evidence={},
                setup_id=requirement.id,
            )
            answer = accepted_answers.get(request.question.question_id)
            if answer is None:
                raise PlanningCompilationError(
                    "human setup description is incomplete at "
                    f"{request.question.question_id}"
                )
            proposal = request.parse_answer(answer)
            setup = bind_manual_setup(
                f"setup-{ordinal:04d}",
                proposal,
                need=need,
                requirement_ids=(requirement.id,),
                known_requirement_ids=known_requirement_ids,
                validation_ids=(validation.id,),
                known_validation_ids={validation.id},
            )
            owner_item_id = next(
                (
                    item.item_id
                    for item in assembly.plan.items
                    if any(
                        requirement.id in unit_by_id[unit_id].requirement_ids
                        for unit_id in assembly.item_unit_ids[item.item_id]
                    )
                ),
                assembly.plan.items[0].item_id,
            )
            setups.append((setup, owner_item_id))
        return tuple(setups)

    def compile_accepted_plan(
        self,
        accepted_answers: Mapping[str, Mapping[str, Any]],
        *,
        plan_id: str,
        plan_path: str,
        proposal_path: str,
    ) -> CompiledPlan:
        """Compile accepted bounded answers into a validated artifact bundle."""
        ledger = self.accepted_ledger(accepted_answers)
        coverage = self.accepted_coverage(accepted_answers)
        assembly = self.accepted_assembly(accepted_answers, plan_id=plan_id)
        if not assembly.units:
            raise PlanningCompilationError(
                "the pinned repository already covers every requirement; no work "
                "items remain to publish"
            )
        proposal = self.accepted_structrr_proposal(accepted_answers)
        dispositions = {item.block_id: item for item in ledger.dispositions}
        coverage_by_id = {item.requirement_id: item for item in coverage}
        evidence = self._evidence_catalog()
        validation = Validation(
            "validation-0001",
            "Run the configured repository verification command.",
            "local_command",
            self.verification_command,
            tuple(
                item.id
                for item in ledger.requirements
                if coverage_by_id[item.id].answer.status != "complete"
            ),
            tuple(
                dict.fromkeys(
                    source_ref
                    for requirement in ledger.requirements
                    for source_ref in requirement.source_refs
                )
            ),
            (),
        )
        if not validation.requirement_ids:
            raise PlanningCompilationError(
                "no unresolved requirement uses the repository verification command"
            )
        validation_coverage = []
        units_by_id = {unit.id: unit for unit in assembly.units}
        for item in assembly.plan.items:
            unit_ids = assembly.item_unit_ids[item.item_id]
            item_validation_ids = tuple(
                dict.fromkeys(
                    validation_id
                    for unit_id in unit_ids
                    for validation_id in units_by_id[unit_id].validation_ids
                )
            )
            for criterion in item.acceptance_criteria:
                validation_coverage.append(
                    {
                        "item_id": item.item_id,
                        "criterion": criterion,
                        "validation_ids": list(item_validation_ids),
                        "reason": (
                            "The configured repository verification command is "
                            "the accepted validation for this criterion."
                        ),
                    }
                )

        human_setups = self._accepted_human_setups(
            ledger, accepted_answers, assembly, validation
        )
        setup_records = [
            {**setup.to_data(), "owner_item_id": owner_item_id}
            for setup, owner_item_id in human_setups
        ]
        setups_by_owner: dict[str, list[ManualSetup]] = {}
        for setup, owner_item_id in human_setups:
            setups_by_owner.setdefault(owner_item_id, []).append(setup)
        plan_items = tuple(
            replace(
                item,
                manual_prerequisite=combine_manual_prerequisite_data(
                    [setup.to_data() for setup in setups_by_owner[item.item_id]],
                    validation_targets=(validation.target,),
                ),
            )
            if item.item_id in setups_by_owner
            else item
            for item in assembly.plan.items
        )

        source_record = {
            "repository": self.source.document.repository,
            "commit_sha": self.source.document.commit_sha,
            "path": self.source.document.path,
            "blob_sha": self.source.document.blob_sha,
            "content_sha256": self.source.document.content_sha256,
        }
        base_record = {
            "commit_sha": self.source.document.commit_sha,
            "structrr_fingerprint": snapshot_digest(self.structrr_baseline or {}),
        }
        manifest = PlanningManifest.create(
            {
                "source": source_record,
                "base": base_record,
                "source_blocks": [
                    {
                        "id": block.id,
                        "disposition": dispositions[block.id].disposition,
                        "requirement_ids": list(dispositions[block.id].requirement_ids),
                        "reason": dispositions[block.id].reason,
                    }
                    for block in self.source.blocks
                ],
                "source_anchors": [anchor.to_data() for anchor in self.source.anchors],
                "requirements": [
                    {
                        "id": requirement.id,
                        "statement": requirement.statement,
                        "kind": requirement.kind,
                        "source_refs": list(requirement.source_refs),
                        "related_requirement_ids": list(
                            requirement.related_requirement_ids
                        ),
                    }
                    for requirement in ledger.requirements
                ],
                "coverage": [
                    {
                        "requirement_id": item.requirement_id,
                        "status": item.answer.status,
                        "evidence_ids": list(item.answer.evidence_ids),
                        "reason": item.answer.reason,
                        **(
                            {"remaining_statement": item.answer.remaining_statement}
                            if item.answer.status == "partial"
                            else {}
                        ),
                    }
                    for item in coverage
                ],
                "validation_coverage": validation_coverage,
                "evidence": [item.to_data() for item in evidence.values()],
                "units": [unit.to_data() for unit in assembly.units],
                "implementation_phases": [
                    phase.to_data() for phase in self._implementation_phases
                ],
                "item_unit_ids": {
                    item_id: list(unit_ids)
                    for item_id, unit_ids in assembly.item_unit_ids.items()
                },
                "runtime_facts": self._runtime_fact_records(assembly.units),
                "validations": [validation.to_data()],
                "setups": setup_records,
                "structrr": {
                    "baseline_fingerprint": proposal.to_data()["baseline_fingerprint"],
                    "proposal_path": proposal_path,
                    "proposal_fingerprint": proposal.to_data()["fingerprint"],
                },
                "latest_repair": None,
            }
        )
        plan = replace(assembly.plan, items=plan_items, planning=manifest.to_data())
        repository_snapshot = {"source": source_record, "base": base_record}
        compiled = compile_plan(
            plan=plan,
            planning_manifest=manifest,
            repository_snapshot=repository_snapshot,
            structrr_proposal=proposal,
            verification_command=self.verification_command,
            plan_path=plan_path,
        )
        report = validate_compiled_plan(compiled, repository_snapshot)
        if not report.valid:
            raise PlanningCompilationError(
                "compiled plan validation failed: " + "; ".join(report.errors)
            )
        return compiled

    def accepted_ledger(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> SourceLedger:
        """Parse completed source-stage answers into canonical typed records."""
        source_answers = {
            question_id: answer
            for question_id, answer in accepted_answers.items()
            if question_id.startswith(
                (
                    "q-extract-requirements-",
                    "q-classify-source-block-",
                    "q-check-requirement-fidelity-",
                    "q-compare-requirement-equivalence-",
                )
            )
        }
        cache_key = hashlib.sha256(
            json.dumps(
                source_answers,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        if (
            cache_key == self._accepted_ledger_cache_key
            and self._accepted_ledger_cache_value is not None
        ):
            return self._accepted_ledger_cache_value
        pending = self._next_source_question(accepted_answers)
        if pending is not None:
            raise ValueError(
                f"source ledger is incomplete at {pending.question.question_id}"
            )
        ledger = self._parse_source_ledger(accepted_answers)
        if cache_key != self._accepted_ledger_cache_key:
            self._fidelity_request_cache.clear()
            self._validated_fidelity_answers.clear()
            self._coverage_request_cache.clear()
            self._accepted_coverage_cache.clear()
            self._accepted_work_unit_cache_key = None
            self._accepted_work_unit_cache_value = None
        self._accepted_ledger_cache_key = cache_key
        self._accepted_ledger_cache_value = ledger
        return ledger

    def _parse_source_ledger(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> SourceLedger:
        anchor_by_id = {anchor.id: anchor for anchor in self.source.anchors}
        raw_records: list[dict[str, Any]] = []
        for block in self.source.blocks:
            cursor: str | None = None
            records: list[dict[str, Any]] = []
            while True:
                extract_request = build_extract_requirements_request(
                    block,
                    anchors=anchor_by_id,
                    after_anchor=cursor,
                )
                page = extract_request.parse_answer(
                    accepted_answers[extract_request.question.question_id]
                )
                records.extend(dict(item) for item in page.records)
                if not page.has_more:
                    break
                cursor = page.next_anchor
            raw_records.extend(records)

        requirements, drafts = allocate_requirements(
            deduplicate_requirement_records(raw_records),
            first_ordinal=1,
            anchors=anchor_by_id,
            ledger=self.source.instruction_ledger,
        )
        ids_by_anchor: dict[str, list[str]] = {}
        ids_by_related_anchor: dict[str, list[str]] = {}
        for requirement in requirements:
            for anchor_id in requirement.source_refs:
                ids_by_anchor.setdefault(anchor_id, []).append(requirement.id)
        for requirement, draft in zip(requirements, drafts, strict=True):
            for anchor_id in draft.related_source_refs:
                ids_by_related_anchor.setdefault(anchor_id, []).append(requirement.id)
        requirements = tuple(
            replace(
                requirement,
                related_requirement_ids=tuple(
                    dict.fromkeys(
                        related
                        for anchor_id in draft.related_source_refs
                        for related in ids_by_anchor.get(anchor_id, [])
                        if related != requirement.id
                    )
                ),
            )
            for requirement, draft in zip(requirements, drafts, strict=True)
        )

        dispositions: list[BlockDisposition] = []
        for block in self.source.blocks:
            known_ids = tuple(
                dict.fromkeys(
                    (
                        *(
                            requirement_id
                            for anchor_id in block.anchor_ids
                            for requirement_id in ids_by_anchor.get(anchor_id, [])
                        ),
                        *(
                            requirement_id
                            for anchor_id in block.anchor_ids
                            for requirement_id in ids_by_related_anchor.get(
                                anchor_id, []
                            )
                        ),
                    )
                )
            )
            disposition_request = build_block_disposition_request(
                block, requirement_ids=known_ids
            )
            dispositions.append(
                disposition_request.parse_answer(
                    accepted_answers[disposition_request.question.question_id]
                )
            )

        revised: list[Requirement] = []
        for requirement in requirements:
            fidelity_request = build_fidelity_request(
                requirement, segmented=self.source
            )
            decision = fidelity_request.parse_answer(
                accepted_answers[fidelity_request.question.question_id]
            )
            revised.append(
                replace(requirement, statement=decision.statement)
                if decision.statement is not None
                else requirement
            )
        return SourceLedger(tuple(revised), tuple(dispositions))

    def accepted_coverage(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> tuple[RequirementCoverage, ...]:
        """Parse and bind the per-requirement coverage answers."""
        ledger = self.accepted_ledger(accepted_answers)
        coverage: list[RequirementCoverage] = []
        self._search_requirements_evidence(ledger.requirements)
        for requirement in ledger.requirements:
            search = self._evidence_search_cache.get(requirement.id)
            evidence = self._evidence_for_requirement(requirement.id)
            request = self._coverage_request_cache.get(requirement.id)
            if request is None:
                request = self._build_coverage_request(requirement, evidence, search)
                self._coverage_request_cache[requirement.id] = request
            raw_answer = accepted_answers[request.question.question_id]
            answer_digest = hashlib.sha256(
                json.dumps(
                    raw_answer,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            cached = self._accepted_coverage_cache.get(requirement.id)
            if cached is not None and cached[0] == answer_digest:
                coverage.append(cached[1])
                continue
            answer = request.parse_answer(raw_answer)
            parsed = create_requirement_coverage(
                requirement,
                answer,
                evidence_by_id={item.id: item for item in evidence},
                supplied_evidence_ids=tuple(item.id for item in evidence),
                revision=1,
            )
            self._accepted_coverage_cache[requirement.id] = (
                answer_digest,
                parsed,
            )
            coverage.append(parsed)
        return tuple(coverage)

    def _build_coverage_request(
        self,
        requirement: Requirement,
        evidence: Sequence[Evidence],
        search: RequirementEvidenceSearch | None,
    ) -> QuestionRequest[Any]:
        if evidence and self.evidence_excerpt_reader is None:
            raise PlanningContractError(
                "/evidence_excerpts",
                "planning evidence excerpts must be supplied by the repository adapter",
            )
        excerpts = (
            {}
            if self.evidence_excerpt_reader is None
            else {item.id: self.evidence_excerpt_reader(item) for item in evidence}
        )
        return build_requirement_coverage_request(
            requirement,
            source=self.source,
            evidence=evidence,
            evidence_excerpts=excerpts,
            repository_search=None if search is None else search.to_data(),
        )


def _normalized_block_fingerprint(text: str) -> str:
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _is_test_path(path: str) -> bool:
    parts = path.casefold().split("/")
    filename = parts[-1] if parts else ""
    return (
        any(part in {"test", "tests", "testing"} for part in parts[:-1])
        or filename.startswith("test_")
        or filename.endswith("_test.py")
    )


def _path_is_in_scope(path: str, allowed_paths: Sequence[str]) -> bool:
    for allowed_path in allowed_paths:
        normalized = allowed_path.rstrip("/")
        if normalized in {"", "."} or path == normalized:
            return True
        if path.startswith(f"{normalized}/"):
            return True
    return False


def _deterministic_block_disposition(
    *,
    block_id: str,
    direct_ids: Sequence[str],
    related_ids: Sequence[str],
    prior_direct_ids: Sequence[tuple[str, ...]],
    has_exact_duplicate: bool,
) -> tuple[Mapping[str, Any], str, str] | None:
    if direct_ids:
        answer: Mapping[str, Any] = {
            "disposition": "normative",
            "requirement_ids": list(dict.fromkeys(direct_ids)),
        }
        return (
            answer,
            "source-block-disposition-v1/direct-citation",
            f"Block {block_id} has direct citations from accepted requirements.",
        )
    if related_ids:
        return None
    if has_exact_duplicate:
        candidates = {tuple(ids) for ids in prior_direct_ids if ids}
        if len(candidates) != 1:
            return None
        requirement_ids = next(iter(candidates))
        return (
            {
                "disposition": "duplicate",
                "requirement_ids": list(requirement_ids),
                "reason": (
                    "This block exactly repeats a block represented by these "
                    "requirements."
                ),
            },
            "source-block-disposition-v1/exact-content-duplicate",
            "Whitespace-normalized exact content maps unambiguously to accepted "
            "requirements.",
        )
    return (
        {
            "disposition": "context",
            "reason": "No extracted requirement cites this block.",
        },
        "source-block-disposition-v1/unreferenced-context",
        "Extraction found no direct or related requirement and no exact duplicate "
        "candidate.",
    )


def _is_completed_zero_hit_search(search: Any, *, pinned_commit: str) -> bool:
    """Accept only explicit complete, pinned, available searches with no hits."""
    code_symbol_coverage = getattr(search, "code_symbol_coverage", None)
    coverage_status = (
        code_symbol_coverage.get("status")
        if isinstance(code_symbol_coverage, Mapping)
        else "complete"
    )
    return (
        search is not None
        and search.base_commit == pinned_commit
        and search.basedpyright_available is True
        and search.rg_search_status == "complete"
        and coverage_status == "complete"
        and not search.evidence
        and search.matching_line_count == 0
        and search.matching_path_count == 0
    )


def _exact_source_text_fidelity_answer(
    request: QuestionRequest[Any],
) -> Mapping[str, Any] | None:
    """Accept only a requirement copied from exactly one pinned source anchor."""
    if request.question.kind != "check_requirement_fidelity":
        return None
    if len(request.source_excerpts) != 1 or len(request.accepted_facts) != 1:
        return None
    requirement = request.accepted_facts[0].value
    statement = requirement.get("statement")
    source_text = request.source_excerpts[0].text
    if not isinstance(statement, str) or not isinstance(source_text, str):
        return None
    if " ".join(statement.split()) != " ".join(source_text.split()):
        return None
    return {
        "decision": "accept",
        "reason": (
            "The requirement statement matches its single pinned source anchor "
            "after whitespace normalization."
        ),
    }


__all__ = ["SourceLedger", "SourceLedgerQuestionMachine"]
