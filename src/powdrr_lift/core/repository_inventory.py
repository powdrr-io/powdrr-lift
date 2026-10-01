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
    symbol_aliases: Mapping[str, tuple[str, ...]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "aliases", self.aliases or {})
        object.__setattr__(self, "populations", self.populations or {})
        object.__setattr__(self, "relationships", self.relationships or {})
        object.__setattr__(self, "symbol_aliases", self.symbol_aliases or {})

    @property
    def fingerprint(self) -> str:
        return _fingerprint(
            {
                "aliases": self.aliases,
                "populations": self.populations,
                "relationships": self.relationships,
                "symbol_aliases": self.symbol_aliases,
            }
        )


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

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> LookupQuery:
        if raw.get("schema_version") != LOOKUP_SCHEMA_VERSION:
            raise InventoryError("lookup query schema is invalid")
        query = cls(
            _string(raw, "source_ref"),
            _string(raw, "source_text"),
            _string(raw, "subject_text"),
            _string(raw, "disposition"),
            _string(raw, "behavior_family"),
            _string(raw, "inventory_fingerprint"),
            _strings(raw, "explicit_names"),
            _string(raw, "structrr_context_fingerprint"),
        )
        if raw.get("fingerprint") != query.fingerprint:
            raise InventoryError("lookup query fingerprint is stale")
        return query


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

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> Candidate:
        record = raw.get("record")
        evidence = raw.get("evidence")
        if not isinstance(record, Mapping) or not isinstance(evidence, Mapping):
            raise InventoryError("candidate is malformed")
        return cls(
            InventoryRecord.from_data(record),
            CandidateEvidence(
                _strings(evidence, "reasons"), int(evidence.get("score", 0))
            ),
        )


@dataclass(frozen=True, slots=True)
class CandidateSet:
    query_fingerprint: str
    inventory_fingerprint: str
    candidates: tuple[Candidate, ...]
    retrieval_status: str = "complete"

    def to_data(self) -> dict[str, Any]:
        return {
            "query_fingerprint": self.query_fingerprint,
            "inventory_fingerprint": self.inventory_fingerprint,
            "candidates": [candidate.to_data() for candidate in self.candidates],
            "retrieval_status": self.retrieval_status,
        }

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> CandidateSet:
        candidates = raw.get("candidates")
        if not isinstance(candidates, list):
            raise InventoryError("candidate set is malformed")
        return cls(
            _string(raw, "query_fingerprint"),
            _string(raw, "inventory_fingerprint"),
            tuple(
                Candidate.from_data(item)
                for item in candidates
                if isinstance(item, Mapping)
            ),
            str(raw.get("retrieval_status", "complete")),
        )


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
            yield InventoryRecord(
                inventory_id=f"python:{relative}::{module}",
                kind="module",
                canonical_name=module.rsplit(".", 1)[-1],
                qualified_name=module,
                normalized_terms=normalize_terms(module.rsplit(".", 1)[-1]),
                aliases=(),
                path=relative,
                span=(1, max(1, len(path.read_text(encoding="utf-8").splitlines()))),
                language="python",
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


def build_python_lookup_context(
    repository_root: Path, inventory: RepositoryInventory
) -> StructrrLookupContext:
    """Derive bounded import, containment, and test links from Python source."""
    root = repository_root.resolve()
    records_by_qualified_name = {
        record.qualified_name: record for record in inventory.records
    }
    modules_by_path = {
        record.path: record
        for record in inventory.records
        if record.kind == "module" and record.language == "python"
    }
    import_names: dict[str, list[InventoryRecord]] = {}
    for record in modules_by_path.values():
        path = Path(record.path)
        module_path = (
            ".".join(path.with_suffix("").parts[:-1])
            if path.name == "__init__.py"
            else ".".join(path.with_suffix("").parts)
        )
        qualified_parts = record.qualified_name.split(".")
        options = {record.qualified_name, module_path}
        if path.parts and path.parts[0] in {"src", "lib"}:
            options.add(
                ".".join(path.with_suffix("").parts[1:]).removesuffix(".__init__")
            )
            options.add(".".join(path.with_suffix("").parts[1:]))
        if qualified_parts and qualified_parts[0] in {"src", "lib"}:
            options.add(".".join(qualified_parts[1:]))
        for name in options:
            if name:
                import_names.setdefault(name, []).append(record)

    relationships: dict[str, set[str]] = {}
    aliases: dict[str, set[str]] = {}
    for name, records in import_names.items():
        aliases.setdefault(name.casefold(), set()).update(
            record.qualified_name for record in records
        )

    def add_relationship(source: str, relation: str, target: str) -> None:
        relationships.setdefault(source.casefold(), set()).add(f"{relation}|{target}")

    for record in inventory.records:
        parent_name = record.qualified_name.rpartition(".")[0]
        if parent_name in records_by_qualified_name:
            add_relationship(parent_name, "contains", record.qualified_name)
            add_relationship(record.qualified_name, "contained_by", parent_name)

    def resolve_module(name: str) -> InventoryRecord | None:
        candidates = import_names.get(name, ())
        return candidates[0] if len(candidates) == 1 else None

    def resolve_relative_module(
        source_path: Path, node: ast.ImportFrom
    ) -> InventoryRecord | None:
        package_path = source_path.parent
        for _ in range(node.level - 1):
            package_path = package_path.parent
        if node.module:
            package_path = package_path.joinpath(*node.module.split("."))
        if not package_path.is_relative_to(root):
            return None
        relative = package_path.relative_to(root).as_posix()
        candidates = (
            f"{relative}/__init__.py",
            f"{relative}.py",
        )
        for candidate in candidates:
            record = modules_by_path.get(candidate)
            if record is not None:
                return record
        return None

    for relative, source_record in modules_by_path.items():
        source_path = root / relative
        try:
            if not source_path.resolve(strict=True).is_relative_to(root):
                continue
        except (OSError, RuntimeError):
            continue
        try:
            tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError, UnicodeError):
            continue
        is_test = _is_python_test_path(relative)
        for node in ast.walk(tree):
            imports: list[tuple[str, str, InventoryRecord, bool]] = []
            if isinstance(node, ast.Import):
                for item in node.names:
                    target = resolve_module(item.name)
                    if target is not None:
                        local_name = item.asname or item.name.split(".", 1)[0]
                        alias_is_exact = item.asname is not None or "." not in item.name
                        imports.append((local_name, item.name, target, alias_is_exact))
            elif isinstance(node, ast.ImportFrom):
                target_module = (
                    resolve_relative_module(source_path, node)
                    if node.level
                    else resolve_module(node.module or "")
                )
                for item in node.names:
                    if item.name == "*":
                        continue
                    target = None
                    alias_is_exact = False
                    if target_module is not None:
                        target = records_by_qualified_name.get(
                            f"{target_module.qualified_name}.{item.name}"
                        )
                        alias_is_exact = target is not None
                        if target is None:
                            target = resolve_module(
                                f"{target_module.qualified_name}.{item.name}"
                            )
                            alias_is_exact = target is not None
                    if target is None:
                        target = target_module
                    if target is not None:
                        local_name = item.asname or item.name
                        imports.append((local_name, item.name, target, alias_is_exact))
            for local_name, imported_name, target, alias_is_exact in imports:
                add_relationship(
                    source_record.qualified_name,
                    "imports",
                    target.qualified_name,
                )
                if not is_test:
                    add_relationship(
                        target.qualified_name,
                        "imported_by",
                        source_record.qualified_name,
                    )
                if alias_is_exact:
                    aliases.setdefault(local_name.casefold(), set()).add(
                        target.qualified_name
                    )
                    aliases.setdefault(imported_name.casefold(), set()).add(
                        target.qualified_name
                    )
                if is_test and alias_is_exact:
                    add_relationship(
                        target.qualified_name,
                        "referenced_by_test",
                        source_record.qualified_name,
                    )

    return StructrrLookupContext(
        relationships={
            key: tuple(sorted(values)) for key, values in relationships.items()
        },
        symbol_aliases={key: tuple(sorted(values)) for key, values in aliases.items()},
    )


def _is_python_test_path(path: str) -> bool:
    parts = Path(path).parts
    return any(part in {"tests", "test", "hardening_tests"} for part in parts) or (
        Path(path).name.startswith("test_") or Path(path).stem.endswith("_test")
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


def inventory_from_source_subjects(
    source_subjects: Sequence[Mapping[str, Any]],
    *,
    commit_ref: str,
    structrr_revision: str,
) -> RepositoryInventory:
    """Adapt validated Structrr source subjects to semantic lookup records."""
    records: list[InventoryRecord] = []
    seen_ids: set[str] = set()
    for index, subject in enumerate(source_subjects):
        inventory_id = _string(subject, "id")
        qualified_name = _string(subject, "qualified_name")
        kind = _string(subject, "kind")
        language = _string(subject, "language")
        path = _string(subject, "path")
        span = subject.get("span")
        if not isinstance(span, Mapping):
            raise InventoryError(f"source subject {index} has no span")
        try:
            start_line = int(span["start_line"])
            end_line = int(span["end_line"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InventoryError(f"source subject {index} has an invalid span") from exc
        if inventory_id in seen_ids:
            raise InventoryError(f"duplicate source subject ID: {inventory_id}")
        seen_ids.add(inventory_id)
        canonical_name = qualified_name.rsplit(".", 1)[-1]
        file_entity_id = subject.get("file_entity_id")
        records.append(
            InventoryRecord(
                inventory_id=inventory_id,
                kind=kind,
                canonical_name=canonical_name,
                qualified_name=qualified_name,
                normalized_terms=normalize_terms(canonical_name),
                aliases=(),
                path=path,
                span=(start_line, end_line),
                language=language,
                component_refs=(file_entity_id,)
                if isinstance(file_entity_id, str) and file_entity_id
                else (),
                structrr_refs=(inventory_id,),
            )
        )
    if not records:
        raise InventoryError("Structrr source subject inventory is empty")
    return RepositoryInventory(
        commit_ref=commit_ref,
        structrr_revision=structrr_revision,
        adapter_revisions=("structrr-source-subject-adapter-v1",),
        records=tuple(sorted(records, key=lambda record: record.inventory_id)),
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
    explicit_names = {name.casefold() for name in query.explicit_names}
    symbol_alias_targets = {
        targets[0].casefold()
        for name in explicit_names
        if len(targets := context.symbol_aliases.get(name, ())) == 1
    }
    relationship_sources = explicit_names | symbol_alias_targets
    import_targets = {
        relationship.partition("|")[2].casefold()
        for name in relationship_sources
        for relationship in context.relationships.get(name, ())
        if relationship.startswith("imports|")
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
        if (
            record.canonical_name.casefold() in explicit_names
            or record.qualified_name.casefold() in explicit_names
            or any(
                record.qualified_name.casefold().endswith("." + name)
                for name in explicit_names
            )
        ):
            reasons.append(("explicit_source_identifier", 90))
        if record.qualified_name.casefold() in symbol_alias_targets:
            reasons.append(("resolved_import_alias", 88))
        if record.qualified_name.casefold() in import_targets:
            reasons.append(("repository_import_relationship", 80))
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
    if candidate_set.retrieval_status != "complete":
        return {"status": "unresolved", "reason_code": candidate_set.retrieval_status}
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
    if len(matches) == 1 and not uncertain:
        return {"status": "bound", "binding_ref": matches[0].record.inventory_id}
    if len(matches) == 1 and uncertain:
        return {"status": "unresolved", "reason_code": "repository_evidence_missing"}
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
    "build_python_lookup_context",
    "build_inventory",
    "enumerate_population",
    "inventory_from_source_subjects",
    "normalize_terms",
    "retrieve_candidates",
]
