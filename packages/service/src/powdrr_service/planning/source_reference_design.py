"""Build protected-worker designs from accepted item proposals and baselines."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from powdrr_service.structrr.actions import normalize_action
from powdrr_service.structrr.design import DesiredState, project_sections
from powdrr_service.structrr.execution_preparation import (
    compile_execution_preparation,
    parse_command_spec,
)
from powdrr_service.structrr.reference_catalog import (
    compile_reference_catalog,
    resolve_reference,
)
from powdrr_service.structrr.snapshot import snapshot_digest
from powdrr_service.structrr.source_reference_handoff import (
    compile_accepted_behavior_scenarios,
    compile_source_reference_handoff,
)

_REFERENCE_SECTIONS = (
    "reference_definitions",
    "active_intent",
    "features",
    "decisions",
    "human-decisions",
    "assumptions",
    "invariants",
    "guidance",
)
_REFERENCE_OPERATION_SECTIONS = frozenset(
    (
        *_REFERENCE_SECTIONS,
        "source_reference_placements",
        "source_reference_preservations",
    )
)
_REFERENCE_FIELDS = frozenset(
    {
        "reference_definitions",
        "source_reference_placements",
        "source_reference_preservations",
    }
)


def compile_source_reference_design(
    *,
    plan_id: str,
    item_id: str,
    proposal: Mapping[str, Any],
    baseline_snapshot: Mapping[str, Any],
    base_commit: str,
    objective: str,
    allowed_paths: tuple[str, ...],
    verification_command: str = "python -m pytest",
    dependency_preconditions: tuple[Mapping[str, Any], ...] = (),
) -> dict[str, Any]:
    """Materialize accepted obligations from the item's pinned baseline and diff.

    Operation envelopes are the authority for source-reference changes. The
    compiler projects them over the exact item baseline. Prose-only test cases
    are preserved in a blocked artifact that cannot reach protected execution.
    """
    if proposal.get("structrr_baseline_fingerprint") != snapshot_digest(
        baseline_snapshot
    ):
        raise ValueError("proposal does not match the pinned item baseline")
    if _REFERENCE_FIELDS & proposal.keys():
        raise ValueError(
            "source-reference records must come from accepted proposal operations"
        )
    verification = proposal.get("verification")
    raw_cases = (
        verification.get("required_test_cases")
        if isinstance(verification, Mapping)
        else None
    )
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError(f"plan item {item_id} has no accepted required test cases")

    cases: list[dict[str, Any]] = []
    missing_case: str | None = None
    for index, raw in enumerate(raw_cases, start=1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"plan item {item_id} test case {index} is malformed")
        case_id = _required_text(raw.get("id"), f"test case {index} id")
        scenario = raw.get("behavior_scenario")
        if not isinstance(scenario, Mapping):
            missing_case = missing_case or case_id
            cases.append(dict(raw))
        else:
            cases.append({**dict(raw), "behavior_scenario": dict(scenario)})

    raw_operations = proposal.get("operations")
    if not isinstance(raw_operations, list):
        raise ValueError("proposal operations must be a list")
    grouped: dict[str, list[dict[str, Any]]] = {}
    seen_operation_ids: set[str] = set()
    for index, raw in enumerate(raw_operations, start=1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"proposal operation {index} must be an object")
        section = raw.get("section")
        if section not in _REFERENCE_OPERATION_SECTIONS:
            continue
        operation_id = _required_text(raw.get("operation_id"), "operation_id")
        if operation_id in seen_operation_ids:
            raise ValueError(f"duplicate proposal operation ID: {operation_id}")
        seen_operation_ids.add(operation_id)
        subject_id = _required_text(raw.get("subject_id"), "subject_id")
        action = normalize_action(raw.get("action"))
        content = raw.get("content")
        if not isinstance(content, Mapping):
            raise ValueError(f"proposal operation {operation_id} has no content")
        identity_key = (
            "obligation_id" if section == "source_reference_placements" else "id"
        )
        if content.get(identity_key, subject_id) != subject_id:
            raise ValueError(
                f"proposal operation {operation_id} content identity differs "
                "from subject_id"
            )
        grouped.setdefault(section, []).append(
            {**dict(content), identity_key: subject_id, "action": action}
        )

    projected = project_sections(baseline_snapshot, grouped)
    reference_document = {
        section: projected.get(section, []) for section in _REFERENCE_SECTIONS
    }
    catalog = compile_reference_catalog(reference_document, base_commit=base_commit)
    placements = projected.get("source_reference_placements", [])
    preservation = projected.get("source_reference_preservations", [])
    if not isinstance(placements, list) or not isinstance(preservation, list):
        raise ValueError("projected source-reference obligations must be lists")
    for section, records, id_key in (
        ("placement", placements, "ref"),
        ("preservation", preservation, "ref"),
    ):
        for record in records:
            if not isinstance(record, Mapping):
                raise ValueError(f"projected {section} obligation is malformed")
            ref = _required_text(record.get(id_key), f"{section} ref")
            resolution = resolve_reference(catalog, ref)
            if resolution.entry is None or (
                section == "placement" and resolution.status != "resolved"
            ):
                raise ValueError(
                    f"{section} obligation references undefined source clause {ref!r}"
                )

    result = {
        "schema_version": "structrr-source-reference-task-v1",
        "plan_id": plan_id,
        "item_id": item_id,
        "design": {
            "required_test_cases": cases,
            "reference_definitions": [
                entry.reference.to_data() for entry in catalog.entries
            ],
            "source_reference_placements": placements,
            "source_reference_preservations": preservation,
        },
    }
    result["design"] = DesiredState.from_snapshot(result["design"]).document
    if missing_case is not None:
        requirement = (
            f"Plan item {item_id} test case {missing_case} lacks an accepted typed "
            "behavior scenario and source-faithfulness provenance. Amend or replan "
            "the accepted plan before protected execution."
        )
        result["status"] = "requires_replan"
        result["amendment_requirement"] = requirement
        result["design"]["compilation_status"] = "requires_replan"
        result["design"]["amendment_requirement"] = requirement
        return result
    compile_accepted_behavior_scenarios(result["design"])
    final_catalog = compile_reference_catalog(result["design"], base_commit=base_commit)
    compile_source_reference_handoff(
        result["design"],
        final_catalog,
        objective=objective,
        allowed_paths=allowed_paths,
    )
    result["preparation"] = compile_execution_preparation(
        result["design"],
        planning_base_commit=base_commit,
        objective=objective,
        allowed_paths=allowed_paths,
        verification_commands=(parse_command_spec(verification_command),),
        dependency_preconditions=dependency_preconditions,
    )
    return result


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value


__all__ = ["compile_source_reference_design"]
