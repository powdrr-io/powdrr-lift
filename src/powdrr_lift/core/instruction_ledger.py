"""Canonical, model-independent representation of a feature description."""

from __future__ import annotations

import hashlib
import json
import re
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
    derivation: str = "deterministic-sentence-v1"

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
        if include_fingerprint:
            data["fingerprint"] = self.fingerprint
        return data


@dataclass(frozen=True, slots=True)
class InstructionLedger:
    source: InstructionSource
    clauses: tuple[InstructionClause, ...]

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.to_data(include_fingerprint=False))

    def to_data(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": INSTRUCTION_LEDGER_SCHEMA_VERSION,
            "source": self.source.to_data(),
            "clauses": [item.to_data() for item in self.clauses],
        }
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
                    derivation=_required_string(item, "derivation"),
                )
            )
        ledger = cls(source=source, clauses=tuple(clauses))
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


@dataclass(frozen=True, slots=True)
class AtomicityDecision:
    """The only semantic fields a model may return for clause atomicity."""

    multiple: bool

    @classmethod
    def from_data(cls, raw: dict[str, Any]) -> AtomicityDecision:
        if set(raw) - {"multiple", "statements"} or not isinstance(
            raw.get("multiple"), bool
        ):
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
    pieces = [piece for piece in re.split(r"(?<=[.!?])\s+", normalized) if piece]
    clauses: list[InstructionClause] = []
    search_start = 0
    for ordinal, piece in enumerate(pieces, start=1):
        start = normalized.find(piece, search_start)
        if start < 0:
            raise InstructionLedgerError("failed to map normalized clause to source")
        end = start + len(piece)
        clauses.append(
            InstructionClause(
                clause_id=f"instruction-{ordinal:03d}",
                source_id=source.source_id,
                ordinal=ordinal,
                text=piece,
                source_span=(offsets[start], offsets[end - 1] + 1),
            )
        )
        search_start = end
    ledger = InstructionLedger(source=source, clauses=tuple(clauses))
    ledger.validate()
    return ledger


def apply_atomicity_decisions(
    ledger: InstructionLedger,
    decisions: dict[str, dict[str, Any]],
) -> InstructionLedger:
    """Apply bounded split results while keeping all IDs compiler-owned."""
    output: list[InstructionClause] = []
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
        for statement in statements:
            output.append(
                InstructionClause(
                    clause_id=f"pending-{len(output) + 1}",
                    source_id=clause.source_id,
                    ordinal=len(output) + 1,
                    text=statement.strip(),
                    source_span=clause.source_span,
                    parent_clause_id=f"candidate:{clause.clause_id}",
                    derivation="bounded-atomicity-v1",
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
            derivation=clause.derivation,
        )
        for index, clause in enumerate(output, start=1)
    )
    result = InstructionLedger(source=ledger.source, clauses=renumbered)
    result.validate()
    return result


def _normalize_with_offsets(text: str) -> tuple[str, list[int]]:
    normalized: list[str] = []
    offsets: list[int] = []
    index = 0
    while index < len(text):
        if text[index].isspace():
            start = index
            while index < len(text) and text[index].isspace():
                index += 1
            if normalized and index < len(text):
                normalized.append(" ")
                offsets.append(start)
            continue
        normalized.append(text[index])
        offsets.append(index)
        index += 1
    return "".join(normalized).strip(), offsets


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
    "InstructionClause",
    "InstructionLedger",
    "InstructionLedgerError",
    "InstructionSource",
    "MAX_ATOMIC_SPLIT_CHILDREN",
    "apply_atomicity_decisions",
    "compile_instruction_ledger",
]
