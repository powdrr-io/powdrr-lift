"""Canonical, model-independent representation of a feature description."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

INSTRUCTION_LEDGER_SCHEMA_VERSION = "instruction-ledger-v1"
MAX_ATOMIC_SPLIT_CHILDREN = 8


class InstructionLedgerError(ValueError):
    """Raised when an instruction ledger or bounded model result is invalid."""


@dataclass(frozen=True, slots=True)
class InstructionSource:
    source_id: str
    work_item_name: str
    text: str
    text_fingerprint: str

    @classmethod
    def capture(cls, work_item_name: str, text: str) -> InstructionSource:
        if not work_item_name.strip():
            raise InstructionLedgerError("work_item_name must not be empty")
        if not text.strip():
            raise InstructionLedgerError("instruction text must not be empty")
        normalized_name = _slug(work_item_name)
        return cls(
            source_id=f"instruction-source:{normalized_name}",
            work_item_name=work_item_name,
            text=text,
            text_fingerprint=_fingerprint(text),
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": "instruction-source-v1",
            "source_id": self.source_id,
            "work_item_name": self.work_item_name,
            "text": self.text,
            "text_fingerprint": self.text_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class InstructionClause:
    clause_id: str
    source_id: str
    ordinal: int
    text: str
    source_span: tuple[int, int]
    parent_clause_id: str | None = None
    validation_group_id: str | None = None
    validation_relation: str = "independent"
    derivation: str = "deterministic-sentence-v1"
    semantic_relations: tuple[ScopeRelation, ...] = ()
    modifier_attachments: tuple[ScopeRelation, ...] = ()

    def __post_init__(self) -> None:
        if not self.clause_id.strip():
            raise InstructionLedgerError("clause_id must not be empty")
        if not self.source_id.strip():
            raise InstructionLedgerError("source_id must not be empty")
        if self.ordinal < 1:
            raise InstructionLedgerError("clause ordinal must be positive")
        if not self.text.strip():
            raise InstructionLedgerError("clause text must not be empty")
        start, end = self.source_span
        if start < 0 or end <= start:
            raise InstructionLedgerError("clause source span must be non-empty")
        if self.validation_relation not in {
            "independent",
            "all_together",
            "ordered",
            "alternatives",
            "conditional",
        }:
            raise InstructionLedgerError("clause validation relation is invalid")
        if self.validation_relation != "independent" and not self.validation_group_id:
            raise InstructionLedgerError(
                "related clauses require a validation group ID"
            )

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.to_data(include_fingerprint=False))

    def to_data(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": "instruction-clause-v1",
            "clause_id": self.clause_id,
            "source_id": self.source_id,
            "ordinal": self.ordinal,
            "text": self.text,
            "source_span": {
                "start": self.source_span[0],
                "end": self.source_span[1],
            },
            "parent_clause_id": self.parent_clause_id,
            "derivation": self.derivation,
        }
        if self.validation_group_id is not None:
            data["validation_group_id"] = self.validation_group_id
            data["validation_relation"] = self.validation_relation
        if self.semantic_relations:
            data["semantic_relations"] = [
                item.to_data() for item in self.semantic_relations
            ]
        if self.modifier_attachments:
            data["modifier_attachments"] = [
                item.to_data() for item in self.modifier_attachments
            ]
        if include_fingerprint:
            data["fingerprint"] = self.fingerprint
        return data


@dataclass(frozen=True, slots=True)
class ScopeRelation:
    """A source-evidenced relation among compiler-owned split children."""

    relation_type: str
    label: str
    child_clause_ids: tuple[str, ...]
    evidence: str

    def to_data(self) -> dict[str, Any]:
        return {
            "relation_type": self.relation_type,
            "label": self.label,
            "child_clause_ids": list(self.child_clause_ids),
            "evidence": self.evidence,
        }

    @classmethod
    def from_data(cls, raw: Any) -> ScopeRelation:
        if not isinstance(raw, dict) or set(raw) != {
            "relation_type",
            "label",
            "child_clause_ids",
            "evidence",
        }:
            raise InstructionLedgerError("scope relation is malformed")
        relation_type = raw["relation_type"]
        label = raw["label"]
        ids = raw["child_clause_ids"]
        evidence = raw["evidence"]
        allowed = {
            "list_relation": {
                "independent_required",
                "shared_predicate",
                "allowed_alternatives",
                "ordered_required",
                "unclear",
            },
            "reference_resolution": {"resolved", "ambiguous", "unresolved"},
            "modifier_attachment": {
                "one_child",
                "specified_children",
                "entire_group",
                "unclear",
            },
            "condition_attachment": {
                "one_child",
                "specified_children",
                "entire_group",
                "unclear",
            },
        }
        if (
            not isinstance(relation_type, str)
            or relation_type not in allowed
            or not isinstance(label, str)
            or label not in allowed[relation_type]
            or not isinstance(ids, list)
            or not ids
            or not all(isinstance(item, str) and item for item in ids)
            or len(set(ids)) != len(ids)
            or not isinstance(evidence, str)
            or not evidence.strip()
        ):
            raise InstructionLedgerError("scope relation is invalid")
        return cls(relation_type, label, tuple(ids), evidence.strip())


@dataclass(frozen=True, slots=True)
class AtomicitySplitDiagnostic:
    """A model split rejected by a deterministic ledger guard."""

    source_clause_id: str
    reason_code: str
    child_indexes: tuple[int, ...]
    source_span: tuple[int, int]
    details: str | None = None

    def to_data(self) -> dict[str, Any]:
        data = {
            "source_clause_id": self.source_clause_id,
            "reason_code": self.reason_code,
            "child_indexes": list(self.child_indexes),
            "source_span": {"start": self.source_span[0], "end": self.source_span[1]},
        }
        if self.details is not None:
            data["details"] = self.details
        return data


@dataclass(frozen=True, slots=True)
class InstructionLedger:
    source: InstructionSource
    clauses: tuple[InstructionClause, ...]
    split_diagnostics: tuple[AtomicitySplitDiagnostic, ...] = ()

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.to_data(include_fingerprint=False))

    def to_data(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": INSTRUCTION_LEDGER_SCHEMA_VERSION,
            "source": self.source.to_data(),
            "clauses": [item.to_data() for item in self.clauses],
        }
        if self.split_diagnostics:
            data["split_diagnostics"] = [
                item.to_data() for item in self.split_diagnostics
            ]
        if include_fingerprint:
            data["fingerprint"] = self.fingerprint
        return data

    @classmethod
    def from_data(cls, raw: dict[str, Any]) -> InstructionLedger:
        if raw.get("schema_version") != INSTRUCTION_LEDGER_SCHEMA_VERSION:
            raise InstructionLedgerError("unsupported instruction ledger schema")
        source_raw = raw.get("source")
        clause_raw = raw.get("clauses")
        if not isinstance(source_raw, dict) or not isinstance(clause_raw, list):
            raise InstructionLedgerError("instruction ledger is incomplete")
        source = InstructionSource(
            source_id=_required_string(source_raw, "source_id"),
            work_item_name=_required_string(source_raw, "work_item_name"),
            text=_required_string(source_raw, "text"),
            text_fingerprint=_required_string(source_raw, "text_fingerprint"),
        )
        clauses: list[InstructionClause] = []
        for item in clause_raw:
            if not isinstance(item, dict):
                raise InstructionLedgerError("instruction clause must be an object")
            span = item.get("source_span")
            if not isinstance(span, dict):
                raise InstructionLedgerError("instruction clause span is missing")
            clauses.append(
                InstructionClause(
                    clause_id=_required_string(item, "clause_id"),
                    source_id=_required_string(item, "source_id"),
                    ordinal=_required_int(item, "ordinal"),
                    text=_required_string(item, "text"),
                    source_span=(
                        _required_int(span, "start"),
                        _required_int(span, "end"),
                    ),
                    parent_clause_id=_optional_string(item, "parent_clause_id"),
                    validation_group_id=_optional_string(item, "validation_group_id"),
                    validation_relation=str(
                        item.get("validation_relation", "independent")
                    ),
                    derivation=_required_string(item, "derivation"),
                    semantic_relations=tuple(
                        ScopeRelation.from_data(value)
                        for value in item.get("semantic_relations", [])
                    ),
                    modifier_attachments=tuple(
                        ScopeRelation.from_data(value)
                        for value in item.get("modifier_attachments", [])
                    ),
                )
            )
        raw_diagnostics = raw.get("split_diagnostics", [])
        if not isinstance(raw_diagnostics, list):
            raise InstructionLedgerError("instruction split diagnostics must be a list")
        diagnostics: list[AtomicitySplitDiagnostic] = []
        for item in raw_diagnostics:
            if not isinstance(item, dict) or not isinstance(
                item.get("child_indexes"), list
            ):
                raise InstructionLedgerError(
                    "instruction split diagnostic is malformed"
                )
            span = item.get("source_span")
            if not isinstance(span, dict):
                raise InstructionLedgerError(
                    "instruction split diagnostic span is missing"
                )
            diagnostics.append(
                AtomicitySplitDiagnostic(
                    source_clause_id=_required_string(item, "source_clause_id"),
                    reason_code=_required_string(item, "reason_code"),
                    child_indexes=tuple(
                        _required_int({"index": value}, "index")
                        for value in item["child_indexes"]
                    ),
                    source_span=(
                        _required_int(span, "start"),
                        _required_int(span, "end"),
                    ),
                    details=(
                        item.get("details")
                        if isinstance(item.get("details"), str)
                        else None
                    ),
                )
            )
        ledger = cls(
            source=source,
            clauses=tuple(clauses),
            split_diagnostics=tuple(diagnostics),
        )
        if raw.get("fingerprint") != ledger.fingerprint:
            raise InstructionLedgerError("instruction ledger fingerprint is stale")
        ledger.validate()
        return ledger

    def validate(self) -> None:
        expected_ids = [
            f"instruction-{index:03d}" for index in range(1, len(self.clauses) + 1)
        ]
        actual_ids = [item.clause_id for item in self.clauses]
        if actual_ids != expected_ids:
            raise InstructionLedgerError(
                "instruction clauses must use deterministic contiguous IDs"
            )
        for index, clause in enumerate(self.clauses, start=1):
            if clause.ordinal != index:
                raise InstructionLedgerError(
                    "instruction clause ordinals are not contiguous"
                )
            if clause.source_id != self.source.source_id:
                raise InstructionLedgerError(
                    "instruction clause references another source"
                )
            start, end = clause.source_span
            if end > len(self.source.text):
                raise InstructionLedgerError(
                    "instruction clause span exceeds source text"
                )
            for relation in (*clause.semantic_relations, *clause.modifier_attachments):
                if any(
                    child_id not in actual_ids for child_id in relation.child_clause_ids
                ):
                    raise InstructionLedgerError(
                        "scope relation references an unknown child clause"
                    )
                evidence = " ".join(relation.evidence.casefold().split())
                parent_text = " ".join(self.source.text[start:end].casefold().split())
                if evidence not in parent_text:
                    raise InstructionLedgerError(
                        "scope relation evidence is not present in its source clause"
                    )
        for diagnostic in self.split_diagnostics:
            start, end = diagnostic.source_span
            if (
                not diagnostic.source_clause_id.strip()
                or diagnostic.reason_code
                not in {"duplicate_child", "empty_child", "invalid_scope_relation"}
                or len(diagnostic.child_indexes)
                < (
                    0
                    if diagnostic.reason_code == "invalid_scope_relation"
                    else 1
                    if diagnostic.reason_code == "empty_child"
                    else 2
                )
                or any(index < 1 for index in diagnostic.child_indexes)
                or start < 0
                or end <= start
                or end > len(self.source.text)
            ):
                raise InstructionLedgerError("instruction split diagnostic is invalid")


@dataclass(frozen=True, slots=True)
class AtomicityDecision:
    """The only semantic fields a model may return for clause atomicity."""

    multiple: bool

    @classmethod
    def from_data(cls, raw: dict[str, Any]) -> AtomicityDecision:
        if set(raw) - {
            "multiple",
            "statements",
            "validation_groups",
            "semantic_relations",
            "modifier_attachments",
        } or not isinstance(raw.get("multiple"), bool):
            raise InstructionLedgerError(
                "atomicity response must contain boolean multiple and optional "
                "statements"
            )
        return cls(multiple=raw["multiple"])


def compile_instruction_ledger(
    work_item_name: str, feature_description: str
) -> InstructionLedger:
    """Capture and deterministically segment an instruction description."""
    source = InstructionSource.capture(work_item_name, feature_description)
    normalized, offsets = _normalize_with_offsets(feature_description)
    clauses: list[InstructionClause] = []
    for ordinal, (start, end) in enumerate(_sentence_spans(normalized), start=1):
        piece = normalized[start:end]
        clauses.append(
            InstructionClause(
                clause_id=f"instruction-{ordinal:03d}",
                source_id=source.source_id,
                ordinal=ordinal,
                text=piece,
                source_span=(offsets[start], offsets[end - 1] + 1),
            )
        )
    ledger = InstructionLedger(source=source, clauses=tuple(clauses))
    ledger.validate()
    return ledger


def apply_atomicity_decisions(
    ledger: InstructionLedger,
    decisions: dict[str, dict[str, Any]],
) -> InstructionLedger:
    """Apply bounded split results while keeping all IDs compiler-owned."""
    output: list[InstructionClause] = []
    diagnostics = list(ledger.split_diagnostics)
    for clause in ledger.clauses:
        raw = decisions.get(clause.clause_id, {"multiple": False})
        decision = AtomicityDecision.from_data(raw)
        if not decision.multiple:
            output.append(clause)
            continue
        statements = raw.get("statements")
        if (
            not isinstance(statements, list)
            or not 2 <= len(statements) <= MAX_ATOMIC_SPLIT_CHILDREN
            or not all(isinstance(item, str) and item.strip() for item in statements)
        ):
            raise InstructionLedgerError(
                f"atomicity split for {clause.clause_id} must contain "
                f"2-{MAX_ATOMIC_SPLIT_CHILDREN} statements"
            )
        duplicate_indexes, empty_indexes = _split_statement_issues(statements)
        if duplicate_indexes or empty_indexes:
            output.append(clause)
            diagnostics.append(
                AtomicitySplitDiagnostic(
                    source_clause_id=clause.clause_id,
                    reason_code=("empty_child" if empty_indexes else "duplicate_child"),
                    child_indexes=empty_indexes or duplicate_indexes,
                    source_span=clause.source_span,
                )
            )
            continue
        validation_groups = _normalize_validation_groups(
            raw.get("validation_groups", []), len(statements)
        )
        try:
            relation_sets = (
                _normalize_scope_relations(
                    raw.get("semantic_relations", []),
                    {"list_relation", "reference_resolution"},
                    len(statements),
                    clause,
                ),
                _normalize_scope_relations(
                    raw.get("modifier_attachments", []),
                    {"modifier_attachment", "condition_attachment"},
                    len(statements),
                    clause,
                ),
            )
        except InstructionLedgerError as error:
            output.append(clause)
            diagnostics.append(
                AtomicitySplitDiagnostic(
                    source_clause_id=clause.clause_id,
                    reason_code="invalid_scope_relation",
                    child_indexes=(),
                    source_span=clause.source_span,
                    details=str(error),
                )
            )
            continue
        next_ordinal = len(output)
        semantic_relations = _materialize_scope_relations(
            relation_sets[0], clause.clause_id, next_ordinal
        )
        modifier_attachments = _materialize_scope_relations(
            relation_sets[1], clause.clause_id, next_ordinal
        )
        for statement in statements:
            statement_ordinal = (
                len(
                    [
                        item
                        for item in output
                        if item.parent_clause_id == f"candidate:{clause.clause_id}"
                    ]
                )
                + 1
            )
            group_id, relation = _atomic_validation_group(
                validation_groups,
                statement_ordinal,
                len(statements),
                clause.clause_id,
            )
            output.append(
                InstructionClause(
                    clause_id=f"pending-{len(output) + 1}",
                    source_id=clause.source_id,
                    ordinal=len(output) + 1,
                    text=statement.strip(),
                    source_span=clause.source_span,
                    parent_clause_id=f"candidate:{clause.clause_id}",
                    validation_group_id=group_id,
                    validation_relation=relation,
                    derivation="bounded-atomicity-v1",
                    semantic_relations=semantic_relations,
                    modifier_attachments=modifier_attachments,
                )
            )
    renumbered = tuple(
        InstructionClause(
            clause_id=f"instruction-{index:03d}",
            source_id=clause.source_id,
            ordinal=index,
            text=clause.text,
            source_span=clause.source_span,
            parent_clause_id=clause.parent_clause_id,
            validation_group_id=clause.validation_group_id,
            validation_relation=clause.validation_relation,
            derivation=clause.derivation,
            semantic_relations=clause.semantic_relations,
            modifier_attachments=clause.modifier_attachments,
        )
        for index, clause in enumerate(output, start=1)
    )
    result = InstructionLedger(
        source=ledger.source,
        clauses=renumbered,
        split_diagnostics=tuple(diagnostics),
    )
    result.validate()
    return result


def _split_statement_issues(
    statements: list[str],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    first_by_text: dict[str, int] = {}
    duplicates: set[int] = set()
    empty: set[int] = set()
    for index, statement in enumerate(statements, start=1):
        normalized = _normalize_split_statement(statement)
        if not normalized:
            empty.add(index)
            continue
        first = first_by_text.get(normalized)
        if first is None:
            first_by_text[normalized] = index
        else:
            duplicates.update((first, index))
    return tuple(sorted(duplicates)), tuple(sorted(empty))


def _normalize_split_statement(statement: str) -> str:
    """Normalize surface-only Markdown differences before duplicate checks."""
    normalized = unicodedata.normalize("NFKC", statement)
    normalized = re.sub(
        r"(?m)^\s{0,3}(?:#{1,6}\s+|(?:[-*+]|\d+[.)])\s+|>\s?)", "", normalized
    )
    normalized = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", normalized)
    normalized = normalized.replace("`", "")
    normalized = re.sub(
        r"(?<!\w)(\*{1,2}|_{1,2}|~~)(?=\S)(.*?\S)\1(?!\w)",
        r"\2",
        normalized,
        flags=re.DOTALL,
    )
    return " ".join(normalized.split()).casefold()


def _normalize_scope_relations(
    raw_relations: Any,
    allowed_types: set[str],
    child_count: int,
    parent: InstructionClause,
) -> list[tuple[str, str, tuple[int, ...], str]]:
    if raw_relations is None:
        return []
    if not isinstance(raw_relations, list):
        raise InstructionLedgerError("scope relations must be a list")
    normalized: list[tuple[str, str, tuple[int, ...], str]] = []
    source_text = parent.text.casefold()
    for raw in raw_relations:
        if isinstance(raw, str):
            try:
                parsed_type, parsed_label, children, parsed_evidence = raw.split("|", 3)
                raw = {
                    "relation_type": parsed_type,
                    "label": parsed_label,
                    "child_indexes": [int(value) for value in children.split(",")],
                    "evidence": parsed_evidence,
                }
            except (TypeError, ValueError) as error:
                raise InstructionLedgerError(
                    "scope relation string is malformed"
                ) from error
        if not isinstance(raw, dict) or set(raw) != {
            "relation_type",
            "label",
            "child_indexes",
            "evidence",
        }:
            raise InstructionLedgerError("scope relation response is malformed")
        kind = raw.get("relation_type")
        label = raw.get("label")
        indexes = raw.get("child_indexes")
        evidence = raw.get("evidence")
        if (
            not isinstance(kind, str)
            or kind not in allowed_types
            or not isinstance(indexes, list)
            or not indexes
            or not all(
                isinstance(index, int)
                and not isinstance(index, bool)
                and 1 <= index <= child_count
                for index in indexes
            )
            or len(set(indexes)) != len(indexes)
            or not isinstance(evidence, str)
            or not evidence.strip()
            or " ".join(evidence.casefold().split())
            not in " ".join(source_text.split())
        ):
            raise InstructionLedgerError(
                "scope relation has invalid child references or source evidence"
            )
        relation = ScopeRelation.from_data(
            {
                "relation_type": kind,
                "label": label,
                "child_clause_ids": [f"child-{index}" for index in indexes],
                "evidence": evidence,
            }
        )
        if relation.relation_type.endswith("attachment"):
            expected_len = {
                "one_child": 1,
                "specified_children": None,
                "entire_group": child_count,
                "unclear": None,
            }[relation.label]
            if expected_len is not None and len(indexes) != expected_len:
                raise InstructionLedgerError(
                    f"{relation.label} attachment has the wrong child scope"
                )
            if relation.label == "entire_group" and set(indexes) != set(
                range(1, child_count + 1)
            ):
                raise InstructionLedgerError(
                    "entire_group attachment must reference every child"
                )
        normalized.append((kind, relation.label, tuple(indexes), relation.evidence))
    return normalized


def _materialize_scope_relations(
    relations: list[tuple[str, str, tuple[int, ...], str]],
    parent_clause_id: str,
    preceding_children: int,
) -> tuple[ScopeRelation, ...]:
    return tuple(
        ScopeRelation(
            relation_type=kind,
            label=label,
            child_clause_ids=tuple(
                f"instruction-{preceding_children + index:03d}" for index in indexes
            ),
            evidence=evidence,
        )
        for kind, label, indexes, evidence in relations
    )


def _normalize_validation_groups(
    raw_groups: Any, statement_count: int
) -> list[dict[str, Any]]:
    if raw_groups is None:
        return []
    if not isinstance(raw_groups, list):
        raise InstructionLedgerError("validation_groups must be a list")
    groups: list[dict[str, Any]] = []
    for group in raw_groups:
        if isinstance(group, str):
            try:
                normalized_group = group.strip().removeprefix("./").strip()
                members_part, parsed_relation = normalized_group.split(
                    ";relation=", maxsplit=1
                )
                members_text = members_part.removeprefix("members=")
                group = {
                    "members": [int(item) for item in members_text.split(",")],
                    "relation": parsed_relation,
                }
            except (ValueError, TypeError) as exc:
                raise InstructionLedgerError(
                    "validation group string is malformed"
                ) from exc
        if not isinstance(group, dict) or set(group) != {"members", "relation"}:
            raise InstructionLedgerError(
                "validation group must contain members and relation"
            )
        members = group.get("members")
        relation_value = group.get("relation")
        if (
            not isinstance(members, list)
            or not members
            or not all(
                isinstance(value, int)
                and not isinstance(value, bool)
                and 1 <= value <= statement_count
                for value in members
            )
            or len(set(members)) != len(members)
            or not isinstance(relation_value, str)
            or relation_value
            not in {"all_together", "ordered", "alternatives", "conditional"}
        ):
            raise InstructionLedgerError("validation group is invalid")
        # A one-member group carries no relationship between statements. The
        # model occasionally emits one when only one result needs no grouping.
        # Unlike an all_together hint, a singleton conditional group signals a
        # lost branch: silently dropping it would erase the source relationship
        # that makes each mode-to-outcome branch required when applicable.
        if len(members) == 1:
            if relation_value == "conditional":
                raise InstructionLedgerError(
                    "conditional validation group must contain at least two branches"
                )
            continue
        groups.append({"members": list(members), "relation": relation_value})

    normalized: list[dict[str, Any]] = []
    for group in groups:
        members = set(group["members"])
        overlapping = [
            existing
            for existing in normalized
            if members.intersection(existing["members"])
        ]
        if not overlapping:
            normalized.append(
                {"members": sorted(members), "relation": group["relation"]}
            )
            continue
        if group["relation"] != "all_together" or any(
            existing["relation"] != "all_together" for existing in overlapping
        ):
            raise InstructionLedgerError(
                "overlapping validation groups cannot be safely combined"
            )
        merged_members = members.union(
            *(set(existing["members"]) for existing in overlapping)
        )
        normalized = [
            existing for existing in normalized if existing not in overlapping
        ]
        normalized.append(
            {"members": sorted(merged_members), "relation": "all_together"}
        )
    return normalized


def _atomic_validation_group(
    raw_groups: Any, statement_ordinal: int, statement_count: int, parent_id: str
) -> tuple[str | None, str]:
    """Return a child's group and relation.

    A conditional group means each applicable branch is required.
    """
    if raw_groups is None:
        raw_groups = []
    if not isinstance(raw_groups, list):
        raise InstructionLedgerError("validation_groups must be a list")
    matching: list[tuple[int, str]] = []
    occupied: set[int] = set()
    for group_index, group in enumerate(raw_groups, start=1):
        if isinstance(group, str):
            try:
                members_part, relation = group.split(";relation=", maxsplit=1)
                members_text = members_part.removeprefix("members=")
                group = {
                    "members": [int(item) for item in members_text.split(",")],
                    "relation": relation,
                }
            except (ValueError, TypeError) as exc:
                raise InstructionLedgerError(
                    "validation group string is malformed"
                ) from exc
        if not isinstance(group, dict) or set(group) != {"members", "relation"}:
            raise InstructionLedgerError(
                "validation group must contain members and relation"
            )
        members = group.get("members")
        relation_value = group.get("relation")
        if (
            not isinstance(members, list)
            or len(members) < 2
            or not all(
                isinstance(value, int)
                and not isinstance(value, bool)
                and 1 <= value <= statement_count
                for value in members
            )
            or len(set(members)) != len(members)
            or relation_value
            not in {"all_together", "ordered", "alternatives", "conditional"}
        ):
            raise InstructionLedgerError("validation group is invalid")
        if occupied.intersection(members):
            raise InstructionLedgerError("validation groups overlap")
        occupied.update(members)
        if statement_ordinal in members:
            matching.append((group_index, str(relation_value)))
    if len(matching) > 1:
        raise InstructionLedgerError("statement belongs to multiple validation groups")
    if not matching:
        return None, "independent"
    group_index, relation = matching[0]
    return f"validation:{parent_id}:{group_index}", relation


def _normalize_with_offsets(text: str) -> tuple[str, list[int]]:
    """Collapse Markdown layout while retaining exact source offsets."""
    retained: list[tuple[str, int]] = []
    source_offset = 0
    structural_headings = {"requirements", "constraints", "out of scope"}
    for line in text.splitlines(keepends=True):
        line_content = line.rstrip("\r\n")
        heading = re.sub(r"^\s*#+\s*", "", line_content).strip().lower()
        if heading in structural_headings:
            source_offset += len(line)
            continue
        marker = re.match(r"^\s*(?:\d+[.)]|[-*+])\s+", line_content)
        marker_end = marker.end() if marker else 0
        retained.extend(
            (character, source_offset + index)
            for index, character in enumerate(line_content)
            if index >= marker_end
        )
        if len(line_content) < len(line):
            retained.append(("\n", source_offset + len(line_content)))
        source_offset += len(line)

    normalized: list[str] = []
    offsets: list[int] = []
    index = 0
    while index < len(retained):
        if retained[index][0].isspace():
            start = index
            while index < len(retained) and retained[index][0].isspace():
                index += 1
            if normalized and index < len(retained):
                normalized.append(" ")
                offsets.append(retained[start][1])
            continue
        normalized.append(retained[index][0])
        offsets.append(retained[index][1])
        index += 1
    while normalized and normalized[-1] == " ":
        normalized.pop()
        offsets.pop()
    return "".join(normalized), offsets


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Split sentence-like clauses without treating list markers or ellipses as ends."""
    spans: list[tuple[int, int]] = []
    start = 0
    for match in re.finditer(r"(?<=[.!?])\s+", text):
        punctuation = match.start() - 1
        if text[punctuation] == "." and (
            punctuation > 0
            and (text[punctuation - 1] == "." or text[punctuation - 1].isdigit())
            or punctuation + 1 < len(text)
            and text[punctuation + 1] == "."
        ):
            continue
        end = punctuation + 1
        if start < end:
            spans.append((start, end))
        start = match.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return result or "feature"


def _required_string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InstructionLedgerError(f"{key} must be a non-empty string")
    return value


def _optional_string(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InstructionLedgerError(f"{key} must be a non-empty string or null")
    return value


def _required_int(raw: dict[str, Any], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise InstructionLedgerError(f"{key} must be an integer")
    return value


__all__ = [
    "AtomicityDecision",
    "AtomicitySplitDiagnostic",
    "InstructionClause",
    "InstructionLedger",
    "InstructionLedgerError",
    "InstructionSource",
    "MAX_ATOMIC_SPLIT_CHILDREN",
    "apply_atomicity_decisions",
    "compile_instruction_ledger",
]
