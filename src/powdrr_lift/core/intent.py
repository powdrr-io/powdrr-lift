"""Canonical durable user intent and deterministic applicability lookup."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

INTENT_SCHEMA_VERSION = "intent-v1"


class IntentKind(StrEnum):
    DECISION = "decision"
    INVARIANT = "invariant"
    PROCEDURE = "procedure"
    GUIDANCE = "guidance"


class IntentTrigger(StrEnum):
    BEFORE_ACTION = "before_action"
    AFTER_ACTION = "after_action"
    ACTION_COMPLETED = "action_completed"
    PHASE_ENTRY = "phase_entry"


@dataclass(frozen=True, slots=True)
class IntentSource:
    """The one canonical owner of the user's exact wording and provenance."""

    intent_id: str
    exact_text: str
    source_ref: str
    supplied_by: str
    created_at: str
    content_fingerprint: str = ""
    schema_version: str = INTENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.intent_id.strip() or not self.exact_text.strip():
            raise ValueError("intent source requires a non-empty id and exact_text")
        fingerprint = _fingerprint(self.exact_text)
        if self.content_fingerprint and self.content_fingerprint != fingerprint:
            raise ValueError("intent source content_fingerprint does not match text")
        object.__setattr__(self, "content_fingerprint", fingerprint)

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "intent_id": self.intent_id,
            "exact_text": self.exact_text,
            "source_ref": self.source_ref,
            "supplied_by": self.supplied_by,
            "created_at": self.created_at,
            "content_fingerprint": self.content_fingerprint,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> IntentSource:
        _strict_keys(
            data,
            {
                "schema_version",
                "intent_id",
                "exact_text",
                "source_ref",
                "supplied_by",
                "created_at",
                "content_fingerprint",
            },
        )
        if data.get("schema_version") != INTENT_SCHEMA_VERSION:
            raise ValueError("unsupported intent source schema version")
        return cls(
            _string(data, "intent_id"),
            _string(data, "exact_text"),
            _string(data, "source_ref"),
            _string(data, "supplied_by"),
            _string(data, "created_at"),
            _string(data, "content_fingerprint"),
        )


@dataclass(frozen=True, slots=True)
class IntentClause:
    clause_id: str
    intent_id: str
    source_span: tuple[int, int]
    kind: IntentKind
    contract: IntentContract
    version: int = 1
    active: bool = True
    supersedes_clause_id: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "clause_id": self.clause_id,
            "intent_id": self.intent_id,
            "source_span": list(self.source_span),
            "kind": self.kind.value,
            "contract": self.contract.to_data(),
            "version": self.version,
            "active": self.active,
            "supersedes_clause_id": self.supersedes_clause_id,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> IntentClause:
        _strict_keys(
            data,
            {
                "clause_id",
                "intent_id",
                "source_span",
                "kind",
                "contract",
                "version",
                "active",
                "supersedes_clause_id",
            },
        )
        span = data.get("source_span")
        if (
            not isinstance(span, Sequence)
            or isinstance(span, (str, bytes))
            or len(span) != 2
            or not all(isinstance(item, int) and item >= 0 for item in span)
            or span[0] > span[1]
        ):
            raise ValueError("intent clause source_span must be [start, end]")
        contract = data.get("contract")
        if not isinstance(contract, Mapping):
            raise ValueError("intent clause contract must be an object")
        return cls(
            _string(data, "clause_id"),
            _string(data, "intent_id"),
            (int(span[0]), int(span[1])),
            IntentKind(_string(data, "kind")),
            IntentContract.from_data(contract),
            _positive_int(data, "version"),
            _bool(data, "active"),
            _optional_string(data.get("supersedes_clause_id")),
        )


@dataclass(frozen=True, slots=True)
class IntentContract:
    """Executable meaning referenced by a clause, without copied source text."""

    selectors: dict[str, tuple[str, ...]] = field(default_factory=dict)
    trigger: IntentTrigger = IntentTrigger.BEFORE_ACTION
    trigger_action: str | None = None
    requirements: tuple[str, ...] = ()
    completion_gate: str | None = None
    precedence: int = 0

    def __post_init__(self) -> None:
        normalized = {
            str(key): tuple(sorted({str(item) for item in values}))
            for key, values in self.selectors.items()
        }
        if any(not key or not values for key, values in normalized.items()):
            raise ValueError("intent selectors require non-empty keys and values")
        object.__setattr__(self, "selectors", normalized)

    def to_data(self) -> dict[str, Any]:
        return {
            "selectors": {
                key: list(values) for key, values in sorted(self.selectors.items())
            },
            "trigger": self.trigger.value,
            "trigger_action": self.trigger_action,
            "requirements": list(self.requirements),
            "completion_gate": self.completion_gate,
            "precedence": self.precedence,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> IntentContract:
        _strict_keys(
            data,
            {
                "selectors",
                "trigger",
                "trigger_action",
                "requirements",
                "completion_gate",
                "precedence",
            },
        )
        raw_selectors = data.get("selectors", {})
        if not isinstance(raw_selectors, Mapping):
            raise ValueError("intent selectors must be an object")
        selectors: dict[str, tuple[str, ...]] = {}
        for key, values in raw_selectors.items():
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                raise ValueError("intent selector values must be arrays")
            selectors[str(key)] = tuple(_string_value(value) for value in values)
        raw_requirements = data.get("requirements", ())
        if not isinstance(raw_requirements, Sequence) or isinstance(
            raw_requirements, (str, bytes)
        ):
            raise ValueError("intent requirements must be an array")
        return cls(
            selectors,
            IntentTrigger(_string_value(data.get("trigger", "before_action"))),
            _optional_string(data.get("trigger_action")),
            tuple(_string_value(item) for item in raw_requirements),
            _optional_string(data.get("completion_gate")),
            int(data.get("precedence", 0)),
        )


def intent_fingerprint(clause: IntentClause) -> str:
    return _fingerprint(json.dumps(clause.to_data(), sort_keys=True))


class IntentIndex:
    """Exact selector index; it never uses semantic similarity to activate rules."""

    def __init__(self, clauses: Sequence[IntentClause] = ()) -> None:
        self._clauses = tuple(clauses)

    def resolve(self, context: Mapping[str, str]) -> tuple[IntentClause, ...]:
        matches = [
            clause
            for clause in self._clauses
            if clause.active and _selectors_match(clause.contract.selectors, context)
        ]
        return tuple(
            sorted(
                matches, key=lambda item: (-item.contract.precedence, item.clause_id)
            )
        )

    @property
    def clauses(self) -> tuple[IntentClause, ...]:
        return self._clauses


class IntentStore:
    """Durable source/clauses store with idempotent source capture and CAS updates."""

    def __init__(self, directory: str | Path) -> None:
        self.path = Path(directory) / "guidance" / "intents.json"
        self.lock_path = self.path.with_suffix(".lock")

    @contextmanager
    def _locked(self) -> Generator[None, None, None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read(self) -> tuple[tuple[IntentSource, ...], tuple[IntentClause, ...]]:
        if not self.path.exists():
            return (), ()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if (
            not isinstance(data, Mapping)
            or data.get("schema_version") != INTENT_SCHEMA_VERSION
        ):
            raise ValueError("unsupported persisted intent schema")
        sources = tuple(
            IntentSource.from_data(item) for item in data.get("sources", ())
        )
        clauses = tuple(
            IntentClause.from_data(item) for item in data.get("clauses", ())
        )
        if {item.intent_id for item in clauses} - {item.intent_id for item in sources}:
            raise ValueError("intent clause references an unknown source")
        return sources, clauses

    def _write(
        self, sources: Sequence[IntentSource], clauses: Sequence[IntentClause]
    ) -> None:
        payload = {
            "schema_version": INTENT_SCHEMA_VERSION,
            "sources": [item.to_data() for item in sources],
            "clauses": [item.to_data() for item in clauses],
        }
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, delete=False
        ) as temporary:
            json.dump(payload, temporary, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, self.path)

    def list(self, *, include_inactive: bool = False) -> tuple[IntentClause, ...]:
        with self._locked():
            _, clauses = self._read()
        return (
            clauses
            if include_inactive
            else tuple(item for item in clauses if item.active)
        )

    def sources(self) -> tuple[IntentSource, ...]:
        with self._locked():
            sources, _ = self._read()
        return sources

    def capture(
        self, source: IntentSource, clauses: Sequence[IntentClause]
    ) -> tuple[IntentSource, tuple[IntentClause, ...], bool]:
        with self._locked():
            sources, current_clauses = self._read()
            existing = next(
                (item for item in sources if item.source_ref == source.source_ref), None
            )
            if existing is not None:
                if existing.content_fingerprint != source.content_fingerprint:
                    raise ValueError(
                        "source_ref already contains different intent text"
                    )
                return (
                    existing,
                    tuple(
                        item
                        for item in current_clauses
                        if item.intent_id == existing.intent_id
                    ),
                    False,
                )
            if any(item.intent_id != source.intent_id for item in clauses):
                raise ValueError("all clauses must reference their captured source")
            if any(item.intent_id == source.intent_id for item in sources):
                raise ValueError(f"intent id already exists: {source.intent_id!r}")
            clause_ids = {item.clause_id for item in current_clauses}
            if clause_ids.intersection(item.clause_id for item in clauses):
                raise ValueError("intent clause id already exists")
            self._write((*sources, source), (*current_clauses, *clauses))
            return source, tuple(clauses), True

    def index(self) -> IntentIndex:
        return IntentIndex(self.list())

    def migrate_legacy_behavior_rules(self) -> int:
        """Translate v1 text guidance once, preserving its original wording."""
        from powdrr_lift.core.behavior_rule import FileBehaviorRuleStore

        legacy = FileBehaviorRuleStore(self.path.parent.parent).list(
            include_inactive=True
        )
        if not legacy:
            return 0
        migrated = 0
        for rule in legacy:
            source = make_intent_source(
                intent_id=f"legacy:{rule.rule_id}",
                exact_text=rule.text,
                source_ref=rule.source_ref,
                supplied_by="legacy-migration",
            )
            clause = IntentClause(
                f"legacy:{rule.rule_id}:v{rule.version}",
                source.intent_id,
                (0, len(source.exact_text)),
                IntentKind.GUIDANCE,
                IntentContract(
                    selectors={key: (value,) for key, value in rule.scope.items()},
                    precedence=rule.precedence,
                ),
                version=rule.version,
                active=rule.active,
            )
            _, _, created = self.capture(source, (clause,))
            migrated += int(created)
        return migrated

    def update_clause(
        self, clause_id: str, replacement: IntentClause, *, expected_version: int
    ) -> IntentClause:
        with self._locked():
            sources, clauses = self._read()
            current = next(
                (item for item in clauses if item.clause_id == clause_id), None
            )
            if current is None:
                raise KeyError(clause_id)
            if current.version != expected_version:
                raise ValueError(f"stale intent clause version for {clause_id!r}")
            if replacement.clause_id == clause_id:
                raise ValueError("intent replacement must have a new clause id")
            if any(item.clause_id == replacement.clause_id for item in clauses):
                raise ValueError("replacement clause id already exists")
            retired = IntentClause(
                current.clause_id,
                current.intent_id,
                current.source_span,
                current.kind,
                current.contract,
                current.version + 1,
                False,
                current.supersedes_clause_id,
            )
            installed = IntentClause(
                replacement.clause_id,
                replacement.intent_id,
                replacement.source_span,
                replacement.kind,
                replacement.contract,
                1,
                True,
                clause_id,
            )
            self._write(
                sources,
                tuple(
                    retired if item.clause_id == clause_id else item for item in clauses
                )
                + (installed,),
            )
            return installed

    def revoke(self, clause_id: str, *, expected_version: int) -> IntentClause:
        with self._locked():
            sources, clauses = self._read()
            current = next(
                (item for item in clauses if item.clause_id == clause_id), None
            )
            if current is None:
                raise KeyError(clause_id)
            if current.version != expected_version:
                raise ValueError(f"stale intent clause version for {clause_id!r}")
            revoked = IntentClause(
                current.clause_id,
                current.intent_id,
                current.source_span,
                current.kind,
                current.contract,
                current.version + 1,
                False,
                current.supersedes_clause_id,
            )
            self._write(
                sources,
                tuple(
                    revoked if item.clause_id == clause_id else item for item in clauses
                ),
            )
            return revoked


def make_intent_source(
    *, intent_id: str, exact_text: str, source_ref: str, supplied_by: str
) -> IntentSource:
    return IntentSource(
        intent_id,
        exact_text,
        source_ref,
        supplied_by,
        datetime.now(UTC).isoformat(),
    )


def _selectors_match(
    selectors: Mapping[str, Sequence[str]], context: Mapping[str, str]
) -> bool:
    for key, expected_values in selectors.items():
        actual = context.get(key)
        if actual is None:
            return False
        if key in {"path", "file_path"}:
            if not any(
                actual == expected or actual.startswith(expected.rstrip("/") + "/")
                for expected in expected_values
            ):
                return False
        elif actual not in expected_values:
            return False
    return True


def _fingerprint(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _strict_keys(data: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ValueError("unknown intent fields: " + ", ".join(sorted(unknown)))


def _string(data: Mapping[str, Any], key: str) -> str:
    return _string_value(data.get(key))


def _string_value(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("intent values must be non-empty strings")
    return value


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return _string_value(value)


def _positive_int(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or value < 1:
        raise ValueError(f"intent {key} must be a positive integer")
    return value


def _bool(data: Mapping[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"intent {key} must be a boolean")
    return value
