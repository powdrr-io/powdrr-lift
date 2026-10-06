"""Typed, source-backed contracts that relate instruction requirements."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

BEHAVIORAL_CONTRACT_SCHEMA_VERSION = "behavioral-contract-v1"
RELATIONSHIP_KINDS = frozenset(
    {
        "defines_interface",
        "constrains_output",
        "applies_when",
        "exception_to",
        "ordered_with",
        "required_across_paths",
        "allowed_alternative",
    }
)


class AcceptanceContractError(ValueError):
    """Raised when a behavioral contract loses source scope or provenance."""


@dataclass(frozen=True, slots=True)
class ContractRelationship:
    kind: str
    source_evidence: str
    target_requirement_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.kind not in RELATIONSHIP_KINDS:
            raise AcceptanceContractError("contract relationship kind is invalid")
        if not self.source_evidence.strip():
            raise AcceptanceContractError("relationship evidence is empty")
        if not self.target_requirement_ids or any(
            not item.strip() for item in self.target_requirement_ids
        ):
            raise AcceptanceContractError("relationship targets are invalid")
        if len(set(self.target_requirement_ids)) != len(self.target_requirement_ids):
            raise AcceptanceContractError("relationship targets contain duplicates")

    def to_data(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source_evidence": self.source_evidence,
            "target_requirement_ids": list(self.target_requirement_ids),
        }


@dataclass(frozen=True, slots=True)
class BehavioralContract:
    contract_id: str
    operation_description: str
    member_requirement_ids: tuple[str, ...]
    supporting_context_ids: tuple[str, ...]
    role_interpretations: Mapping[str, Mapping[str, Any]]
    relationships: tuple[ContractRelationship, ...]
    unresolved_questions: tuple[str, ...]
    shared_constraints: tuple[str, ...] = ()
    schema_version: str = BEHAVIORAL_CONTRACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != BEHAVIORAL_CONTRACT_SCHEMA_VERSION:
            raise AcceptanceContractError("unsupported behavioral contract schema")
        if not self.contract_id.strip() or not self.operation_description.strip():
            raise AcceptanceContractError("contract identity or operation is empty")
        if not self.member_requirement_ids or any(
            not item.strip() for item in self.member_requirement_ids
        ):
            raise AcceptanceContractError("behavioral contract has no members")
        if len(set(self.member_requirement_ids)) != len(self.member_requirement_ids):
            raise AcceptanceContractError("behavioral contract members are duplicated")
        if set(self.member_requirement_ids).intersection(self.supporting_context_ids):
            raise AcceptanceContractError("context cannot be a contract member")
        if len(set(self.supporting_context_ids)) != len(self.supporting_context_ids):
            raise AcceptanceContractError("supporting context IDs are duplicated")
        if set(self.role_interpretations) != set(self.member_requirement_ids):
            raise AcceptanceContractError(
                "contract role interpretations are incomplete"
            )
        member_set = set(self.member_requirement_ids)
        for edge in self.relationships:
            if not set(edge.target_requirement_ids).issubset(member_set):
                raise AcceptanceContractError(
                    "relationship targets must be contract members"
                )
        if any(not item.strip() for item in self.unresolved_questions):
            raise AcceptanceContractError("contract unresolved question is empty")
        if any(not item.strip() for item in self.shared_constraints):
            raise AcceptanceContractError("contract shared constraint is empty")

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.to_data(include_fingerprint=False),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def to_data(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "contract_id": self.contract_id,
            "operation_description": self.operation_description,
            "member_requirement_ids": list(self.member_requirement_ids),
            "supporting_context_ids": list(self.supporting_context_ids),
            "role_interpretations": {
                key: dict(value) for key, value in self.role_interpretations.items()
            },
            "relationships": [item.to_data() for item in self.relationships],
            "unresolved_questions": list(self.unresolved_questions),
            "shared_constraints": list(self.shared_constraints),
        }
        if include_fingerprint:
            data["fingerprint"] = self.fingerprint
        return data

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> BehavioralContract:
        if set(raw) != {
            "schema_version",
            "contract_id",
            "operation_description",
            "member_requirement_ids",
            "supporting_context_ids",
            "role_interpretations",
            "relationships",
            "unresolved_questions",
            "shared_constraints",
            "fingerprint",
        }:
            raise AcceptanceContractError(
                "behavioral contract has unknown or missing fields"
            )
        members = raw.get("member_requirement_ids")
        contexts = raw.get("supporting_context_ids")
        roles = raw.get("role_interpretations")
        relationships = raw.get("relationships")
        questions = raw.get("unresolved_questions")
        constraints = raw.get("shared_constraints", [])
        if (
            not isinstance(members, list)
            or not all(isinstance(item, str) for item in members)
            or not isinstance(contexts, list)
            or not all(isinstance(item, str) for item in contexts)
            or not isinstance(roles, Mapping)
            or not all(isinstance(value, Mapping) for value in roles.values())
            or not isinstance(relationships, list)
            or not all(isinstance(item, Mapping) for item in relationships)
            or not isinstance(questions, list)
            or not all(isinstance(item, str) for item in questions)
            or not isinstance(constraints, list)
            or not all(isinstance(item, str) for item in constraints)
        ):
            raise AcceptanceContractError("behavioral contract fields are malformed")
        edges: list[ContractRelationship] = []
        for item in relationships:
            if set(item) != {"kind", "source_evidence", "target_requirement_ids"}:
                raise AcceptanceContractError(
                    "relationship has unknown or missing fields"
                )
            targets = item.get("target_requirement_ids")
            if not isinstance(targets, list) or not all(
                isinstance(value, str) for value in targets
            ):
                raise AcceptanceContractError("relationship targets are malformed")
            edges.append(
                ContractRelationship(
                    kind=_required_text(item, "kind"),
                    source_evidence=_required_text(item, "source_evidence"),
                    target_requirement_ids=tuple(targets),
                )
            )
        contract = cls(
            contract_id=_required_text(raw, "contract_id"),
            operation_description=_required_text(raw, "operation_description"),
            member_requirement_ids=tuple(members),
            supporting_context_ids=tuple(contexts),
            role_interpretations={
                str(key): dict(value) for key, value in roles.items()
            },
            relationships=tuple(edges),
            unresolved_questions=tuple(questions),
            shared_constraints=tuple(constraints),
            schema_version=_required_text(raw, "schema_version"),
        )
        if raw.get("fingerprint") != contract.fingerprint:
            raise AcceptanceContractError("behavioral contract fingerprint is stale")
        return contract


def validate_contracts(
    contracts: Sequence[BehavioralContract],
    *,
    requirement_ids: Sequence[str],
    context_ids: Sequence[str],
    source_text_by_id: Mapping[str, str],
) -> None:
    """Validate membership coverage, context separation, and quoted evidence."""
    required = set(requirement_ids)
    context = set(context_ids)
    if required.intersection(context):
        raise AcceptanceContractError("context source is included as a requirement")
    covered: set[str] = set()
    for contract in contracts:
        members = set(contract.member_requirement_ids)
        contexts = set(contract.supporting_context_ids)
        if not members.issubset(required):
            raise AcceptanceContractError(
                "contract member is not a product requirement"
            )
        if not contexts.issubset(context):
            raise AcceptanceContractError("contract context reference is not context")
        covered.update(members)
        for edge in contract.relationships:
            if not any(
                edge.source_evidence in source_text_by_id.get(ref, "")
                for ref in (
                    *contract.member_requirement_ids,
                    *contract.supporting_context_ids,
                )
            ):
                raise AcceptanceContractError(
                    "relationship evidence is absent from cited source clauses"
                )
    if covered != required:
        raise AcceptanceContractError("behavioral contracts omit product requirements")


def _required_text(raw: Mapping[str, Any], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value.strip():
        raise AcceptanceContractError(f"{name} must be a non-empty string")
    return value.strip()


__all__ = [
    "AcceptanceContractError",
    "BehavioralContract",
    "ContractRelationship",
    "RELATIONSHIP_KINDS",
    "validate_contracts",
]
