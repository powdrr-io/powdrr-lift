"""Typed, source-backed contracts that relate instruction requirements."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from powdrr_lift.core.behavior_contract import CriterionQuality

BEHAVIORAL_CONTRACT_SCHEMA_VERSION = "behavioral-contract-v1"
ACCEPTANCE_CRITERION_SCHEMA_VERSION = "acceptance-criterion-v1"
CRITERION_KINDS = frozenset(
    {
        "interface",
        "transformation",
        "state_transition",
        "invariant",
        "rejection",
        "compatibility",
    }
)
ASSERTION_RELATIONS = frozenset(
    {"equals", "contains", "absent", "raises", "satisfies", "unchanged"}
)
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


@dataclass(frozen=True, slots=True)
class CriterionAssertion:
    """One source-linked observation and expected relationship."""

    assertion_id: str
    observation: str
    relation: str
    expected: Any
    source_refs: tuple[str, ...]
    basis: str

    def __post_init__(self) -> None:
        if not self.assertion_id.strip() or not self.observation.strip():
            raise AcceptanceContractError("criterion assertion identity is empty")
        if self.relation not in ASSERTION_RELATIONS:
            raise AcceptanceContractError("criterion assertion relation is invalid")
        if self.expected is None:
            raise AcceptanceContractError("criterion assertion expected value is empty")
        if not self.source_refs or any(not item.strip() for item in self.source_refs):
            raise AcceptanceContractError("criterion assertion source refs are empty")
        if len(set(self.source_refs)) != len(self.source_refs):
            raise AcceptanceContractError(
                "criterion assertion source refs are duplicated"
            )
        if self.basis not in {"source_derived", "repository_supported"}:
            raise AcceptanceContractError("criterion assertion basis is invalid")
        if self.relation == "satisfies" and (
            not isinstance(self.expected, str)
            or not self.expected.strip()
            or _is_vague_predicate(self.expected)
        ):
            raise AcceptanceContractError(
                "satisfies assertion requires a concrete predicate"
            )

    def to_data(self) -> dict[str, Any]:
        return {
            "assertion_id": self.assertion_id,
            "observation": self.observation,
            "relation": self.relation,
            "expected": self.expected,
            "source_refs": list(self.source_refs),
            "basis": self.basis,
        }


@dataclass(frozen=True, slots=True)
class AcceptanceCriterion:
    """Typed observable behavior description; it is not executable test code."""

    criterion_id: str
    contract_id: str
    kind: str
    source_refs: tuple[str, ...]
    setup: Any
    operation: str
    events: tuple[Any, ...]
    assertions: tuple[CriterionAssertion, ...]
    unresolved_questions: tuple[str, ...]
    quality: CriterionQuality
    fingerprint: str = ""
    schema_version: str = ACCEPTANCE_CRITERION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ACCEPTANCE_CRITERION_SCHEMA_VERSION:
            raise AcceptanceContractError("unsupported acceptance criterion schema")
        if not self.criterion_id.strip() or not self.contract_id.strip():
            raise AcceptanceContractError("criterion identity is empty")
        if self.kind not in CRITERION_KINDS:
            raise AcceptanceContractError("criterion kind is invalid")
        if not self.source_refs or any(not item.strip() for item in self.source_refs):
            raise AcceptanceContractError("criterion source refs are empty")
        if len(set(self.source_refs)) != len(self.source_refs):
            raise AcceptanceContractError("criterion source refs are duplicated")
        if not isinstance(self.operation, str) or not self.operation.strip():
            raise AcceptanceContractError("criterion operation is empty")
        if self.quality.requirement_status != "preserved" or (
            self.quality.criterion_status == "not_applicable"
        ):
            raise AcceptanceContractError("acceptance criterion quality is invalid")
        if not self.assertions or len(self.assertions) > 4:
            raise AcceptanceContractError("criterion needs one to four assertions")
        assertion_ids = [item.assertion_id for item in self.assertions]
        if len(set(assertion_ids)) != len(assertion_ids):
            raise AcceptanceContractError("criterion assertion IDs are duplicated")
        if any(
            not set(item.source_refs).issubset(self.source_refs)
            for item in self.assertions
        ):
            raise AcceptanceContractError("assertion source refs exceed criterion refs")
        if set(self.source_refs) != {
            source_id for item in self.assertions for source_id in item.source_refs
        }:
            raise AcceptanceContractError(
                "criterion source refs must be exercised by its assertions"
            )
        if self.setup is None:
            raise AcceptanceContractError("criterion setup is missing")
        if self.kind in {
            "transformation",
            "state_transition",
            "invariant",
            "rejection",
        } and (not isinstance(self.setup, (Mapping, list, str)) or not self.setup):
            raise AcceptanceContractError(
                f"{self.kind} criterion needs an observable setup or population"
            )
        if self.kind == "state_transition" and not self.events:
            raise AcceptanceContractError("state transition criterion needs events")
        if self.kind == "state_transition" and any(
            not isinstance(item, Mapping) or not item for item in self.events
        ):
            raise AcceptanceContractError("state transition events must be objects")
        if self.kind == "rejection" and not any(
            item.relation == "raises" for item in self.assertions
        ):
            raise AcceptanceContractError(
                "rejection criterion needs a source-specified failure assertion"
            )
        if any(
            isinstance(item.expected, str)
            and " ".join(item.expected.casefold().split())
            == " ".join(item.observation.casefold().split())
            for item in self.assertions
        ):
            raise AcceptanceContractError(
                "criterion assertion is circular: expected repeats its observation"
            )
        if any(
            not isinstance(item, str) or not item.strip()
            for item in self.unresolved_questions
        ):
            raise AcceptanceContractError("criterion unresolved question is invalid")
        try:
            json.dumps(
                self._payload(include_fingerprint=False),
                sort_keys=True,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise AcceptanceContractError("criterion contains non-JSON values") from exc

    def _payload(self, *, include_fingerprint: bool) -> dict[str, Any]:
        data = {
            "schema_version": self.schema_version,
            "criterion_id": self.criterion_id,
            "contract_id": self.contract_id,
            "kind": self.kind,
            "source_refs": list(self.source_refs),
            "setup": self.setup,
            "operation": self.operation,
            "events": list(self.events),
            "assertions": [item.to_data() for item in self.assertions],
            "unresolved_questions": list(self.unresolved_questions),
            "quality": self.quality.to_data(),
        }
        if include_fingerprint:
            data["fingerprint"] = self.fingerprint or self.calculate_fingerprint()
        return data

    def calculate_fingerprint(self) -> str:
        encoded = json.dumps(
            self._payload(include_fingerprint=False),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def to_data(self) -> dict[str, Any]:
        return self._payload(include_fingerprint=True)

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> AcceptanceCriterion:
        required = {
            "schema_version",
            "criterion_id",
            "contract_id",
            "kind",
            "source_refs",
            "setup",
            "operation",
            "events",
            "assertions",
            "unresolved_questions",
            "quality",
            "fingerprint",
        }
        if set(raw) != required:
            raise AcceptanceContractError("criterion has unknown or missing fields")
        assertions_raw = raw.get("assertions")
        if not isinstance(assertions_raw, list):
            raise AcceptanceContractError("criterion assertions are malformed")
        assertions: list[CriterionAssertion] = []
        for item in assertions_raw:
            if not isinstance(item, Mapping) or set(item) != {
                "assertion_id",
                "observation",
                "relation",
                "expected",
                "source_refs",
                "basis",
            }:
                raise AcceptanceContractError(
                    "criterion assertion fields are malformed"
                )
            assertions.append(
                CriterionAssertion(
                    assertion_id=_required_text(item, "assertion_id"),
                    observation=_required_text(item, "observation"),
                    relation=_required_text(item, "relation"),
                    expected=item.get("expected"),
                    source_refs=_text_tuple(
                        item.get("source_refs"), "assertion source_refs"
                    ),
                    basis=_required_text(item, "basis"),
                )
            )
        criterion = cls(
            criterion_id=_required_text(raw, "criterion_id"),
            contract_id=_required_text(raw, "contract_id"),
            kind=_required_text(raw, "kind"),
            source_refs=_text_tuple(raw.get("source_refs"), "criterion source_refs"),
            setup=raw.get("setup"),
            operation=_required_text(raw, "operation"),
            events=_value_tuple(raw.get("events"), "criterion events"),
            assertions=tuple(assertions),
            unresolved_questions=_text_tuple(
                raw.get("unresolved_questions"),
                "unresolved_questions",
                allow_empty=True,
            ),
            quality=(
                CriterionQuality.from_data(raw["quality"])
                if isinstance(raw.get("quality"), Mapping)
                else _invalid_quality()
            ),
            fingerprint=_required_text(raw, "fingerprint"),
            schema_version=_required_text(raw, "schema_version"),
        )
        if criterion.fingerprint != criterion.calculate_fingerprint():
            raise AcceptanceContractError("criterion fingerprint is stale")
        return criterion


def _is_vague_predicate(value: str) -> bool:
    normalized = " ".join(value.casefold().split())
    return any(
        phrase in normalized
        for phrase in (
            "works correctly",
            "behaves correctly",
            "as expected",
            "is valid",
        )
    )


def _text_tuple(raw: Any, name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(
        isinstance(item, str) and item.strip() for item in raw
    ):
        raise AcceptanceContractError(f"{name} must be a list of non-empty strings")
    if not allow_empty and not raw:
        raise AcceptanceContractError(f"{name} cannot be empty")
    return tuple(raw)


def _value_tuple(raw: Any, name: str) -> tuple[Any, ...]:
    if not isinstance(raw, list):
        raise AcceptanceContractError(f"{name} must be a list")
    return tuple(raw)


def _invalid_quality() -> CriterionQuality:
    raise AcceptanceContractError("criterion quality is malformed")


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
