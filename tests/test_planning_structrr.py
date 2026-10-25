from __future__ import annotations

from typing import Any

import pytest

from powdrr_lift.workrr.planning.assembly import PlannedWrite, WorkUnit
from powdrr_lift.workrr.planning.contracts import PlanningContractError
from powdrr_lift.workrr.planning.structrr import (
    BoundStructuralSubject,
    StructrrPlanningError,
    StructuralSubjectPage,
    UnorderedStructrrConflict,
    bind_structural_operation,
    compile_structrr_plan,
    complete_subject_enumeration,
    parse_structural_change,
    parse_structural_subject_page,
)


def _unit(
    unit_id: str = "unit-01",
    requirement_ids: tuple[str, ...] = ("req-01",),
    *,
    writes: tuple[PlannedWrite, ...] = (),
) -> WorkUnit:
    return WorkUnit(
        unit_id,
        requirement_ids,
        "Deliverable",
        "core",
        "python",
        writes,
        "Implement the deliverable.",
        ("The deliverable works.",),
        ("validation-01",),
    )


def _record(
    subject_ref: str, *, requirement_ids: list[str] | None = None
) -> dict[str, Any]:
    return {
        "section": "entities",
        "subject_ref": subject_ref,
        "new_identity_description": None,
        "action": "changed",
        "requirement_ids": requirement_ids or ["req-01"],
        "unit_ids": ["unit-01"],
        "source_refs": ["anchor-01"],
    }


def _candidate_page(
    records: list[dict[str, Any]], *, has_more: bool, next_anchor: str | None
) -> dict[str, Any]:
    return {"records": records, "has_more": has_more, "next_anchor": next_anchor}


def _parse_page(
    value: dict[str, Any],
    *,
    page_ordinal: int = 1,
    units: tuple[WorkUnit, ...] | None = None,
    requirements: set[str] | None = None,
    baseline: dict[str, Any] | None = None,
    prior: set[tuple[str, str]] | None = None,
) -> StructuralSubjectPage:
    return parse_structural_subject_page(
        value,
        plan_id="plan-01",
        item_id="item-01",
        requirement_ids=requirements or {"req-01"},
        units=units or (_unit(),),
        source_ids={"anchor-01"},
        projected_baseline=baseline or {"entities": []},
        prior_subject_ids=prior or set(),
        page_ordinal=page_ordinal,
    )


def test_structural_subject_pages_cover_all_subjects_and_require_final_page() -> None:
    baseline = {"entities": [{"id": f"entity-{index:02d}"} for index in range(12)]}
    first = _parse_page(
        _candidate_page(
            [_record(f"entity-{index:02d}") for index in range(8)],
            has_more=True,
            next_anchor="structural-page-0002",
        ),
        baseline=baseline,
    )
    second = _parse_page(
        _candidate_page(
            [_record(f"entity-{index:02d}") for index in range(8, 12)],
            has_more=False,
            next_anchor=None,
        ),
        page_ordinal=2,
        baseline=baseline,
        prior={("entities", subject.subject_id) for subject in first.records},
    )

    assert len(complete_subject_enumeration((first, second))) == 12
    with pytest.raises(StructrrPlanningError, match="incomplete"):
        complete_subject_enumeration((first,))


def test_subject_page_rejects_wrong_cursor_and_duplicate_subject() -> None:
    with pytest.raises(PlanningContractError, match="runtime-owned cursor"):
        _parse_page(
            _candidate_page([_record("entity-01")], has_more=True, next_anchor="next"),
            baseline={"entities": [{"id": "entity-01"}]},
        )

    baseline = {"entities": [{"id": "entity-01"}]}
    with pytest.raises(PlanningContractError, match="already enumerated"):
        _parse_page(
            _candidate_page([_record("entity-01")], has_more=False, next_anchor=None),
            baseline=baseline,
            prior={("entities", "entity-01")},
        )


def test_subject_requirements_must_be_covered_by_its_units() -> None:
    units = (_unit("unit-01", ("req-01",)), _unit("unit-02", ("req-02",)))
    row = _record("entity-01", requirement_ids=["req-01", "req-02"])
    row["unit_ids"] = ["unit-01"]
    with pytest.raises(PlanningContractError, match="cited requirements"):
        _parse_page(
            _candidate_page([row], has_more=False, next_anchor=None),
            units=units,
            requirements={"req-01", "req-02"},
            baseline={"entities": [{"id": "entity-01"}]},
        )


def test_new_file_subject_is_bound_to_a_planned_path() -> None:
    write = PlannedWrite("src/new.py", "implementation", "python", ("anchor-01",), ())
    unit = _unit(writes=(write,))
    candidate = {
        "section": "files",
        "subject_ref": None,
        "new_identity_description": "src/new.py",
        "action": "added",
        "requirement_ids": ["req-01"],
        "unit_ids": [unit.id],
        "source_refs": ["anchor-01"],
    }
    page = _parse_page(
        _candidate_page([candidate], has_more=False, next_anchor=None), units=(unit,)
    )
    subject = page.records[0]
    change = parse_structural_change(
        {
            "result": "change",
            "desired_content": {"path": "src/new.py"},
            "reason": "A new source subject is needed.",
        },
        subject=subject,
        taxonomy_entity_types=set(),
        planned_paths={"src/new.py"},
        known_entity_ids=set(),
    )

    operation = bind_structural_operation(change)
    assert operation is not None
    assert operation["subject_id"] == "path::src/new.py"
    assert operation["content"]["stable_key"] == "path::src/new.py"
    assert operation["content"]["id"] == "file:src/new.py"


def test_change_response_rejects_model_assigned_identity_fields() -> None:
    subject = BoundStructuralSubject(
        "entity-01",
        "entities",
        "item-01",
        "changed",
        ("req-01",),
        ("unit-01",),
        ("anchor-01",),
        False,
    )
    with pytest.raises(PlanningContractError, match="code-assigned"):
        parse_structural_change(
            {
                "result": "change",
                "desired_content": {"id": "invented", "description": "change"},
                "reason": "Update the entity.",
            },
            subject=subject,
            taxonomy_entity_types={"module"},
            planned_paths=set(),
            known_entity_ids={"entity-01"},
        )


def test_structural_compilation_projects_predecessor_subjects() -> None:
    first = {
        "operation_id": "item-01:entities:entity-02",
        "item_id": "item-01",
        "section": "entities",
        "subject_id": "entity-02",
        "action": "added",
        "content": {"id": "entity-02", "type": "module", "name": "Module"},
    }
    second = {
        "operation_id": "item-02:entity_relationships:rel-01",
        "item_id": "item-02",
        "section": "entity_relationships",
        "subject_id": "rel-01",
        "action": "added",
        "content": {
            "id": "rel-01",
            "source": "entity-01",
            "target": "entity-02",
            "relationship": "contains",
        },
    }
    proposal = compile_structrr_plan(
        plan_id="plan-01",
        baseline_snapshot={"entities": [{"id": "entity-01", "type": "module"}]},
        items=[
            {"id": "item-01", "dependencies": [], "allowed_paths": ["src/a.py"]},
            {
                "id": "item-02",
                "dependencies": ["item-01"],
                "allowed_paths": ["src/b.py"],
            },
        ],
        item_order=["item-01", "item-02"],
        operations=[first, second],
        source_ref="plan.md#sha256:abc",
    )

    assert proposal.item_baselines["item-02"]["entities"][-1]["id"] == "entity-02"
    assert (
        proposal.expected_final_snapshot["entity_relationships"][0]["target"]
        == "entity-02"
    )


def test_unordered_items_cannot_claim_the_same_structural_subject() -> None:
    operation = {
        "section": "entities",
        "subject_id": "entity-01",
        "action": "changed",
        "content": {"id": "entity-01", "description": "updated"},
    }
    items = [
        {"id": item_id, "dependencies": [], "allowed_paths": [f"{item_id}.py"]}
        for item_id in ("item-01", "item-02")
    ]
    with pytest.raises(UnorderedStructrrConflict, match="unordered items"):
        compile_structrr_plan(
            plan_id="plan-01",
            baseline_snapshot={"entities": [{"id": "entity-01"}]},
            items=items,
            item_order=["item-01", "item-02"],
            operations=[
                {**operation, "operation_id": "op-01", "item_id": "item-01"},
                {**operation, "operation_id": "op-02", "item_id": "item-02"},
            ],
            source_ref="plan.md#sha256:abc",
        )


def test_operation_identity_and_attribution_are_bound_by_runtime() -> None:
    subject = BoundStructuralSubject(
        "entity-01",
        "entities",
        "item-01",
        "changed",
        ("req-01",),
        ("unit-01",),
        ("anchor-01",),
        False,
    )
    change = parse_structural_change(
        {
            "result": "change",
            "desired_content": {"description": "Updated description."},
            "reason": "The requirement changes the description.",
        },
        subject=subject,
        taxonomy_entity_types={"module"},
        planned_paths=set(),
        known_entity_ids={"entity-01"},
    )

    operation = bind_structural_operation(change)
    assert operation == {
        "operation_id": "item-01:entities:entity-01",
        "item_id": "item-01",
        "section": "entities",
        "subject_id": "entity-01",
        "action": "changed",
        "content": {
            "description": "Updated description.",
            "id": "entity-01",
            "action": "changed",
        },
    }
