"""Compile reviewed test obligations into bounded authoring requests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class TestCaseRequestError(ValueError):
    """A reviewed obligation cannot be represented as a typed test request."""

    __test__ = False


@dataclass(frozen=True, slots=True)
class TestCaseRequest:
    case_id: str
    requirement: str
    test_type: str
    scenario: str
    expected_outcome: str
    public_boundary: tuple[str, ...]
    fixtures: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    inputs: Mapping[str, Any]
    expected_observations: tuple[str, ...]
    permitted_substitutions: tuple[str, ...]
    baseline_policy: str
    assigned_test_path: str

    def to_data(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "requirement": self.requirement,
            "test_type": self.test_type,
            "scenario": self.scenario,
            "expected_outcome": self.expected_outcome,
            "public_boundary": list(self.public_boundary),
            "fixtures": list(self.fixtures),
            "evidence_refs": list(self.evidence_refs),
            "inputs": dict(self.inputs),
            "expected_observations": list(self.expected_observations),
            "permitted_substitutions": list(self.permitted_substitutions),
            "baseline_policy": self.baseline_policy,
            "assigned_test_path": self.assigned_test_path,
        }


def compile_test_case_requests(
    obligations: Sequence[Mapping[str, Any]],
    *,
    planned_cases: Sequence[Mapping[str, Any]],
    public_boundary: Sequence[Mapping[str, Any] | str],
    baseline_policy: str,
    assigned_test_path: str,
) -> tuple[TestCaseRequest, ...]:
    """Bind accepted case semantics to runtime-owned path and baseline facts."""
    if baseline_policy not in {"new_behavior", "existing_behavior"}:
        raise TestCaseRequestError("unsupported test baseline policy")
    cases_by_id: dict[str, Mapping[str, Any]] = {}
    for case in planned_cases:
        case_id = case.get("id")
        if (
            not isinstance(case_id, str)
            or not case_id.strip()
            or case_id in cases_by_id
        ):
            raise TestCaseRequestError("planned test cases require unique IDs")
        cases_by_id[case_id] = case

    boundary_records = tuple(
        item if isinstance(item, str) else item.get("path") for item in public_boundary
    )
    boundary = tuple(
        dict.fromkeys(
            item.strip()
            for item in boundary_records
            if isinstance(item, str) and item.strip()
        )
    )
    result = []
    obligation_ids: set[str] = set()
    for obligation in obligations:
        case_id = obligation.get("obligation_id")
        requirement = obligation.get("source_text")
        if (
            not isinstance(case_id, str)
            or not case_id.strip()
            or case_id in obligation_ids
            or not isinstance(requirement, str)
            or not requirement.strip()
        ):
            raise TestCaseRequestError(
                "obligations require unique IDs and requirements"
            )
        obligation_ids.add(case_id)
        planned = cases_by_id.get(case_id, {})
        test_type = planned.get("test_type", "integration")
        scenario = planned.get("scenario", requirement)
        expected = planned.get("expected_outcome", requirement)
        if test_type not in {"unit", "integration", "end_to_end", "contract"}:
            raise TestCaseRequestError(f"{case_id}: unsupported test type")
        if not isinstance(scenario, str) or not scenario.strip():
            raise TestCaseRequestError(f"{case_id}: scenario is required")
        if not isinstance(expected, str) or not expected.strip():
            raise TestCaseRequestError(f"{case_id}: expected outcome is required")

        behavior = planned.get("behavior_scenario", {})
        behavior = behavior if isinstance(behavior, Mapping) else {}
        dimensions = behavior.get("dimensions", {})
        inputs = dict(dimensions) if isinstance(dimensions, Mapping) else {}
        story_case = obligation.get("case", {})
        story_case = story_case if isinstance(story_case, Mapping) else {}
        initial_conditions = story_case.get("initial_conditions", ())
        if isinstance(initial_conditions, Sequence) and not isinstance(
            initial_conditions, (str, bytes)
        ):
            if initial_conditions:
                inputs["initial_conditions"] = list(initial_conditions)
        fixtures_value = planned.get("fixtures", ())
        fixtures = (
            tuple(
                dict.fromkeys(
                    item.strip()
                    for item in fixtures_value
                    if isinstance(item, str) and item.strip()
                )
            )
            if isinstance(fixtures_value, Sequence)
            and not isinstance(fixtures_value, (str, bytes))
            else ()
        )
        if isinstance(initial_conditions, Sequence) and not isinstance(
            initial_conditions, (str, bytes)
        ):
            fixtures = tuple(
                dict.fromkeys(
                    (
                        *fixtures,
                        *(item for item in initial_conditions if isinstance(item, str)),
                    )
                )
            )
        evidence = behavior.get("evidence", planned.get("evidence_refs", ()))
        evidence_refs = (
            tuple(
                dict.fromkeys(
                    item.strip()
                    for item in evidence
                    if isinstance(item, str) and item.strip()
                )
            )
            if isinstance(evidence, Sequence) and not isinstance(evidence, (str, bytes))
            else ()
        )
        observations = obligation.get(
            "expected_observations", planned.get("expected_observations", ())
        )
        if not isinstance(observations, Sequence) or isinstance(
            observations, (str, bytes)
        ):
            observations = ()
        observation_text = tuple(
            dict.fromkeys(
                item.strip()
                if isinstance(item, str)
                else (
                    f"{item['id']}: {item['description'].strip()}"
                    if isinstance(item.get("id"), str)
                    else item["description"].strip()
                )
                for item in observations
                if (isinstance(item, str) and item.strip())
                or (
                    isinstance(item, Mapping)
                    and isinstance(item.get("description"), str)
                    and item["description"].strip()
                )
            )
        ) or (expected.strip(),)
        substitutions = planned.get("permitted_substitutions", ())
        if not isinstance(substitutions, Sequence) or isinstance(
            substitutions, (str, bytes)
        ):
            raise TestCaseRequestError(
                f"{case_id}: permitted substitutions must be a list"
            )
        substitution_text = tuple(
            item.strip()
            for item in substitutions
            if isinstance(item, str) and item.strip()
        )
        result.append(
            TestCaseRequest(
                case_id=case_id,
                requirement=requirement.strip(),
                test_type=str(test_type),
                scenario=scenario.strip(),
                expected_outcome=expected.strip(),
                public_boundary=boundary,
                fixtures=fixtures,
                evidence_refs=evidence_refs,
                inputs=inputs,
                expected_observations=observation_text,
                permitted_substitutions=substitution_text,
                baseline_policy=baseline_policy,
                assigned_test_path=assigned_test_path,
            )
        )
    unknown = set(cases_by_id) - obligation_ids
    if unknown:
        raise TestCaseRequestError(
            f"planned cases do not match reviewed obligations: {sorted(unknown)}"
        )
    return tuple(result)


__all__ = ["TestCaseRequest", "TestCaseRequestError", "compile_test_case_requests"]
