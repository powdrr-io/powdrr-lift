"""Versioned observations and outcomes for one GitHub issue work item."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

SCHEMA = "https://powdrr.io/schemas/github-worker-work-item-state-v1"
ITEM_STATES = {"active", "awaiting_ci", "awaiting_human", "fulfilled", "cancelled"}
ATTEMPT_OUTCOMES = {"running", "succeeded", "failed", "interrupted"}
EVIDENCE_STATES = {"confirmed", "missing", "failed", "unknown"}


@dataclass(frozen=True, slots=True)
class WorkItemIdentity:
    repository: str
    issue_number: int
    plan_id: str
    item_id: str
    input_contract_digest: str

    def __post_init__(self) -> None:
        if (
            not self.repository.strip()
            or not self.plan_id.strip()
            or not self.item_id.strip()
        ):
            raise ValueError("work item identity fields must be non-empty")
        if type(self.issue_number) is not int or self.issue_number < 1:
            raise ValueError("issue_number must be a positive integer")
        if not self.input_contract_digest.startswith("sha256:"):
            raise ValueError("input_contract_digest must be a sha256 fingerprint")

    def to_data(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "issue_number": self.issue_number,
            "plan_id": self.plan_id,
            "item_id": self.item_id,
            "input_contract_digest": self.input_contract_digest,
        }


@dataclass(frozen=True, slots=True)
class WorkItemState:
    """Item outcome kept separately from the most recent attempt outcome."""

    identity: WorkItemIdentity
    item_state: str
    attempt_outcome: str
    attempt_id: str
    owner_generation: int
    publication_state: str
    ci_state: str
    evidence_state: str
    observed_head: str | None = None
    ci_head: str | None = None
    check_policy_digest: str | None = None
    pull_requests: tuple[Mapping[str, Any], ...] = ()
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SCHEMA:
            raise ValueError("unsupported work item state schema")
        if self.item_state not in ITEM_STATES:
            raise ValueError("unsupported item state")
        if self.attempt_outcome not in ATTEMPT_OUTCOMES:
            raise ValueError("unsupported attempt outcome")
        if not self.attempt_id.strip():
            raise ValueError("attempt_id must be non-empty")
        if type(self.owner_generation) is not int or self.owner_generation < 1:
            raise ValueError("owner_generation must be positive")
        if self.publication_state not in {"none", "pending", "published", "ambiguous"}:
            raise ValueError("unsupported publication state")
        if self.ci_state not in {
            "not_observed",
            "pending",
            "passed",
            "failed",
            "stale",
            "unknown",
        }:
            raise ValueError("unsupported CI state")
        if self.evidence_state not in EVIDENCE_STATES:
            raise ValueError("unsupported evidence state")
        if self.ci_state == "passed" and (
            not self.observed_head
            or self.ci_head != self.observed_head
            or not self.check_policy_digest
        ):
            raise ValueError("passed CI must bind the observed head and check policy")
        if self.ci_state == "stale" and self.ci_head == self.observed_head:
            raise ValueError("stale CI must name a different head")
        pr_numbers = [pr.get("number") for pr in self.pull_requests]
        if any(type(number) is not int or number < 1 for number in pr_numbers):
            raise ValueError("pull request records require positive numbers")
        if len(pr_numbers) > 1 and self.publication_state != "ambiguous":
            raise ValueError("multiple publication candidates must be marked ambiguous")
        if self.item_state == "fulfilled" and (
            self.evidence_state != "confirmed" or self.publication_state != "published"
        ):
            raise ValueError("fulfilled items require confirmed published evidence")
        if self.publication_state == "ambiguous" and self.item_state == "fulfilled":
            raise ValueError("ambiguous publication cannot fulfill an item")

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> WorkItemState:
        if raw.get("schema") != SCHEMA or not isinstance(raw.get("identity"), Mapping):
            raise ValueError("invalid work item state envelope")
        identity = WorkItemIdentity(**raw["identity"])
        prs = raw.get("pull_requests", [])
        if not isinstance(prs, list) or any(
            not isinstance(item, Mapping) for item in prs
        ):
            raise ValueError("pull_requests must be a list of records")
        return cls(
            identity=identity,
            item_state=raw["item_state"],
            attempt_outcome=raw["attempt_outcome"],
            attempt_id=raw["attempt_id"],
            owner_generation=raw["owner_generation"],
            publication_state=raw["publication_state"],
            ci_state=raw["ci_state"],
            evidence_state=raw["evidence_state"],
            observed_head=raw.get("observed_head"),
            ci_head=raw.get("ci_head"),
            check_policy_digest=raw.get("check_policy_digest"),
            pull_requests=tuple(dict(item) for item in prs),
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "identity": self.identity.to_data(),
            "item_state": self.item_state,
            "attempt_outcome": self.attempt_outcome,
            "attempt_id": self.attempt_id,
            "owner_generation": self.owner_generation,
            "publication_state": self.publication_state,
            "ci_state": self.ci_state,
            "evidence_state": self.evidence_state,
            "observed_head": self.observed_head,
            "ci_head": self.ci_head,
            "check_policy_digest": self.check_policy_digest,
            "pull_requests": [dict(item) for item in self.pull_requests],
        }
