"""Deterministic source-ledger question ordering for initial planning."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from powdrr_service.planning.assembly import (
    MAX_WORK_UNIT_PROPOSALS_PER_PAGE,
    MAX_WORK_UNIT_REQUIREMENTS_PER_QUESTION,
    AllocationGroup,
    ItemDescription,
    PathFact,
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
from powdrr_service.planning.dependencies import (
    DependencyGraph,
    UnitDependencyEdge,
    Validation,
    build_dependency_graph_review_requests,
    compile_dependency_graph,
)
from powdrr_service.planning.questions import (
    QuestionRequest,
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
)
from powdrr_service.planning.structrr import (
    BoundStructuralSubject,
    bind_structural_operation,
    build_structural_change_request,
    build_subject_enumeration_request,
    compile_structrr_plan,
)

from powdrr_lift.structrr.rebase import snapshot_digest
from powdrr_lift.workrr.git import slugify_workflow_id
from powdrr_lift.workrr.github_issue_queue import PlannedWorkItem, ReviewedExecutionPlan
from powdrr_lift.workrr.multipr_structrr import MultiPRStructrrProposal
from powdrr_lift.workrr.planning.evidence import (
    Evidence,
    RequirementCoverage,
    RequirementEvidenceSearch,
    build_requirement_coverage_request,
    create_requirement_coverage,
)


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
        repository_root: str | Path | None = None,
        evidence_by_requirement: Mapping[str, tuple[Evidence, ...]] | None = None,
        evidence_search: Callable[
            [Sequence[Requirement]], Mapping[str, RequirementEvidenceSearch]
        ]
        | None = None,
        verification_command: str = "make test",
        repository_paths: Mapping[str, PathFact] | None = None,
        plan_id: str = "planning-session",
        structrr_baseline: Mapping[str, Any] | None = None,
        taxonomy_entity_types: tuple[str, ...] = (),
    ) -> None:
        source.validate()
        self.source = source
        self.repository_root = Path(
            repository_root or source.document.repository
        ).resolve()
        self.evidence_by_requirement = dict(evidence_by_requirement or {})
        self.evidence_search = evidence_search
        self._evidence_search_cache: dict[str, RequirementEvidenceSearch] = {}
        self.verification_command = verification_command
        self.repository_paths = dict(repository_paths or {})
        self.plan_id = plan_id
        self.structrr_baseline = (
            None if structrr_baseline is None else dict(structrr_baseline)
        )
        self.taxonomy_entity_types = taxonomy_entity_types
        self._invalid_accepted_question_ids: set[str] = set()
        self._oversized_work_unit_groups: set[tuple[str, ...]] = set()
        self._single_proposal_requirements: set[str] = set()
        self._supplemental_work_unit_requirements: set[str] = set()

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

    def _next_source_question(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> QuestionRequest[Any] | None:
        requests = self._next_source_questions(accepted_answers)
        return requests[0] if requests else None

    def _next_source_questions(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> tuple[QuestionRequest[Any], ...]:
        raw_records: list[dict[str, Any]] = []
        anchor_by_id = {anchor.id: anchor for anchor in self.source.anchors}
        pending_extractions: list[QuestionRequest[Any]] = []

        for block in self.source.blocks:
            after_anchor: str | None = None
            page_records: list[dict[str, Any]] = []
            block_incomplete = False
            while True:
                extract_request = build_extract_requirements_request(
                    block,
                    anchors=anchor_by_id,
                    related_blocks=self._adjacent_blocks(block),
                    after_anchor=after_anchor,
                    accepted_records=page_records,
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
            return tuple(pending_extractions)

        requirements, drafts = allocate_requirements(
            raw_records,
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
            if disposition_request.question.question_id not in accepted_answers:
                pending_source_questions.append(disposition_request)
            else:
                disposition_request.parse_answer(
                    accepted_answers[disposition_request.question.question_id]
                )

        for requirement in requirements:
            fidelity_request = build_fidelity_request(
                requirement, segmented=self.source
            )
            answer = accepted_answers.get(fidelity_request.question.question_id)
            if answer is None:
                pending_source_questions.append(fidelity_request)
            else:
                fidelity_request.parse_answer(answer)

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
        return (*disposition_requests, *batch_question_requests(fidelity_requests))

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
        ledger = self._parse_source_ledger(accepted_answers)
        self._search_requirements_evidence(ledger.requirements)
        coverage_requests: list[QuestionRequest[Any]] = []
        for requirement in ledger.requirements:
            search = self._evidence_search_cache.get(requirement.id)
            evidence = self.evidence_by_requirement.get(
                requirement.id, () if search is None else search.evidence
            )
            coverage_request = build_requirement_coverage_request(
                requirement,
                source=self.source,
                evidence=evidence,
                repository_root=self.repository_root,
                repository_search=(None if search is None else search.to_data()),
            )
            if coverage_request.question.question_id not in accepted_answers:
                coverage_requests.append(coverage_request)
        if coverage_requests:
            return batch_question_requests(coverage_requests)

        work_unit_requests = tuple(
            request
            for request in self._work_unit_requests(accepted_answers)
            if request is not None
        )
        if work_unit_requests:
            return work_unit_requests

        downstream_requests: list[QuestionRequest[Any]] = []
        for request in self._item_description_requests(accepted_answers):
            if request.question.question_id not in accepted_answers:
                downstream_requests.append(request)

        for request in self._dependency_requests(accepted_answers):
            if request.question.question_id not in accepted_answers:
                downstream_requests.append(request)
        if downstream_requests:
            return tuple(downstream_requests)

        return tuple(
            request
            for request in self._structrr_requests(accepted_answers)
            if request.question.question_id not in accepted_answers
        )

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

    def _work_unit_requests(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> tuple[QuestionRequest[Any] | None, ...]:
        self._supplemental_work_unit_requirements.clear()
        ledger = self._parse_source_ledger(accepted_answers)
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
        evidence = {
            item.id: item
            for items in self.evidence_by_requirement.values()
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
        )

    def _work_unit_example(
        self, context: WorkUnitContext, requirement_ids: tuple[str, ...]
    ) -> Mapping[str, Any]:
        requirement_id = requirement_ids[0]
        requirement = context.requirements[requirement_id]
        component_id = sorted(context.component_ids)[0]
        validation_id = sorted(context.validation_catalog)[0]
        return {
            "records": [
                {
                    "requirement_ids": [requirement_id],
                    "deliverable": "Concise example deliverable",
                    "component": component_id,
                    "implementation_runtime": "neutral",
                    "planned_writes": [
                        {
                            "path": "example-output.txt",
                            "role": "documentation",
                            "runtime": None,
                            "source_refs": [requirement.source_refs[0]],
                            "evidence_ids": [],
                        }
                    ],
                    "implementation_step": "Describe one concrete implementation step.",
                    "acceptance_criteria": ["State one observable result."],
                    "validation_ids": [validation_id],
                    "required_outputs": [],
                }
            ],
            "has_more": False,
            "next_anchor": None,
        }

    def accepted_work_units(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> tuple[WorkUnit, ...]:
        context, proposals = self._accepted_work_unit_proposals(accepted_answers)
        return allocate_work_units(proposals, context=context).units

    def _accepted_work_unit_proposals(
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
        return tuple(
            build_item_description_request(group, by_id)
            for group in allocate_work_unit_groups(units).groups
        )

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
        context, proposals = self._accepted_work_unit_proposals(accepted_answers)
        allocation = allocate_work_units(proposals, context=context)
        return build_dependency_graph_review_requests(
            allocation.units, allocation.pending_outputs
        )

    def accepted_dependency_edges(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> tuple[UnitDependencyEdge, ...]:
        context, proposals = self._accepted_work_unit_proposals(accepted_answers)
        allocation = allocate_work_units(proposals, context=context)
        edges: list[UnitDependencyEdge] = []
        for request in build_dependency_graph_review_requests(
            allocation.units, allocation.pending_outputs
        ):
            answer = request.parse_answer(
                accepted_answers[request.question.question_id]
            )
            edges.extend(answer)
        return tuple(edges)

    def accepted_assembly(
        self,
        accepted_answers: Mapping[str, Mapping[str, Any]],
        *,
        plan_id: str,
        workflow_directory: str | Path = "docs/workflows",
    ) -> PlanningAssembly:
        """Freeze CP-SAT groups and project confirmed unit edges into plan items."""
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
                    "unit_ids": list(unit_ids),
                    "units": [units[unit_id] for unit_id in unit_ids],
                }
            )
        return tuple(records)

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
        order = assembly.dependency_graph.item_order
        prerequisites: dict[str, set[str]] = {item_id: set() for item_id in order}
        for edge in assembly.dependency_graph.item_edges:
            prerequisites[edge.after_item_id].add(edge.before_item_id)
        completed: set[str] = set()
        prior_operations: list[Mapping[str, Any]] = []
        while len(completed) < len(order):
            ready_item_ids = tuple(
                item_id
                for item_id in order
                if item_id not in completed and prerequisites[item_id] <= completed
            )
            if not ready_item_ids:
                raise PlanningCompilationError(
                    "Structrr planning item graph has no ready item"
                )
            prior_records = tuple(
                record_by_id[item_id] for item_id in order if item_id in completed
            )
            projected = (
                self.structrr_baseline
                if not prior_records
                else compile_structrr_plan(
                    plan_id=self.plan_id,
                    baseline_snapshot=self.structrr_baseline,
                    items=prior_records,
                    item_order=tuple(
                        item_id for item_id in order if item_id in completed
                    ),
                    operations=prior_operations,
                    source_ref=self._structrr_source_ref(),
                ).expected_final_snapshot
            )
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
                page_ordinal = 1
                item_subjects: list[BoundStructuralSubject] = []
                while True:
                    enumeration_request = build_subject_enumeration_request(
                        plan_id=self.plan_id,
                        item_id=item_id,
                        units=units,
                        source=self.source,
                        source_refs=source_refs,
                        projected_baseline=projected,
                        prior_subjects=item_subjects,
                        page_ordinal=page_ordinal,
                    )
                    answer = accepted_answers.get(
                        enumeration_request.question.question_id
                    )
                    if answer is None:
                        pending_enumerations.append(enumeration_request)
                        break
                    page = enumeration_request.parse_answer(answer)
                    item_subjects.extend(page.records)
                    if not page.has_more:
                        subjects_by_item[item_id] = tuple(item_subjects)
                        break
                    page_ordinal += 1
            if pending_enumerations:
                return batch_question_requests(pending_enumerations)

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
            for item_id in ready_item_ids:
                record = record_by_id[item_id]
                units = tuple(record["units"])
                planned_paths = {
                    write.path for unit in units for write in unit.planned_writes
                }
                for subject in subjects_by_item[item_id]:
                    change_request = build_structural_change_request(
                        subject,
                        projected_baseline=projected,
                        taxonomy_entity_types=self.taxonomy_entity_types,
                        planned_paths=planned_paths,
                        known_entity_ids=known_entity_ids,
                        valid_example={
                            "result": "unchanged",
                            "reason": (
                                "The projected state already satisfies this subject."
                            ),
                        },
                    )
                    answer = accepted_answers.get(change_request.question.question_id)
                    if answer is None:
                        pending_changes.append(change_request)
                        continue
                    change = change_request.parse_answer(answer)
                    operation = bind_structural_operation(change)
                    if operation is not None:
                        prior_operations.append(operation)
            if pending_changes:
                return batch_question_requests(pending_changes)
            completed.update(ready_item_ids)
        return ()

    def _accepted_structural_subjects(
        self,
        item_id: str,
        units: tuple[WorkUnit, ...],
        source_refs: tuple[str, ...],
        projected_baseline: Mapping[str, Any],
        accepted_answers: Mapping[str, Mapping[str, Any]],
    ) -> tuple[BoundStructuralSubject, ...]:
        subjects: list[BoundStructuralSubject] = []
        page_ordinal = 1
        prior_subjects: list[BoundStructuralSubject] = []
        while True:
            request = build_subject_enumeration_request(
                plan_id=self.plan_id,
                item_id=item_id,
                units=units,
                source=self.source,
                source_refs=source_refs,
                projected_baseline=projected_baseline,
                prior_subjects=prior_subjects,
                page_ordinal=page_ordinal,
            )
            answer = accepted_answers.get(request.question.question_id)
            if answer is None:
                raise ValueError(
                    f"Structrr subject enumeration is incomplete at "
                    f"{request.question.question_id}"
                )
            page = request.parse_answer(answer)
            subjects.extend(page.records)
            prior_subjects.extend(page.records)
            if not page.has_more:
                return tuple(subjects)
            page_ordinal += 1

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
        for item_index, record in enumerate(item_records):
            item_id = str(record["id"])
            units = tuple(record["units"])
            source_refs = tuple(
                dict.fromkeys(
                    ref
                    for unit in units
                    for requirement_id in unit.requirement_ids
                    for ref in requirement_by_id[requirement_id].source_refs
                )
            )
            previous_records = item_records[:item_index]
            if previous_records:
                previous_ids = {str(item["id"]) for item in previous_records}
                prior_order = [
                    candidate
                    for candidate in assembly.dependency_graph.item_order
                    if candidate in previous_ids
                ]
                projected = compile_structrr_plan(
                    plan_id=self.plan_id,
                    baseline_snapshot=self.structrr_baseline,
                    items=previous_records,
                    item_order=prior_order,
                    operations=operations,
                    source_ref=self._structrr_source_ref(),
                ).expected_final_snapshot
            else:
                projected = self.structrr_baseline
            subjects = self._accepted_structural_subjects(
                item_id,
                units,
                source_refs,
                projected,
                accepted_answers,
            )
            for subject in subjects:
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
        return compile_structrr_plan(
            plan_id=self.plan_id,
            baseline_snapshot=self.structrr_baseline,
            items=item_records,
            item_order=assembly.dependency_graph.item_order,
            operations=operations,
            source_ref=self._structrr_source_ref(),
        )

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
        if any(item.kind == "human_setup" for item in ledger.requirements):
            raise PlanningCompilationError(
                "explicit human setup requirements need the setup planning stage"
            )
        assembly = self.accepted_assembly(accepted_answers, plan_id=plan_id)
        if not assembly.units:
            raise PlanningCompilationError(
                "the pinned repository already covers every requirement; no work "
                "items remain to publish"
            )
        proposal = self.accepted_structrr_proposal(accepted_answers)
        dispositions = {item.block_id: item for item in ledger.dispositions}
        coverage_by_id = {item.requirement_id: item for item in coverage}
        evidence = {
            item.id: item
            for items in self.evidence_by_requirement.values()
            for item in items
        }
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
                "item_unit_ids": {
                    item_id: list(unit_ids)
                    for item_id, unit_ids in assembly.item_unit_ids.items()
                },
                "runtime_facts": [
                    {
                        "unit_id": unit.id,
                        "path": write.path,
                        "runtime": write.runtime,
                    }
                    for unit in assembly.units
                    for write in unit.planned_writes
                ],
                "validations": [validation.to_data()],
                "setups": [],
                "structrr": {
                    "baseline_fingerprint": proposal.to_data()["baseline_fingerprint"],
                    "proposal_path": proposal_path,
                    "proposal_fingerprint": proposal.to_data()["fingerprint"],
                },
                "latest_repair": None,
            }
        )
        plan = replace(assembly.plan, planning=manifest.to_data())
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
        pending = self._next_source_question(accepted_answers)
        if pending is not None:
            raise ValueError(
                f"source ledger is incomplete at {pending.question.question_id}"
            )
        return self._parse_source_ledger(accepted_answers)

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
                    related_blocks=self._adjacent_blocks(block),
                    after_anchor=cursor,
                    accepted_records=records,
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
            raw_records,
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
            evidence = self.evidence_by_requirement.get(
                requirement.id, () if search is None else search.evidence
            )
            request = build_requirement_coverage_request(
                requirement,
                source=self.source,
                evidence=evidence,
                repository_root=self.repository_root,
                repository_search=(None if search is None else search.to_data()),
            )
            answer = request.parse_answer(
                accepted_answers[request.question.question_id]
            )
            coverage.append(
                create_requirement_coverage(
                    requirement,
                    answer,
                    evidence_by_id={item.id: item for item in evidence},
                    supplied_evidence_ids=tuple(item.id for item in evidence),
                    revision=1,
                )
            )
        return tuple(coverage)

    def _adjacent_blocks(self, block: Any) -> tuple[Any, ...]:
        by_id = {item.id: item for item in self.source.blocks}
        result = []
        for block_id in (block.previous_block_id, block.next_block_id):
            adjacent = by_id.get(block_id) if block_id is not None else None
            if adjacent is not None:
                result.append(adjacent)
        return tuple(result)


__all__ = ["SourceLedger", "SourceLedgerQuestionMachine"]
