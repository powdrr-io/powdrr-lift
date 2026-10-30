from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

import powdrr_service.planning.structrr as structrr_module
import pytest
from powdrr_service.planning.assembly import PlannedWrite, WorkUnit
from powdrr_service.planning.dependencies import Validation
from powdrr_service.planning.questions import build_question_messages
from powdrr_service.planning.source import (
    PinnedSourceDocument,
    Requirement,
    segment_markdown_source,
)
from powdrr_service.planning.structrr import (
    BoundStructuralSubject,
    StructrrPlanningError,
    StructuralSubjectPage,
    UnorderedStructrrConflict,
    bind_structural_operation,
    build_structural_change_request,
    build_subject_enumeration_request,
    compile_structrr_plan,
    complete_subject_enumeration,
    parse_structural_change,
    parse_structural_subject_page,
)

from powdrr_lift.workrr.planning.contracts import PlanningContractError
from powdrr_lift.workrr.planning.driver import (
    SourceLedger,
    SourceLedgerQuestionMachine,
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
    source_ids: set[str] | None = None,
) -> StructuralSubjectPage:
    return parse_structural_subject_page(
        value,
        plan_id="plan-01",
        item_id="item-01",
        requirement_ids=requirements or {"req-01"},
        units=units or (_unit(),),
        source_ids=source_ids or {"anchor-01"},
        projected_baseline=baseline or {"entities": []},
        prior_subject_ids=prior or set(),
        page_ordinal=page_ordinal,
    )


def test_structural_questions_send_only_relevant_baseline_subjects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unrelated_payload = "unrelated-baseline-detail-" + ("x" * 1_000)
    baseline = {
        "entities": [
            {"id": f"file:src/unrelated-{index}.py", "description": unrelated_payload}
            for index in range(80)
        ]
        + [{"id": "file:src/relevant.py", "type": "Source file"}],
        "entity_relationships": [],
        "files": [
            {"path": f"src/unrelated-{index}.py", "summary": unrelated_payload}
            for index in range(50)
        ]
        + [
            {"path": "src/relevant.py", "summary": "Relevant source file."},
            {"path": "src/projected.py", "summary": unrelated_payload},
        ],
        "source_subjects": [
            {
                "stable_key": f"python::unrelated_{index}",
                "path": f"src/unrelated-{index}.py",
                "qualified_name": unrelated_payload,
            }
            for index in range(80)
        ]
        + [
            {
                "stable_key": "python::relevant",
                "path": "src/relevant.py",
                "kind": "module",
                "qualified_name": "relevant",
            }
        ],
    }
    unit = _unit(
        writes=(
            PlannedWrite("src/relevant.py", "implementation", "python", (), ()),
            PlannedWrite("src/projected.py", "implementation", "python", (), ()),
        )
    )
    text = "# Plan\n\nUpdate the relevant module.\n"
    source = segment_markdown_source(
        PinnedSourceDocument(
            repository="owner/repo",
            commit_sha="a" * 40,
            path="docs/plan.md",
            blob_sha="b" * 40,
            content_sha256=hashlib.sha256(text.encode()).hexdigest(),
            text=text,
        )
    )
    source_ref = source.anchors[0].id
    monkeypatch.setattr(
        structrr_module, "snapshot_digest", lambda _snapshot: "sha256:constant"
    )

    enumeration = build_subject_enumeration_request(
        plan_id="plan",
        item_id="item-0001",
        units=(unit,),
        source=source,
        source_refs=(source_ref,),
        projected_baseline=baseline,
    )
    assert "new_identity_description is non-null, action must be added" in (
        enumeration.exact_question
    )
    enumeration_messages = build_question_messages(enumeration)
    enumeration_input = enumeration_messages[1]["content"]
    without_projected_file = build_subject_enumeration_request(
        plan_id="plan",
        item_id="item-0001",
        units=(unit,),
        source=source,
        source_refs=(source_ref,),
        projected_baseline={
            **baseline,
            "files": [
                record
                for record in baseline["files"]
                if record["path"] != "src/projected.py"
            ],
        },
    )

    subject = BoundStructuralSubject(
        "python::relevant",
        "files",
        "item-0001",
        "changed",
        ("req-01",),
        (unit.id,),
        (source_ref,),
        False,
        "src/relevant.py",
    )
    change = build_structural_change_request(
        subject,
        projected_baseline=baseline,
        taxonomy_entity_types=("Source file",),
        planned_paths={"src/relevant.py"},
        known_entity_ids={f"file:src/unrelated-{index}.py" for index in range(80)},
        valid_example={"result": "unchanged", "reason": "Already satisfied."},
    )
    change_input = build_question_messages(change)[1]["content"]

    assert "python::relevant" in enumeration_input
    assert "path::src/projected.py" in enumeration_input
    assert (
        enumeration.question.input_digest
        != without_projected_file.question.input_digest
    )
    assert "Never describe an existing file subject as new" in (
        enumeration.exact_question
    )
    allowed_subject_refs = enumeration.answer_schema["properties"]["records"]["items"][
        "properties"
    ]["subject_ref"]["enum"]
    assert "path::src/projected.py" in allowed_subject_refs
    assert "path::src/relevant.py" in allowed_subject_refs
    assert "src/relevant.py::src/relevant.py" not in allowed_subject_refs
    assert "python::unrelated_0" not in enumeration_input
    assert unrelated_payload not in enumeration_input
    assert "python::relevant" in change_input
    assert "python::unrelated_0" not in change_input
    assert unrelated_payload not in change_input


def test_structural_subject_answer_is_reasked_when_projected_context_changes() -> None:
    text = "# Plan\n\nImplement the requested behavior.\n"
    source = segment_markdown_source(
        PinnedSourceDocument(
            repository="owner/repo",
            commit_sha="a" * 40,
            path="docs/plan.md",
            blob_sha="b" * 40,
            content_sha256=hashlib.sha256(text.encode()).hexdigest(),
            text=text,
        )
    )
    source_ref = source.anchors[0].id
    machine: Any = SourceLedgerQuestionMachine(
        source,
        plan_id="plan-01",
        structrr_baseline={"entities": [], "entity_relationships": [], "files": []},
    )
    unit = _unit(
        writes=(PlannedWrite("src/new.py", "implementation", "python", (), ()),)
    )
    item_record = {
        "id": "item-0001",
        "dependencies": [],
        "allowed_paths": ["src/new.py"],
        "acceptance_criteria": [],
        "unit_ids": [unit.id],
        "units": [unit],
    }
    machine._structrr_item_records = lambda _answers: (item_record,)
    machine.accepted_assembly = lambda _answers, *, plan_id: SimpleNamespace(
        dependency_graph=SimpleNamespace(item_order=("item-0001",), item_edges=())
    )
    machine.accepted_ledger = lambda _answers: SimpleNamespace(
        requirements=(SimpleNamespace(id="req-01", source_refs=(source_ref,)),)
    )

    first_request = machine._structrr_requests({})[0]
    question_id = first_request.question.question_id
    machine.set_accepted_question_input_digests({question_id: "sha256:" + "0" * 64})
    stale_answer = {"records": [], "has_more": False, "next_anchor": None}

    requests = machine._structrr_requests({question_id: stale_answer})

    assert [request.question.question_id for request in requests] == [question_id]
    assert machine.invalid_accepted_question_ids == frozenset({question_id})
    assert machine._request_is_pending(requests[0], {question_id: stale_answer})


def test_explicit_human_setup_requirement_becomes_an_item_prerequisite() -> None:
    text = "# Plan\n\nA maintainer must install the signed provider.\n"
    source = segment_markdown_source(
        PinnedSourceDocument(
            repository="owner/repo",
            commit_sha="a" * 40,
            path="docs/plan.md",
            blob_sha="b" * 40,
            content_sha256=hashlib.sha256(text.encode()).hexdigest(),
            text=text,
        )
    )
    source_ref = source.anchors[0].id
    requirement = Requirement(
        "req-0001",
        "A maintainer must install the signed provider.",
        "human_setup",
        (source_ref,),
        (),
        (),
    )
    ledger = SourceLedger((requirement,), ())
    machine: Any = SourceLedgerQuestionMachine(source, plan_id="plan-01")

    requests = machine._explicit_human_setup_requests(ledger, {})

    assert len(requests) == 1
    request = requests[0]
    assert request.question.kind == "describe_human_setup"
    assert request.question.question_id == "describe-human-setup-req-0001"
    answer = dict(request.valid_example)
    validation = Validation(
        "validation-0001",
        "Run the verification command.",
        "local_command",
        "make test",
        (requirement.id,),
        (source_ref,),
        (),
    )
    assembly = SimpleNamespace(
        plan=SimpleNamespace(items=(SimpleNamespace(item_id="item-0001"),)),
        item_unit_ids={"item-0001": ("unit-0001",)},
        units=(SimpleNamespace(id="unit-0001", requirement_ids=(requirement.id,)),),
    )

    setups = machine._accepted_human_setups(
        ledger,
        {request.question.question_id: answer},
        assembly,
        validation,
    )

    assert len(setups) == 1
    setup, owner_item_id = setups[0]
    assert owner_item_id == "item-0001"
    assert setup.title == answer["title"]
    assert setup.trigger == "after_implementation_before_validation"
    assert setup.unblocks_validation_ids == (validation.id,)


def test_file_structural_change_omits_unrelated_entity_identity_index() -> None:
    subject = BoundStructuralSubject(
        "attestation/scripts/verify-all.sh",
        "files",
        "item-0001",
        "added",
        ("req-01",),
        ("unit-0001",),
        ("anchor-01",),
        False,
        "attestation/scripts/verify-all.sh",
    )
    request = build_structural_change_request(
        subject,
        projected_baseline={"entities": [], "entity_relationships": [], "files": []},
        taxonomy_entity_types=("Source file",),
        planned_paths={"attestation/scripts/verify-all.sh"},
        known_entity_ids={f"entity-{index:05d}" for index in range(4_000)},
        valid_example={"result": "unchanged", "reason": "Already satisfied."},
    )

    serialized_input = build_question_messages(request)[1]["content"]

    assert request.valid_example["result"] == "change"
    assert request.valid_example["desired_content"] == {
        "path": "attestation/scripts/verify-all.sh"
    }
    assert "one concise reason of at most 200 characters" in request.exact_question
    assert "entity-identities" not in serialized_input
    assert len(serialized_input) < 24_000


def test_entity_structural_change_omits_sibling_baseline_records() -> None:
    subject = BoundStructuralSubject(
        "python::module",
        "entities",
        "item-0001",
        "changed",
        ("req-01",),
        ("unit-0001",),
        ("anchor-01",),
        False,
        "src/module.py",
    )
    sibling_payload = "unrelated-sibling-description-" + ("x" * 300)
    baseline = {
        "entities": [
            {"id": "python::module", "type": "Module", "path": "src/module.py"},
            *[
                {
                    "id": f"python::module.sibling_{index}",
                    "type": "Function",
                    "name": sibling_payload,
                    "path": "src/module.py",
                }
                for index in range(100)
            ],
        ],
        "entity_relationships": [],
        "files": [
            {
                "path": "src/module.py",
                "summary": "large file summary marker " + ("s" * 2_000),
            }
        ],
        "source_subjects": [
            {
                "stable_key": "python::module",
                "path": "src/module.py",
                "qualified_name": "module",
            },
            *[
                {
                    "stable_key": f"python::module.sibling_{index}",
                    "path": "src/module.py",
                    "file_entity_id": "python::module",
                    "qualified_name": sibling_payload,
                }
                for index in range(100)
            ],
        ],
    }
    request = build_structural_change_request(
        subject,
        projected_baseline=baseline,
        taxonomy_entity_types=("Module",)
        + tuple(f"EntityType{index:04d}" for index in range(600)),
        planned_paths={"src/module.py"},
        known_entity_ids={"python::module"},
        valid_example={"result": "unchanged", "reason": "Already satisfied."},
    )

    serialized_input = build_question_messages(request)[1]["content"]

    assert "python::module" in serialized_input
    assert "python::module.sibling_0" not in serialized_input
    assert sibling_payload not in serialized_input
    assert "large file summary marker" not in serialized_input
    assert "EntityType0000" in serialized_input
    assert serialized_input.count("EntityType0000") == 1
    assert (
        "one of the supplied entity-taxonomy entity_types"
        in (request.answer_field_definitions["desired_content"])
    )
    assert len(serialized_input) < 24_000


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


def test_file_subject_can_reference_projected_file_entity_id() -> None:
    baseline = {
        "entities": [{"id": "file:src/existing.py", "path": "src/existing.py"}],
        "entity_relationships": [],
        "files": [{"path": "src/existing.py"}],
        "source_subjects": [],
    }
    record = _record("file:src/existing.py")
    record["section"] = "files"

    page = _parse_page(
        _candidate_page([record], has_more=False, next_anchor=None),
        baseline=baseline,
    )

    assert len(page.records) == 1
    assert page.records[0].subject_id == "path::src/existing.py"


def test_subject_page_accepts_all_known_source_references() -> None:
    source_ids = {f"anchor-{index:02d}" for index in range(9)}
    row = _record("entity-01")
    row["source_refs"] = sorted(source_ids)

    page = _parse_page(
        _candidate_page([row], has_more=False, next_anchor=None),
        baseline={"entities": [{"id": "entity-01"}]},
        source_ids=source_ids,
    )

    assert page.records[0].source_refs == tuple(sorted(source_ids))


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


def test_existing_projected_file_path_is_a_valid_structural_subject() -> None:
    path = "src/yaml_strict.py"
    baseline = {"files": [{"path": path}], "source_subjects": []}
    candidate = {
        "section": "files",
        "subject_ref": f"path::{path}",
        "new_identity_description": None,
        "action": "changed",
        "requirement_ids": ["req-01"],
        "unit_ids": ["unit-01"],
        "source_refs": ["anchor-01"],
    }

    page = _parse_page(
        _candidate_page([candidate], has_more=False, next_anchor=None),
        baseline=baseline,
    )

    assert page.records[0].subject_id == f"path::{path}"
    assert page.records[0].planned_path == path


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
