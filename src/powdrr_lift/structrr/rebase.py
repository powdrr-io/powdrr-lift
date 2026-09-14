"""Compare Structrr snapshots and classify proposal rebase impact.

This module deliberately has no repository or agent dependencies.  It operates
on validated Structrr mappings so Workrr and Procedrr can use the same
deterministic result during proposal authoring and merge reconciliation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

RebaseClassification = Literal[
    "clean",
    "mechanically_rebased",
    "context_refresh_required",
    "targeted_update_required",
    "conflicted",
    "invalidated",
]
ChangeArea = Literal["entity", "relationship", "source_subject", "source_binding"]
ChangeKind = Literal[
    "added",
    "removed",
    "changed",
    "presentation",
    "moved",
    "renamed",
    "ambiguous",
]


@dataclass(frozen=True, slots=True)
class StructrrChange:
    """One semantic difference between two Structrr snapshots."""

    area: ChangeArea
    kind: ChangeKind
    identity: str
    before: Mapping[str, Any] | None = None
    after: Mapping[str, Any] | None = None
    changed_fields: tuple[str, ...] = ()
    reason: str = ""

    def to_data(self) -> dict[str, Any]:
        """Return a stable, machine-readable representation."""
        return {
            "area": self.area,
            "kind": self.kind,
            "identity": self.identity,
            "before": dict(self.before) if self.before is not None else None,
            "after": dict(self.after) if self.after is not None else None,
            "changed_fields": list(self.changed_fields),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class StructrrRemapping:
    """An identity change that can be carried forward mechanically."""

    area: ChangeArea
    before: str
    after: str
    reason: str

    def to_data(self) -> dict[str, str]:
        return {
            "area": self.area,
            "before": self.before,
            "after": self.after,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class StructrrRebaseReport:
    """Deterministic impact report for rebasing a proposal."""

    classification: RebaseClassification
    baseline_digest: str
    current_digest: str
    changes: tuple[StructrrChange, ...] = ()
    affected_changes: tuple[StructrrChange, ...] = ()
    remappings: tuple[StructrrRemapping, ...] = ()
    conflicts: tuple[StructrrChange, ...] = ()
    amendment_items: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return self.classification == "clean"

    @property
    def requires_targeted_review(self) -> bool:
        return self.classification in {
            "context_refresh_required",
            "targeted_update_required",
            "conflicted",
            "invalidated",
        }

    def to_data(self) -> dict[str, Any]:
        """Return a report suitable for YAML/JSON or a Workrr context packet."""
        return {
            "classification": self.classification,
            "baseline_digest": self.baseline_digest,
            "current_digest": self.current_digest,
            "changes": [change.to_data() for change in self.changes],
            "affected_changes": [change.to_data() for change in self.affected_changes],
            "remappings": [remapping.to_data() for remapping in self.remappings],
            "conflicts": [change.to_data() for change in self.conflicts],
            "amendment_items": list(self.amendment_items),
        }


def snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    """Return a content address for a Structrr snapshot.

    Mapping keys and semantically unordered snapshot collections are normalized
    before hashing.  This makes formatting and YAML ordering irrelevant while
    preserving the values that define the snapshot.
    """

    canonical = _canonical_snapshot(snapshot)
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def rebase_structrr_snapshot(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    referenced_entity_ids: Iterable[str] = (),
    referenced_source_subject_keys: Iterable[str] = (),
    referenced_binding_ids: Iterable[str] = (),
) -> StructrrRebaseReport:
    """Compare snapshots and classify the impact on referenced proposal data.

    The full diff is retained for auditability, but only changes touching the
    supplied proposal references can require amendment.  A caller with no
    references gets a complete snapshot comparison with no affected changes.
    """

    changes: list[StructrrChange] = []
    remappings: list[StructrrRemapping] = []
    changes.extend(_compare_keyed("entity", baseline, current, "entities"))
    changes.extend(
        _compare_keyed("relationship", baseline, current, "entity_relationships")
    )

    source_changes, source_remappings = _compare_source_subjects(baseline, current)
    changes.extend(source_changes)
    remappings.extend(source_remappings)
    changes.extend(_compare_bindings(baseline, current, remappings))

    entity_refs = {value for value in referenced_entity_ids if value}
    subject_refs = {value for value in referenced_source_subject_keys if value}
    binding_refs = {value for value in referenced_binding_ids if value}
    affected = tuple(
        change
        for change in changes
        if _is_affected(change, entity_refs, subject_refs, binding_refs)
    )
    conflicts = tuple(change for change in affected if _is_conflict(change))
    affected_remappings = tuple(
        remapping
        for remapping in remappings
        if remapping.before in subject_refs or remapping.after in subject_refs
    )
    classification = _classify(affected, conflicts, affected_remappings)
    amendment_items = tuple(_amendment_item(change) for change in affected)
    return StructrrRebaseReport(
        classification=classification,
        baseline_digest=snapshot_digest(baseline),
        current_digest=snapshot_digest(current),
        changes=tuple(changes),
        affected_changes=affected,
        remappings=affected_remappings,
        conflicts=conflicts,
        amendment_items=amendment_items,
    )


def _compare_keyed(
    area: Literal["entity", "relationship"],
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    collection_name: str,
) -> list[StructrrChange]:
    before = _records_by_key(baseline.get(collection_name), _record_key(area))
    after = _records_by_key(current.get(collection_name), _record_key(area))
    changes: list[StructrrChange] = []
    for identity in sorted(before.keys() - after.keys()):
        changes.append(
            StructrrChange(area, "removed", identity, before=before[identity])
        )
    for identity in sorted(after.keys() - before.keys()):
        changes.append(StructrrChange(area, "added", identity, after=after[identity]))
    for identity in sorted(before.keys() & after.keys()):
        changed_fields = _changed_fields(before[identity], after[identity])
        if not changed_fields:
            continue
        kind: ChangeKind = (
            "presentation"
            if area == "entity" and set(changed_fields) <= _PRESENTATION_FIELDS
            else "changed"
        )
        changes.append(
            StructrrChange(
                area,
                kind,
                identity,
                before=before[identity],
                after=after[identity],
                changed_fields=changed_fields,
            )
        )
    return changes


def _compare_source_subjects(
    baseline: Mapping[str, Any], current: Mapping[str, Any]
) -> tuple[list[StructrrChange], list[StructrrRemapping]]:
    before = _records_by_key(
        baseline.get("source_subjects"), lambda item: _text(item, "stable_key")
    )
    after = _records_by_key(
        current.get("source_subjects"), lambda item: _text(item, "stable_key")
    )
    changes: list[StructrrChange] = []
    remappings: list[StructrrRemapping] = []
    common = before.keys() & after.keys()
    for identity in sorted(common):
        changed_fields = _changed_fields(before[identity], after[identity])
        if not changed_fields:
            continue
        kind: ChangeKind = (
            "moved"
            if set(changed_fields) <= {"id", "path", "file_entity_id"}
            else "changed"
        )
        changes.append(
            StructrrChange(
                "source_subject",
                kind,
                identity,
                before=before[identity],
                after=after[identity],
                changed_fields=changed_fields,
            )
        )

    unmatched_before = {
        key: value for key, value in before.items() if key not in common
    }
    unmatched_after = {key: value for key, value in after.items() if key not in common}
    paired_before: set[str] = set()
    paired_after: set[str] = set()
    for old_key, old_subject in unmatched_before.items():
        candidates = [
            (new_key, new_subject)
            for new_key, new_subject in unmatched_after.items()
            if new_key not in paired_after
            and _text(old_subject, "kind") == _text(new_subject, "kind")
            and _text(old_subject, "qualified_name")
            == _text(new_subject, "qualified_name")
        ]
        if len(candidates) == 1:
            new_key, new_subject = candidates[0]
            paired_before.add(old_key)
            paired_after.add(new_key)
            changes.append(
                StructrrChange(
                    "source_subject",
                    "moved",
                    old_key,
                    before=old_subject,
                    after=new_subject,
                    reason="Qualified symbol identity is preserved across a path move.",
                )
            )
            remappings.append(
                StructrrRemapping(
                    "source_subject",
                    old_key,
                    new_key,
                    "Qualified symbol identity is preserved across a path move.",
                )
            )

    for old_key, old_subject in unmatched_before.items():
        if old_key in paired_before:
            continue
        candidates = [
            (new_key, new_subject)
            for new_key, new_subject in unmatched_after.items()
            if new_key not in paired_after
            and _text(old_subject, "kind") == _text(new_subject, "kind")
            and _text(old_subject, "path") == _text(new_subject, "path")
            and _spans_overlap(old_subject.get("span"), new_subject.get("span"))
        ]
        if len(candidates) == 1:
            new_key, new_subject = candidates[0]
            paired_before.add(old_key)
            paired_after.add(new_key)
            changes.append(
                StructrrChange(
                    "source_subject",
                    "renamed",
                    old_key,
                    before=old_subject,
                    after=new_subject,
                    reason="The same source span and kind has a new qualified name.",
                )
            )
            remappings.append(
                StructrrRemapping(
                    "source_subject",
                    old_key,
                    new_key,
                    "The same source span and kind has a new qualified name.",
                )
            )

    for identity in sorted(set(unmatched_before) - paired_before):
        changes.append(
            StructrrChange(
                "source_subject", "removed", identity, before=before[identity]
            )
        )
    for identity in sorted(set(unmatched_after) - paired_after):
        changes.append(
            StructrrChange("source_subject", "added", identity, after=after[identity])
        )
    return changes, remappings


def _compare_bindings(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    remappings: Sequence[StructrrRemapping],
) -> list[StructrrChange]:
    baseline_subjects = _subject_stable_keys(baseline)
    current_subjects = _subject_stable_keys(current)
    remap = {item.before: item.after for item in remappings}

    def key(
        item: Mapping[str, Any], subjects: Mapping[str, str], apply_remap: bool
    ) -> str:
        subject_key = subjects.get(_text(item, "subject_id"), _text(item, "subject_id"))
        if apply_remap:
            subject_key = remap.get(subject_key, subject_key)
        return "|".join(
            (subject_key, _text(item, "entity_id"), _text(item, "relationship"))
        )

    before = _records_by_key(
        baseline.get("source_bindings"),
        lambda item: key(item, baseline_subjects, True),
    )
    after = _records_by_key(
        current.get("source_bindings"),
        lambda item: key(item, current_subjects, False),
    )
    changes: list[StructrrChange] = []
    for identity in sorted(before.keys() - after.keys()):
        changes.append(
            StructrrChange(
                "source_binding", "removed", identity, before=before[identity]
            )
        )
    for identity in sorted(after.keys() - before.keys()):
        changes.append(
            StructrrChange("source_binding", "added", identity, after=after[identity])
        )
    for identity in sorted(before.keys() & after.keys()):
        changed_fields = _changed_fields(before[identity], after[identity])
        if changed_fields:
            kind: ChangeKind = (
                "moved"
                if set(changed_fields)
                <= {"id", "subject_id", "path", "span", "stable_key"}
                else "changed"
            )
            changes.append(
                StructrrChange(
                    "source_binding",
                    kind,
                    identity,
                    before=before[identity],
                    after=after[identity],
                    changed_fields=changed_fields,
                )
            )
    return changes


def _subject_stable_keys(snapshot: Mapping[str, Any]) -> dict[str, str]:
    subjects = snapshot.get("source_subjects")
    if not isinstance(subjects, Sequence) or isinstance(subjects, (str, bytes)):
        return {}
    return {
        _text(subject, "id"): _text(subject, "stable_key")
        for subject in subjects
        if isinstance(subject, Mapping) and _text(subject, "id")
    }


def _classify(
    affected: Sequence[StructrrChange],
    conflicts: Sequence[StructrrChange],
    remappings: Sequence[StructrrRemapping],
) -> RebaseClassification:
    if conflicts:
        if any(change.kind in {"removed", "ambiguous"} for change in conflicts):
            return "invalidated"
        return "conflicted"
    if not affected:
        return "clean"
    kinds = {change.kind for change in affected}
    if kinds <= {"moved", "renamed", "presentation"}:
        return (
            "mechanically_rebased"
            if remappings or kinds <= {"moved", "renamed"}
            else "context_refresh_required"
        )
    if "changed" in kinds:
        return "targeted_update_required"
    return "context_refresh_required"


def _is_affected(
    change: StructrrChange,
    entity_refs: set[str],
    subject_refs: set[str],
    binding_refs: set[str],
) -> bool:
    if not entity_refs and not subject_refs and not binding_refs:
        return False
    if change.area == "entity":
        return change.identity in entity_refs
    if change.area == "source_subject":
        return bool(
            {
                change.identity,
                _text(change.before, "stable_key"),
                _text(change.after, "stable_key"),
            }
            & subject_refs
        )
    if change.area == "source_binding":
        before = change.before or {}
        after = change.after or {}
        return bool(
            {change.identity, _text(before, "id"), _text(after, "id")} & binding_refs
            or {_text(before, "entity_id"), _text(after, "entity_id")} & entity_refs
            or {_text(before, "subject_id"), _text(after, "subject_id")} & subject_refs
        )
    before = change.before or {}
    after = change.after or {}
    return bool(
        {
            _text(before, "source"),
            _text(before, "target"),
            _text(after, "source"),
            _text(after, "target"),
        }
        & entity_refs
    )


def _is_conflict(change: StructrrChange) -> bool:
    if change.kind in {"removed", "ambiguous"}:
        return True
    if change.area == "relationship":
        return True
    if change.area == "source_subject":
        return change.kind == "changed"
    if change.area == "source_binding":
        return change.kind == "changed"
    return change.kind == "changed"


def _amendment_item(change: StructrrChange) -> str:
    prefix = f"{change.area}:{change.identity}"
    if change.kind in {"moved", "renamed"}:
        return f"Remap {prefix} and refresh its source provenance."
    if change.kind == "presentation":
        return f"Refresh context for {prefix}; no design decision is invalidated."
    if change.kind == "removed":
        return (
            f"Decide whether the proposal should retire {prefix} "
            "or select a replacement."
        )
    return (
        f"Reconsider {prefix} against the changed current design and source evidence."
    )


_PRESENTATION_FIELDS = {"description", "title", "summary", "rationale"}


def _record_key(area: str) -> Callable[[Mapping[str, Any]], str]:
    if area == "entity":
        return lambda item: _text(item, "id")
    return lambda item: (
        _text(item, "id")
        or "|".join(
            (_text(item, "source"), _text(item, "relationship"), _text(item, "target"))
        )
    )


def _records_by_key(
    value: object, key_fn: Callable[[Mapping[str, Any]], str]
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return {}
    return {
        key: item
        for item in value
        if isinstance(item, Mapping)
        for key in [key_fn(item)]
        if key
    }


def _changed_fields(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> tuple[str, ...]:
    return tuple(
        sorted(
            key
            for key in set(before) | set(after)
            if key not in {"action"} and before.get(key) != after.get(key)
        )
    )


def _spans_overlap(before: object, after: object) -> bool:
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return False
    before_start, before_end = before.get("start_line"), before.get("end_line")
    after_start, after_end = after.get("start_line"), after.get("end_line")
    return (
        isinstance(before_start, int)
        and isinstance(before_end, int)
        and isinstance(after_start, int)
        and isinstance(after_end, int)
        and before_start <= after_end
        and after_start <= before_end
    )


def _text(value: object, key: str) -> str:
    if not isinstance(value, Mapping):
        return ""
    item = value.get(key)
    return item.strip() if isinstance(item, str) else str(item or "").strip()


def _canonical_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    collections = {
        "entities",
        "entity_relationships",
        "source_subjects",
        "source_bindings",
    }
    result: dict[str, Any] = {}
    for key, value in snapshot.items():
        if (
            key in collections
            and isinstance(value, Sequence)
            and not isinstance(value, (str, bytes))
        ):
            records = [item for item in value if isinstance(item, Mapping)]
            result[key] = sorted((_canonical(item) for item in records), key=_json_key)
        else:
            result[key] = _canonical(value)
    return result


def _canonical(value: object) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_canonical(item) for item in value]
    return value


def _json_key(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
