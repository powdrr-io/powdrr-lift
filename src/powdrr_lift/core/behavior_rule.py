"""Versioned, explainable user operating rules."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
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

    def _list_unlocked(
        self, *, include_inactive: bool = False
    ) -> tuple[BehaviorRule, ...]:
        if not self.path.exists():
            return ()
        rules = tuple(
            BehaviorRule.from_data(item)
            for item in json.loads(self.path.read_text(encoding="utf-8"))
        )
        return (
            rules if include_inactive else tuple(rule for rule in rules if rule.active)
        )

    def _write_rules(self, rules: list[BehaviorRule]) -> None:
        payload = json.dumps([item.to_data() for item in rules], indent=2) + "\n"
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, delete=False
        ) as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, self.path)

    def list(self, *, include_inactive: bool = False) -> tuple[BehaviorRule, ...]:
        with self._locked():
            return self._list_unlocked(include_inactive=include_inactive)

    def explain(self, rule_id: str) -> dict[str, Any]:
        """Return the durable rule and its replacement lineage for inspection."""
        with self._locked():
            rules = self._list_unlocked(include_inactive=True)
        current = next((item for item in rules if item.rule_id == rule_id), None)
        if current is None:
            raise KeyError(rule_id)
        superseded_by = tuple(
            item.rule_id for item in rules if item.supersedes_rule_id == rule_id
        )
        return {
            "rule": current.to_data(),
            "superseded_by": superseded_by,
            "applicable": current.active,
        }

    def save(
        self, rule: BehaviorRule, *, expected_version: int | None = None
    ) -> BehaviorRule:
        with self._locked():
            rules = list(self._list_unlocked(include_inactive=True))
            current = next(
                (item for item in rules if item.rule_id == rule.rule_id), None
            )
            if current is not None and expected_version != current.version:
                raise ValueError(f"stale behavior rule version for {rule.rule_id!r}")
            if current is None and expected_version not in (None, 0):
                raise ValueError(
                    f"unexpected behavior rule version for {rule.rule_id!r}"
                )
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
            self._write_rules(rules)
            return saved

    def supersede(
        self,
        rule_id: str,
        replacement: BehaviorRule,
        *,
        expected_version: int,
    ) -> BehaviorRule:
        """Atomically deactivate a rule and install its replacement."""
        with self._locked():
            rules = list(self._list_unlocked(include_inactive=True))
            current = next((item for item in rules if item.rule_id == rule_id), None)
            if current is None:
                raise KeyError(rule_id)
            if current.version != expected_version:
                raise ValueError(f"stale behavior rule version for {rule_id!r}")
            if replacement.rule_id == rule_id:
                raise ValueError("a behavior rule cannot supersede itself")
            if any(item.rule_id == replacement.rule_id for item in rules):
                raise ValueError(
                    f"behavior rule already exists: {replacement.rule_id!r}"
                )
            retired = BehaviorRule(
                current.rule_id,
                current.text,
                current.normalized_text,
                current.source_ref,
                current.scope,
                current.precedence,
                current.version + 1,
                active=False,
                expires_at=current.expires_at,
                supersedes_rule_id=current.supersedes_rule_id,
            )
            installed = BehaviorRule(
                replacement.rule_id,
                replacement.text,
                replacement.normalized_text,
                replacement.source_ref,
                replacement.scope,
                replacement.precedence,
                1,
                active=True,
                expires_at=replacement.expires_at,
                supersedes_rule_id=rule_id,
            )
            self._write_rules(
                [retired if item.rule_id == rule_id else item for item in rules]
                + [installed]
            )
            return installed

    def revoke(self, rule_id: str, *, expected_version: int) -> BehaviorRule:
        with self._locked():
            current = next(
                (
                    item
                    for item in self._list_unlocked(include_inactive=True)
                    if item.rule_id == rule_id
                ),
                None,
            )
            if current is None:
                raise KeyError(rule_id)
            if current.version != expected_version:
                raise ValueError(f"stale behavior rule version for {rule_id!r}")
            rules = [
                BehaviorRule(
                    rule_id,
                    current.text,
                    current.normalized_text,
                    current.source_ref,
                    current.scope,
                    current.precedence,
                    current.version + 1,
                    active=False,
                    expires_at=current.expires_at,
                    supersedes_rule_id=current.supersedes_rule_id,
                )
                if item.rule_id == rule_id
                else item
                for item in self._list_unlocked(include_inactive=True)
            ]
            payload = json.dumps([item.to_data() for item in rules], indent=2) + "\n"
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as temporary:
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, self.path)
            return next(item for item in rules if item.rule_id == rule_id)
