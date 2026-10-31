"""Declarative follow-up relationships between semantic actions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ActionFact:
    action: str
    attributes: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ActionRelationship:
    relationship_id: str
    source_action: str
    follow_up_action: str
    obligation_description: str
    required_attributes: frozenset[str] = frozenset()
    excluded_attributes: frozenset[str] = frozenset()
    priority: int = 0

    def matches(self, fact: ActionFact) -> bool:
        return (
            fact.action == self.source_action
            and self.required_attributes <= fact.attributes
            and not self.excluded_attributes.intersection(fact.attributes)
        )


@dataclass(frozen=True, slots=True)
class RelationshipObligation:
    obligation_id: str
    relationship_id: str
    required_action: str
    description: str
    source_action: str
    target_ref: str | None = None


BUILTIN_ACTION_RELATIONSHIPS: tuple[ActionRelationship, ...] = (
    ActionRelationship(
        "review-edit-requires-validation",
        "edit_for_review_comment",
        "run_validation",
        "Run successful validation after changing code for a review comment.",
    ),
    ActionRelationship(
        "review-edit-requires-thread-resolution",
        "edit_for_review_comment",
        "resolve_review_thread",
        "Resolve the exact review thread after the correction is validated.",
        required_attributes=frozenset({"validated"}),
    ),
    ActionRelationship(
        "mutable-row-requires-optimistic-lock",
        "change_mutable_row",
        "add_optimistic_lock",
        "Use optimistic locking for the mutable database row change.",
        excluded_attributes=frozenset({"optimistic_locking"}),
    ),
    ActionRelationship(
        "mutable-row-requires-concurrency-evidence",
        "change_mutable_row",
        "run_concurrency_test",
        "Produce concurrency evidence for the mutable database row change.",
        excluded_attributes=frozenset({"concurrency_evidence"}),
    ),
)


def expand_action_relationships(
    facts: tuple[ActionFact, ...],
    *,
    relationships: tuple[ActionRelationship, ...] = BUILTIN_ACTION_RELATIONSHIPS,
) -> tuple[RelationshipObligation, ...]:
    """Expand one bounded relationship pass with deterministic deduplication."""

    obligations: dict[str, RelationshipObligation] = {}
    for fact in facts:
        for relationship in relationships:
            if relationship.matches(fact):
                obligation_id = f"{relationship.relationship_id}:{fact.action}"
                obligations[obligation_id] = RelationshipObligation(
                    obligation_id,
                    relationship.relationship_id,
                    relationship.follow_up_action,
                    relationship.obligation_description,
                    fact.action,
                    next(
                        (
                            attribute
                            for attribute in sorted(fact.attributes)
                            if attribute.startswith(("thread:", "row:"))
                        ),
                        None,
                    ),
                )
    return tuple(sorted(obligations.values(), key=lambda item: item.obligation_id))


def validate_relationship_graph(
    relationships: tuple[ActionRelationship, ...],
) -> tuple[str, ...]:
    """Reject relationship cycles before they can create unbounded work."""

    edges = {item.source_action: item.follow_up_action for item in relationships}
    errors: list[str] = []
    for start in edges:
        seen: set[str] = set()
        current: str | None = start
        while current in edges:
            if current in seen:
                errors.append(f"relationship cycle contains {current!r}")
                break
            seen.add(current)
            current = edges[current]
    return tuple(sorted(set(errors)))
