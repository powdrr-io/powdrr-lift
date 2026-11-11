"""Pinned story ownership and amendment impact for the existing planner.

Allocation is intent, never execution evidence. Whole-story ownership is a safe
default; callers may select smaller validated slices without changing identity.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from powdrr_service.decision_obligation import content_fingerprint
from powdrr_service.structrr.proposal import ProposalRevision
from powdrr_service.structrr.user_stories import (
    STORY_SECTIONS,
    StoryCatalog,
    case_digest,
    parse_story_catalog,
    parse_story_changes,
    project_story_changes,
    story_contract_digest,
    validate_story_catalog,
)


class StoryAllocationError(ValueError):
    """A planned story is absent, unconfirmed, or no longer matches its pin."""


@dataclass(frozen=True, slots=True)
class StoryPlanRef:
    story_id: str
    contract_digest: str
    step_ids: tuple[str, ...]
    transition_ids: tuple[str, ...]
    case_ids: tuple[str, ...]
    dependency_digests: tuple[tuple[str, str], ...] = ()
    case_digests: tuple[tuple[str, str], ...] = ()

    def to_data(self) -> dict[str, Any]:
        return {
            "story_id": self.story_id,
            "contract_digest": self.contract_digest,
            "step_ids": list(self.step_ids),
            "transition_ids": list(self.transition_ids),
            "case_ids": list(self.case_ids),
            "dependency_digests": dict(self.dependency_digests),
            "case_digests": dict(self.case_digests),
        }


def parse_story_refs(value: Any) -> tuple[StoryPlanRef, ...]:
    if not isinstance(value, list):
        raise StoryAllocationError("story_refs must be an array")
    refs = []
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != {
            "story_id",
            "contract_digest",
            "step_ids",
            "transition_ids",
            "case_ids",
            "dependency_digests",
            "case_digests",
        }:
            raise StoryAllocationError("story reference has unknown or missing fields")
        identity, digest = raw["story_id"], raw["contract_digest"]
        if (
            not isinstance(identity, str)
            or not re.fullmatch(r"story:[A-Za-z0-9][A-Za-z0-9_.\-/]*", identity)
            or not _digest(digest)
        ):
            raise StoryAllocationError("story identity or contract digest is invalid")
        lists = []
        for field, kind in (
            ("step_ids", "step"),
            ("transition_ids", "transition"),
            ("case_ids", "case"),
        ):
            values = raw[field]
            if (
                not isinstance(values, list)
                or any(
                    not isinstance(item, str)
                    or not re.fullmatch(
                        re.escape(identity) + ":" + kind + r":[1-9][0-9]*", item
                    )
                    for item in values
                )
                or len(set(values)) != len(values)
            ):
                raise StoryAllocationError(
                    f"{field} must contain unique owned identities"
                )
            lists.append(tuple(sorted(values)))
        dependencies = raw["dependency_digests"]
        if not isinstance(dependencies, Mapping) or any(
            not isinstance(key, str) or not key or not _digest(item)
            for key, item in dependencies.items()
        ):
            raise StoryAllocationError("story dependency digests are invalid")
        cases = raw["case_digests"]
        if (
            not isinstance(cases, Mapping)
            or set(cases) != set(lists[2])
            or any(not _digest(value) for value in cases.values())
        ):
            raise StoryAllocationError(
                "case_digests must pin exactly the allocated cases"
            )
        refs.append(
            StoryPlanRef(
                identity,
                digest,
                *lists,
                tuple(sorted(dependencies.items())),
                tuple(sorted(cases.items())),
            )
        )
    if len({ref.story_id for ref in refs}) != len(refs):
        raise StoryAllocationError("story references must be unique")
    return tuple(refs)


def _digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None
    )


def _dependencies(
    snapshot: Mapping[str, Any], catalog: StoryCatalog, story_id: str
) -> tuple[tuple[str, str], ...]:
    story = catalog.stories_by_id[story_id]
    result = {
        link.story_id: story_contract_digest(catalog, link.story_id)
        for link in story.related_story_refs
    }
    subjects = [("invariants", identity) for identity in story.invariant_refs]
    subjects.extend(("modules", identity) for identity in story.related_modules)
    subjects.extend(
        (ref.section, ref.subject_id)
        for step in catalog.user_story_steps
        if step.story_id == story_id
        for ref in step.functionality_refs
    )
    for section, identity in subjects:
        record = next(
            (r for r in snapshot.get(section, []) if r.get("id") == identity), None
        )
        if record is None and section == "modules":
            record = next(
                (r for r in snapshot.get("entities", []) if r.get("id") == identity),
                None,
            )
        if record is None:
            raise StoryAllocationError(
                f"{story_id}: missing dependency {section}:{identity}"
            )
        result[section + ":" + identity] = content_fingerprint(record)
    return tuple(sorted(result.items()))


def pin_story(snapshot: Mapping[str, Any], story_id: str) -> StoryPlanRef:
    catalog = parse_story_catalog(snapshot)
    validate_story_catalog(catalog).require_valid()
    story = catalog.stories_by_id.get(story_id)
    if story is None:
        raise StoryAllocationError(f"{story_id}: story was removed or is unknown")
    digest = story_contract_digest(catalog, story_id)
    if story.confirmation is None or story.confirmation.contract_digest != digest:
        raise StoryAllocationError(
            f"{story_id}: current confirmation required; "
            "return to story-design-interview"
        )
    return StoryPlanRef(
        story_id,
        digest,
        tuple(
            sorted(
                step.id
                for step in catalog.user_story_steps
                if step.story_id == story_id
            )
        ),
        tuple(
            sorted(
                edge.id
                for edge in catalog.user_story_transitions
                if edge.story_id == story_id
            )
        ),
        tuple(
            sorted(
                case.id
                for case in catalog.user_story_cases
                if case.story_id == story_id
            )
        ),
        _dependencies(snapshot, catalog, story_id),
        tuple(
            sorted(
                (case.id, case_digest(catalog, case.id))
                for case in catalog.user_story_cases
                if case.story_id == story_id
            )
        ),
    )


def validate_story_refs(
    refs: Sequence[StoryPlanRef], snapshot: Mapping[str, Any]
) -> None:
    for ref in refs:
        parse_story_refs([ref.to_data()])
        current = pin_story(snapshot, ref.story_id)
        if (ref.contract_digest, ref.dependency_digests) != (
            current.contract_digest,
            current.dependency_digests,
        ):
            raise StoryAllocationError(
                f"{ref.story_id}: story/context conflict; "
                "refresh approved design and re-plan"
            )
        for field in ("step_ids", "transition_ids", "case_ids"):
            unknown = set(getattr(ref, field)) - set(getattr(current, field))
            if unknown:
                raise StoryAllocationError(
                    f"{ref.story_id}: removed or unknown {field}: {sorted(unknown)}"
                )
        if any(
            dict(current.case_digests).get(key) != value
            for key, value in ref.case_digests
        ):
            raise StoryAllocationError(
                f"{ref.story_id}: case revision conflict; re-plan selected cases"
            )


def merge_story_refs(
    groups: Sequence[Sequence[StoryPlanRef]],
) -> tuple[StoryPlanRef, ...]:
    result: dict[str, StoryPlanRef] = {}
    for refs in groups:
        for ref in refs:
            previous = result.get(ref.story_id)
            if previous is None:
                result[ref.story_id] = ref
            else:
                if any(
                    key in dict(previous.case_digests)
                    and dict(previous.case_digests)[key] != value
                    for key, value in ref.case_digests
                ):
                    raise StoryAllocationError(
                        f"{ref.story_id}: conflicting case revisions"
                    )
                if (previous.contract_digest, previous.dependency_digests) != (
                    ref.contract_digest,
                    ref.dependency_digests,
                    tuple(
                        sorted(
                            {
                                **dict(previous.case_digests),
                                **dict(ref.case_digests),
                            }.items()
                        )
                    ),
                ):
                    raise StoryAllocationError(
                        f"{ref.story_id}: conflicting allocation revisions"
                    )
                result[ref.story_id] = StoryPlanRef(
                    ref.story_id,
                    ref.contract_digest,
                    tuple(sorted(set(previous.step_ids) | set(ref.step_ids))),
                    tuple(
                        sorted(set(previous.transition_ids) | set(ref.transition_ids))
                    ),
                    tuple(sorted(set(previous.case_ids) | set(ref.case_ids))),
                    ref.dependency_digests,
                )
    return tuple(result[key] for key in sorted(result))


def source_story_refs(
    text: str, snapshot: Mapping[str, Any]
) -> tuple[StoryPlanRef, ...]:
    """Read explicit story declarations or typed refs, without code inference."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError:
        raw = None
    if not isinstance(raw, Mapping):
        blocks = re.findall(r"(?m)^```ya?ml\s*\n(.*?)^```\s*$", text, re.S)
        if not blocks and re.search(r"(?m)^\s*(?:story_refs|user_stor\w*):", text):
            raise StoryAllocationError(
                "Story declarations require valid YAML or a complete YAML fenced block"
            )
        return merge_story_refs(
            [source_story_refs(block, snapshot) for block in blocks]
        )
    if "story_refs" in raw:
        if any(raw.get(section) for section in STORY_SECTIONS):
            raise StoryAllocationError(
                "source must choose declarations or story_refs without shadowing either"
            )
        refs = parse_story_refs(raw["story_refs"])
        validate_story_refs(refs, snapshot)
        if any(ref != pin_story(snapshot, ref.story_id) for ref in refs):
            raise StoryAllocationError(
                "source story_refs must select the full story; partition "
                "ownership in the plan, not the design input"
            )
        return refs
    records = []
    ids = set()
    for section in STORY_SECTIONS:
        section_records = raw.get(section, [])
        if not isinstance(section_records, list):
            raise StoryAllocationError(f"source {section} must be an array")
        for record in section_records:
            if not isinstance(record, Mapping) or not isinstance(record.get("id"), str):
                raise StoryAllocationError(
                    "source story declaration requires an identity"
                )
            records.append(record)
            identity = record["id"]
            owner = (
                identity
                if section == "user_stories"
                else ":".join(identity.split(":")[:2])
            )
            if not (
                section == "user_stories"
                and record.get("action") in {"remove", "removed", "deleted"}
            ):
                ids.add(owner)
    if records:
        declaration = raw
        if all(not isinstance(record.get("action"), str) for record in records):
            declaration = {
                **raw,
                "structrr_schema": "https://powdrr.io/schema/structrr-bootstrap-v1",
            }
        projected = project_story_changes(snapshot, parse_story_changes(declaration))
        for identity in ids:
            if pin_story(projected, identity) != pin_story(snapshot, identity):
                raise StoryAllocationError(
                    f"{identity}: source changes require an approved "
                    "story proposal before planning"
                )
    return tuple(pin_story(snapshot, identity) for identity in sorted(ids))


def story_planning_context(
    snapshot: Mapping[str, Any], refs: Sequence[StoryPlanRef]
) -> dict[str, Any]:
    """Return complete selected graphs and direct dependencies without recursion."""
    selected = {ref.story_id for ref in refs}
    dependencies = {key for ref in refs for key, _ in ref.dependency_digests}
    story_ids = selected | {key for key in dependencies if key.startswith("story:")}
    catalog = parse_story_catalog(snapshot)
    data: dict[str, Any] = {
        section: [
            record
            for record in records
            if record.get("story_id", record["id"]) in story_ids
        ]
        for section, records in catalog.to_data().items()
    }
    data["dependencies"] = [
        {"section": section, "record": dict(record)}
        for section, records in snapshot.items()
        if isinstance(records, list)
        for record in records
        if isinstance(record, Mapping)
        and section + ":" + str(record.get("id", "")) in dependencies
    ]
    return data


def story_amendment_impact(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    allocations: Mapping[str, Sequence[StoryPlanRef]],
    receipts: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Report exact changed primitives and historical evidence without rewriting it."""
    old, new = parse_story_catalog(before), parse_story_catalog(after)
    old_data, new_data = old.to_data(), new.to_data()
    changes: dict[str, list[str]] = {}
    changed_stories: set[str] = set()
    for section in old_data:
        left = {r["id"]: r for r in old_data[section]}
        right = {r["id"]: r for r in new_data[section]}
        changed = sorted(
            key for key in left.keys() | right.keys() if left.get(key) != right.get(key)
        )
        changes[section] = changed
        for key in changed:
            record = right.get(key) or left[key]
            changed_stories.add(record.get("story_id", key))
    affected: dict[str, list[str]] = {}
    for item_id, refs in allocations.items():
        for ref in refs:
            try:
                validate_story_refs((ref,), after)
            except ValueError:
                affected.setdefault(item_id, []).append(ref.story_id)
                changed_stories.add(ref.story_id)
    stale = []
    for index, receipt in enumerate(receipts):
        if any(
            ref.get("story_id") in changed_stories
            for ref in receipt.get("story_refs", [])
        ):
            stale.append(
                {
                    "receipt_id": receipt.get("id", str(index)),
                    "status": "stale",
                    "historical_receipt": dict(receipt),
                }
            )
    observation_changes = []
    old_observations = {
        o.id: o for step in old.user_story_steps for o in step.expected_observations
    }
    new_observations = {
        o.id: o for step in new.user_story_steps for o in step.expected_observations
    }
    for identity in old_observations.keys() | new_observations.keys():
        if old_observations.get(identity) != new_observations.get(identity):
            observation_changes.append(identity)
    functionality = sorted(
        {
            (ref.section, ref.subject_id)
            for catalog in (old, new)
            for step in catalog.user_story_steps
            if step.story_id in changed_stories
            for ref in step.functionality_refs
        }
    )
    return {
        "changed_subjects": changes,
        "affected_story_ids": sorted(changed_stories),
        "affected_plan_items": affected,
        "historical_receipts": stale,
        "changed_observation_ids": sorted(observation_changes),
        "affected_case_ids": sorted(
            {
                case.id
                for catalog in (old, new)
                for case in catalog.user_story_cases
                if case.story_id in changed_stories
            }
        ),
        "linked_functionality": [
            {"section": section, "subject_id": identity}
            for section, identity in functionality
        ],
        "route": "story-design-interview" if changed_stories else None,
        "requires_approved_proposal": bool(changed_stories),
    }


def story_proposal_context(
    baseline: Mapping[str, Any],
    source: Mapping[str, Any],
    proposal: ProposalRevision,
    *,
    base_commit: str,
) -> dict[str, Any]:
    """Apply the same readiness and context transport to single-feature execution."""
    from powdrr_service.structrr.design import DesignHandoff

    if not source.get("story_refs") and not any(
        source.get(section) for section in STORY_SECTIONS
    ):
        return {}
    handoff = DesignHandoff.compile(baseline, proposal, base_commit=base_commit)
    candidate = handoff.to_data()["desired"]["document"]
    refs = source_story_refs(yaml.safe_dump(dict(source)), candidate)
    if not refs:
        return {}
    data = {
        "story_refs": [ref.to_data() for ref in refs],
        "story_context": story_planning_context(candidate, refs),
        "coverage_status": "planned",
    }
    return {**data, "fingerprint": content_fingerprint(data)}
