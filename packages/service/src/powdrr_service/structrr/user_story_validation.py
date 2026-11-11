"""Structural graph validation, separate from semantic readiness and test proof."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from powdrr_service.structrr.user_stories import StoryCatalog


@dataclass(frozen=True, slots=True)
class StoryValidationIssue:
    code: str
    subject_id: str
    message: str


@dataclass(frozen=True, slots=True)
class StoryValidationReport:
    issues: tuple[StoryValidationIssue, ...]
    unmapped_step_ids: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.issues

    def require_valid(self) -> None:
        from powdrr_service.structrr.user_stories import StoryContractError

        if self.issues:
            issue = self.issues[0]
            raise StoryContractError(issue.code, issue.subject_id, issue.message)


def _reachable(entry: str, adjacency: Mapping[str, Sequence[str]]) -> set[str]:
    seen: set[str] = set()
    pending = [entry]
    while pending:
        node = pending.pop()
        if node not in seen:
            seen.add(node)
            pending.extend(adjacency.get(node, ()))
    return seen


def validate_story_catalog(
    catalog: StoryCatalog, reference_catalog: Mapping[str, Any] | None = None
) -> StoryValidationReport:
    """Validate complete graphs; optionally resolve external IDs in a pinned snapshot.

    Source refs resolve against `source_refs` (a list of durable IDs) and the
    snapshot's instruction/source/decision records. Passing no external catalog
    checks local graph structure only, never readiness or external authority.
    """
    from powdrr_service.structrr.user_stories import (
        _STORY_ID,
        STORY_SECTIONS,
        UserStory,
        UserStoryTransition,
        _ordinal,
        story_contract_digest,
    )

    issues: list[StoryValidationIssue] = []

    def issue(code: str, subject: str, message: str) -> None:
        issues.append(StoryValidationIssue(code, subject, message))

    records = [
        record for section in STORY_SECTIONS for record in getattr(catalog, section)
    ]
    observations = [
        o for step in catalog.user_story_steps for o in step.expected_observations
    ]
    seen: set[str] = set()
    for record in [*records, *observations]:
        if record.id in seen:
            issue("story_duplicate_id", record.id, "identity occurs more than once")
        seen.add(record.id)
    stories = catalog.stories_by_id
    for record in records:
        if not record.source_refs:
            issue(
                "story_source_missing", record.id, "requires durable source references"
            )
        if not isinstance(record, UserStory) and record.story_id not in stories:
            issue("story_owner_missing", record.id, "owning story is absent")
    if reference_catalog is not None:
        known: set[str] = set()
        section_ids: dict[str, set[str]] = {}
        for section, raw in reference_catalog.items():
            if isinstance(raw, (list, tuple)):
                ids = {
                    str(r.get("id", r.get("clause_id", "")))
                    for r in raw
                    if isinstance(r, Mapping)
                }
                section_ids[section] = ids
                known.update(ids)
        known.update(
            r for r in reference_catalog.get("source_refs", []) if isinstance(r, str)
        )
        for record in records:
            for ref in record.source_refs:
                if ref not in known:
                    issue("story_source_unresolved", record.id, ref)
        for story in stories.values():
            if story.confirmation and story.confirmation.decision_ref not in known:
                issue(
                    "story_reference_unresolved",
                    story.id,
                    story.confirmation.decision_ref,
                )
            for ref in story.invariant_refs:
                if ref not in section_ids.get("invariants", set()):
                    issue("story_reference_unresolved", story.id, ref)
            for ref in story.related_modules:
                if ref not in section_ids.get("modules", set()) | section_ids.get(
                    "entities", set()
                ):
                    issue("story_reference_unresolved", story.id, ref)
        for step in catalog.user_story_steps:
            for ref in step.functionality_refs:
                if ref.subject_id not in section_ids.get(ref.section, set()):
                    issue("story_reference_unresolved", step.id, ref.subject_id)
    for story in stories.values():
        if not story.scope:
            issue("story_scope_missing", story.id, "story requires a declared scope")
        if not _STORY_ID.fullmatch(story.id):
            issue("story_id_invalid", story.id, "requires a story namespace ID")
        steps = {s.id: s for s in catalog.user_story_steps if s.story_id == story.id}
        edges = {
            e.id: e for e in catalog.user_story_transitions if e.story_id == story.id
        }
        cases = [c for c in catalog.user_story_cases if c.story_id == story.id]
        obs = {o.id: o for s in steps.values() for o in s.expected_observations}
        retired = story.identity_state.retired_ids
        if len(set(retired)) != len(retired):
            issue("story_duplicate_id", story.id, "retired IDs must be unique")
        for kind, identities in (
            ("step", steps),
            ("transition", edges),
            ("case", {c.id: c for c in cases}),
            ("observation", obs),
        ):
            allocated = list(identities) + [
                r for r in retired if _ordinal(r, story.id, kind) is not None
            ]
            for identity in allocated:
                ordinal = _ordinal(identity, story.id, kind)
                if ordinal is None:
                    issue(
                        "story_id_invalid",
                        identity,
                        "ID must belong to its story and kind",
                    )
                elif ordinal >= getattr(story.identity_state, "next_" + kind):
                    issue(
                        "story_counter_invalid",
                        identity,
                        "allocator counter must exceed allocated ordinals",
                    )
            for identity in set(identities) & set(retired):
                issue("story_id_retired", identity, "retired identity cannot be reused")
        for identity in retired:
            if not any(
                _ordinal(identity, story.id, k) is not None
                for k in ("step", "transition", "case", "observation")
            ):
                issue("story_id_invalid", identity, "invalid retired identity")
        for ref in story.related_story_refs:
            if ref.story_id not in stories:
                issue("story_reference_unresolved", story.id, ref.story_id)
        outgoing: dict[str, list[UserStoryTransition]] = {s: [] for s in steps}
        reverse: dict[str, list[str]] = {s: [] for s in steps}
        for edge in edges.values():
            if edge.from_step_id not in steps or edge.to_step_id not in steps:
                issue(
                    "story_endpoint_invalid",
                    edge.id,
                    "endpoints must belong to the same story",
                )
                continue
            outgoing[edge.from_step_id].append(edge)
            reverse[edge.to_step_id].append(edge.from_step_id)
        for step in steps.values():
            successors = outgoing[step.id]
            is_action = step.kind in {"user_action", "system_action"}
            if (step.action is not None) != is_action or (
                (step.outcome is not None) != (step.kind == "outcome")
            ):
                issue(
                    "story_step_fields_invalid",
                    step.id,
                    "fields do not match step kind",
                )
            if step.kind == "outcome":
                if successors or not step.expected_observations:
                    issue(
                        "story_outcome_invalid",
                        step.id,
                        "outcome requires observations and no successors",
                    )
            elif not successors:
                issue(
                    "story_dead_end", step.id, "nonterminal step requires a successor"
                )
            kinds = [e.condition.kind for e in successors]
            if step.kind == "decision":
                if (
                    len(successors) < 2
                    or "always" in kinds
                    or kinds.count("otherwise") > 1
                ):
                    issue(
                        "story_decision_invalid",
                        step.id,
                        "requires at least two alternatives and at most one fallback",
                    )
            elif len(successors) > 1 or any(k != "always" for k in kinds):
                issue(
                    "story_successor_invalid",
                    step.id,
                    "nondecision requires one unconditional successor",
                )
        if story.entry_step_id not in steps:
            issue("story_entry_missing", story.id, "entry step is absent")
        reachable = _reachable(
            story.entry_step_id,
            {k: [e.to_step_id for e in v] for k, v in outgoing.items()},
        )
        terminating: set[str] = set()
        for step in steps.values():
            if step.kind == "outcome":
                terminating.update(_reachable(step.id, reverse))
        for step in steps.values():
            if step.id not in reachable:
                issue(
                    "story_step_unreachable",
                    step.id,
                    "step cannot be reached from entry",
                )
            if step.id not in terminating:
                issue("story_outcome_unreachable", step.id, "no outcome is reachable")
        for case in cases:
            current = story.entry_step_id
            visited_obs: set[str] = set()
            if current in steps:
                visited_obs.update(o.id for o in steps[current].expected_observations)
            for edge_id in case.transition_ids:
                case_edge = edges.get(edge_id)
                if case_edge is None or case_edge.from_step_id != current:
                    issue("story_case_disconnected", case.id, edge_id)
                    break
                current = case_edge.to_step_id
                if current in steps:
                    visited_obs.update(
                        o.id for o in steps[current].expected_observations
                    )
            terminal = steps.get(current)
            if terminal is None or terminal.kind != "outcome":
                issue("story_case_nonterminal", case.id, "case must end at an outcome")
            elif not {o.id for o in terminal.expected_observations} <= set(
                case.expected_observations
            ):
                issue(
                    "story_case_observation_missing",
                    case.id,
                    "case omits terminal observations",
                )
            if (
                not case.expected_observations
                or not set(case.expected_observations) <= visited_obs
            ):
                issue(
                    "story_case_observation_invalid",
                    case.id,
                    "observations must exist on the case path",
                )
        if (
            story.confirmation
            and story.confirmation.contract_digest
            != story_contract_digest(catalog, story.id)
        ):
            issue(
                "story_confirmation_stale",
                story.id,
                "confirmation is for a different contract",
            )
    return StoryValidationReport(
        tuple(issues),
        tuple(
            sorted(s.id for s in catalog.user_story_steps if not s.functionality_refs)
        ),
    )


def validate_story_document(document: Mapping[str, Any]) -> StoryValidationReport:
    """Validate declarations or the shape of a proposal's story operations.

    A delta needs its pinned baseline for graph validation, performed by the
    shared projector. It cannot independently establish a valid graph.
    """
    from powdrr_service.structrr.user_stories import (
        STORY_SECTIONS,
        StoryContractError,
        parse_story_catalog,
        parse_story_changes,
    )

    try:
        is_delta = any(
            isinstance(record, Mapping) and isinstance(record.get("action"), str)
            for section in STORY_SECTIONS
            for record in (
                document.get(section, [])
                if isinstance(document.get(section), list)
                else []
            )
        )
        if is_delta:
            parse_story_changes(document)
            return StoryValidationReport(())
        return validate_story_catalog(parse_story_catalog(document))
    except StoryContractError as error:
        return StoryValidationReport(
            (StoryValidationIssue(error.code, error.subject_id, str(error)),)
        )
