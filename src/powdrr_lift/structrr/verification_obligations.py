"""Compile feature-specific verification obligations before implementation."""

from __future__ import annotations

import fnmatch
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from powdrr_lift.core.verification_contract import VerificationContract
from powdrr_lift.structrr.proposal import ProposalRevision

VERIFICATION_OBLIGATION_SCHEMA_VERSION = "verification-obligation-v1"


@dataclass(frozen=True, slots=True)
class AffectedIntentClosure:
    """The deterministic set of intent and entity subjects touched by a proposal."""

    intent_ids: tuple[str, ...]
    entity_ids: tuple[str, ...]
    anticipated_paths: tuple[str, ...]
    explanations: tuple[str, ...]

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.to_data(include_fingerprint=False))

    def to_data(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "intent_ids": list(self.intent_ids),
            "entity_ids": list(self.entity_ids),
            "anticipated_paths": list(self.anticipated_paths),
            "explanations": list(self.explanations),
        }
        if include_fingerprint:
            data["fingerprint"] = self.fingerprint
        return data


@dataclass(frozen=True, slots=True)
class VerificationObligation:
    """One contract that must be freshly proven for this feature."""

    obligation_id: str
    contract_id: str
    contract_fingerprint: str
    intent_refs: tuple[str, ...]
    provider: str
    selector: str
    profile: str
    expectation: str
    applicability: Mapping[str, Any]
    applicability_explanation: str
    protected_inputs: tuple[str, ...]
    verifier_fingerprint: str
    provider_inventory_fingerprint: str

    @classmethod
    def from_data(cls, value: Mapping[str, Any]) -> VerificationObligation:
        return cls(
            obligation_id=str(value["obligation_id"]),
            contract_id=str(value["contract_id"]),
            contract_fingerprint=str(value["contract_fingerprint"]),
            intent_refs=tuple(str(item) for item in value.get("intent_refs", [])),
            provider=str(value["provider"]),
            selector=str(value["selector"]),
            profile=str(value["profile"]),
            expectation=str(value["expectation"]),
            applicability=dict(value.get("applicability", {})),
            applicability_explanation=str(value.get("applicability_explanation", "")),
            protected_inputs=tuple(
                str(item) for item in value.get("protected_inputs", [])
            ),
            verifier_fingerprint=str(value["verifier_fingerprint"]),
            provider_inventory_fingerprint=str(value["provider_inventory_fingerprint"]),
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "contract_id": self.contract_id,
            "contract_fingerprint": self.contract_fingerprint,
            "intent_refs": list(self.intent_refs),
            "provider": self.provider,
            "selector": self.selector,
            "profile": self.profile,
            "expectation": self.expectation,
            "applicability": dict(self.applicability),
            "applicability_explanation": self.applicability_explanation,
            "protected_inputs": list(self.protected_inputs),
            "verifier_fingerprint": self.verifier_fingerprint,
            "provider_inventory_fingerprint": self.provider_inventory_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class VerificationObligationCompilation:
    """Complete pre-implementation proof packet, including blocking diagnostics."""

    closure: AffectedIntentClosure
    obligations: tuple[VerificationObligation, ...]
    excluded_contracts: tuple[dict[str, Any], ...]
    failures: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.failures

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.to_data(include_fingerprint=False))

    def to_data(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": VERIFICATION_OBLIGATION_SCHEMA_VERSION,
            "closure": self.closure.to_data(),
            "obligations": [item.to_data() for item in self.obligations],
            "excluded_contracts": list(self.excluded_contracts),
            "failures": list(self.failures),
            "complete": self.complete,
        }
        if include_fingerprint:
            data["fingerprint"] = self.fingerprint
        return data


def compile_verification_obligations(
    proposal: ProposalRevision,
    *,
    active_intents: Sequence[Mapping[str, Any]] = (),
    contracts: Sequence[VerificationContract] = (),
    provider_inventory: Sequence[Mapping[str, Any]] = (),
    anticipated_paths: Sequence[str] = (),
    relationships: Sequence[Mapping[str, Any]] = (),
    previous_contracts: Sequence[VerificationContract] = (),
) -> VerificationObligationCompilation:
    """Compile the exact contract set affected by a proposal.

    Selection is intentionally mechanical. A contract is selected when its
    protected intent, entity, or input path intersects the proposal closure.
    No model output participates in selection or evidence acceptance.
    """

    active_by_id = {
        _text(item.get("clause_id") or item.get("intent_id")): item
        for item in active_intents
        if _text(item.get("clause_id") or item.get("intent_id"))
    }
    intent_ids, entity_ids, path_values, explanations = _compile_closure(
        proposal,
        active_by_id=active_by_id,
        anticipated_paths=anticipated_paths,
        relationships=relationships,
    )
    closure = AffectedIntentClosure(
        tuple(sorted(intent_ids)),
        tuple(sorted(entity_ids)),
        tuple(sorted(path_values)),
        tuple(explanations),
    )
    inventory = {
        (
            _text(item.get("provider")),
            _text(item.get("profile")),
            _text(item.get("selector")),
        ): item
        for item in provider_inventory
        if isinstance(item, Mapping)
    }
    obligations: list[VerificationObligation] = []
    excluded: list[dict[str, Any]] = []
    failures: list[str] = []
    selected_ids: set[str] = set()
    previous_contract_ids = {item.contract_id for item in previous_contracts}
    planned_contract_ids = {
        operation.subject_id
        for operation in proposal.operations
        if operation.section == "required_test_cases" and operation.action == "add"
    }
    # Contracts introduced by the current feature are planned tests. Their
    # selectors are created by the coding worker, so they cannot be present in
    # the baseline provider inventory yet. Only contracts inherited from the
    # baseline require an already-discovered executable selector.
    planned_contract_ids.update(
        contract.contract_id
        for contract in contracts
        if contract.contract_id not in previous_contract_ids
    )
    for contract in contracts:
        errors = contract.validation_errors()
        if errors:
            failures.append(
                f"contract {contract.contract_id or '<unnamed>'} is incomplete: "
                + "; ".join(errors)
            )
            continue
        if contract.status == "superseded":
            excluded.append(
                {
                    "contract_id": contract.contract_id,
                    "reason": "contract is superseded",
                }
            )
            continue
        reason = _selection_reason(
            contract,
            intent_ids=intent_ids,
            entity_ids=entity_ids,
            anticipated_paths=path_values,
        )
        if reason is None:
            excluded.append(
                {
                    "contract_id": contract.contract_id,
                    "reason": "outside affected intent, entity, and input closure",
                }
            )
            continue
        selected_ids.add(contract.contract_id)
        if contract.applicability is None:
            failures.append(
                f"contract {contract.contract_id} has no deterministic applicability"
            )
            continue
        inventory_entry = inventory.get(
            (contract.provider or "", contract.profile or "", contract.selector or "")
        )
        if inventory_entry is None:
            if contract.contract_id in planned_contract_ids and (
                contract.contract_id not in previous_contract_ids
            ):
                planned_fingerprint = "planned:" + contract.fingerprint
                inventory_entry = {
                    "fingerprint": planned_fingerprint,
                    "verifier_fingerprint": planned_fingerprint,
                }
            else:
                failures.append(
                    f"contract {contract.contract_id} selector is missing from "
                    f"provider inventory: {contract.provider}/{contract.profile}/"
                    f"{contract.selector}"
                )
                continue
        inventory_fingerprint = _text(inventory_entry.get("fingerprint"))
        if not inventory_fingerprint:
            failures.append(
                f"provider inventory entry for {contract.contract_id} has no "
                "fingerprint"
            )
            continue
        verifier_fingerprint = _text(
            inventory_entry.get("verifier_fingerprint")
            or inventory_entry.get("fingerprint")
        )
        payload = {
            "schema_version": VERIFICATION_OBLIGATION_SCHEMA_VERSION,
            "contract": contract.fingerprint,
            "intent_refs": sorted(contract.intent_refs),
            "applicability": dict(contract.applicability),
            "reason": reason,
            "closure": closure.fingerprint,
            "verifier": verifier_fingerprint,
            "inventory": inventory_fingerprint,
        }
        obligation_id = _fingerprint(payload)
        obligations.append(
            VerificationObligation(
                obligation_id=obligation_id,
                contract_id=contract.contract_id,
                contract_fingerprint=contract.fingerprint,
                intent_refs=contract.intent_refs,
                provider=contract.provider or "",
                selector=contract.selector or "",
                profile=contract.profile or "",
                expectation=contract.expectation or "",
                applicability=dict(contract.applicability),
                applicability_explanation=reason,
                protected_inputs=contract.protected_inputs,
                verifier_fingerprint=verifier_fingerprint,
                provider_inventory_fingerprint=inventory_fingerprint,
            )
        )
    for contract in contracts:
        if contract.status == "active" and contract.contract_id not in selected_ids:
            continue
    current_ids = {contract.contract_id for contract in contracts}
    represented_changes = {
        operation.subject_id
        for operation in proposal.operations
        if operation.section == "required_test_cases"
    }
    for previous in previous_contracts:
        if (
            previous.status == "active"
            and previous.contract_id not in current_ids
            and previous.contract_id not in represented_changes
        ):
            failures.append(
                f"active contract {previous.contract_id} was removed without an "
                "explicit required_test_cases proposal operation"
            )
    failures.extend(_new_intent_contract_failures(proposal, contracts))
    failures.extend(_materialized_intent_contract_failures(active_intents, contracts))
    failures.extend(_required_test_intent_failures(proposal, active_by_id))
    return VerificationObligationCompilation(
        closure=closure,
        obligations=tuple(sorted(obligations, key=lambda item: item.contract_id)),
        excluded_contracts=tuple(
            sorted(excluded, key=lambda item: item["contract_id"])
        ),
        failures=tuple(dict.fromkeys(failures)),
    )


def _compile_closure(
    proposal: ProposalRevision,
    *,
    active_by_id: Mapping[str, Mapping[str, Any]],
    anticipated_paths: Sequence[str],
    relationships: Sequence[Mapping[str, Any]],
) -> tuple[set[str], set[str], set[str], list[str]]:
    intent_ids: set[str] = set()
    entity_ids: set[str] = set()
    paths = {path.strip() for path in anticipated_paths if path.strip()}
    explanations: list[str] = []
    for operation in proposal.operations:
        content = operation.content
        intent_values = _values(
            content, "intent_refs", "intent_ids", "intent_id", "clause_id"
        )
        for value in intent_values:
            if value in active_by_id or value.startswith(("intent.", "spec:")):
                intent_ids.add(value)
                explanations.append(
                    f"intent {value} traced from {operation.operation_id}"
                )
        entity_values = _values(
            content, "entity_refs", "entity_ids", "entity_id", "subject_id"
        )
        entity_ids.update(entity_values)
        for value in _values(content, "paths", "protected_inputs", "path", "file"):
            paths.add(value)
    changed_entities = set(entity_ids)
    changed_entities.update(
        operation.subject_id
        for operation in proposal.operations
        if operation.section in {"entities", "entity_relationships"}
    )
    changed = True
    while changed:
        changed = False
        for relationship in relationships:
            source = _text(relationship.get("source"))
            target = _text(relationship.get("target"))
            if source in changed_entities and target not in changed_entities:
                changed_entities.add(target)
                changed = True
            elif target in changed_entities and source not in changed_entities:
                changed_entities.add(source)
                changed = True
    entity_ids = changed_entities
    return intent_ids, entity_ids, paths, explanations


def _selection_reason(
    contract: VerificationContract,
    *,
    intent_ids: set[str],
    entity_ids: set[str],
    anticipated_paths: set[str],
) -> str | None:
    direct = sorted(set(contract.intent_refs) & intent_ids)
    if direct:
        return "protected intent intersects affected closure: " + ", ".join(direct)
    protected_entities = set()
    for value in contract.protected_inputs:
        if value.startswith("entity:"):
            protected_entities.add(value.removeprefix("entity:"))
    related = sorted(protected_entities & entity_ids)
    if related:
        return "protected entity intersects relationship closure: " + ", ".join(related)
    matched_paths = sorted(
        protected
        for protected in contract.protected_inputs
        if any(_path_matches(protected, path) for path in anticipated_paths)
    )
    if matched_paths:
        return "protected input intersects anticipated change: " + ", ".join(
            matched_paths
        )
    return None


def _new_intent_contract_failures(
    proposal: ProposalRevision, contracts: Sequence[VerificationContract]
) -> list[str]:
    protected = {ref for contract in contracts for ref in contract.intent_refs}
    failures: list[str] = []
    intent_sections = {"invariants", "guidance", "intent", "intents"}
    for operation in proposal.operations:
        if operation.action != "add" or operation.section not in intent_sections:
            continue
        referenced = _values(
            operation.content, "intent_refs", "intent_ids", "intent_id", "clause_id"
        )
        candidate = _text(referenced[0] if referenced else operation.content.get("id"))
        if candidate and candidate not in protected:
            failures.append(
                f"new or altered intent {candidate} has no verification contract"
            )
    return failures


def _materialized_intent_contract_failures(
    active_intents: Sequence[Mapping[str, Any]],
    contracts: Sequence[VerificationContract],
) -> list[str]:
    """Require sentence-derived intent clauses to have explicit coverage."""
    contract_refs = {
        reference for contract in contracts for reference in contract.intent_refs
    }
    failures: list[str] = []
    for clause in active_intents:
        clause_id = _text(clause.get("clause_id") or clause.get("intent_id"))
        source_ref = _text(clause.get("source_ref"))
        if not clause_id or not source_ref.startswith("feature-obligation:"):
            continue
        verification = clause.get("verification")
        if (
            isinstance(verification, Mapping)
            and verification.get("mode") == "non_obligating"
        ):
            rationale = verification.get("rationale")
            if isinstance(rationale, str) and rationale.strip():
                continue
            failures.append(
                f"materialized intent {clause_id} declares non_obligating "
                "without a rationale"
            )
            continue
        if clause_id not in contract_refs:
            failures.append(
                f"materialized intent {clause_id} has no verification contract; "
                "add its exact clause_id to required_test_cases.intent_refs or "
                "declare verification.mode=non_obligating with a rationale"
            )
    return failures


def _required_test_intent_failures(
    proposal: ProposalRevision, active_by_id: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    """Reject added test obligations that do not trace to current intent."""
    failures: list[str] = []
    for operation in proposal.operations:
        if operation.section != "required_test_cases" or operation.action != "add":
            continue
        refs = _values(
            operation.content, "intent_refs", "intent_ids", "intent_id", "clause_id"
        )
        if any(
            ref in active_by_id or ref.startswith(("intent.", "spec:")) for ref in refs
        ):
            continue
        failures.append(
            f"required test case {operation.subject_id} has no active intent reference"
        )
    return failures


def _path_matches(pattern: str, path: str) -> bool:
    normalized_pattern = pattern.rstrip("/") or pattern
    normalized_path = path.rstrip("/") or path
    return fnmatch.fnmatch(normalized_path, normalized_pattern) or fnmatch.fnmatch(
        normalized_path, normalized_pattern + "/**"
    )


def _values(mapping: Mapping[str, Any], *keys: str) -> tuple[str, ...]:
    values: list[str] = []
    for key in keys:
        raw = mapping.get(key)
        if isinstance(raw, str) and raw.strip():
            values.append(raw.strip())
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            values.extend(
                item.strip() for item in raw if isinstance(item, str) and item.strip()
            )
    return tuple(dict.fromkeys(values))


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "AffectedIntentClosure",
    "VERIFICATION_OBLIGATION_SCHEMA_VERSION",
    "VerificationObligation",
    "VerificationObligationCompilation",
    "compile_verification_obligations",
]
