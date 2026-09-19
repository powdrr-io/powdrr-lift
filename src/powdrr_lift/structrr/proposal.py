"""Deterministic proposal revisions compiled from Structrr plan data."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from powdrr_lift.structrr.rebase import snapshot_digest

PROPOSAL_REVISION_SCHEMA_VERSION = "proposal-revision-v1"


@dataclass(frozen=True, slots=True)
class ProposalOperation:
    operation_id: str
    section: str
    subject_id: str
    action: str
    content: Mapping[str, Any]

    def to_data(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "section": self.section,
            "subject_id": self.subject_id,
            "action": self.action,
            "content": _canonical(dict(self.content)),
        }


@dataclass(frozen=True, slots=True)
class ProposalRevision:
    proposal_id: str
    structrr_baseline_fingerprint: str
    operations: tuple[ProposalOperation, ...]
    acceptance_criteria: tuple[str, ...]
    must_preserve: tuple[str, ...]
    non_goals: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    source_refs: tuple[str, ...]
    schema_version: str = PROPOSAL_REVISION_SCHEMA_VERSION

    def to_data(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "structrr_baseline_fingerprint": self.structrr_baseline_fingerprint,
            "operations": [operation.to_data() for operation in self.operations],
            "acceptance_criteria": list(self.acceptance_criteria),
            "must_preserve": list(self.must_preserve),
            "non_goals": list(self.non_goals),
            "allowed_paths": list(self.allowed_paths),
            "source_refs": list(self.source_refs),
        }
        if include_fingerprint:
            data["fingerprint"] = self.fingerprint
        return data

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.to_data(include_fingerprint=False),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> ProposalRevision:
        """Decode and verify a persisted proposal revision artifact."""
        schema_version = raw.get("schema_version")
        if schema_version != PROPOSAL_REVISION_SCHEMA_VERSION:
            raise ValueError(
                f"proposal revision has unsupported schema version: {schema_version!r}"
            )
        operations_raw = raw.get("operations")
        if not isinstance(operations_raw, list):
            raise ValueError("proposal revision operations must be a list")
        operations = tuple(_operation_from_data(item) for item in operations_raw)
        revision = cls(
            proposal_id=_required_string(raw, "proposal_id"),
            structrr_baseline_fingerprint=_required_string(
                raw, "structrr_baseline_fingerprint"
            ),
            operations=operations,
            acceptance_criteria=_string_tuple(raw, "acceptance_criteria"),
            must_preserve=_string_tuple(raw, "must_preserve"),
            non_goals=_string_tuple(raw, "non_goals"),
            allowed_paths=_string_tuple(raw, "allowed_paths"),
            source_refs=_string_tuple(raw, "source_refs"),
            schema_version=schema_version,
        )
        if raw.get("fingerprint") != revision.fingerprint:
            raise ValueError("proposal revision fingerprint does not match its content")
        return revision


def compile_proposal_revision(
    proposal_id: str,
    baseline: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    acceptance_criteria: Sequence[str],
    must_preserve: Sequence[str],
    non_goals: Sequence[str],
    allowed_paths: Sequence[str],
    source_refs: Sequence[str],
) -> ProposalRevision:
    """Compile one canonical proposal identity from validated plan inputs."""
    operations: list[ProposalOperation] = []
    seen_ids: set[str] = set()
    for section, value in plan.items():
        if not isinstance(section, str) or not isinstance(value, list):
            continue
        for index, item in enumerate(value, start=1):
            if not isinstance(item, Mapping):
                continue
            raw_action = item.get("action")
            if raw_action not in {"added", "deleted", "removed"}:
                continue
            action = "remove" if raw_action in {"deleted", "removed"} else "add"
            raw_subject = item.get("id", f"item-{index}")
            subject_id = str(raw_subject)
            operation_id = f"{action}:{section}:{subject_id}"
            if operation_id in seen_ids:
                operation_id = f"{operation_id}:{index}"
            seen_ids.add(operation_id)
            operations.append(
                ProposalOperation(
                    operation_id=operation_id,
                    section=section,
                    subject_id=subject_id,
                    action=action,
                    content=dict(item),
                )
            )
    return ProposalRevision(
        proposal_id=proposal_id,
        structrr_baseline_fingerprint=snapshot_digest(baseline),
        operations=tuple(operations),
        acceptance_criteria=tuple(dict.fromkeys(acceptance_criteria)),
        must_preserve=tuple(dict.fromkeys(must_preserve)),
        non_goals=tuple(dict.fromkeys(non_goals)),
        allowed_paths=tuple(dict.fromkeys(allowed_paths)),
        source_refs=tuple(dict.fromkeys(source_refs)),
    )


def load_proposal_revision(path: Path) -> ProposalRevision:
    """Load one persisted proposal revision and verify its self-hash."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read proposal revision {path}: {error}") from error
    if not isinstance(raw, Mapping):
        raise ValueError(f"proposal revision {path} must contain a mapping")
    return ProposalRevision.from_data(raw)


def validate_proposal_revision(path: Path, expected: ProposalRevision) -> None:
    """Reject a persisted artifact that no longer represents current inputs."""
    actual = load_proposal_revision(path)
    if actual.fingerprint != expected.fingerprint:
        raise ValueError(
            "proposal revision is stale: "
            f"expected {expected.fingerprint}, found {actual.fingerprint}"
        )


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_canonical(item) for item in value]
    return value


def _operation_from_data(raw: Any) -> ProposalOperation:
    if not isinstance(raw, Mapping):
        raise ValueError("proposal revision operation must contain a mapping")
    content = raw.get("content")
    if not isinstance(content, Mapping):
        raise ValueError("proposal revision operation content must contain a mapping")
    return ProposalOperation(
        operation_id=_required_string(raw, "operation_id"),
        section=_required_string(raw, "section"),
        subject_id=_required_string(raw, "subject_id"),
        action=_required_string(raw, "action"),
        content=content,
    )


def _required_string(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"proposal revision field {key!r} must be a non-empty string")
    return value


def _string_tuple(raw: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"proposal revision field {key!r} must be a list of strings")
    return tuple(value)


__all__ = [
    "PROPOSAL_REVISION_SCHEMA_VERSION",
    "ProposalOperation",
    "ProposalRevision",
    "compile_proposal_revision",
    "load_proposal_revision",
    "validate_proposal_revision",
]
