"""Service-owned story declarations, identities and graph contracts.

Parsing establishes structural validity, never user approval or behavioral proof.
Confirmation authority must be checked by the interviewing adapter against its
recorded response; a declaration alone cannot establish readiness.
"""

from __future__ import annotations

import json
import re
import types
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from powdrr_service.decision_obligation import content_fingerprint
from powdrr_service.structrr.user_story_validation import (
    StoryValidationReport as StoryValidationReport,
)
from powdrr_service.structrr.user_story_validation import (
    validate_story_catalog as validate_story_catalog,
)

StorySection = Literal[
    "user_stories", "user_story_steps", "user_story_transitions", "user_story_cases"
]
STORY_SECTIONS: tuple[StorySection, ...] = (
    "user_stories",
    "user_story_steps",
    "user_story_transitions",
    "user_story_cases",
)
_LOCAL_KINDS = {
    "user_story_steps": "step",
    "user_story_transitions": "transition",
    "user_story_cases": "case",
}
_STORY_ID = re.compile(r"story:[A-Za-z0-9][A-Za-z0-9_.\-/]*\Z")
_PLACEHOLDER = re.compile(r"\b(?:TODO|TBD|FIXME)\b|<[^>]+>")


class StoryContractError(ValueError):
    def __init__(self, code: str, subject_id: str, message: str) -> None:
        self.code, self.subject_id = code, subject_id
        super().__init__(f"{code}: {subject_id}: {message}")


@dataclass(frozen=True, slots=True)
class IdentityState:
    next_step: int
    next_transition: int
    next_case: int
    next_observation: int
    retired_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StoryConfirmation:
    contract_digest: str
    decision_ref: str
    authority: Literal["user_confirmation", "reviewed_source"]


@dataclass(frozen=True, slots=True)
class RelatedStory:
    story_id: str
    relationship: Literal["uses", "related"]


@dataclass(frozen=True, slots=True)
class UserStory:
    id: str
    title: str
    narrative: str
    actor: str
    goal: str
    preconditions: tuple[str, ...]
    scope: tuple[str, ...]
    entry_step_id: str
    source_refs: tuple[str, ...]
    identity_state: IdentityState
    invariant_refs: tuple[str, ...] = ()
    related_modules: tuple[str, ...] = ()
    related_story_refs: tuple[RelatedStory, ...] = ()
    confirmation: StoryConfirmation | None = None


@dataclass(frozen=True, slots=True)
class StoryAction:
    actor: str
    operation: str
    input: str


@dataclass(frozen=True, slots=True)
class StoryObservation:
    id: str
    description: str
    boundary: str


@dataclass(frozen=True, slots=True)
class StoryStateEffect:
    subject: str
    before: str
    after: str
    persistence: str


@dataclass(frozen=True, slots=True)
class FunctionalityReference:
    section: Literal["entities", "features", "interfaces"]
    subject_id: str


@dataclass(frozen=True, slots=True)
class UserStoryStep:
    id: str
    story_id: str
    kind: Literal["user_action", "system_action", "decision", "outcome"]
    description: str
    source_refs: tuple[str, ...]
    action: StoryAction | None = None
    expected_observations: tuple[StoryObservation, ...] = ()
    functionality_refs: tuple[FunctionalityReference, ...] = ()
    outcome: Literal["success", "failure", "cancelled"] | None = None
    state_effects: tuple[StoryStateEffect, ...] = ()


@dataclass(frozen=True, slots=True)
class TransitionCondition:
    kind: Literal["always", "predicate", "otherwise"]
    description: str


@dataclass(frozen=True, slots=True)
class UserStoryTransition:
    id: str
    story_id: str
    from_step_id: str
    to_step_id: str
    condition: TransitionCondition
    source_refs: tuple[str, ...]
    description: str | None = None


@dataclass(frozen=True, slots=True)
class UserStoryCase:
    id: str
    story_id: str
    description: str
    initial_conditions: tuple[str, ...]
    transition_ids: tuple[str, ...]
    expected_observations: tuple[str, ...]
    source_refs: tuple[str, ...]
    input_examples: tuple[Mapping[str, Any], ...] = ()
    coverage_tags: tuple[
        Literal[
            "normal", "boundary", "invalid", "recovery", "cancellation", "interaction"
        ],
        ...,
    ] = ()


_RECORD_TYPES = dict(
    zip(
        STORY_SECTIONS,
        (UserStory, UserStoryStep, UserStoryTransition, UserStoryCase),
        strict=True,
    )
)


def _data(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _data(getattr(value, f.name))
            for f in fields(value)
            if getattr(value, f.name) is not None
        }
    if isinstance(value, Mapping):
        return {key: _data(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_data(item) for item in value]
    return value


def _decode(annotation: Any, value: Any, pointer: str) -> Any:
    origin, args = get_origin(annotation), get_args(annotation)
    if annotation is Any:
        # Fixtures are JSON data, never executable commands or custom objects.
        try:
            return json.loads(json.dumps(value, allow_nan=False))
        except (ValueError, TypeError) as error:
            raise StoryContractError(
                "story_field_invalid", pointer, "requires JSON data"
            ) from error
    if origin in (Union, types.UnionType):
        if value is None and type(None) in args:
            return None
        for option in args:
            if option is not type(None):
                try:
                    return _decode(option, value, pointer)
                except StoryContractError:
                    continue
        raise StoryContractError(
            "story_field_invalid", pointer, "invalid optional field"
        )
    if origin is Literal:
        if value not in args or not isinstance(value, str):
            raise StoryContractError(
                "story_field_invalid", pointer, f"expected one of {args}"
            )
        return value
    if origin is tuple:
        if not isinstance(value, (list, tuple)):
            raise StoryContractError(
                "story_field_invalid", pointer, "requires an array"
            )
        return tuple(
            _decode(args[0], item, f"{pointer}/{i}") for i, item in enumerate(value)
        )
    if origin is Mapping:
        if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
            raise StoryContractError(
                "story_field_invalid", pointer, "requires a string-keyed object"
            )
        return {k: _decode(args[1], v, f"{pointer}/{k}") for k, v in value.items()}
    if isinstance(annotation, type) and is_dataclass(annotation):
        if not isinstance(value, Mapping):
            raise StoryContractError(
                "story_field_invalid", pointer, "requires an object"
            )
        names = {f.name for f in fields(annotation)}
        if unknown := set(value) - names:
            raise StoryContractError(
                "story_unknown_field", pointer, str(sorted(unknown))
            )
        hints = get_type_hints(annotation)
        try:
            return annotation(
                **{k: _decode(hints[k], v, f"{pointer}/{k}") for k, v in value.items()}
            )
        except TypeError as error:
            raise StoryContractError(
                "story_field_missing", pointer, str(error)
            ) from error
    if annotation is str:
        if (
            not isinstance(value, str)
            or not value.strip()
            or (not pointer.endswith("/content_json") and len(value) > 2000)
            or (not pointer.endswith("/content_json") and _PLACEHOLDER.search(value))
        ):
            raise StoryContractError(
                "story_field_invalid",
                pointer,
                "requires nonempty resolved text of at most 2000 characters",
            )
    elif annotation is int:
        if type(value) is not int or value <= 0:
            raise StoryContractError(
                "story_field_invalid", pointer, "requires a positive integer"
            )
    else:
        raise TypeError(f"unsupported story field type {annotation!r}")
    return value


@dataclass(frozen=True, slots=True)
class StoryCatalog:
    user_stories: tuple[UserStory, ...] = ()
    user_story_steps: tuple[UserStoryStep, ...] = ()
    user_story_transitions: tuple[UserStoryTransition, ...] = ()
    user_story_cases: tuple[UserStoryCase, ...] = ()

    def to_data(self) -> dict[str, Any]:
        return {
            section: [
                _data(item)
                for item in sorted(getattr(self, section), key=lambda item: item.id)
            ]
            for section in STORY_SECTIONS
        }

    @property
    def stories_by_id(self) -> Mapping[str, UserStory]:
        return {item.id: item for item in self.user_stories}

    @property
    def steps_by_id(self) -> Mapping[str, UserStoryStep]:
        return {item.id: item for item in self.user_story_steps}

    @property
    def transitions_by_id(self) -> Mapping[str, UserStoryTransition]:
        return {item.id: item for item in self.user_story_transitions}

    @property
    def observations_by_id(self) -> Mapping[str, StoryObservation]:
        return {
            observation.id: observation
            for step in self.user_story_steps
            for observation in step.expected_observations
        }

    @property
    def fingerprint(self) -> str:
        return content_fingerprint(canonical_story_data(self))


def parse_story_catalog(snapshot: Mapping[str, Any]) -> StoryCatalog:
    versions = snapshot.get("section_versions", {})
    if not isinstance(versions, Mapping):
        raise StoryContractError(
            "story_version_invalid", "section_versions", "requires an object"
        )
    collections: dict[str, Any] = {}
    for section, record_type in _RECORD_TYPES.items():
        if section in versions and (
            type(versions[section]) is not int or versions[section] != 1
        ):
            raise StoryContractError(
                "story_version_invalid", section, "only version 1 is supported"
            )
        raw = snapshot.get(section, [])
        if not isinstance(raw, list):
            raise StoryContractError(
                "story_field_invalid", section, "requires an array"
            )
        records = tuple(
            _decode(record_type, item, f"{section}/{i}") for i, item in enumerate(raw)
        )
        collections[section] = records
    return StoryCatalog(**collections)


def _ordinal(identity: str, story_id: str, kind: str) -> int | None:
    match = re.fullmatch(re.escape(story_id) + ":" + kind + r":([1-9][0-9]*)", identity)
    return int(match[1]) if match else None


def _unordered(value: Any, key: str = "") -> Any:
    """Normalize set-like fields while retaining walks and fixture array order."""
    if isinstance(value, Mapping):
        return {k: _unordered(v, k) for k, v in value.items()}
    if key == "input_examples":
        return _data(value)
    if isinstance(value, (list, tuple)):
        items = [_unordered(v) for v in value]
        if key in {
            *STORY_SECTIONS,
            "expected_observations",
            "source_refs",
            "invariant_refs",
            "related_modules",
            "related_story_refs",
            "functionality_refs",
            "retired_ids",
            "coverage_tags",
            "preconditions",
            "scope",
            "initial_conditions",
            "state_effects",
        }:
            items.sort(key=lambda v: json.dumps(v, sort_keys=True))
        return items
    return value


def canonical_story_data(catalog: StoryCatalog) -> dict[str, Any]:
    return dict(_unordered(catalog.to_data()))


def story_contract_digest(catalog: StoryCatalog, story_id: str) -> str:
    story = catalog.stories_by_id[story_id]
    record = _data(story)
    for key in ("confirmation", "identity_state", "source_refs"):
        record.pop(key, None)
    steps = [_data(s) for s in catalog.user_story_steps if s.story_id == story_id]
    edges = [_data(e) for e in catalog.user_story_transitions if e.story_id == story_id]
    for item in [*steps, *edges]:
        item.pop("source_refs", None)
    return content_fingerprint(
        _unordered(
            {
                "story": record,
                "user_story_steps": steps,
                "user_story_transitions": edges,
            }
        )
    )


def case_digest(catalog: StoryCatalog, case_id: str) -> str:
    case = next(c for c in catalog.user_story_cases if c.id == case_id)
    return content_fingerprint(
        _unordered(
            {
                "case": _data(case),
                "story_contract_digest": story_contract_digest(catalog, case.story_id),
            }
        )
    )


def validate_story_evolution(before: StoryCatalog, after: StoryCatalog) -> None:
    """Require monotonic counters and retirement across one atomic transaction."""
    old_data, new_data = before.to_data(), after.to_data()
    for story in before.user_stories:
        current = after.stories_by_id.get(story.id)
        if current is None:
            continue
        for kind in ("step", "transition", "case", "observation"):
            if getattr(current.identity_state, "next_" + kind) < getattr(
                story.identity_state, "next_" + kind
            ):
                raise StoryContractError("story_counter_regressed", story.id, kind)
        old_ids = {
            r["id"]
            for section in STORY_SECTIONS[1:]
            for r in old_data[section]
            if r["story_id"] == story.id
        }
        old_ids.update(
            o.id
            for s in before.user_story_steps
            if s.story_id == story.id
            for o in s.expected_observations
        )
        new_ids = {
            r["id"]
            for section in STORY_SECTIONS[1:]
            for r in new_data[section]
            if r["story_id"] == story.id
        }
        new_ids.update(
            o.id
            for s in after.user_story_steps
            if s.story_id == story.id
            for o in s.expected_observations
        )
        if not (old_ids - new_ids | set(story.identity_state.retired_ids)) <= set(
            current.identity_state.retired_ids
        ):
            raise StoryContractError(
                "story_retirement_missing", story.id, "removed IDs must remain retired"
            )
        for identity in new_ids - old_ids:
            for kind in ("step", "transition", "case", "observation"):
                ordinal = _ordinal(identity, story.id, kind)
                if ordinal is not None and ordinal < getattr(
                    story.identity_state, "next_" + kind
                ):
                    raise StoryContractError(
                        "story_id_reused",
                        identity,
                        "new identity predates allocator high-water mark",
                    )


@dataclass(frozen=True, slots=True)
class StoryEditRequest:
    section: Literal[
        "user_stories", "user_story_steps", "user_story_transitions", "user_story_cases"
    ]
    action: Literal["add", "change", "remove"]
    story_id: str
    content: Mapping[str, Any]
    subject_id: str | None = None


@dataclass(frozen=True, slots=True)
class AllocatedStoryEdit:
    checkpoint_key: str
    request_fingerprint: str
    baseline_fingerprint: str
    section: str
    subject_id: str
    action: str
    content_json: str
    expected_before: str | None
    identity_state: IdentityState

    def to_data(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> AllocatedStoryEdit:
        result: AllocatedStoryEdit = _decode(cls, raw, "allocated_edit")
        return result


def allocate_story_edit(
    catalog: StoryCatalog,
    edit_request: StoryEditRequest,
    checkpoint_key: str,
    *,
    pending_edits: Sequence[AllocatedStoryEdit] = (),
    accepted_history_ids: frozenset[str] = frozenset(),
) -> AllocatedStoryEdit:
    """Allocate once and persist the returned record with the caller's checkpoint.

    Pass the checkpoint's prior allocations for batch edits and exact replay.
    A new story ID is derived from checkpoint identity, never mutable prose.
    The caller supplies accepted history IDs to prevent whole-story reuse.
    """
    if not checkpoint_key.strip():
        raise StoryContractError(
            "story_checkpoint_missing",
            edit_request.story_id,
            "checkpoint key is required",
        )
    if edit_request.section not in STORY_SECTIONS or edit_request.action not in {
        "add",
        "change",
        "remove",
    }:
        raise StoryContractError(
            "story_edit_invalid", edit_request.story_id, "unsupported edit"
        )
    request_fingerprint = content_fingerprint(asdict(edit_request))
    for previous in pending_edits:
        if previous.baseline_fingerprint != catalog.fingerprint:
            raise StoryContractError(
                "story_snapshot_stale",
                previous.subject_id,
                "pending allocation is stale",
            )
        if previous.checkpoint_key == checkpoint_key:
            if previous.request_fingerprint != request_fingerprint:
                raise StoryContractError(
                    "story_checkpoint_conflict",
                    checkpoint_key,
                    "checkpoint already allocated another edit",
                )
            return previous
    identity = edit_request.subject_id
    content = dict(edit_request.content)
    if edit_request.section == "user_stories" and edit_request.action == "add":
        identity = (
            "story:" + content_fingerprint(checkpoint_key).removeprefix("sha256:")[:24]
        )
        if identity in catalog.stories_by_id or identity in accepted_history_ids:
            raise StoryContractError(
                "story_id_reused", identity, "story ID already allocated"
            )
        if (
            any(k in content for k in ("id", "identity_state", "confirmation"))
            or edit_request.subject_id
        ):
            raise StoryContractError(
                "story_id_not_allocated",
                identity,
                "story identity and allocation state are compiler-owned",
            )
        content.update(id=identity, identity_state=_data(IdentityState(1, 1, 1, 1, ())))
        content.setdefault("entry_step_id", identity + ":step:1")
        story = _decode(UserStory, content, identity)
    else:
        story = catalog.stories_by_id.get(edit_request.story_id)
        if story is None:
            creation = next(
                (
                    edit
                    for edit in pending_edits
                    if edit.section == "user_stories"
                    and edit.action == "add"
                    and edit.subject_id == edit_request.story_id
                ),
                None,
            )
            if creation is None:
                raise StoryContractError(
                    "story_owner_missing", edit_request.story_id, "story is absent"
                )
            story = _decode(
                UserStory, json.loads(creation.content_json), creation.subject_id
            )
    state = story.identity_state
    for previous in pending_edits:
        raw = json.loads(previous.content_json)
        if raw.get("story_id", previous.subject_id) == story.id:
            state = previous.identity_state
    if (
        edit_request.section == "user_stories"
        and edit_request.action == "change"
        and any(key in content for key in ("identity_state", "confirmation"))
    ):
        raise StoryContractError(
            "story_authority_invalid",
            story.id,
            "allocator and confirmation are not semantic edit inputs",
        )
    if edit_request.action == "add" and edit_request.section in _LOCAL_KINDS:
        if identity is not None or "id" in content:
            raise StoryContractError(
                "story_id_not_allocated", story.id, "addition IDs are compiler-owned"
            )
        kind = _LOCAL_KINDS[edit_request.section]
        identity = f"{story.id}:{kind}:{getattr(state, 'next_' + kind)}"
        state = replace(state, **{"next_" + kind: getattr(state, "next_" + kind) + 1})
        content.update(id=identity, story_id=story.id)
    if identity is None:
        raise StoryContractError(
            "story_id_missing", story.id, "existing subject ID is required"
        )
    existing = next(
        (r for r in catalog.to_data()[edit_request.section] if r["id"] == identity),
        None,
    )
    if edit_request.action != "add" and existing is None:
        raise StoryContractError("story_subject_missing", identity, "subject absent")
    if existing is not None and existing.get("story_id", existing["id"]) != story.id:
        raise StoryContractError(
            "story_owner_invalid", identity, "edit belongs to another story"
        )
    if edit_request.action == "remove" and identity != story.id:
        removed = {identity}
        if edit_request.section == "user_story_steps":
            removed.update(
                o["id"] for o in (existing or {}).get("expected_observations", [])
            )
        state = replace(
            state, retired_ids=tuple(sorted({*state.retired_ids, *removed}))
        )
    if (
        edit_request.action != "remove"
        and edit_request.section == "user_story_steps"
        and "expected_observations" in content
    ):
        observations = []
        existing_ids = {
            o["id"] for o in (existing or {}).get("expected_observations", [])
        }
        for raw in content["expected_observations"]:
            observation = dict(raw)
            if "id" not in observation:
                observation["id"] = f"{story.id}:observation:{state.next_observation}"
                state = replace(state, next_observation=state.next_observation + 1)
            elif observation["id"] not in existing_ids:
                raise StoryContractError(
                    "story_id_not_allocated",
                    observation["id"],
                    "observation IDs are compiler-owned",
                )
            observations.append(observation)
        content["expected_observations"] = observations
        removed = existing_ids - {o["id"] for o in observations}
        state = replace(
            state, retired_ids=tuple(sorted({*state.retired_ids, *removed}))
        )
    content["id"] = identity
    if edit_request.section != "user_stories":
        content["story_id"] = story.id
    return AllocatedStoryEdit(
        checkpoint_key,
        request_fingerprint,
        catalog.fingerprint,
        edit_request.section,
        identity,
        edit_request.action,
        json.dumps(content, sort_keys=True),
        content_fingerprint(existing) if existing else None,
        state,
    )


def preview_story_edits(
    catalog: StoryCatalog,
    allocated_edits: Sequence[AllocatedStoryEdit],
    expected_snapshot_digest: str,
) -> StoryCatalog:
    """Project an authoring draft with identity checks but no readiness claim."""
    from powdrr_service.structrr.design import _project_sections

    if catalog.fingerprint != expected_snapshot_digest:
        raise StoryContractError(
            "story_snapshot_stale", "catalog", "snapshot digest differs"
        )
    operations: dict[str, list[dict[str, Any]]] = {}
    states: dict[str, IdentityState] = {}
    changed_contracts: set[str] = set()
    for edit in allocated_edits:
        if edit.baseline_fingerprint != expected_snapshot_digest:
            raise StoryContractError(
                "story_snapshot_stale", edit.subject_id, "allocation is stale"
            )
        existing = next(
            (r for r in catalog.to_data()[edit.section] if r["id"] == edit.subject_id),
            None,
        )
        if (
            content_fingerprint(existing) if existing else None
        ) != edit.expected_before:
            raise StoryContractError(
                "story_before_stale", edit.subject_id, "subject fingerprint differs"
            )
        content = json.loads(edit.content_json)
        owner = content.get("story_id", edit.subject_id)
        if edit.section != "user_story_cases" and (
            edit.action != "change"
            or any(
                key
                not in {
                    "id",
                    "story_id",
                    "source_refs",
                    "identity_state",
                    "confirmation",
                }
                and (existing or {}).get(key) != value
                for key, value in content.items()
            )
        ):
            changed_contracts.add(owner)
        previous = states.get(owner, edit.identity_state)
        states[owner] = IdentityState(
            **{
                f"next_{kind}": max(
                    getattr(previous, f"next_{kind}"),
                    getattr(edit.identity_state, f"next_{kind}"),
                )
                for kind in ("step", "transition", "case", "observation")
            },
            retired_ids=tuple(
                sorted(set(previous.retired_ids) | set(edit.identity_state.retired_ids))
            ),
        )
        operations.setdefault(edit.section, []).append(
            {"id": edit.subject_id, "action": edit.action, "content": content}
        )
    for story_id, state in states.items():
        if any(op["id"] == story_id for op in operations.get("user_stories", [])):
            for op in operations["user_stories"]:
                if op["id"] == story_id and op["action"] != "remove":
                    op["content"]["identity_state"] = _data(state)
                    if story_id in changed_contracts:
                        op["content"]["confirmation"] = None
        else:
            operations.setdefault("user_stories", []).append(
                {
                    "id": story_id,
                    "action": "change",
                    "identity_state": _data(state),
                    **({"confirmation": None} if story_id in changed_contracts else {}),
                }
            )
    result = _project_sections(catalog.to_data(), operations)
    candidate = parse_story_catalog(result)
    validate_story_evolution(catalog, candidate)
    return candidate


def apply_story_edits(
    catalog: StoryCatalog,
    allocated_edits: Sequence[AllocatedStoryEdit],
    expected_snapshot_digest: str,
) -> StoryCatalog:
    """Publish only a structurally complete projected edit transaction."""
    candidate = preview_story_edits(catalog, allocated_edits, expected_snapshot_digest)
    validate_story_catalog(candidate).require_valid()
    return candidate


@dataclass(frozen=True, slots=True)
class StorySectionChange:
    """The existing operation envelope retains a step's nested action intact."""

    section: StorySection
    id: str
    action: Literal["add", "change", "remove"]
    content_json: str
    expected_before: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            **(
                {"expected_before": self.expected_before}
                if self.expected_before
                else {}
            ),
            "id": self.id,
            "action": self.action,
            "content": json.loads(self.content_json),
        }


def parse_story_changes(document: Mapping[str, Any]) -> tuple[StorySectionChange, ...]:
    from powdrr_service.structrr.actions import normalize_action

    if (
        document.get("structrr_schema")
        == "https://powdrr.io/schema/structrr-bootstrap-v1"
    ):
        return tuple(
            StorySectionChange(
                section, record["id"], "add", json.dumps(record, sort_keys=True)
            )
            for section in STORY_SECTIONS
            for record in parse_story_catalog(document).to_data()[section]
        )
    changes = []
    for section in STORY_SECTIONS:
        raw = document.get(section, [])
        if not isinstance(raw, list):
            raise StoryContractError(
                "story_field_invalid", section, "requires an array"
            )
        for item in raw:
            if not isinstance(item, Mapping) or set(item) - {
                "id",
                "action",
                "content",
                "expected_before",
            }:
                raise StoryContractError(
                    "story_operation_invalid",
                    section,
                    "requires an id/action/content operation envelope",
                )
            identity = _decode(str, item.get("id"), section)
            try:
                action = normalize_action(item.get("action"))
            except ValueError as error:
                raise StoryContractError(
                    "story_operation_invalid", identity, str(error)
                ) from error
            expected_before = item.get("expected_before")
            if expected_before is not None and (
                not isinstance(expected_before, str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_before)
            ):
                raise StoryContractError(
                    "story_operation_invalid",
                    identity,
                    "invalid expected-before fingerprint",
                )
            content = item.get("content", {"id": identity})
            if (
                not isinstance(content, Mapping)
                or content.get("id", identity) != identity
            ):
                raise StoryContractError(
                    "story_operation_invalid",
                    identity,
                    "content identity differs from envelope",
                )
            content = {**content, "id": identity}
            record_type = _RECORD_TYPES[section]
            hints = get_type_hints(record_type)
            if set(content) - set(hints):
                raise StoryContractError(
                    "story_unknown_field", identity, "unknown content field"
                )
            if action == "remove" and set(content) != {"id"}:
                raise StoryContractError(
                    "story_operation_invalid",
                    identity,
                    "removal content must identify only the subject",
                )
            if action == "add":
                _decode(record_type, content, identity)
            else:
                for key, value in content.items():
                    _decode(hints[key], value, identity + "/" + key)
            changes.append(
                StorySectionChange(
                    section, identity, action, json.dumps(content, sort_keys=True)
                )
            )
    return tuple(changes)


def merge_story_catalogs(
    baseline: StoryCatalog, current: StoryCatalog, proposed: StoryCatalog
) -> StoryCatalog:
    """Three-way merge disjoint primitives, then check the complete graph.

    No semantic conflict is resolved automatically. A changed combined contract
    cannot retain a confirmation for either predecessor's digest.
    """
    base_data, current_data, proposed_data = (
        baseline.to_data(),
        current.to_data(),
        proposed.to_data(),
    )
    result = current.to_data()
    for section in STORY_SECTIONS:
        base = {r["id"]: r for r in base_data[section]}
        latest = {r["id"]: r for r in current_data[section]}
        desired = {r["id"]: r for r in proposed_data[section]}
        for identity in base.keys() | desired.keys():
            before, now, after = (
                base.get(identity),
                latest.get(identity),
                desired.get(identity),
            )
            if before == after:
                continue
            if now != before and now != after:
                raise StoryContractError(
                    "story_merge_conflict",
                    identity,
                    "concurrent edits to one primitive",
                )
            if after is None:
                latest.pop(identity, None)
            else:
                latest[identity] = after
        result[section] = list(latest.values())
    merged = parse_story_catalog(result)
    validate_story_evolution(current, merged)
    validate_story_catalog(merged).require_valid()
    return merged


def project_story_changes(
    snapshot: Mapping[str, Any], changes: Sequence[StorySectionChange]
) -> dict[str, Any]:
    """Project one reviewed changelist as a complete graph transaction."""
    from powdrr_service.structrr.design import project_sections

    sections: dict[str, list[dict[str, Any]]] = {}
    for change in changes:
        existing = next(
            (
                item
                for item in snapshot.get(change.section, [])
                if item.get("id") == change.id
            ),
            None,
        )
        content = json.loads(change.content_json)
        # A canonical source can already contain the exact accepted addition.
        # Normalize defaults before comparison; conflicting declarations still
        # fail rather than overwriting reviewed meaning.
        if change.action == "add" and existing is not None:
            record_type = _RECORD_TYPES[change.section]
            if _data(_decode(record_type, existing, change.id)) == _data(
                _decode(record_type, content, change.id)
            ):
                continue
        sections.setdefault(change.section, []).append(change.to_data())
    return project_sections(snapshot, sections)
