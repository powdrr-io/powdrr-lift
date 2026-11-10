"""Canonical, immutable meaning for source-addressable product definitions.

These records belong in accepted design data, not in source comments or a second
mutable intent store. Catalog compilation projects them without inventing intent.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

REFERENCE_DEFINITION_SCHEMA = "structrr-reference-definition-v1"
REFERENCE_KINDS = frozenset(
    {"feature", "decision", "assumption", "invariant", "procedure", "guidance"}
)
REFERENCE_LIFECYCLES = frozenset(
    {
        "proposed",
        "accepted",
        "active",
        "deprecated",
        "retired",
        "superseded",
        "rejected",
    }
)


def validate_reference_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9_./:-]{1,256}", value
    ):
        raise ValueError("reference ID must match the source annotation grammar")
    return value


def required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} requires non-empty text")
    return value


def text_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} requires a sequence of strings")
    return tuple(required_text(item, field) for item in value)


@dataclass(frozen=True, slots=True)
class ReferenceDefinition:
    id: str
    kind: str
    statement: str
    source_ref: str
    version: int = 1
    lifecycle: str = "active"
    rationale: str | None = None
    invalidation_triggers: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    successor: str | None = None

    def __post_init__(self) -> None:
        validate_reference_id(self.id)
        if not isinstance(self.kind, str) or self.kind not in REFERENCE_KINDS:
            raise ValueError("unsupported canonical reference kind")
        required_text(self.statement, "statement")
        required_text(self.source_ref, "source_ref")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("reference version must be a positive integer")
        if (
            not isinstance(self.lifecycle, str)
            or self.lifecycle not in REFERENCE_LIFECYCLES
        ):
            raise ValueError("unsupported canonical reference lifecycle")
        if self.rationale is not None:
            required_text(self.rationale, "rationale")
        for field in ("invalidation_triggers", "evidence_refs"):
            values = getattr(self, field)
            if not isinstance(values, tuple):
                raise ValueError(f"{field} must be immutable")
            text_tuple(values, field)
        if self.invalidation_triggers and self.kind != "assumption":
            raise ValueError("only assumptions declare invalidation triggers")
        if self.successor is not None:
            validate_reference_id(self.successor)
            if self.successor == self.id or self.lifecycle == "active":
                raise ValueError("successors require an inactive, distinct reference")

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": REFERENCE_DEFINITION_SCHEMA,
            "id": self.id,
            "kind": self.kind,
            "statement": self.statement,
            "source_ref": self.source_ref,
            "version": self.version,
            "lifecycle": self.lifecycle,
            "rationale": self.rationale,
            "invalidation_triggers": list(self.invalidation_triggers),
            "evidence_refs": list(self.evidence_refs),
            "successor": self.successor,
        }

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> ReferenceDefinition:
        fields = {
            "schema_version",
            "id",
            "kind",
            "statement",
            "source_ref",
            "version",
            "lifecycle",
            "rationale",
            "invalidation_triggers",
            "evidence_refs",
            "successor",
        }
        if (
            set(raw) != fields
            or raw.get("schema_version") != REFERENCE_DEFINITION_SCHEMA
        ):
            raise ValueError("unsupported or invalid canonical reference definition")
        try:
            return cls(
                **{
                    key: text_tuple(value, key)
                    if key in {"invalidation_triggers", "evidence_refs"}
                    else value
                    for key, value in raw.items()
                    if key != "schema_version"
                }
            )
        except TypeError as error:
            raise ValueError("invalid canonical reference definition field") from error
