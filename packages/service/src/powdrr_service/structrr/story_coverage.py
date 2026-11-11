"""Finite story paths and pinned test references; coverage is not execution proof."""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from powdrr_service.decision_obligation import content_fingerprint
from powdrr_service.structrr.user_stories import (
    StoryCatalog,
    _data,
    _decode,
    case_digest,
    story_contract_digest,
    validate_story_catalog,
)


class StoryCoverageError(ValueError):
    """Story coverage is invalid, incomplete, or stale."""


@dataclass(frozen=True, slots=True)
class StoryCaseRef:
    story_id: str
    contract_digest: str
    case_id: str
    case_digest: str
    step_ids: tuple[str, ...]
    transition_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]

    def to_data(self) -> dict[str, Any]:
        return _data(self)


def parse_story_case_refs(value: Any) -> tuple[StoryCaseRef, ...]:
    if not isinstance(value, list):
        raise StoryCoverageError(
            "story_refs must be an array of pinned case references"
        )
    refs = tuple(_decode(StoryCaseRef, item, "story_refs") for item in value)
    if len({(ref.story_id, ref.case_id) for ref in refs}) != len(refs):
        raise StoryCoverageError("duplicate story case reference")
    for ref in refs:
        if (
            not ref.step_ids
            or not ref.observation_ids
            or len(set(ref.observation_ids)) != len(ref.observation_ids)
        ):
            raise StoryCoverageError(
                "case references require steps and unique observations"
            )
        if not ref.case_id.startswith(ref.story_id + ":case:") or any(
            not identity.startswith(ref.story_id + ":" + kind + ":")
            for kind, ids in (
                ("step", ref.step_ids),
                ("transition", ref.transition_ids),
                ("observation", ref.observation_ids),
            )
            for identity in ids
        ):
            raise StoryCoverageError("case references contain foreign identities")
        if any(
            not digest.startswith("sha256:")
            or len(digest) != 71
            or any(c not in "0123456789abcdef" for c in digest[7:])
            for digest in (ref.contract_digest, ref.case_digest)
        ):
            raise StoryCoverageError("case references require sha256 digests")
    return refs


def story_case_ref(catalog: StoryCatalog, case_id: str) -> StoryCaseRef:
    validate_story_catalog(catalog).require_valid()
    case = next((case for case in catalog.user_story_cases if case.id == case_id), None)
    if case is None:
        raise StoryCoverageError(f"unknown story case: {case_id}")
    story = catalog.stories_by_id[case.story_id]
    digest = story_contract_digest(catalog, story.id)
    if story.confirmation is None or story.confirmation.contract_digest != digest:
        raise StoryCoverageError(f"{story.id}: current story confirmation required")
    steps = [story.entry_step_id]
    for edge_id in case.transition_ids:
        edge = catalog.transitions_by_id[edge_id]
        if edge.from_step_id != steps[-1]:
            raise StoryCoverageError(f"{case.id}: disconnected case walk")
        steps.append(edge.to_step_id)
    return StoryCaseRef(
        story.id,
        digest,
        case.id,
        case_digest(catalog, case.id),
        tuple(steps),
        case.transition_ids,
        tuple(sorted(case.expected_observations)),
    )


def validate_story_case_refs(
    refs: Sequence[StoryCaseRef], catalog: StoryCatalog
) -> None:
    for ref in refs:
        if ref != story_case_ref(catalog, ref.case_id):
            raise StoryCoverageError(
                f"{ref.case_id}: stale or altered story case reference"
            )


@dataclass(frozen=True, slots=True)
class CaseCandidate:
    story_id: str
    transition_ids: tuple[str, ...]
    step_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]

    @property
    def key(self) -> str:
        return content_fingerprint(_data(self))

    def to_data(self) -> dict[str, Any]:
        return {**_data(self), "candidate_key": self.key}


@dataclass(frozen=True, slots=True)
class CaseExpansion:
    status: str
    candidates: tuple[CaseCandidate, ...]
    uncovered_targets: tuple[str, ...]
    edge_expansions: int

    def to_data(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "candidates": [c.to_data() for c in self.candidates],
            "uncovered_targets": list(self.uncovered_targets),
            "edge_expansions": self.edge_expansions,
        }


def _back_edges(catalog: StoryCatalog, story_id: str) -> set[str]:
    """Deterministic DFS identifies cycle-closing transitions, without recursion."""
    outgoing: dict[str, list[Any]] = {}
    for edge in sorted(catalog.user_story_transitions, key=lambda edge: edge.id):
        if edge.story_id == story_id:
            outgoing.setdefault(edge.from_step_id, []).append(edge)
    entry = catalog.stories_by_id[story_id].entry_step_id
    colors: dict[str, int] = {entry: 1}
    stack = [(entry, iter(outgoing.get(entry, [])))]
    result = set()
    while stack:
        node, edges = stack[-1]
        edge = next(edges, None)
        if edge is None:
            colors[node] = 2
            stack.pop()
        elif colors.get(edge.to_step_id) == 1:
            result.add(edge.id)
        elif colors.get(edge.to_step_id, 0) == 0:
            colors[edge.to_step_id] = 1
            stack.append((edge.to_step_id, iter(outgoing.get(edge.to_step_id, []))))
    return result


def _candidate(
    catalog: StoryCatalog, story_id: str, path: tuple[str, ...]
) -> CaseCandidate:
    steps = [catalog.stories_by_id[story_id].entry_step_id]
    for identity in path:
        edge = catalog.transitions_by_id[identity]
        if edge.from_step_id != steps[-1]:
            raise StoryCoverageError("disconnected candidate path")
        steps.append(edge.to_step_id)
    observations = sorted(
        {
            observation.id
            for identity in steps
            for observation in catalog.steps_by_id[identity].expected_observations
        }
    )
    return CaseCandidate(story_id, path, tuple(steps), tuple(observations))


def _targets(
    candidate: CaseCandidate,
    catalog: StoryCatalog,
    back_edges: set[str],
    *,
    observations: Sequence[str] | None = None,
) -> set[str]:
    result = {"transition:" + identity for identity in candidate.transition_ids}
    result.update(
        "observation:" + identity
        for identity in (
            observations if observations is not None else candidate.observation_ids
        )
    )
    result.update(
        "outcome:" + identity
        for identity in candidate.step_ids
        if catalog.steps_by_id[identity].kind == "outcome"
    )
    result.update(
        f"loop:{identity}:{min(2, candidate.transition_ids.count(identity))}"
        for identity in back_edges
    )
    return result


def expand_story_cases(
    catalog: StoryCatalog,
    story_id: str,
    *,
    edge_budget: int = 1000,
    include_declared: bool = True,
) -> CaseExpansion:
    """Find finite terminal walks with zero/one/repeated cycle-closing edges.

    A search limit returns uncovered targets and usable candidates, never a false
    claim of completeness. This is graph coverage, not input-domain coverage.
    """
    validate_story_catalog(catalog).require_valid()
    if (
        isinstance(edge_budget, bool)
        or not isinstance(edge_budget, int)
        or edge_budget < 1
    ):
        raise StoryCoverageError("edge_budget must be a positive integer")
    story = catalog.stories_by_id[story_id]
    if (
        story.confirmation is None
        or story.confirmation.contract_digest
        != story_contract_digest(catalog, story_id)
    ):
        raise StoryCoverageError(f"{story_id}: current confirmation required")
    edges = sorted(
        (edge for edge in catalog.user_story_transitions if edge.story_id == story_id),
        key=lambda edge: edge.id,
    )
    steps = [step for step in catalog.user_story_steps if step.story_id == story_id]
    required = {"transition:" + edge.id for edge in edges}
    required.update("outcome:" + step.id for step in steps if step.kind == "outcome")
    required.update(
        "observation:" + o.id for step in steps for o in step.expected_observations
    )
    back_edges = _back_edges(catalog, story_id)
    required.update(
        f"loop:{identity}:{count}" for identity in back_edges for count in (0, 1, 2)
    )
    covered: set[str] = set()
    if include_declared:
        for case in catalog.user_story_cases:
            if case.story_id == story_id:
                covered.update(
                    _targets(
                        _candidate(catalog, story_id, case.transition_ids),
                        catalog,
                        back_edges,
                        observations=case.expected_observations,
                    )
                )
    queue: deque[tuple[str, tuple[str, ...]]] = deque([(story.entry_step_id, ())])
    candidates = []
    expansions = 0
    while queue and required - covered:
        node, path = queue.popleft()
        if catalog.steps_by_id[node].kind == "outcome":
            candidate = _candidate(catalog, story_id, path)
            targets = _targets(candidate, catalog, back_edges)
            if targets - covered:
                candidates.append(candidate)
                covered.update(targets)
            continue
        for edge in edges:
            if edge.from_step_id != node:
                continue
            if expansions >= edge_budget:
                return CaseExpansion(
                    "case_expansion_incomplete",
                    tuple(candidates),
                    tuple(sorted(required - covered)),
                    expansions,
                )
            expansions += 1
            if path.count(edge.id) >= (2 if edge.id in back_edges else 3):
                continue
            queue.append((edge.to_step_id, (*path, edge.id)))
    return CaseExpansion(
        "graph_coverage_complete"
        if not required - covered
        else "case_expansion_incomplete",
        tuple(candidates),
        tuple(sorted(required - covered)),
        expansions,
    )


def declared_case_coverage(catalog: StoryCatalog, story_id: str) -> tuple[str, ...]:
    """Return missing declared targets without confusing candidate paths with cases."""
    validate_story_catalog(catalog).require_valid()
    back_edges = _back_edges(catalog, story_id)
    required = {
        "transition:" + edge.id
        for edge in catalog.user_story_transitions
        if edge.story_id == story_id
    }
    required.update(
        "outcome:" + step.id
        for step in catalog.user_story_steps
        if step.story_id == story_id and step.kind == "outcome"
    )
    required.update(
        "observation:" + observation.id
        for step in catalog.user_story_steps
        if step.story_id == story_id
        for observation in step.expected_observations
    )
    required.update(
        f"loop:{identity}:{count}" for identity in back_edges for count in (0, 1, 2)
    )
    for case in catalog.user_story_cases:
        if case.story_id == story_id:
            required.difference_update(
                _targets(
                    _candidate(catalog, story_id, case.transition_ids),
                    catalog,
                    back_edges,
                    observations=case.expected_observations,
                )
            )
    return tuple(sorted(required))


def story_case_obligations(
    catalog: StoryCatalog, case_ids: Sequence[str]
) -> list[dict[str, Any]]:
    result = []
    for identity in sorted(set(case_ids)):
        ref = story_case_ref(catalog, identity)
        case = next(case for case in catalog.user_story_cases if case.id == identity)
        observations = {
            o.id: _data(o)
            for step in catalog.user_story_steps
            for o in step.expected_observations
        }
        result.append(
            {
                "obligation_id": "SC-" + content_fingerprint(ref.to_data())[7:23],
                "source_text": case.description,
                "story_refs": [ref.to_data()],
                "case": _data(case),
                "expected_observations": [
                    observations[key] for key in ref.observation_ids
                ],
            }
        )
    return result
