"""Versioned, explainable user operating rules."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BEHAVIOR_RULE_SCHEMA_VERSION = "behavior-rule-v1"


@dataclass(frozen=True, slots=True)
class BehaviorRule:
    rule_id: str
    text: str
    normalized_text: str
    source_ref: str
    scope: dict[str, str]
    precedence: int = 0
    version: int = 1
    active: bool = True
    expires_at: str | None = None
    supersedes_rule_id: str | None = None
    schema_version: str = BEHAVIOR_RULE_SCHEMA_VERSION

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "rule_id": self.rule_id,
            "text": self.text,
            "normalized_text": self.normalized_text,
            "source_ref": self.source_ref,
            "scope": self.scope,
            "precedence": self.precedence,
            "version": self.version,
            "active": self.active,
            "expires_at": self.expires_at,
            "supersedes_rule_id": self.supersedes_rule_id,
        }

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> BehaviorRule:
        if data.get("schema_version") != BEHAVIOR_RULE_SCHEMA_VERSION:
            raise ValueError("unsupported behavior rule schema version")
        return cls(
            data["rule_id"],
            data["text"],
            data["normalized_text"],
            data["source_ref"],
            dict(data["scope"]),
            data.get("precedence", 0),
            data.get("version", 1),
            data.get("active", True),
            data.get("expires_at"),
            data.get("supersedes_rule_id"),
        )

    def is_applicable(
        self, context: dict[str, str], *, now: datetime | None = None
    ) -> bool:
        if not self.active or any(
            context.get(key) != value for key, value in self.scope.items()
        ):
            return False
        if self.expires_at is None:
            return True
        return (now or datetime.now(UTC)) < datetime.fromisoformat(self.expires_at)


def normalize_behavior_text(text: str) -> str:
    return " ".join(text.strip().casefold().split())


def nominate_behavior_rule(
    text: str, *, rule_id: str, source_ref: str, scope: dict[str, str]
) -> BehaviorRule:
    if not text.strip() or not source_ref or not scope:
        raise ValueError("A behavior rule requires text, source_ref, and scope.")
    return BehaviorRule(
        rule_id, text.strip(), normalize_behavior_text(text), source_ref, dict(scope)
    )


def applicable_behavior_rules(
    rules: tuple[BehaviorRule, ...], context: dict[str, str]
) -> tuple[BehaviorRule, ...]:
    return tuple(
        sorted(
            (rule for rule in rules if rule.is_applicable(context)),
            key=lambda rule: (-rule.precedence, rule.rule_id),
        )
    )


class FileBehaviorRuleStore:
    def __init__(self, workflow_directory: str | Path) -> None:
        self.path = Path(workflow_directory) / "guidance" / "behavior-rules.json"

    def list(self, *, include_inactive: bool = False) -> tuple[BehaviorRule, ...]:
        if not self.path.exists():
            return ()
        rules = tuple(
            BehaviorRule.from_data(item)
            for item in json.loads(self.path.read_text(encoding="utf-8"))
        )
        return (
            rules if include_inactive else tuple(rule for rule in rules if rule.active)
        )

    def save(
        self, rule: BehaviorRule, *, expected_version: int | None = None
    ) -> BehaviorRule:
        rules = list(self.list(include_inactive=True))
        current = next((item for item in rules if item.rule_id == rule.rule_id), None)
        if current is not None and expected_version != current.version:
            raise ValueError(f"stale behavior rule version for {rule.rule_id!r}")
        if current is None and expected_version not in (None, 0):
            raise ValueError(f"unexpected behavior rule version for {rule.rule_id!r}")
        saved = BehaviorRule(
            rule.rule_id,
            rule.text,
            rule.normalized_text,
            rule.source_ref,
            rule.scope,
            rule.precedence,
            (current.version + 1 if current else 1),
            rule.active,
            rule.expires_at,
            rule.supersedes_rule_id,
        )
        rules = [saved if item.rule_id == rule.rule_id else item for item in rules]
        if current is None:
            rules.append(saved)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([item.to_data() for item in rules], indent=2) + "\n",
            encoding="utf-8",
        )
        return saved

    def revoke(self, rule_id: str, *, expected_version: int) -> BehaviorRule:
        current = next(
            (
                item
                for item in self.list(include_inactive=True)
                if item.rule_id == rule_id
            ),
            None,
        )
        if current is None:
            raise KeyError(rule_id)
        if current.version != expected_version:
            raise ValueError(f"stale behavior rule version for {rule_id!r}")
        return self.save(
            BehaviorRule(
                rule_id,
                current.text,
                current.normalized_text,
                current.source_ref,
                current.scope,
                current.precedence,
                active=False,
                expires_at=current.expires_at,
                supersedes_rule_id=current.supersedes_rule_id,
            ),
            expected_version=expected_version,
        )
