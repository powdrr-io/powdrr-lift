"""Immutable repository inventory and deterministic subject lookup primitives."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

INVENTORY_SCHEMA_VERSION = "semantic-repository-inventory-v1"
LOOKUP_SCHEMA_VERSION = "semantic-lookup-query-v1"
POPULATION_RECEIPT_SCHEMA_VERSION = "population-enumeration-receipt-v1"
LOOKUP_REVISION = "repository-lookup-v1"


class InventoryError(ValueError):
    """Raised when inventory or binding evidence is invalid."""


def normalize_terms(value: str) -> tuple[str, ...]:
    """Return retrieval keys without changing the authoritative source name."""
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    value = re.sub(r"[_./:-]+", " ", value)
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    words = [word.casefold() for word in value.split()]
    return tuple(
        word for word in words if word not in {"a", "an", "the", "all", "every"}
    )


@dataclass(frozen=True, slots=True)
class InventoryRecord:
    inventory_id: str
    kind: str
    canonical_name: str
    qualified_name: str
    normalized_terms: tuple[str, ...]
    aliases: tuple[str, ...]
    path: str
    span: tuple[int, int]
    language: str
    component_refs: tuple[str, ...] = ()
    structrr_refs: tuple[str, ...] = ()
    relationship_refs: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    test_refs: tuple[str, ...] = ()
    evidence_fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.inventory_id.strip() or not self.canonical_name.strip():
            raise InventoryError("inventory record requires an ID and canonical name")
        if self.span[0] < 1 or self.span[1] < self.span[0]:
            raise InventoryError("inventory record span is invalid")
        object.__setattr__(
            self,
            "evidence_fingerprint",
            self.evidence_fingerprint
            or _fingerprint(self.to_data(include_fingerprint=False)),
        )

    def to_data(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "inventory_id": self.inventory_id,
            "kind": self.kind,
            "canonical_name": self.canonical_name,
            "qualified_name": self.qualified_name,
            "normalized_terms": list(self.normalized_terms),
            "aliases": list(self.aliases),
            "path": self.path,
            "span": {"start_line": self.span[0], "end_line": self.span[1]},
            "language": self.language,
            "component_refs": list(self.component_refs),
            "structrr_refs": list(self.structrr_refs),
            "relationship_refs": list(self.relationship_refs),
            "capabilities": list(self.capabilities),
            "test_refs": list(self.test_refs),
        }
        if include_fingerprint:
            result["evidence_fingerprint"] = self.evidence_fingerprint
        return result

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> InventoryRecord:
        span = raw.get("span")
        if not isinstance(span, Mapping):
            raise InventoryError("inventory record span is missing")
        return cls(
            inventory_id=_string(raw, "inventory_id"),
            kind=_string(raw, "kind"),
            canonical_name=_string(raw, "canonical_name"),
            qualified_name=_string(raw, "qualified_name"),
            normalized_terms=_strings(raw, "normalized_terms"),
            aliases=_strings(raw, "aliases"),
            path=_string(raw, "path"),
            span=(int(span["start_line"]), int(span["end_line"])),
            language=_string(raw, "language"),
            component_refs=_strings(raw, "component_refs"),
            structrr_refs=_strings(raw, "structrr_refs"),
            relationship_refs=_strings(raw, "relationship_refs"),
            capabilities=_strings(raw, "capabilities"),
            test_refs=_strings(raw, "test_refs"),
            evidence_fingerprint=_string(raw, "evidence_fingerprint"),
        )


@dataclass(frozen=True, slots=True)
class RepositoryInventory:
    commit_ref: str
    structrr_revision: str
    adapter_revisions: tuple[str, ...]
    records: tuple[InventoryRecord, ...]

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.to_data(include_fingerprint=False))

    def require(self, inventory_id: str) -> InventoryRecord:
        for record in self.records:
            if record.inventory_id == inventory_id:
                return record
        raise InventoryError(f"inventory record is missing: {inventory_id}")

    def to_data(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "commit_ref": self.commit_ref,
            "structrr_revision": self.structrr_revision,
            "adapter_revisions": list(self.adapter_revisions),
            "records": [record.to_data() for record in self.records],
        }
        if include_fingerprint:
            result["fingerprint"] = self.fingerprint
        return result

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> RepositoryInventory:
        records = raw.get("records")
        if raw.get("schema_version") != INVENTORY_SCHEMA_VERSION or not isinstance(
            records, list
        ):
            raise InventoryError("repository inventory is malformed")
        return cls(
            _string(raw, "commit_ref"),
            _string(raw, "structrr_revision"),
            _strings(raw, "adapter_revisions"),
            tuple(
                InventoryRecord.from_data(item)
                for item in records
                if isinstance(item, Mapping)
            ),
        )


@dataclass(frozen=True, slots=True)
class StructrrLookupContext:
    aliases: Mapping[str, tuple[str, ...]] = None  # type: ignore[assignment]
    populations: Mapping[str, tuple[str, ...]] = None  # type: ignore[assignment]
    relationships: Mapping[str, tuple[str, ...]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "aliases", self.aliases or {})
        object.__setattr__(self, "populations", self.populations or {})
        object.__setattr__(self, "relationships", self.relationships or {})


@dataclass(frozen=True, slots=True)
class LookupQuery:
    source_ref: str
    source_text: str
    subject_text: str
    disposition: str
    behavior_family: str
    inventory_fingerprint: str
    explicit_names: tuple[str, ...] = ()
    structrr_context_fingerprint: str = ""

    @property
    def normalized_terms(self) -> tuple[str, ...]:
        return normalize_terms(self.subject_text)

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.to_data(include_fingerprint=False))

    def to_data(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": LOOKUP_SCHEMA_VERSION,
            "source_ref": self.source_ref,
            "source_text": self.source_text,
            "subject_text": self.subject_text,
            "normalized_terms": list(self.normalized_terms),
            "disposition": self.disposition,
            "behavior_family": self.behavior_family,
            "inventory_fingerprint": self.inventory_fingerprint,
            "explicit_names": list(self.explicit_names),
            "structrr_context_fingerprint": self.structrr_context_fingerprint,
            "lookup_revision": LOOKUP_REVISION,
        }
        if include_fingerprint:
            result["fingerprint"] = self.fingerprint
        return result


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    reasons: tuple[str, ...]
    score: int


@dataclass(frozen=True, slots=True)
class Candidate:
    record: InventoryRecord
    evidence: CandidateEvidence

    def to_data(self) -> dict[str, Any]:
        return {
            "record": self.record.to_data(),
            "evidence": {
                "reasons": list(self.evidence.reasons),
                "score": self.evidence.score,
            },
        }


@dataclass(frozen=True, slots=True)
class CandidateSet:
    query_fingerprint: str
    inventory_fingerprint: str
    candidates: tuple[Candidate, ...]

    def to_data(self) -> dict[str, Any]:
        return {
            "query_fingerprint": self.query_fingerprint,
            "inventory_fingerprint": self.inventory_fingerprint,
            "candidates": [candidate.to_data() for candidate in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class PopulationReceipt:
    population_ref: str
    inventory_fingerprint: str
    membership_rule_ref: str
    members: tuple[str, ...]
    complete: bool

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": POPULATION_RECEIPT_SCHEMA_VERSION,
            "population_ref": self.population_ref,
            "inventory_fingerprint": self.inventory_fingerprint,
            "membership_rule_ref": self.membership_rule_ref,
            "members": list(self.members),
            "complete": self.complete,
        }


class InventoryAdapter(Protocol):
    revision: str

    def collect(self, repo_root: Path) -> Iterable[InventoryRecord]: ...


class PythonInventoryAdapter:
    revision = "python-ast-v1"

    def collect(self, repo_root: Path) -> Iterable[InventoryRecord]:
        for path in sorted(repo_root.rglob("*.py")):
            if any(part in {".git", ".venv", "__pycache__"} for part in path.parts):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                continue
            relative = path.relative_to(repo_root).as_posix()
            module = (
                relative.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
            )
            for node in ast.walk(tree):
                if not isinstance(
                    node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    continue
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                qualified = f"{module}.{node.name}"
                yield InventoryRecord(
                    inventory_id=f"python:{relative}::{node.name}",
                    kind=kind,
                    canonical_name=node.name,
                    qualified_name=qualified,
                    normalized_terms=normalize_terms(node.name),
                    aliases=(),
                    path=relative,
                    span=(node.lineno, getattr(node, "end_lineno", node.lineno)),
                    language="python",
                )


def build_inventory(
    repo_root: Path,
    *,
    commit_ref: str,
    structrr_revision: str,
    adapters: Sequence[InventoryAdapter] = (PythonInventoryAdapter(),),
) -> RepositoryInventory:
    records = tuple(
        sorted(
            (record for adapter in adapters for record in adapter.collect(repo_root)),
            key=lambda item: item.inventory_id,
        )
    )
    return RepositoryInventory(
        commit_ref,
        structrr_revision,
        tuple(adapter.revision for adapter in adapters),
        records,
    )


def retrieve_candidates(
    query: LookupQuery,
    inventory: RepositoryInventory,
    context: StructrrLookupContext | None = None,
) -> CandidateSet:
    context = context or StructrrLookupContext()
    terms = set(query.normalized_terms)
    alias_terms = {
        normalize_terms(alias)
        for alias in context.aliases.get(query.subject_text.casefold(), ())
    }
    found: dict[str, CandidateEvidence] = {}
    for record in inventory.records:
        record_terms = set(record.normalized_terms)
        reasons: list[tuple[str, int]] = []
        if record.canonical_name.casefold() == query.subject_text.casefold():
            reasons.append(("exact_canonical_name", 100))
        if any(
            alias.casefold() == query.subject_text.casefold()
            for alias in record.aliases
        ):
            reasons.append(("exact_accepted_alias", 95))
        if frozenset(record_terms) == frozenset(terms) and terms:
            reasons.append(("exact_normalized_token_set", 85))
        if alias_terms.intersection({frozenset(record_terms)}):
            reasons.append(("structrr_alias", 95))
        overlap = len(terms.intersection(record_terms))
        if overlap:
            reasons.append(("lexical_overlap", min(overlap * 5, 20)))
        if not reasons:
            continue
        score = max(points for _, points in reasons)
        found[record.inventory_id] = CandidateEvidence(
            tuple(sorted(reason for reason, _ in reasons)), score
        )
    candidates = tuple(
        Candidate(inventory.require(record_id), evidence)
        for record_id, evidence in sorted(
            found.items(), key=lambda item: (-item[1].score, item[0])
        )
    )
    if len(candidates) > 64:
        raise InventoryError("candidate_overflow")
    return CandidateSet(query.fingerprint, inventory.fingerprint, candidates)


def aggregate_candidate_relations(
    candidate_set: CandidateSet,
    decisions: Mapping[str, str],
    *,
    quantifier: str,
    population_refs: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if set(decisions) != {
        candidate.record.inventory_id for candidate in candidate_set.candidates
    }:
        raise InventoryError("candidate relation decision coverage is incomplete")
    matches = [
        candidate
        for candidate in candidate_set.candidates
        if decisions[candidate.record.inventory_id] == "matches"
    ]
    uncertain = [
        candidate
        for candidate in candidate_set.candidates
        if decisions[candidate.record.inventory_id] == "insufficient_evidence"
    ]
    if len(matches) == 1:
        return {"status": "bound", "binding_ref": matches[0].record.inventory_id}
    if not matches and uncertain:
        return {"status": "unresolved", "reason_code": "repository_evidence_missing"}
    if not matches:
        return {"status": "unresolved", "reason_code": "no_candidate"}
    population_refs = population_refs or {}
    common = {
        population_refs.get(candidate.record.inventory_id) for candidate in matches
    }
    common.discard(None)
    if len(common) == 1:
        population_ref = next(iter(common))
        if quantifier == "every":
            return {"status": "bound", "binding_ref": population_ref}
    return {"status": "unresolved", "reason_code": "multiple_candidates"}


def enumerate_population(
    inventory: RepositoryInventory,
    *,
    population_ref: str,
    membership_rule_ref: str,
    member_ids: Sequence[str],
    complete: bool,
) -> PopulationReceipt:
    known = {record.inventory_id for record in inventory.records}
    members = tuple(sorted(set(member_ids)))
    if not set(members).issubset(known):
        raise InventoryError("population receipt contains an unknown member")
    return PopulationReceipt(
        population_ref, inventory.fingerprint, membership_rule_ref, members, complete
    )


def _fingerprint(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _string(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InventoryError(f"{key} must be a non-empty string")
    return value


def _strings(raw: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise InventoryError(f"{key} must be a string array")
    return tuple(value)


__all__ = [
    "Candidate",
    "CandidateSet",
    "InventoryError",
    "InventoryRecord",
    "LookupQuery",
    "PopulationReceipt",
    "PythonInventoryAdapter",
    "RepositoryInventory",
    "StructrrLookupContext",
    "aggregate_candidate_relations",
    "build_inventory",
    "enumerate_population",
    "normalize_terms",
    "retrieve_candidates",
]
