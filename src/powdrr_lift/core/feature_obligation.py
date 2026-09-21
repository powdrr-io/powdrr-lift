"""Compiler-owned design, obligation, and test-contract records.

Models may provide semantic prose, but they do not provide identifiers,
references, selectors, or fingerprints.  This module turns the instruction
ledger and semantic design responses into a small canonical projection that
the rest of the feature flow can validate and render.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from powdrr_lift.core.instruction_ledger import InstructionLedger


class FeatureObligationError(ValueError):
    """Raised when a canonical design projection cannot be compiled."""


ACTIONABLE_KINDS = frozenset(
    {"entity", "feature", "interface", "invariant", "guidance", "non_goal"}
)
SEMANTIC_KINDS = ACTIONABLE_KINDS | {"nonactionable"}


@dataclass(frozen=True, slots=True)
class DesignProjection:
    clause_id: str
    kind: str
    description: str
    acceptance_criterion: str
    expected_test: str

    @property
    def design_id(self) -> str:
        return f"design:{self.clause_id}"

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.to_data(include_fingerprint=False))

    def to_data(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        data = {
            "design_id": self.design_id,
            "clause_id": self.clause_id,
            "kind": self.kind,
            "description": self.description,
            "acceptance_criterion": self.acceptance_criterion,
            "expected_test": self.expected_test,
        }
        if include_fingerprint:
            data["fingerprint"] = self.fingerprint
        return data


@dataclass(frozen=True, slots=True)
class FeatureObligation:
    clause_id: str
    projection: DesignProjection

    @property
    def obligation_id(self) -> str:
        return f"obligation:{self.clause_id}"

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.to_data(include_fingerprint=False))

    def to_data(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        data = {
            "obligation_id": self.obligation_id,
            "clause_id": self.clause_id,
            "design_id": self.projection.design_id,
            "description": self.projection.description,
            "acceptance_criterion": self.projection.acceptance_criterion,
            "expected_test": self.projection.expected_test,
        }
        if include_fingerprint:
            data["fingerprint"] = self.fingerprint
        return data


@dataclass(frozen=True, slots=True)
class RequiredTestContract:
    obligation_id: str
    work_item_slug: str
    description: str = ""

    @property
    def test_id(self) -> str:
        return f"test:{self.obligation_id}"

    @property
    def ordinal(self) -> int:
        return int(self.obligation_id.rsplit("-", 1)[-1])

    @property
    def selector(self) -> str:
        return (
            f"tests/test_{self.work_item_slug}_instruction_{self.ordinal:03d}.py"
            f"::test_instruction_{self.ordinal:03d}"
        )

    @property
    def name_hint(self) -> str:
        slug = re.sub(
            r"[^a-z0-9]+",
            "_",
            (
                self.description
                or f"{self.work_item_slug}_instruction_{self.ordinal:03d}"
            ).casefold(),
        ).strip("_")
        return f"test_{slug[:100].rstrip('_')}"

    def to_data(self) -> dict[str, Any]:
        return {
            "id": self.test_id,
            "contract_id": self.test_id,
            "obligation_id": self.obligation_id,
            "intent_refs": [f"intent:{self.obligation_id}"],
            "provider": "pytest",
            "profile": "pytest",
            "selector": self.selector,
            "selector_status": "planned",
            "name_hint": self.name_hint,
            "description": f"Verify {self.obligation_id}.",
            "expectation": "pass",
            "applicability": {"mode": "required"},
            "status": "active",
        }


@dataclass(frozen=True, slots=True)
class CanonicalFeatureDesign:
    work_item_name: str
    ledger_fingerprint: str
    projections: tuple[DesignProjection, ...]
    obligations: tuple[FeatureObligation, ...]
    test_contracts: tuple[RequiredTestContract, ...]

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": "feature-design-v2",
            "work_item_name": self.work_item_name,
            "ledger_fingerprint": self.ledger_fingerprint,
            "projections": [item.to_data() for item in self.projections],
            "obligations": [item.to_data() for item in self.obligations],
            "required_test_cases": [item.to_data() for item in self.test_contracts],
            "structrr": {
                "source_refs": ["instruction-ledger"],
                "active_intent": [
                    {
                        "clause_id": f"intent:{item.obligation_id}",
                        "intent_id": f"intent:{item.obligation_id}",
                        "kind": item.projection.kind,
                        "statement": item.projection.description,
                        "source_ref": item.obligation_id,
                        "active": True,
                    }
                    for item in self.obligations
                ],
                "acceptance_criteria": [
                    {
                        "id": f"acceptance:{item.obligation_id}",
                        "description": item.projection.acceptance_criterion,
                        "source_ref": item.obligation_id,
                    }
                    for item in self.obligations
                ],
            },
        }

    def validate(self, ledger: InstructionLedger) -> None:
        if self.ledger_fingerprint != ledger.fingerprint:
            raise FeatureObligationError("canonical design uses a stale ledger")
        clause_ids = tuple(item.clause_id for item in ledger.clauses)
        projection_ids = tuple(item.clause_id for item in self.projections)
        if projection_ids != clause_ids:
            raise FeatureObligationError(
                "every instruction clause must have exactly one design projection"
            )
        obligation_ids = tuple(item.clause_id for item in self.obligations)
        if any(item not in clause_ids for item in obligation_ids):
            raise FeatureObligationError(
                "every obligation must reference an instruction clause"
            )
        if tuple(sorted(obligation_ids, key=clause_ids.index)) != obligation_ids:
            raise FeatureObligationError(
                "obligations must preserve instruction ledger order"
            )
        if len(self.test_contracts) != len(self.obligations):
            raise FeatureObligationError(
                "every obligation must have exactly one required test contract"
            )
        for obligation, contract in zip(
            self.obligations, self.test_contracts, strict=True
        ):
            if contract.obligation_id != obligation.obligation_id:
                raise FeatureObligationError(
                    f"test contract is not bound to {obligation.obligation_id}"
                )


def compile_feature_design(
    ledger: InstructionLedger,
    work_item_name: str,
    semantic_obligations: Sequence[Mapping[str, Any]],
    *,
    required_clause_ids: Sequence[str] | None = None,
) -> CanonicalFeatureDesign:
    """Compile semantic model output against the compiler-owned ledger order."""
    if not work_item_name.strip():
        raise FeatureObligationError("work_item_name must not be empty")
    if len(semantic_obligations) != len(ledger.clauses):
        raise FeatureObligationError(
            "semantic design count must match the instruction ledger"
        )
    projections: list[DesignProjection] = []
    semantic_projections: dict[str, DesignProjection] = {}
    ledger_clause_ids = tuple(item.clause_id for item in ledger.clauses)
    slug = _slug(work_item_name)
    for clause, semantic in zip(ledger.clauses, semantic_obligations, strict=True):
        design = semantic.get("design")
        if design is None:
            design = semantic
        if not isinstance(design, Mapping):
            raise FeatureObligationError(
                f"missing semantic design for {clause.clause_id}"
            )
        projection = DesignProjection(
            clause_id=clause.clause_id,
            kind=_required_text(design, "kind"),
            description=_required_text(design, "description"),
            acceptance_criterion=_required_text(design, "acceptance_criterion"),
            expected_test=_required_text(design, "expected_test"),
        )
        projections.append(projection)
        semantic_projections[clause.clause_id] = projection
    selected_clause_ids = (
        ledger_clause_ids if required_clause_ids is None else tuple(required_clause_ids)
    )
    if len(set(selected_clause_ids)) != len(selected_clause_ids):
        raise FeatureObligationError("required instruction clauses are duplicated")
    if any(clause_id not in semantic_projections for clause_id in selected_clause_ids):
        raise FeatureObligationError(
            "required instruction clause is missing a design projection"
        )
    obligations = []
    for clause_id in selected_clause_ids:
        projection = semantic_projections[clause_id]
        if projection.kind not in ACTIONABLE_KINDS:
            continue
        if projection.kind == "non_goal":
            clause = next(
                item for item in ledger.clauses if item.clause_id == clause_id
            )
            if not _is_explicit_product_prohibition(clause.text):
                raise FeatureObligationError(
                    f"non_goal classification for {clause_id} is not supported by "
                    "an explicit product prohibition"
                )
        obligations.append(FeatureObligation(clause_id, projection))
    if not obligations:
        raise FeatureObligationError(
            "instruction clauses contain no actionable feature obligations"
        )
    result = CanonicalFeatureDesign(
        work_item_name=work_item_name,
        ledger_fingerprint=ledger.fingerprint,
        projections=tuple(projections),
        obligations=tuple(obligations),
        test_contracts=tuple(
            RequiredTestContract(item.obligation_id, slug, item.projection.description)
            for item in obligations
        ),
    )
    result.validate(ledger)
    return result


def _is_explicit_product_prohibition(text: str) -> bool:
    lowered = text.casefold()
    markers = (
        "do not ",
        "don't ",
        "must not ",
        "should not ",
        "out of scope",
        "not required",
        "exclude ",
    )
    return any(marker in lowered for marker in markers)


def _required_text(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FeatureObligationError(f"{key} must be non-empty semantic prose")
    return value.strip()


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "feature"


__all__ = [
    "CanonicalFeatureDesign",
    "DesignProjection",
    "FeatureObligation",
    "FeatureObligationError",
    "RequiredTestContract",
    "compile_feature_design",
]
