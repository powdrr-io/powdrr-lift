"""Derived, explainable contract for one execution prompt boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from powdrr_lift.core.intent import IntentClause, IntentSource, IntentStore


@dataclass(frozen=True, slots=True)
class EffectiveContract:
    """Ephemeral projection; durable records retain only its IDs and fingerprint."""

    fingerprint: str
    clauses: tuple[IntentClause, ...]
    sources: tuple[IntentSource, ...]
    context: dict[str, str]
    conflicts: tuple[str, ...] = ()

    @property
    def clause_ids(self) -> tuple[str, ...]:
        return tuple(item.clause_id for item in self.clauses)

    @property
    def rule_ids(self) -> tuple[str, ...]:
        return self.clause_ids

    def to_data(self, *, include_source_text: bool = True) -> dict[str, Any]:
        sources = {item.intent_id: item for item in self.sources}
        clauses: list[dict[str, Any]] = []
        for clause in self.clauses:
            item: dict[str, Any] = {
                "clause_id": clause.clause_id,
                "intent_id": clause.intent_id,
                "kind": clause.kind.value,
                "version": clause.version,
                "selectors": clause.contract.selectors,
                "trigger": clause.contract.trigger.value,
                "trigger_action": clause.contract.trigger_action,
                "requirements": list(clause.contract.requirements),
                "completion_gate": clause.contract.completion_gate,
            }
            if include_source_text:
                source = sources.get(clause.intent_id)
                if source is not None:
                    item["source_ref"] = source.source_ref
                    item["text"] = source.exact_text
            clauses.append(item)
        return {
            "contract_fingerprint": self.fingerprint,
            "clause_ids": list(self.clause_ids),
            "clauses": clauses,
            "conflicts": list(self.conflicts),
        }

    def explain(self) -> dict[str, Any]:
        return {
            "context": dict(self.context),
            "contract": self.to_data(),
            "resolution": "exact selector match; semantic similarity is not used",
        }


def resolve_effective_contract(
    store: IntentStore, context: Mapping[str, str]
) -> EffectiveContract:
    clauses = store.index().resolve(context)
    sources_by_id = {item.intent_id: item for item in store.sources()}
    sources = tuple(
        sources_by_id[item.intent_id]
        for item in clauses
        if item.intent_id in sources_by_id
    )
    identity = [
        {"clause_id": item.clause_id, "version": item.version} for item in clauses
    ]
    fingerprint = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode("utf-8")
        ).hexdigest()
    )
    conflicts: list[str] = []
    for index, left in enumerate(clauses):
        for right in clauses[index + 1 :]:
            if (
                left.contract.precedence == right.contract.precedence
                and left.kind is not right.kind
                and left.contract.selectors == right.contract.selectors
            ):
                conflicts.append(f"{left.clause_id}:{right.clause_id}")
    return EffectiveContract(
        fingerprint, clauses, sources, dict(context), tuple(sorted(conflicts))
    )
