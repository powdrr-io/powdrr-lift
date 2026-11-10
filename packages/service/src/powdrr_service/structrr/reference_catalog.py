"""Frozen source-reference context compiled only from caller-authorized design.

The caller establishes accepted-document and amendment authorization. This pure
compiler checks their shape and revision binding; it does not authenticate users,
read candidate files, infer intent from comments, or install accepted state.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from powdrr_service.decision_obligation import content_fingerprint
from powdrr_service.structrr.active_intent import resolve_active_intent
from powdrr_service.structrr.design import DESIGN_SCHEMA, DesiredState
from powdrr_service.structrr.reference_definitions import (
    REFERENCE_LIFECYCLES,
    ReferenceDefinition,
    required_text,
    text_tuple,
    validate_reference_id,
)

REFERENCE_CATALOG_SCHEMA = "structrr-reference-catalog-v1"
AUTHORIZED_AMENDMENT_SCHEMA = "structrr-authorized-reference-amendment-v1"
REFERENCE_RENDERER_REVISION = "structrr-reference-renderer-v1"


def _digest(value: object) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", value):
        raise ValueError("invalid reference artifact digest")


def _base_commit(value: object) -> None:
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-f0-9]{40}|[a-f0-9]{64}", value
    ):
        raise ValueError("base_commit requires a full Git commit ID")


def _records(
    document: Mapping[str, Any], section: str
) -> tuple[Mapping[str, Any], ...]:
    values = document.get(section, [])
    if not isinstance(values, list) or any(
        not isinstance(item, Mapping) for item in values
    ):
        raise ValueError(f"{section} requires a list of canonical records")
    return tuple(values)


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    reference: ReferenceDefinition
    authority: str = "accepted"

    def __post_init__(self) -> None:
        if not isinstance(self.reference, ReferenceDefinition):
            raise ValueError("catalog entry requires a canonical definition")
        if not isinstance(self.authority, str) or self.authority not in {
            "accepted",
            "authorized_amendment",
        }:
            raise ValueError("invalid catalog authority")

    def to_data(self) -> dict[str, Any]:
        return {**self.reference.to_data(), "authority": self.authority}

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> CatalogEntry:
        return cls(
            ReferenceDefinition.from_data(
                {key: value for key, value in raw.items() if key != "authority"}
            ),
            raw.get("authority", ""),
        )


@dataclass(frozen=True, slots=True)
class AuthorizedReferenceAmendment:
    authorization_ref: str
    base_commit: str
    accepted_document_digest: str
    definitions: tuple[ReferenceDefinition, ...]

    def __post_init__(self) -> None:
        required_text(self.authorization_ref, "authorization_ref")
        _base_commit(self.base_commit)
        _digest(self.accepted_document_digest)
        if not isinstance(self.definitions, tuple) or any(
            not isinstance(item, ReferenceDefinition) for item in self.definitions
        ):
            raise ValueError(
                "amendment definitions must be immutable canonical records"
            )
        if len({item.id for item in self.definitions}) != len(self.definitions):
            raise ValueError("duplicate amendment reference IDs")
        object.__setattr__(
            self,
            "definitions",
            tuple(sorted(self.definitions, key=lambda item: item.id)),
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": AUTHORIZED_AMENDMENT_SCHEMA,
            "authorization_ref": self.authorization_ref,
            "base_commit": self.base_commit,
            "accepted_document_digest": self.accepted_document_digest,
            "definitions": [
                item.to_data()
                for item in sorted(self.definitions, key=lambda item: item.id)
            ],
        }

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> AuthorizedReferenceAmendment:
        if (
            set(raw)
            != {
                "schema_version",
                "authorization_ref",
                "base_commit",
                "accepted_document_digest",
                "definitions",
            }
            or raw.get("schema_version") != AUTHORIZED_AMENDMENT_SCHEMA
        ):
            raise ValueError(
                "expected a workflow-authorized reference amendment envelope"
            )
        return cls(
            raw["authorization_ref"],
            raw["base_commit"],
            raw["accepted_document_digest"],
            tuple(
                ReferenceDefinition.from_data(item)
                for item in _records(raw, "definitions")
            ),
        )


@dataclass(frozen=True, slots=True)
class ReferenceCatalog:
    base_commit: str
    accepted_document_digest: str
    authorized_amendment_digest: str | None
    entries: tuple[CatalogEntry, ...]
    renderer_revision: str = REFERENCE_RENDERER_REVISION

    def __post_init__(self) -> None:
        _base_commit(self.base_commit)
        _digest(self.accepted_document_digest)
        if self.authorized_amendment_digest is not None:
            _digest(self.authorized_amendment_digest)
        if self.renderer_revision != REFERENCE_RENDERER_REVISION:
            raise ValueError("unsupported reference renderer revision")
        if not isinstance(self.entries, tuple) or any(
            not isinstance(item, CatalogEntry) for item in self.entries
        ):
            raise ValueError("catalog entries must be immutable")
        ids = [item.reference.id for item in self.entries]
        if ids != sorted(set(ids)):
            raise ValueError("catalog entries must have unique sorted IDs")
        if self.authorized_amendment_digest is None and any(
            item.authority == "authorized_amendment" for item in self.entries
        ):
            raise ValueError("amended entries require an amendment digest")

    def to_data(self, *, include_digest: bool = True) -> dict[str, Any]:
        data = {
            "schema_version": REFERENCE_CATALOG_SCHEMA,
            "base_commit": self.base_commit,
            "accepted_document_digest": self.accepted_document_digest,
            "authorized_amendment_digest": self.authorized_amendment_digest,
            "entries": [entry.to_data() for entry in self.entries],
            "renderer_revision": self.renderer_revision,
        }
        if include_digest:
            data["catalog_digest"] = self.digest
        return data

    @property
    def digest(self) -> str:
        return content_fingerprint(self.to_data(include_digest=False))

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> ReferenceCatalog:
        expected = {
            "schema_version",
            "base_commit",
            "accepted_document_digest",
            "authorized_amendment_digest",
            "entries",
            "renderer_revision",
            "catalog_digest",
        }
        if (
            set(raw) != expected
            or raw.get("schema_version") != REFERENCE_CATALOG_SCHEMA
        ):
            raise ValueError("unsupported or invalid reference catalog schema")
        catalog = cls(
            raw["base_commit"],
            raw["accepted_document_digest"],
            raw["authorized_amendment_digest"],
            tuple(CatalogEntry.from_data(item) for item in _records(raw, "entries")),
            raw["renderer_revision"],
        )
        if raw["catalog_digest"] != catalog.digest:
            raise ValueError("reference catalog digest mismatch")
        return catalog


@dataclass(frozen=True, slots=True)
class ReferenceResolution:
    ref: str
    status: str
    entry: CatalogEntry | None
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.ref, str):
            raise ValueError("resolution ref must be text")
        required_text(self.reason, "reason")
        if self.status == "unresolved_reference":
            if self.entry is not None:
                raise ValueError("unresolved reference cannot claim an entry")
        elif self.status in {"resolved", "inactive_reference"}:
            if (
                not isinstance(self.entry, CatalogEntry)
                or self.entry.reference.id != self.ref
            ):
                raise ValueError("reference resolution requires its exact entry")
            if (self.status == "resolved") != (
                self.entry.reference.lifecycle == "active"
            ):
                raise ValueError("reference resolution conflicts with lifecycle")
        else:
            raise ValueError("invalid reference resolution status")

    def to_data(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "status": self.status,
            "entry": self.entry.to_data() if self.entry else None,
            "reason": self.reason,
        }

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> ReferenceResolution:
        if set(raw) != {"ref", "status", "entry", "reason"}:
            raise ValueError("invalid reference resolution schema")
        entry = raw["entry"]
        if entry is not None and not isinstance(entry, Mapping):
            raise ValueError("invalid reference resolution entry")
        return cls(
            raw["ref"],
            raw["status"],
            CatalogEntry.from_data(entry) if entry is not None else None,
            raw["reason"],
        )


def _definition(raw: Mapping[str, Any], kind: str) -> ReferenceDefinition:
    """Explicit legacy-section adapter; delivery state is not product meaning."""
    if "schema_version" in raw:
        definition = ReferenceDefinition.from_data(raw)
        if definition.kind != kind:
            raise ValueError("canonical definition kind disagrees with its section")
        return definition
    if not isinstance(raw.get("active", True), bool):
        raise ValueError("active must be boolean")
    lifecycle = raw.get("lifecycle")
    if lifecycle is None:
        state = raw.get("state", raw.get("status"))
        lifecycle = (
            state
            if isinstance(state, str) and state in REFERENCE_LIFECYCLES
            else "active"
        )
        if (
            raw.get("action") in {"removed", "deleted"}
            or raw.get("active") is False
            or state == "removed"
        ):
            lifecycle = "retired" if lifecycle == "active" else lifecycle
    if "active" in raw and raw["active"] != (lifecycle == "active"):
        raise ValueError("active flag conflicts with lifecycle")
    if raw.get("action") in {"removed", "deleted"} and lifecycle == "active":
        raise ValueError("removed reference conflicts with active lifecycle")
    ref = validate_reference_id(raw.get("clause_id", raw.get("id")))
    statement = raw.get(
        "statement",
        raw.get("summary") if kind == "decision" else raw.get("description"),
    )
    return ReferenceDefinition(
        ref,
        kind,
        required_text(statement, f"{kind} statement"),
        required_text(
            raw.get("source_ref", raw.get("source", f"document:{ref}")), "source_ref"
        ),
        raw.get("version", 1),
        lifecycle,
        raw.get("rationale"),
        text_tuple(raw.get("invalidation_triggers", []), "invalidation_triggers"),
        text_tuple(raw.get("evidence_refs", []), "evidence_refs"),
        raw.get("successor"),
    )


def compile_reference_catalog(
    accepted_document: Mapping[str, Any],
    *,
    base_commit: str,
    authorized_amendment: Mapping[str, Any] | None = None,
) -> ReferenceCatalog:
    """Project accepted meaning and explicitly revision-bound amendments.

    No feature overlay is passed to the active-intent resolver. The amendment
    parameter accepts only an explicit authorization envelope, not a candidate
    feature document. Authorization provenance must be established by the caller.
    """
    accepted_digest = content_fingerprint(accepted_document)
    if accepted_document.get("schema_version") == DESIGN_SCHEMA:
        accepted_document = DesiredState.from_data(accepted_document).document
    definitions: dict[str, ReferenceDefinition] = {}

    def add(definition: ReferenceDefinition) -> None:
        existing = definitions.get(definition.id)
        if existing is not None and existing != definition:
            raise ValueError(f"conflicting reference ID: {definition.id}")
        definitions[definition.id] = definition

    for raw in _records(accepted_document, "reference_definitions"):
        add(ReferenceDefinition.from_data(raw))
    legacy = (
        _records(accepted_document, "active_intent")
        if "active_intent" in accepted_document
        else tuple(
            {"kind": kind, **raw}
            for section, kind in (("invariants", "invariant"), ("guidance", "guidance"))
            for raw in _records(accepted_document, section)
        )
    )
    if "active_intent" in accepted_document:
        legacy += tuple(
            {"kind": kind, **raw}
            for section, kind in (("invariants", "invariant"), ("guidance", "guidance"))
            for raw in _records(accepted_document, section)
            if _definition(raw, raw.get("kind", kind)).lifecycle != "active"
        )
    legacy_definitions: dict[str, ReferenceDefinition] = {}
    successors: dict[str, str] = {}

    def remember_successor(
        raw: Mapping[str, Any], definition: ReferenceDefinition
    ) -> None:
        predecessor = raw.get("supersedes_clause_id", raw.get("replaces"))
        if predecessor is not None:
            validate_reference_id(predecessor)
            if predecessor in successors and successors[predecessor] != definition.id:
                raise ValueError("conflicting reference successors")
            successors[predecessor] = definition.id

    for raw in legacy:
        definition = _definition(raw, raw.get("kind", "invariant"))
        prior = legacy_definitions.get(definition.id)
        if prior is not None and prior != definition:
            raise ValueError(f"conflicting reference ID: {definition.id}")
        legacy_definitions[definition.id] = definition
        remember_successor(raw, definition)
    active = resolve_active_intent(
        baseline_document={
            "active_intent": [
                {
                    "clause_id": d.id,
                    "kind": d.kind,
                    "statement": d.statement,
                    "source_ref": d.source_ref,
                    "version": d.version,
                    "active": d.lifecycle == "active",
                }
                for d in legacy_definitions.values()
            ]
        }
    )
    for clause in active:
        add(legacy_definitions[clause.clause_id])
    for definition in legacy_definitions.values():
        if definition.lifecycle != "active":
            add(definition)
    for section, kind in (
        ("features", "feature"),
        ("decisions", "decision"),
        ("human-decisions", "decision"),
        ("assumptions", "assumption"),
    ):
        for raw in _records(accepted_document, section):
            definition = _definition(raw, kind)
            add(definition)
            remember_successor(raw, definition)
    for predecessor, successor in successors.items():
        if predecessor not in definitions:
            continue
        definition = definitions[predecessor]
        if definition.lifecycle == "active":
            raise ValueError("superseded clause must be explicitly inactive")
        if definition.successor is not None and definition.successor != successor:
            raise ValueError("conflicting reference successors")
        definitions[predecessor] = replace(definition, successor=successor)
    entries = {ref: CatalogEntry(definition) for ref, definition in definitions.items()}
    amendment_digest = None
    if authorized_amendment is not None:
        amendment = AuthorizedReferenceAmendment.from_data(authorized_amendment)
        if (
            amendment.base_commit != base_commit
            or amendment.accepted_document_digest != accepted_digest
        ):
            raise ValueError(
                "authorized amendment is bound to a different design or base"
            )
        amendment_digest = content_fingerprint(amendment.to_data())
        for definition in amendment.definitions:
            current = entries.get(definition.id)
            if current is not None and definition.version <= current.reference.version:
                raise ValueError("an amendment must advance the reference version")
            entries[definition.id] = CatalogEntry(definition, "authorized_amendment")
    return ReferenceCatalog(
        base_commit,
        accepted_digest,
        amendment_digest,
        tuple(entries[key] for key in sorted(entries)),
    )


def resolve_reference(catalog: ReferenceCatalog, ref: str) -> ReferenceResolution:
    """Resolve exact opaque IDs; inactive references never redirect implicitly."""
    try:
        validate_reference_id(ref)
    except ValueError:
        return ReferenceResolution(
            ref, "unresolved_reference", None, "invalid reference ID"
        )
    entry = next((item for item in catalog.entries if item.reference.id == ref), None)
    if entry is None:
        return ReferenceResolution(
            ref,
            "unresolved_reference",
            None,
            "reference is absent from the frozen catalog",
        )
    if entry.reference.lifecycle != "active":
        return ReferenceResolution(
            ref,
            "inactive_reference",
            entry,
            f"reference is {entry.reference.lifecycle}; "
            "inspect its successor explicitly",
        )
    return ReferenceResolution(ref, "resolved", entry, "active canonical reference")
