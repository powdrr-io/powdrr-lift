"""Runtime matching for durable behavior guidance."""

from __future__ import annotations

from powdrr_lift.core.behavior_rule import (
    BehaviorRule,
    FileBehaviorRuleStore,
    applicable_behavior_rules,
)


def load_applicable_guidance(
    store: FileBehaviorRuleStore, context: dict[str, str]
) -> tuple[BehaviorRule, ...]:
    """Return only active, scoped rules for the current execution context."""

    return applicable_behavior_rules(store.list(), context)
