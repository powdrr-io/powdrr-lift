"""Compact, compiler-owned context packets for code-editing workers."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from powdrr_lift.core.acceptance_contract import (
    AcceptanceContractError,
    AcceptanceCriterion,
)
from powdrr_lift.core.behavior_contract import (
    BehaviorScenario,
    compile_behavior_scenarios,
    render_behavior_matrix,
)
from powdrr_lift.core.boolean_expression import (
    BooleanExpressionError,
    render_boolean_sentence,
)
from powdrr_lift.core.contract_closure import render_contract_closure
from powdrr_lift.core.instruction_ledger import BooleanCombination
from powdrr_lift.structrr.obligation_evidence import ObligationEvidenceContract


@dataclass(frozen=True, slots=True)
class RepositoryContextPacket:
    """Stable repository facts supplied to a coding worker."""

    allowed_paths: tuple[str, ...]
    validation_profiles: tuple[str, ...]
    existing_tests: tuple[Mapping[str, Any], ...] = ()

    def to_data(self) -> dict[str, Any]:
        return {
            "allowed_paths": list(self.allowed_paths),
            "validation_profiles": list(self.validation_profiles),
            "existing_tests": [dict(item) for item in self.existing_tests],
        }


@dataclass(frozen=True, slots=True)
class ImplementationPacket:
    """Minimal semantic handoff; structural identity stays with Powdrr."""

    objective: str
    obligations: tuple[str, ...]
    required_tests: tuple[Mapping[str, Any], ...]
    repository: RepositoryContextPacket
    behavior_scenarios: tuple[BehaviorScenario, ...] = ()
    contract_closure: Mapping[str, Any] | None = None
    external_contract_requirements: tuple[Mapping[str, Any], ...] = ()
    external_contract_notes: tuple[Mapping[str, Any], ...] = ()
    obligation_evidence_contracts: tuple[ObligationEvidenceContract, ...] = ()
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = ()
    acceptance_logic: tuple[Mapping[str, Any], ...] = ()

    def for_obligation(self, ordinal: int) -> ImplementationPacket:
        """Return the smallest packet needed for one implementation turn."""
        if ordinal < 1 or ordinal > len(self.obligations):
            raise ValueError(f"obligation ordinal out of range: {ordinal}")
        index = ordinal - 1
        required_tests = (
            (self.required_tests[index],) if index < len(self.required_tests) else ()
        )
        return ImplementationPacket(
            objective=self.objective,
            obligations=(self.obligations[index],),
            required_tests=required_tests,
            repository=self.repository,
            behavior_scenarios=(
                (self.behavior_scenarios[index],)
                if index < len(self.behavior_scenarios)
                else ()
            ),
            contract_closure=self.contract_closure,
            external_contract_requirements=self.external_contract_requirements,
            external_contract_notes=self.external_contract_notes,
            obligation_evidence_contracts=self.obligation_evidence_contracts[
                index : index + 1
            ],
            acceptance_criteria=self.acceptance_criteria,
            acceptance_logic=self.acceptance_logic,
        )

    def for_task(
        self,
        *,
        objective: str,
        acceptance_criteria: Sequence[str],
    ) -> ImplementationPacket:
        """Return a worker packet scoped to one compiled code task."""
        task_objective = objective.strip()
        if not task_objective:
            raise ValueError("task packet objective must not be empty")
        tests = tuple(
            {"description": criterion.strip()}
            for criterion in acceptance_criteria
            if isinstance(criterion, str) and criterion.strip()
        )
        if not tests:
            raise ValueError("task packet requires acceptance criteria")
        return ImplementationPacket(
            objective=task_objective,
            obligations=(task_objective,),
            required_tests=tests,
            repository=self.repository,
            behavior_scenarios=self.behavior_scenarios,
            contract_closure=self.contract_closure,
            external_contract_requirements=self.external_contract_requirements,
            external_contract_notes=self.external_contract_notes,
            obligation_evidence_contracts=self.obligation_evidence_contracts,
            acceptance_criteria=self.acceptance_criteria,
            acceptance_logic=self.acceptance_logic,
        )

    def to_data(self) -> dict[str, Any]:
        data = {
            "schema_version": "implementation-packet-v3",
            "objective": self.objective,
            "obligations": [
                {"ordinal": index, "description": description}
                for index, description in enumerate(self.obligations, start=1)
            ],
            "required_tests": [
                {
                    "ordinal": index,
                    "description": str(item.get("description", "")),
                }
                for index, item in enumerate(self.required_tests, start=1)
            ],
            "repository": self.repository.to_data(),
            "behavior_scenarios": [item.to_data() for item in self.behavior_scenarios],
            "acceptance_criteria": [
                item.to_data() for item in self.acceptance_criteria
            ],
            "acceptance_logic": [dict(item) for item in self.acceptance_logic],
        }
        if self.contract_closure is not None:
            data["contract_closure"] = dict(self.contract_closure)
        if self.external_contract_requirements:
            data["external_contract_requirements"] = [
                dict(item) for item in self.external_contract_requirements
            ]
        if self.external_contract_notes:
            data["external_contract_notes"] = [
                dict(item) for item in self.external_contract_notes
            ]
        if self.obligation_evidence_contracts:
            data["obligation_evidence_contracts"] = [
                item.to_data() for item in self.obligation_evidence_contracts
            ]
        return data

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> ImplementationPacket:
        schema_version = raw.get("schema_version")
        if schema_version not in {
            "implementation-packet-v1",
            "implementation-packet-v2",
            "implementation-packet-v3",
        }:
            raise ValueError("unsupported implementation packet schema")
        if schema_version in {
            "implementation-packet-v2",
            "implementation-packet-v3",
        } and ("acceptance_criteria" not in raw):
            raise ValueError("implementation packet v2 requires acceptance criteria")
        raw_acceptance_logic = raw.get("acceptance_logic", [])
        if not isinstance(raw_acceptance_logic, list) or not all(
            isinstance(item, Mapping) for item in raw_acceptance_logic
        ):
            raise ValueError("implementation packet acceptance logic is malformed")
        if schema_version == "implementation-packet-v3" and (
            "acceptance_logic" not in raw
        ):
            raise ValueError("implementation packet v3 requires acceptance logic")
        raw_obligations = raw.get("obligations")
        raw_tests = raw.get("required_tests")
        repository = raw.get("repository")
        if not isinstance(raw_obligations, list) or not isinstance(raw_tests, list):
            raise ValueError("implementation packet is incomplete")
        if not isinstance(repository, Mapping):
            raise ValueError("implementation packet repository context is missing")
        obligations = tuple(
            str(item["description"])
            for item in raw_obligations
            if isinstance(item, Mapping) and isinstance(item.get("description"), str)
        )
        tests = tuple(
            {"description": item.get("description", "")}
            for item in raw_tests
            if isinstance(item, Mapping)
        )
        existing = repository.get("existing_tests", [])
        if not isinstance(existing, list):
            raise ValueError("implementation packet existing tests are malformed")
        raw_scenarios = raw.get("behavior_scenarios", [])
        if not isinstance(raw_scenarios, list) or not all(
            isinstance(item, Mapping) for item in raw_scenarios
        ):
            raise ValueError("implementation packet behavior scenarios are malformed")
        raw_closure = raw.get("contract_closure")
        if raw_closure is not None and not isinstance(raw_closure, Mapping):
            raise ValueError("implementation packet contract closure is malformed")
        raw_external_requirements = raw.get("external_contract_requirements", [])
        if not isinstance(raw_external_requirements, list) or not all(
            isinstance(item, Mapping) for item in raw_external_requirements
        ):
            raise ValueError(
                "implementation packet external requirements are malformed"
            )
        raw_external_notes = raw.get("external_contract_notes", [])
        if not isinstance(raw_external_notes, list) or not all(
            isinstance(item, Mapping) for item in raw_external_notes
        ):
            raise ValueError("implementation packet external notes are malformed")
        raw_evidence_contracts = raw.get("obligation_evidence_contracts", [])
        if not isinstance(raw_evidence_contracts, list) or not all(
            isinstance(item, Mapping) for item in raw_evidence_contracts
        ):
            raise ValueError("implementation packet evidence contracts are malformed")
        raw_criteria = raw.get("acceptance_criteria", [])
        if not isinstance(raw_criteria, list) or not all(
            isinstance(item, Mapping) for item in raw_criteria
        ):
            raise ValueError("implementation packet acceptance criteria are malformed")
        packet = cls(
            objective=str(raw.get("objective", "")).strip(),
            obligations=obligations,
            required_tests=tests,
            repository=RepositoryContextPacket(
                allowed_paths=tuple(
                    str(item) for item in repository.get("allowed_paths", [])
                ),
                validation_profiles=tuple(
                    str(item) for item in repository.get("validation_profiles", [])
                ),
                existing_tests=tuple(
                    item for item in existing if isinstance(item, Mapping)
                ),
            ),
            behavior_scenarios=compile_behavior_scenarios(raw_scenarios),
            contract_closure=(
                dict(raw_closure) if isinstance(raw_closure, Mapping) else None
            ),
            external_contract_requirements=tuple(
                dict(item) for item in raw_external_requirements
            ),
            external_contract_notes=tuple(dict(item) for item in raw_external_notes),
            obligation_evidence_contracts=tuple(
                ObligationEvidenceContract.from_data(item)
                for item in raw_evidence_contracts
            ),
            acceptance_criteria=tuple(
                AcceptanceCriterion.from_data(item) for item in raw_criteria
            ),
            acceptance_logic=tuple(_normalize_acceptance_logic(raw_acceptance_logic)),
        )
        if not packet.objective.strip() or not packet.obligations:
            raise ValueError("implementation packet is missing required content")
        return packet

    def render(self) -> str:
        """Render the worker-facing validation contract.

        Obligations, provenance, and structural identity remain compiler-owned
        artifacts. Rendering them here duplicated the same requirements in the
        execution unit, intent packet, and acceptance criteria. The worker only
        needs the executable behavioral contracts that it must make true.
        """
        test_lines = []
        for index, item in enumerate(self.required_tests, start=1):
            description = (
                str(item.get("description", "")).strip().split(" Oracle:", 1)[0]
            )
            test_lines.append(
                f"- T{index:02d} — add a focused test proving {description}"
            )
        test_lines = test_lines or ["- none"]
        if self.behavior_scenarios:
            behavior_text = render_behavior_matrix(self.behavior_scenarios)
        else:
            behavior_text = "\n".join(
                (
                    "Required behavioral tests:",
                    "Make every required behavioral test below pass. These tests "
                    "are the executable acceptance contract; do not weaken or "
                    "delete them.",
                    *test_lines,
                    "Run the focused required tests after implementation. If a "
                    "test is broken because of an import, fixture, API, assertion, "
                    "or other test defect, repair the test and implementation as "
                    "needed; never weaken the behavioral assertion.",
                )
            )
        sections = [behavior_text]
<<<<<<< HEAD
        if self.acceptance_logic:
            sections.append(_render_acceptance_logic(self.acceptance_logic))
        if accepted_criteria:
            sections.append(
                _render_acceptance_criteria(
                    accepted_criteria, criterion_assumption_scenarios
                )
            )
        unresolved_criteria = tuple(
=======
        accepted_criteria = tuple(
>>>>>>> parent of baa0893c (Merge pull request #933 from powdrr-io/codex/acceptance-criteria-pr7)
            item
            for item in self.acceptance_criteria
            if item.quality.criterion_status == "checkable"
        )
        if accepted_criteria:
            proposed = [
                "Reviewed observable acceptance criteria:",
                "Implement these source-supported checks while preserving all "
                "instruction requirements.",
            ]
            for criterion in accepted_criteria:
                refs = ", ".join(criterion.source_refs)
                proposed.append(
                    f"- [{criterion.kind}; {criterion.criterion_id}; sources: {refs}] "
                    f"{criterion.operation}"
                )
                proposed.append(
                    "  Setup: "
                    + json.dumps(criterion.setup, ensure_ascii=False, sort_keys=True)
                )
                for event in criterion.events:
                    proposed.append(
                        "  Event: "
                        + json.dumps(event, ensure_ascii=False, sort_keys=True)
                    )
                for assertion in criterion.assertions:
                    expected = json.dumps(
                        assertion.expected, ensure_ascii=False, sort_keys=True
                    )
                    proposed.append(
                        f"  Assert {assertion.observation} {assertion.relation} "
                        f"{expected}"
                    )
                for question in criterion.unresolved_questions:
                    proposed.append(f"  Open question: {question}")
            sections.append("\n".join(proposed))
        if self.obligation_evidence_contracts:
            rendered = ["Instruction obligation evidence expectations:"]
            for contract in self.obligation_evidence_contracts:
                rendered.append(
                    f"- {contract.obligation_id} "
                    f"({contract.normative_strength.value}): "
                    f"diff={contract.diff_expectation.value}; "
                    "review routes="
                    f"{', '.join(route.value for route in contract.review_routes)}. "
                    f"{contract.rationale}"
                )
            sections.append("\n".join(rendered))
        if self.external_contract_requirements:
            rendered = ["External contract requirements (accepted and scoped):"]
            for index, requirement in enumerate(
                self.external_contract_requirements, start=1
            ):
                rendered.append(f"{index}. {requirement.get('requirement', '')}")
                rendered.append(
                    "   Source: "
                    f"{requirement.get('canonical_url', '')}"
                    f" (profile: {requirement.get('profile', 'unspecified')})."
                )
                quote = requirement.get("source_quote")
                if isinstance(quote, str) and quote.strip():
                    rendered.append(f"   Supporting excerpt: {quote.strip()}")
                rendered.append(
                    f"   Scope rationale: {requirement.get('rationale', '')}"
                )
            sections.append("\n".join(rendered))
        if self.external_contract_notes:
            rendered_notes = [
                "Unresolved external contract questions (do not assume an answer):"
            ]
            for index, note in enumerate(self.external_contract_notes, start=1):
                claim = note.get("claim", {})
                if not isinstance(claim, Mapping):
                    claim = {}
                rendered_notes.append(
                    f"{index}. Candidate: {claim.get('candidate_requirement', '')}"
                )
                rendered_notes.append(
                    f"   Source: {claim.get('canonical_url', '')}"
                    f" (profile: {claim.get('profile', 'unspecified')})."
                )
                rendered_notes.append(f"   Uncertainty: {note.get('rationale', '')}")
            sections.append("\n".join(rendered_notes))
        if self.contract_closure is not None:
            sections.append(render_contract_closure(self.contract_closure))
        return "\n\n".join(section for section in sections if section)


<<<<<<< HEAD
def _render_acceptance_criteria(
    criteria: Sequence[AcceptanceCriterion],
    assumption_scenarios: Sequence[BehaviorScenario] = (),
) -> str:
    """Render reviewed criteria as readable checks grouped by source contract."""
    grouped: dict[str, list[AcceptanceCriterion]] = {}
    for criterion in criteria:
        grouped.setdefault(criterion.contract_id, []).append(criterion)

    lines = [
        "Observable acceptance checks:",
        "These reviewed checks express required behavior. Literal setup values "
        "are examples unless an assertion states their significance; preserve "
        "the stated relationships and outcomes.",
    ]
    for contract_ordinal, contract_criteria in enumerate(grouped.values(), start=1):
        first = contract_criteria[0]
        lines.extend(("", f"Contract {contract_ordinal}: {first.operation}"))
        for index, criterion in enumerate(contract_criteria, start=1):
            setup = json.dumps(
                criterion.setup,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            description = f"{index}. Start with {setup}."
            if criterion.operation != first.operation:
                description += f" Perform {criterion.operation}."
            for event in criterion.events:
                description += (
                    " Then apply "
                    + json.dumps(
                        event, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                    + "."
                )
            lines.append(description)
            for assertion in criterion.assertions:
                expected = json.dumps(
                    assertion.expected,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                lines.append(
                    f"   Check that {assertion.observation} {assertion.relation} "
                    f"{expected}."
                )
    assumptions = [
        item for scenario in assumption_scenarios for item in scenario.assumptions
    ]
    if assumptions:
        lines.extend(("", "Necessary implementation choices and assumptions:"))
        for assumption in assumptions:
            lines.append(
                f"- {assumption['dimension']}: {assumption['resolution']} "
                f"(basis: {assumption['basis']}; "
                f"{assumption['rationale']})."
            )
    return "\n".join(lines)


def _render_acceptance_logic(combinations: Sequence[Mapping[str, Any]]) -> str:
    """Render the source split tree that governs how atomic checks combine."""
    lines = [
        "Acceptance logic from the original instruction:",
        "The atomic acceptance checks below combine according to these exact "
        "expressions. In particular, alternatives are not all required at once.",
    ]
    for index, combination in enumerate(combinations, start=1):
        expression = combination.get("expression")
        children = combination.get("child_requirements", [])
        if not isinstance(expression, Mapping) or not isinstance(children, list):
            raise ValueError("implementation packet acceptance logic is malformed")
        lines.append(f"{index}. {combination.get('reconstructed_sentence', '')}")
        lines.append(
            "   Boolean expression: "
            + json.dumps(expression, ensure_ascii=False, sort_keys=True)
        )
        for atom_index, child in enumerate(children, start=1):
            if not isinstance(child, Mapping):
                raise ValueError("implementation packet acceptance logic is malformed")
            lines.append(f"   Atom {atom_index}: " + str(child.get("source_text", "")))
    return "\n".join(lines)


=======
>>>>>>> parent of baa0893c (Merge pull request #933 from powdrr-io/codex/acceptance-criteria-pr7)
def compile_implementation_packet(
    *,
    objective: str,
    obligations: Sequence[str],
    required_tests: Sequence[Mapping[str, Any]],
    allowed_paths: Sequence[str],
    validation_profiles: Sequence[str],
    existing_tests: Sequence[Mapping[str, Any]] = (),
    behavior_scenarios: Sequence[Mapping[str, Any]] = (),
    contract_closure: Mapping[str, Any] | None = None,
    external_contract_requirements: Sequence[Mapping[str, Any]] = (),
    external_contract_notes: Sequence[Mapping[str, Any]] = (),
    obligation_evidence_contracts: Sequence[Mapping[str, Any]] = (),
    acceptance_criteria: Sequence[Mapping[str, Any]] = (),
    acceptance_logic: Sequence[Mapping[str, Any]] = (),
) -> ImplementationPacket:
    """Normalize worker inputs and reject incomplete executable contracts."""
    if not objective.strip():
        raise ValueError("implementation packet objective must not be empty")
    normalized_obligations = tuple(
        item.strip() for item in obligations if isinstance(item, str) and item.strip()
    )
    if not normalized_obligations:
        raise ValueError("implementation packet requires obligations")
    normalized_tests: list[Mapping[str, Any]] = []
    for item in required_tests:
        if not isinstance(item, Mapping):
            raise ValueError("implementation packet test contract is malformed")
        description = item.get("description", "")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("implementation packet test lacks description")
        normalized_tests.append({"description": description.strip()})
    if not normalized_tests:
        raise ValueError("implementation packet requires test contracts")
    embedded_scenarios = tuple(
        item["behavior_scenario"]
        for item in required_tests
        if isinstance(item.get("behavior_scenario"), Mapping)
    )
    evidence_contracts = tuple(
        ObligationEvidenceContract.from_data(item)
        for item in obligation_evidence_contracts
    )
    evidence_by_clause = {item.clause_id: item for item in evidence_contracts}
    if evidence_contracts and len(evidence_by_clause) != len(evidence_contracts):
        raise ValueError(
            "implementation packet has duplicate evidence contract clauses"
        )
    if evidence_contracts and len(evidence_contracts) != len(normalized_obligations):
        raise ValueError(
            "implementation packet evidence contracts must cover every obligation"
        )
    try:
        criteria = tuple(
            AcceptanceCriterion.from_data(item) for item in acceptance_criteria
        )
    except AcceptanceContractError as error:
        raise ValueError(
            f"implementation packet acceptance criteria are invalid: {error}"
        ) from error
    return ImplementationPacket(
        objective=objective.strip(),
        obligations=normalized_obligations,
        required_tests=tuple(normalized_tests),
        repository=RepositoryContextPacket(
            allowed_paths=tuple(dict.fromkeys(str(item) for item in allowed_paths)),
            validation_profiles=tuple(
                dict.fromkeys(str(item) for item in validation_profiles)
            ),
            existing_tests=tuple(
                {
                    key: item.get(key)
                    for key in ("provider", "profile", "selector", "fingerprint")
                }
                for item in existing_tests
                if isinstance(item, Mapping) and isinstance(item.get("selector"), str)
            ),
        ),
        behavior_scenarios=compile_behavior_scenarios(
            (*embedded_scenarios, *behavior_scenarios)
        ),
        contract_closure=(
            dict(contract_closure) if contract_closure is not None else None
        ),
        external_contract_requirements=tuple(
            dict(item) for item in external_contract_requirements
        ),
        external_contract_notes=tuple(dict(item) for item in external_contract_notes),
        obligation_evidence_contracts=evidence_contracts,
        acceptance_criteria=criteria,
        acceptance_logic=tuple(_normalize_acceptance_logic(acceptance_logic)),
    )


def _normalize_acceptance_logic(
    raw_combinations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if isinstance(raw_combinations, (str, bytes)) or not isinstance(
        raw_combinations, Sequence
    ):
        raise ValueError("implementation packet acceptance logic is malformed")
    normalized: list[dict[str, Any]] = []
    for raw in raw_combinations:
        if not isinstance(raw, Mapping):
            raise ValueError("implementation packet acceptance logic is malformed")
        base = {
            key: raw.get(key)
            for key in (
                "parent_clause_id",
                "child_clause_ids",
                "expression",
                "reconstructed_sentence",
            )
        }
        children = raw.get("child_requirements")
        if not isinstance(children, list) or not all(
            isinstance(item, Mapping) for item in children
        ):
            raise ValueError("implementation packet acceptance logic is malformed")
        try:
            combination = BooleanCombination.from_data(base)
        except (BooleanExpressionError, TypeError, ValueError) as error:
            raise ValueError(
                "implementation packet acceptance logic is malformed"
            ) from error
        child_ids = [item.get("requirement_id") for item in children]
        child_texts = [item.get("source_text") for item in children]
        if tuple(child_ids) != combination.child_clause_ids or not all(
            isinstance(item, str) and item.strip() for item in child_texts
        ):
            raise ValueError("implementation packet acceptance logic atoms are invalid")
        try:
            _, reconstructed = render_boolean_sentence(
                child_texts, combination.expression
            )
        except BooleanExpressionError as error:
            raise ValueError(
                "implementation packet acceptance logic is malformed"
            ) from error
        if reconstructed != combination.reconstructed_sentence:
            raise ValueError("implementation packet acceptance logic is stale")
        normalized.append(
            {
                **combination.to_data(),
                "child_requirements": [
                    {"requirement_id": item, "source_text": text}
                    for item, text in zip(child_ids, child_texts, strict=True)
                ],
            }
        )
    return normalized


__all__ = [
    "ImplementationPacket",
    "RepositoryContextPacket",
    "compile_implementation_packet",
]
