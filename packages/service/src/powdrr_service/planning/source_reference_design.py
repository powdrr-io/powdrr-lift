"""Build committed protected-worker designs from accepted per-PR test plans."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from powdrr_service.behavior_contract import BEHAVIOR_DIMENSIONS
from powdrr_service.structrr.source_reference_handoff import (
    compile_accepted_behavior_scenarios,
)


def compile_source_reference_design(
    *, plan_id: str, item_id: str, proposal: Mapping[str, Any]
) -> dict[str, Any]:
    """Bind the reviewed test scenarios to the protected task handoff schema."""
    verification = proposal.get("verification")
    raw_cases = (
        verification.get("required_test_cases")
        if isinstance(verification, Mapping)
        else None
    )
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError(f"plan item {item_id} has no accepted required test cases")

    cases: list[dict[str, Any]] = []
    scenarios: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_cases, start=1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"plan item {item_id} test case {index} is malformed")
        case_id = _required_text(raw.get("id"), f"test case {index} id")
        description = _required_text(
            raw.get("description"), f"test case {case_id} description"
        )
        scenario_text = _required_text(
            raw.get("scenario"), f"test case {case_id} scenario"
        )
        expected = _required_text(
            raw.get("expected_outcome"), f"test case {case_id} expected_outcome"
        )
        test_type = _required_text(raw.get("test_type"), f"test case {case_id} type")
        source_record = {
            "plan_id": plan_id,
            "item_id": item_id,
            "id": case_id,
            "description": description,
            "scenario": scenario_text,
            "expected_outcome": expected,
            "test_type": test_type,
        }
        fingerprint = hashlib.sha256(
            json.dumps(source_record, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        behavior_scenario = {
            "schema_version": "behavior-scenario-v1",
            "scenario_id": f"{item_id}-{index:03d}",
            "subject": description,
            "given": "The reviewed plan item is implemented in the candidate.",
            "when": scenario_text,
            "then": expected,
            "dimensions": {name: "not_applicable" for name in BEHAVIOR_DIMENSIONS},
            "evidence": [description],
            "validator": f"{test_type} test for the accepted scenario",
            "faithfulness_ref": {"fingerprint": fingerprint},
        }
        scenarios.append(behavior_scenario)
        cases.append({"id": case_id, "behavior_scenario": behavior_scenario})

    # Validate the exact scenarios that the runtime handoff will consume.
    result = {
        "schema_version": "structrr-source-reference-task-v1",
        "plan_id": plan_id,
        "item_id": item_id,
        "design": {
            "required_test_cases": cases,
            "source_reference_placements": [],
            "source_reference_preservations": [],
        },
    }
    compile_accepted_behavior_scenarios(result["design"])
    return result


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return " ".join(value.split())


__all__ = ["compile_source_reference_design"]
