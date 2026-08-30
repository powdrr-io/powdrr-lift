"""Action relationship expansion and obligation closure."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from powdrr_lift.core.action_relationship import (
    BUILTIN_ACTION_RELATIONSHIPS,
    ActionFact,
    ActionRelationship,
    RelationshipObligation,
    expand_action_relationships,
    validate_relationship_graph,
)
from powdrr_lift.core.execution_state import (
    ExecutionObligation,
    ExecutionState,
    ObligationStatus,
)


@dataclass(frozen=True, slots=True)
class RelationshipExpansion:
    obligations: tuple[Any, ...]
    errors: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors


def expand_obligations(
    facts: tuple[ActionFact, ...],
    *,
    relationships: tuple[ActionRelationship, ...] = BUILTIN_ACTION_RELATIONSHIPS,
) -> RelationshipExpansion:
    errors = validate_relationship_graph(relationships)
    if errors:
        return RelationshipExpansion((), errors)
    return RelationshipExpansion(
        expand_action_relationships(facts, relationships=relationships)
    )


def expand_execution_obligations(
    state: ExecutionState,
    *,
    action_instance_id: str,
    action: str,
    attributes: frozenset[str] = frozenset(),
    relationships: tuple[ActionRelationship, ...] = BUILTIN_ACTION_RELATIONSHIPS,
) -> RelationshipExpansion:
    """Expand an action into durable, instance-bound obligations."""
    _ = state
    expansion = expand_obligations(
        (ActionFact(action, attributes),), relationships=relationships
    )
    return RelationshipExpansion(
        tuple(
            ExecutionObligation(
                obligation_id=f"{action_instance_id}:{item.obligation_id}",
                description=item.description,
                source_action_instance_id=action_instance_id,
                required_action=item.required_action,
                relationship_id=item.relationship_id,
            )
            for item in expansion.obligations
        ),
        expansion.errors,
    )


def satisfy_execution_obligation(
    obligation: ExecutionObligation,
    *,
    action_instance_id: str,
    action: str,
) -> bool:
    """Close an obligation only with its exact source action instance."""
    return (
        obligation.source_action_instance_id == action_instance_id
        and obligation.required_action == action
    )


def unresolved_obligations(
    obligations: tuple[ExecutionObligation, ...],
) -> tuple[ExecutionObligation, ...]:
    return tuple(item for item in obligations if item.status is ObligationStatus.OPEN)


def action_can_complete(
    action: str, open_obligations: tuple[RelationshipObligation, ...]
) -> bool:
    """Require prerequisite follow-ups before dependent follow-ups."""

    if action != "resolve_review_thread":
        return True
    return not any(
        item.required_action == "run_validation" for item in open_obligations
    )


def satisfy_obligation(
    obligation: RelationshipObligation, completed_action: str
) -> bool:
    """Report whether a completed action exactly closes the obligation."""

    return completed_action == obligation.required_action


def explain_obligation(obligation: RelationshipObligation) -> str:
    return (
        f"{obligation.obligation_id}: action {obligation.required_action!r} "
        "is required "
        f"because of {obligation.source_action!r} ({obligation.relationship_id}). "
        f"{obligation.description}"
    )
