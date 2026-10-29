from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest
from powdrr_service.planning.assembly import (
    MAX_WORK_UNIT_PROPOSALS_PER_PAGE,
    MAX_WORK_UNIT_REQUIREMENTS_PER_QUESTION,
    AllocationGroup,
    AssemblyError,
    PathFact,
    RuntimeClassificationRequired,
    WorkUnit,
    WorkUnitContext,
    allocate_work_unit_groups,
    allocate_work_units,
    build_item_description_request,
    build_unit_cohesion_request,
    build_work_unit_proposal_request,
    parse_item_description,
    parse_work_unit_proposal,
)

from powdrr_lift.workrr.plan_sizing import (
    ExplicitWorkUnit,
    allocate_explicit_work_units,
)
from powdrr_lift.workrr.planning.contracts import PlanningContractError
from powdrr_lift.workrr.planning.driver import SourceLedgerQuestionMachine
from powdrr_lift.workrr.planning.source import (
    PinnedSourceDocument,
    Requirement,
    segment_markdown_source,
)


def _context(
    *, path: str = "Tests/Validator.swift", runtime: str = "swift"
) -> tuple[WorkUnitContext, str]:
    text = f"# Validation\n\nAdd `{path}` to validate Python behavior from Swift.\n"
    commit = "a" * 40
    document = PinnedSourceDocument(
        "repo",
        commit,
        "plan.md",
        "b" * 40,
        hashlib.sha256(text.encode()).hexdigest(),
        text,
    )
    source = segment_markdown_source(document)
    anchor = next(item for item in source.anchors if item.clause_id is not None)
    requirement = Requirement(
        "req-0001",
        "Add an acceptance test for the Python behavior.",
        "behavior",
        (anchor.id,),
        ("group-0001",),
        (),
    )
    context = WorkUnitContext(
        requirements={requirement.id: requirement},
        unresolved_requirement_ids=(requirement.id,),
        source=source,
        evidence={},
        validation_catalog={"validation-0001": {"description": "Run tests"}},
        component_ids=frozenset({"tests"}),
        repository_paths={path: PathFact(path, runtime)},
        ambiguous_runtime_resolutions={},
    )
    return context, anchor.id


def _proposal(
    context: WorkUnitContext,
    anchor: str,
    *,
    path: str = "Tests/Validator.swift",
    runtime: str = "swift",
) -> dict[str, object]:
    return {
        "requirement_ids": ["req-0001"],
        "deliverable": "A focused acceptance test.",
        "component": "tests",
        "implementation_runtime": runtime,
        "planned_writes": [
            {
                "path": path,
                "role": "test",
                "runtime": runtime,
                "source_refs": [anchor],
                "evidence_ids": [],
            }
        ],
        "implementation_step": "Add the acceptance test for the behavior.",
        "acceptance_criteria": ["The test covers the required behavior."],
        "validation_ids": ["validation-0001"],
        "required_outputs": [],
    }


def _unit(index: int, *, component: str = "core", runtime: str = "neutral") -> WorkUnit:
    return WorkUnit(
        f"unit-{index:04d}",
        (f"req-{index:04d}",),
        f"Deliverable {index}",
        component,
        runtime,
        (),
        f"Implement step {index}.",
        (f"Criterion {index}.",),
        ("validation-0001",),
    )


def test_swift_write_remains_swift_when_description_mentions_python() -> None:
    context, anchor = _context()

    proposal = parse_work_unit_proposal(_proposal(context, anchor), context=context)

    assert proposal.implementation_runtime == "swift"


def test_python_test_that_tests_swift_is_a_python_write() -> None:
    context, anchor = _context(path="tests/test_swift_feature.py", runtime="python")
    answer = _proposal(
        context, anchor, path="tests/test_swift_feature.py", runtime="python"
    )

    proposal = parse_work_unit_proposal(answer, context=context)

    assert proposal.planned_writes[0].runtime == "python"
    assert proposal.implementation_runtime == "python"


def test_new_shell_script_runtime_is_derived_without_repository_evidence() -> None:
    path = "attestation/scripts/verify-all.sh"
    context, anchor = _context(path=path, runtime="shell")
    context = replace(context, repository_paths={})
    answer = _proposal(context, anchor, path=path, runtime="shell")
    path_anchor = next(
        item.id
        for item in context.source.anchors
        if path in context.source.document.text[item.start_offset : item.end_offset]
    )
    requirement = replace(context.requirements["req-0001"], source_refs=(path_anchor,))
    context = replace(context, requirements={requirement.id: requirement})
    answer["planned_writes"][0]["source_refs"] = [path_anchor]  # type: ignore[index]

    proposal = parse_work_unit_proposal(answer, context=context)

    assert proposal.planned_writes[0].runtime == "shell"
    assert proposal.implementation_runtime == "shell"


def test_actual_python_and_swift_writes_cannot_share_a_unit() -> None:
    context, anchor = _context()
    answer = _proposal(context, anchor)
    python_write = dict(answer["planned_writes"][0])  # type: ignore[index]
    python_write.update(path="tests/test_helper.py", runtime="python")
    context = replace(
        context,
        repository_paths={
            **context.repository_paths,
            "tests/test_helper.py": PathFact("tests/test_helper.py", "python"),
        },
    )
    answer["planned_writes"] = [answer["planned_writes"][0], python_write]  # type: ignore[index]

    with pytest.raises(
        PlanningContractError, match="cannot mix implementation runtimes"
    ):
        parse_work_unit_proposal(answer, context=context)


def test_unknown_extension_requires_runtime_classification() -> None:
    context, anchor = _context(path="include/Widget.h", runtime="c++")
    answer = _proposal(context, anchor, path="include/Widget.h", runtime="c++")
    context = replace(
        context,
        repository_paths={"include/Widget.h": PathFact("include/Widget.h")},
    )

    with pytest.raises(RuntimeClassificationRequired):
        parse_work_unit_proposal(answer, context=context)


def test_new_extensionless_data_file_can_be_explicitly_runtime_neutral() -> None:
    context, anchor = _context()
    context = replace(context, repository_paths={})
    answer = _proposal(
        context,
        anchor,
        path="attestation/keys/authorized-keys",
        runtime="neutral",
    )
    planned_writes = answer["planned_writes"]
    assert isinstance(planned_writes, list)
    planned_writes[0]["runtime"] = None

    proposal = parse_work_unit_proposal(answer, context=context)

    assert proposal.implementation_runtime == "neutral"
    assert proposal.planned_writes[0].runtime is None


@pytest.mark.parametrize(
    "path",
    [
        "attestation/macos/Package.resolved",
        "attestation/config/sandbox-profile.sb",
        "installer/launchd.plist",
        "config/install-paths.conf",
        "test-results/section13-macos-github.log",
    ],
)
def test_platform_data_file_extensions_are_runtime_neutral(path: str) -> None:
    context, anchor = _context(path=path, runtime="neutral")
    context = replace(context, repository_paths={})
    answer = _proposal(context, anchor, path=path, runtime="neutral")
    planned_writes = answer["planned_writes"]
    assert isinstance(planned_writes, list)
    planned_writes[0]["runtime"] = None

    proposal = parse_work_unit_proposal(answer, context=context)

    assert proposal.implementation_runtime == "neutral"
    assert proposal.planned_writes[0].runtime is None


def test_gitkeep_is_neutral_inside_a_source_named_directory() -> None:
    directory = "attestation/github/workflows/"
    path = f"{directory}.gitkeep"
    context, anchor = _context(path=directory, runtime="neutral")
    context = replace(context, repository_paths={})
    path_anchor = next(
        item.id
        for item in context.source.anchors
        if directory
        in context.source.document.text[item.start_offset : item.end_offset]
    )
    requirement = replace(context.requirements["req-0001"], source_refs=(path_anchor,))
    context = replace(context, requirements={requirement.id: requirement})
    answer = _proposal(context, anchor, path=path, runtime="neutral")
    answer["planned_writes"][0]["source_refs"] = [path_anchor]  # type: ignore[index]
    answer["planned_writes"][0]["runtime"] = None  # type: ignore[index]

    proposal = parse_work_unit_proposal(answer, context=context)

    assert proposal.planned_writes[0].path == path
    assert proposal.planned_writes[0].runtime is None


def test_new_path_is_allowed_in_a_new_directory() -> None:
    new_path = "attestation/providers/local/run.sh"
    context, anchor = _context(path="legacy/verify-all.sh", runtime="shell")
    context = replace(context, repository_paths={})
    answer = _proposal(context, anchor, path=new_path, runtime="shell")

    proposal = parse_work_unit_proposal(answer, context=context)

    assert proposal.planned_writes[0].path == new_path


def test_allocation_assigns_ids_and_requires_every_unresolved_requirement() -> None:
    context, anchor = _context()
    proposal = parse_work_unit_proposal(_proposal(context, anchor), context=context)

    result = allocate_work_units([proposal], context=context)

    assert [unit.id for unit in result.units] == ["unit-0001"]
    assert result.units[0].requirement_ids == ("req-0001",)
    missing = replace(context, unresolved_requirement_ids=("req-0001", "req-0002"))
    with pytest.raises(AssemblyError, match="leave unresolved requirements uncovered"):
        allocate_work_units([proposal], context=missing)


def test_cp_sat_targets_three_units_and_never_exceeds_four() -> None:
    allocation = allocate_work_unit_groups([_unit(index) for index in range(1, 8)])

    assert [len(group.unit_ids) for group in allocation.groups] == [3, 4]
    assert all(len(group.unit_ids) <= 4 for group in allocation.groups)
    assert {unit for group in allocation.groups for unit in group.unit_ids} == {
        f"unit-{index:04d}" for index in range(1, 8)
    }


def test_cp_sat_splits_actual_mixed_runtime_writes() -> None:
    allocation = allocate_work_unit_groups(
        [_unit(1, runtime="swift"), _unit(2, runtime="python")]
    )

    assert len(allocation.groups) == 2
    assert {group.implementation_runtime for group in allocation.groups} == {
        "swift",
        "python",
    }


def test_explicit_cp_sat_does_not_infer_runtime_from_unit_prose() -> None:
    result = allocate_explicit_work_units(
        [
            ExplicitWorkUnit("unit-0001", "tests", "swift", 1),
            ExplicitWorkUnit("unit-0002", "tests", "swift", 1),
        ]
    )

    assert result.groups == (("unit-0001", "unit-0002"),)


def test_standalone_units_are_not_grouped_with_product_work() -> None:
    allocation = allocate_work_unit_groups(
        [_unit(1), _unit(2), _unit(3)], standalone_unit_ids=frozenset({"unit-0002"})
    )

    assert next(
        group for group in allocation.groups if "unit-0002" in group.unit_ids
    ).unit_ids == ("unit-0002",)


def test_scope_description_must_assign_each_unit_once() -> None:
    group = AllocationGroup(
        "allocation-0001", ("unit-0001", "unit-0002"), (), (), "neutral", 2
    )
    valid = {
        "title": "Add the parser behavior",
        "objective": "Implement and validate the parser behavior.",
        "scope": [
            {"description": "Build both units.", "unit_ids": ["unit-0001", "unit-0002"]}
        ],
    }

    assert parse_item_description(valid, group=group).title == "Add the parser behavior"
    invalid = {
        **valid,
        "scope": [{"description": "Only one.", "unit_ids": ["unit-0001"]}],
    }
    with pytest.raises(
        PlanningContractError, match="cover each allocated unit exactly once"
    ):
        parse_item_description(invalid, group=group)


def test_item_description_question_example_passes_its_parser() -> None:
    group = AllocationGroup(
        "allocation-0001", ("unit-0001",), ("req-0001",), ("core",), "neutral", 1
    )
    request = build_item_description_request(group, {"unit-0001": _unit(1)})

    request.validate()


def test_work_unit_question_has_a_parser_valid_example_and_exact_context() -> None:
    context, anchor = _context()
    example_proposal = _proposal(context, anchor)
    example = {
        "records": [example_proposal],
        "has_more": False,
        "next_anchor": None,
    }

    request = build_work_unit_proposal_request(
        context, ["req-0001"], valid_example=example
    )

    request.validate()
    assert request.question.kind == "propose_work_units"
    assert "requirement's own source_refs" in request.exact_question
    assert "runtime to null for non-executable artifacts" in request.exact_question
    assert "If every supplied requirement is covered" in request.exact_question
    assert (
        "requirements remain uncovered" in request.answer_field_definitions["has_more"]
    )
    assert (
        request.answer_schema["properties"]["records"]["maxItems"]
        == MAX_WORK_UNIT_PROPOSALS_PER_PAGE
    )
    record_schema = request.answer_schema["properties"]["records"]["items"]
    assert record_schema["properties"]["validation_ids"]["items"]["enum"] == [
        "validation-0001"
    ]
    assert MAX_WORK_UNIT_REQUIREMENTS_PER_QUESTION == 4
    with pytest.raises(PlanningContractError, match="must contain"):
        request.parse_answer(
            {
                "records": [example_proposal] * 4,
                "has_more": False,
                "next_anchor": None,
            }
        )

    with pytest.raises(ValueError, match="one to four requirements"):
        build_work_unit_proposal_request(
            context,
            ["req-0001"] * 5,
            valid_example=example,
        )

    prefixed_validation = dict(example_proposal)
    prefixed_validation["validation_ids"] = ["validation:validation-0001"]
    with pytest.raises(PlanningContractError, match="unknown references"):
        request.parse_answer(
            {"records": [prefixed_validation], "has_more": False, "next_anchor": None}
        )

    second_requirement = replace(context.requirements["req-0001"], id="req-0002")
    two_requirement_context = replace(
        context,
        requirements={
            **context.requirements,
            second_requirement.id: second_requirement,
        },
        unresolved_requirement_ids=("req-0001", "req-0002"),
    )
    larger_batch = build_work_unit_proposal_request(
        two_requirement_context,
        ["req-0001", "req-0002"],
        valid_example=example,
    )
    assert larger_batch.question.question_id != request.question.question_id
    next_page = build_work_unit_proposal_request(
        two_requirement_context,
        ["req-0001", "req-0002"],
        valid_example=example,
        page_after="req-0001",
    )
    assert next_page.question.question_id != larger_batch.question.question_id
    one_proposal = build_work_unit_proposal_request(
        two_requirement_context,
        ["req-0001"],
        valid_example=example,
        maximum_proposals=1,
    )
    default_single = build_work_unit_proposal_request(
        two_requirement_context,
        ["req-0001"],
        valid_example=example,
    )
    assert one_proposal.answer_schema["properties"]["records"]["maxItems"] == 1
    assert one_proposal.question.question_id != default_single.question.question_id
    with pytest.raises(PlanningContractError, match="between 0 and 1 entries"):
        one_proposal.parse_answer(
            {
                "records": [example_proposal, example_proposal],
                "has_more": False,
                "next_anchor": None,
            }
        )


def test_work_unit_page_cursor_is_derived_and_cannot_skip_uncovered_requirements() -> (
    None
):
    base_context, anchor = _context()
    requirement_ids = ("req-0013", "req-0014", "req-0015", "req-0016")
    base_requirement = base_context.requirements["req-0001"]
    requirements = {
        requirement_id: replace(base_requirement, id=requirement_id)
        for requirement_id in requirement_ids
    }
    context = replace(
        base_context,
        requirements=requirements,
        unresolved_requirement_ids=requirement_ids,
    )
    proposals = []
    for requirement_id in requirement_ids[:3]:
        proposal = _proposal(context, anchor)
        proposal["requirement_ids"] = [requirement_id]
        proposals.append(proposal)
    request = build_work_unit_proposal_request(
        context,
        requirement_ids,
        valid_example={
            "records": [proposals[0]],
            "has_more": False,
            "next_anchor": None,
        },
    )

    assert request.answer_schema["properties"]["next_anchor"]["enum"] == [
        *requirement_ids[:-1],
        None,
    ]
    page = request.parse_answer(
        {"records": proposals, "has_more": True, "next_anchor": None}
    )
    assert page.next_anchor == "req-0015"
    page_with_first_uncovered_cursor = request.parse_answer(
        {"records": proposals, "has_more": True, "next_anchor": "req-0016"}
    )
    assert page_with_first_uncovered_cursor.next_anchor == "req-0015"

    with pytest.raises(PlanningContractError, match="must not skip requirements"):
        request.parse_answer(
            {
                "records": [proposals[0], proposals[2]],
                "has_more": True,
                "next_anchor": "req-0015",
            }
        )

    final_proposal = _proposal(context, anchor)
    final_proposal["requirement_ids"] = ["req-0016"]
    next_request = build_work_unit_proposal_request(
        context,
        requirement_ids,
        valid_example={
            "records": [final_proposal],
            "has_more": False,
            "next_anchor": None,
        },
        page_after=page.next_anchor,
    )
    final_page = next_request.parse_answer(
        {"records": [final_proposal], "has_more": False, "next_anchor": None}
    )
    assert final_page.next_anchor is None


def test_work_unit_continuation_only_includes_uncovered_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_context, anchor = _context()
    requirement_ids = ("req-0013", "req-0014", "req-0015", "req-0016")
    base_requirement = base_context.requirements["req-0001"]
    requirements = {
        requirement_id: replace(base_requirement, id=requirement_id)
        for requirement_id in requirement_ids
    }
    context = replace(
        base_context,
        requirements=requirements,
        unresolved_requirement_ids=requirement_ids,
    )
    machine = SourceLedgerQuestionMachine(context.source)
    ledger = SimpleNamespace(requirements=tuple(requirements.values()))
    coverage = tuple(
        SimpleNamespace(
            requirement_id=requirement_id,
            answer=SimpleNamespace(status="missing"),
        )
        for requirement_id in requirement_ids
    )
    monkeypatch.setattr(machine, "_parse_source_ledger", lambda _answers: ledger)
    monkeypatch.setattr(machine, "accepted_coverage", lambda _answers: coverage)
    monkeypatch.setattr(
        machine, "_work_unit_context", lambda _ledger, _unresolved: context
    )

    initial_request = machine._work_unit_requests({})[0]
    assert initial_request is not None
    initial_request.validate()
    example_records = initial_request.valid_example["records"]
    assert len(example_records) == 1
    assert set(example_records[0]) == {
        "requirement_ids",
        "deliverable",
        "component",
        "implementation_runtime",
        "planned_writes",
        "implementation_step",
        "acceptance_criteria",
        "validation_ids",
        "required_outputs",
    }
    proposals = []
    for requirement_id in requirement_ids[:3]:
        proposal = _proposal(context, anchor)
        proposal["requirement_ids"] = [requirement_id]
        proposals.append(proposal)
    accepted = {
        initial_request.question.question_id: {
            "records": proposals,
            "has_more": True,
            "next_anchor": None,
        }
    }

    continuation = machine._work_unit_requests(accepted)[0]

    assert continuation is not None
    assert continuation.question.subject_ids == ("req-0016",)
    final_proposal = _proposal(context, anchor)
    final_proposal["requirement_ids"] = ["req-0016"]
    accepted[continuation.question.question_id] = {
        "records": [final_proposal],
        "has_more": False,
        "next_anchor": None,
    }
    monkeypatch.setattr(machine, "accepted_ledger", lambda _answers: ledger)
    _, reconstructed_proposals = machine._accepted_work_unit_proposals(accepted)
    assert {
        requirement_id
        for proposal in reconstructed_proposals
        for requirement_id in proposal.requirement_ids
    } == set(requirement_ids)

    machine.set_oversized_work_unit_groups((requirement_ids,))
    split_requests = machine._work_unit_requests({})
    assert tuple(
        request.question.subject_ids
        for request in split_requests
        if request is not None
    ) == tuple((requirement_id,) for requirement_id in requirement_ids)
    machine.set_oversized_work_unit_groups((requirement_ids, ("req-0016",)))
    single_request = next(
        request
        for request in machine._work_unit_requests({})
        if request is not None and request.question.subject_ids == ("req-0016",)
    )
    assert single_request.answer_schema["properties"]["records"]["maxItems"] == 1


def test_cohesion_question_example_passes_its_parser() -> None:
    context, anchor = _context()
    proposal = parse_work_unit_proposal(_proposal(context, anchor), context=context)

    request = build_unit_cohesion_request(
        proposal, context=context, subject_id="proposal-0001"
    )

    request.validate()
    assert request.question.kind == "check_unit_cohesion"


def test_cohesion_split_can_allocate_one_requirement_to_multiple_units() -> None:
    context, anchor = _context()
    proposal = parse_work_unit_proposal(_proposal(context, anchor), context=context)
    split = {
        "decision": "split",
        "reason": "Implementation and focused acceptance test are separate steps.",
        "replacements": [proposal.to_data(), proposal.to_data()],
    }

    from powdrr_service.planning.assembly import parse_cohesion_decision

    result = parse_cohesion_decision(split, original=proposal, context=context)

    assert result.decision == "split"
    assert len(result.replacements) == 2
