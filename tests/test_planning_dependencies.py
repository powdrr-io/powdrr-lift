from __future__ import annotations

import hashlib

import pytest
from powdrr_service.planning.assembly import WorkUnit
from powdrr_service.planning.dependencies import (
    DependencyCandidate,
    DependencyCycleError,
    DependencyError,
    ManualSetup,
    UnitDependencyEdge,
    Validation,
    apply_setup_ownership,
    bind_confirmed_edge,
    bind_manual_setup,
    bind_unit_prerequisites,
    bind_validation,
    build_dependency_confirmation_request,
    build_human_setup_description_request,
    build_output_resolution_request,
    build_requires_human_setup_request,
    build_setup_equivalence_request,
    build_validation_coverage_request,
    build_validation_description_request,
    compile_dependency_graph,
    compile_manual_setup_ownership,
    parse_dependency_confirmation,
    parse_human_setup_need,
    parse_manual_setup_proposal,
    parse_manual_setup_record,
    parse_output_resolution,
    parse_validation_coverage,
    parse_validation_proposal,
    parse_validation_record,
)

from powdrr_lift.workrr.planning.contracts import PlanningContractError
from powdrr_lift.workrr.planning.evidence import Evidence
from powdrr_lift.workrr.planning.source import (
    PinnedSourceDocument,
    Requirement,
    SegmentedSource,
    segment_markdown_source,
)


def _context() -> tuple[SegmentedSource, dict[str, Requirement], str, Evidence]:
    text = "# Integration\n\nThe integration check needs a registered application.\n"
    document = PinnedSourceDocument(
        "repo",
        "a" * 40,
        "plan.md",
        "b" * 40,
        hashlib.sha256(text.encode()).hexdigest(),
        text,
    )
    source = segment_markdown_source(document)
    anchor = next(item for item in source.anchors if item.clause_id is not None)
    requirement = Requirement(
        "req-0001",
        "Run the trusted integration check.",
        "acceptance",
        (anchor.id,),
        ("group-0001",),
        (),
    )
    evidence = Evidence(
        "evidence-0001",
        "c" * 40,
        "src/controller.py",
        1,
        20,
        "d" * 64,
        "implementation",
        "run_check",
    )
    return source, {requirement.id: requirement}, anchor.id, evidence


def _validation(anchor: str, *, validation_id: str = "val-0001") -> Validation:
    return Validation(
        validation_id,
        "Run the report-only integration check.",
        "ci_check",
        "gh workflow run integration-report.yml",
        ("req-0001",),
        (anchor,),
        (),
    )


def _unit(unit_id: str, requirement_id: str = "req-0001") -> WorkUnit:
    return WorkUnit(
        unit_id,
        (requirement_id,),
        f"Deliverable {unit_id}",
        "integration",
        "python",
        (),
        f"Implement {unit_id}.",
        (f"{unit_id} works.",),
        ("val-0001",),
    )


def _setup(
    setup_id: str, validation_id: str, anchor: str, *, title: str
) -> ManualSetup:
    return ManualSetup(
        setup_id,
        ("req-0001",),
        "human",
        title,
        ("Register the application.", "Install it in the repository."),
        "Confirm the integration check can access the installation.",
        "after_implementation_before_validation",
        (validation_id,),
        (anchor,),
    )


def test_validation_proposal_is_bound_to_runtime_id_and_requirement() -> None:
    _, _, anchor, evidence = _context()
    raw = {
        "description": "Verify the controller output.",
        "kind": "local_command",
        "target": "bash scripts/verify.sh",
        "source_refs": [anchor],
        "evidence_ids": [evidence.id],
    }

    proposal = parse_validation_proposal(
        raw, source_ids={anchor}, evidence_ids={evidence.id}
    )
    bound = bind_validation(
        "val-0001",
        proposal,
        requirement_ids=["req-0001"],
        known_requirement_ids={"req-0001"},
    )

    assert bound.id == "val-0001"
    assert bound.requirement_ids == ("req-0001",)


def test_validation_coverage_is_individual_and_false_requires_empty_refs() -> None:
    _, _, anchor, _ = _context()
    validation = _validation(anchor)
    request = build_validation_coverage_request(
        "unit-0001:criterion-01",
        "The integration report can run against the installed app.",
        {validation.id: validation},
    )
    request.validate()
    assert request.question.kind == "check_validation_coverage"
    with pytest.raises(PlanningContractError, match="must be empty"):
        parse_validation_coverage(
            {
                "covered": False,
                "validation_ids": [validation.id],
                "reason": "The check does not exercise the integration report.",
            },
            validation_ids={validation.id},
        )


def test_output_resolution_only_selects_candidate_units_and_known_evidence() -> None:
    evidence = {"evidence-0001"}
    candidates = {"unit-0001"}
    selected = parse_output_resolution(
        {"result": "unit", "unit_id": "unit-0001", "reason": "Produces the config."},
        consumer_unit_id="unit-0002",
        candidate_unit_ids=candidates,
        evidence_ids=evidence,
    )
    assert selected.unit_id == "unit-0001"
    with pytest.raises(PlanningContractError, match="supplied candidate"):
        parse_output_resolution(
            {"result": "unit", "unit_id": "unit-9999", "reason": "Invented."},
            consumer_unit_id="unit-0002",
            candidate_unit_ids=candidates,
            evidence_ids=evidence,
        )
    with pytest.raises(PlanningContractError, match="depend on itself"):
        parse_output_resolution(
            {"result": "unit", "unit_id": "unit-0002", "reason": "Self."},
            consumer_unit_id="unit-0002",
            candidate_unit_ids={"unit-0002"},
            evidence_ids=evidence,
        )


def test_external_output_must_cite_supplied_evidence() -> None:
    value = {
        "result": "external",
        "evidence_ids": ["evidence-0001"],
        "description": "Existing hosted integration service.",
        "reason": "The service is operated outside the repository.",
    }

    result = parse_output_resolution(
        value,
        consumer_unit_id="unit-0002",
        candidate_unit_ids=set(),
        evidence_ids={"evidence-0001"},
    )

    assert result.result == "external"
    with pytest.raises(PlanningContractError, match="unknown references"):
        parse_output_resolution(
            {**value, "evidence_ids": ["evidence-0099"]},
            consumer_unit_id="unit-0002",
            candidate_unit_ids=set(),
            evidence_ids={"evidence-0001"},
        )


def test_dependency_confirmation_has_exact_required_and_independent_variants() -> None:
    required = parse_dependency_confirmation(
        {
            "result": "required",
            "output": "The registered app configuration.",
            "requirement_ids": ["req-0001"],
            "reason": "The integration check consumes this configuration.",
        },
        allowed_requirement_ids={"req-0001"},
    )
    independent = parse_dependency_confirmation(
        {"result": "independent", "reason": "The units use separate outputs."},
        allowed_requirement_ids={"req-0001"},
    )

    assert required.required
    edge = bind_confirmed_edge("unit-0001", "unit-0002", required)
    assert edge is not None
    assert edge.before_unit_id == "unit-0001"
    assert edge.after_unit_id == "unit-0002"
    bound_units = bind_unit_prerequisites(
        [_unit("unit-0001"), _unit("unit-0002", "req-0002")], [edge]
    )
    assert bound_units[1].prerequisite_unit_ids == ("unit-0001",)
    assert not independent.required
    assert bind_confirmed_edge("unit-0001", "unit-0002", independent) is None
    with pytest.raises(PlanningContractError, match="unknown references"):
        parse_dependency_confirmation(
            {
                "result": "required",
                "output": "Unknown output.",
                "requirement_ids": ["req-9999"],
                "reason": "It is needed.",
            },
            allowed_requirement_ids={"req-0001"},
        )


def test_dependency_edges_project_to_items_and_compute_ready_set() -> None:
    units = [
        _unit("unit-0001"),
        _unit("unit-0002", "req-0002"),
        _unit("unit-0003", "req-0003"),
    ]
    graph = compile_dependency_graph(
        units,
        {"unit-0001": "item-0001", "unit-0002": "item-0002", "unit-0003": "item-0003"},
        [
            UnitDependencyEdge(
                "unit-0001",
                "unit-0002",
                "Controller configuration.",
                "The test needs the controller output.",
                ("req-0001", "req-0002"),
            )
        ],
        item_order_hint=("item-0001", "item-0002", "item-0003"),
    )

    assert graph.item_order == ("item-0001", "item-0002", "item-0003")
    assert graph.ready_item_ids == ("item-0001", "item-0003")
    assert graph.item_edges[0].before_item_id == "item-0001"
    assert graph.item_edges[0].after_item_id == "item-0002"


def test_dependency_cycles_are_reported_instead_of_dropped() -> None:
    units = [_unit("unit-0001"), _unit("unit-0002")]
    edges = [
        UnitDependencyEdge("unit-0001", "unit-0002", "one", "needed", ("req-0001",)),
        UnitDependencyEdge("unit-0002", "unit-0001", "two", "needed", ("req-0001",)),
    ]

    with pytest.raises(DependencyCycleError, match="dependency cycle"):
        compile_dependency_graph(
            units,
            {"unit-0001": "item-0001", "unit-0002": "item-0002"},
            edges,
            item_order_hint=("item-0001", "item-0002"),
        )


def test_ci_bootstrap_dependency_is_added_only_with_explicit_requirement() -> None:
    units = [_unit("unit-0001"), _unit("unit-0002", "req-0002")]

    graph = compile_dependency_graph(
        units,
        {"unit-0001": "ci-bootstrap", "unit-0002": "feature"},
        [],
        item_order_hint=("ci-bootstrap", "feature"),
        ci_bootstrap_item_id="ci-bootstrap",
        ci_bootstrap_requirement_ids=("req-0001",),
    )

    assert graph.ready_item_ids == ("ci-bootstrap",)
    assert graph.item_edges[0].reason.startswith("CI bootstrap")


def test_human_setup_contract_rejects_sentinels_and_unknown_validations() -> None:
    _, _, anchor, _ = _context()
    need = parse_human_setup_need(
        {
            "required": True,
            "reason": "A maintainer must install the GitHub App.",
            "source_refs": [anchor],
            "evidence_ids": [],
        },
        source_ids={anchor},
        evidence_ids=set(),
    )
    proposal = parse_manual_setup_proposal(
        {
            "title": "Install the GitHub App",
            "instructions": ["Register it.", "Install it in the repository."],
            "verification": "Confirm the integration workflow can access the app.",
            "source_refs": [anchor],
        },
        source_ids={anchor},
    )
    setup = bind_manual_setup(
        "setup-0001",
        proposal,
        need=need,
        requirement_ids=["req-0001"],
        known_requirement_ids={"req-0001"},
        validation_ids=["val-0001"],
        known_validation_ids={"val-0001"},
    )

    assert need.required
    assert setup.owner == "human"
    assert setup.trigger == "after_implementation_before_validation"
    with pytest.raises(PlanningContractError, match="concrete human action"):
        parse_human_setup_need(
            {
                "required": False,
                "reason": "No manual prerequisite.",
                "source_refs": [anchor],
                "evidence_ids": [],
            },
            source_ids={anchor},
            evidence_ids=set(),
        )
    with pytest.raises(PlanningContractError, match="known validation IDs"):
        bind_manual_setup(
            "setup-0002",
            proposal,
            need=need,
            requirement_ids=["req-0001"],
            known_requirement_ids={"req-0001"},
            validation_ids=["item-0001"],
            known_validation_ids={"val-0001"},
        )
    not_required = parse_human_setup_need(
        {
            "required": False,
            "reason": "The validation uses only repository-local configuration.",
            "source_refs": [anchor],
            "evidence_ids": [],
        },
        source_ids={anchor},
        evidence_ids=set(),
    )
    with pytest.raises(DependencyError, match="was not required"):
        bind_manual_setup(
            "setup-0003",
            proposal,
            need=not_required,
            requirement_ids=["req-0001"],
            known_requirement_ids={"req-0001"},
            validation_ids=["val-0001"],
            known_validation_ids={"val-0001"},
        )


def test_setup_record_rejects_item_id_as_validation_reference() -> None:
    _, _, anchor, evidence = _context()
    validation = parse_validation_record(
        {
            "id": "val-0001",
            "description": "Run the app integration check.",
            "kind": "ci_check",
            "target": "gh workflow run integration-report.yml",
            "requirement_ids": ["req-0001"],
            "source_refs": [anchor],
            "evidence_ids": [evidence.id],
        },
        requirement_ids={"req-0001"},
        source_ids={anchor},
        evidence_ids={evidence.id},
    )
    setup = parse_manual_setup_record(
        {
            "id": "setup-0001",
            "requirement_ids": ["req-0001"],
            "owner": "human",
            "title": "Install the app",
            "instructions": ["Register it."],
            "verification": "Confirm the integration check can access the app.",
            "trigger": "after_implementation_before_validation",
            "unblocks_validation_ids": ["val-0001"],
            "source_refs": [anchor],
        },
        requirement_ids={"req-0001"},
        validation_ids={"val-0001"},
        source_ids={anchor},
    )

    assert validation.id == "val-0001"
    assert setup.unblocks_validation_ids == (validation.id,)
    with pytest.raises(PlanningContractError, match="unknown references"):
        parse_manual_setup_record(
            {**setup.to_data(), "unblocks_validation_ids": ["item-0001"]},
            requirement_ids={"req-0001"},
            validation_ids={"val-0001"},
            source_ids={anchor},
        )


def test_equivalent_shared_setup_is_owned_once_and_unblocks_named_validations() -> None:
    _, _, anchor, _ = _context()
    first = _setup("setup-0001", "val-0001", anchor, title="Install the app")
    second = _setup("setup-0002", "val-0002", anchor, title="Configure the app")
    validations = {
        "val-0001": _validation(anchor),
        "val-0002": Validation(
            "val-0002",
            "Run permission check",
            "ci_check",
            "gh workflow run permissions.yml",
            ("req-0001",),
            (anchor,),
            (),
        ),
    }

    ownership = compile_manual_setup_ownership(
        [first, second],
        validation_to_item={"val-0001": "item-0001", "val-0002": "item-0002"},
        validations=validations,
        item_order=("item-0001", "item-0002"),
        equivalence_groups=(("setup-0001", "setup-0002"),),
    )

    prerequisite = ownership.manual_prerequisites["item-0001"]
    assert prerequisite["instructions"] == list(first.instructions)
    assert "gh workflow run integration-report.yml" in prerequisite["unblocks"]
    assert "gh workflow run permissions.yml" in prerequisite["unblocks"]
    assert "PR-" not in prerequisite["unblocks"]
    assert ownership.added_item_edges[0].before_item_id == "item-0001"
    assert ownership.added_item_edges[0].after_item_id == "item-0002"

    graph = compile_dependency_graph(
        [_unit("unit-0001"), _unit("unit-0002", "req-0002")],
        {"unit-0001": "item-0001", "unit-0002": "item-0002"},
        [],
        item_order_hint=("item-0001", "item-0002"),
    )
    resumed = apply_setup_ownership(graph, ownership)
    assert resumed.ready_item_ids == ("item-0001",)


def test_setup_question_requests_have_parser_valid_examples() -> None:
    source, requirements, anchor, evidence = _context()
    validation = _validation(anchor)
    need = parse_human_setup_need(
        {
            "required": True,
            "reason": "An administrator must install the application.",
            "source_refs": [anchor],
            "evidence_ids": [evidence.id],
        },
        source_ids={anchor},
        evidence_ids={evidence.id},
    )

    request = build_requires_human_setup_request(
        validation,
        requirements=requirements,
        source=source,
        evidence={evidence.id: evidence},
    )
    request.validate()
    description = build_human_setup_description_request(
        validation,
        need,
        source=source,
        evidence={evidence.id: evidence},
    )
    description.validate()
    assert description.question.expected_answer_schema == "manual-setup-description-v1"


def test_validation_description_and_dependency_questions_are_parser_bound() -> None:
    source, requirements, anchor, evidence = _context()
    requirement = requirements["req-0001"]
    validation_example = {
        "records": [
            {
                "description": "Run the integration report check.",
                "kind": "ci_check",
                "target": "gh workflow run integration-report.yml",
                "source_refs": [anchor],
                "evidence_ids": [evidence.id],
            }
        ],
        "has_more": False,
        "next_anchor": None,
    }
    validation_request = build_validation_description_request(
        requirement,
        source=source,
        evidence={evidence.id: evidence},
        anchor_order=[anchor],
        valid_example=validation_example,
    )
    validation_request.validate()
    next_validation_page = build_validation_description_request(
        requirement,
        source=source,
        evidence={evidence.id: evidence},
        anchor_order=[anchor, anchor + "-later"],
        valid_example=validation_example,
        after_anchor=anchor,
    )
    assert (
        next_validation_page.question.question_id
        != validation_request.question.question_id
    )
    dependency_request = build_dependency_confirmation_request(
        _unit("unit-0001"),
        _unit("unit-0002", "req-0002"),
        "The controller configuration.",
        allowed_requirement_ids={"req-0001", "req-0002"},
    )
    another_output_request = build_dependency_confirmation_request(
        _unit("unit-0001"),
        _unit("unit-0002", "req-0002"),
        "The generated migration.",
        allowed_requirement_ids={"req-0001", "req-0002"},
    )
    dependency_request.validate()
    another_output_request.validate()

    assert validation_request.question.kind == "describe_requirement_validations"
    assert dependency_request.question.kind == "confirm_dependency"
    assert (
        dependency_request.question.question_id
        != another_output_request.question.question_id
    )


def test_output_and_setup_equivalence_questions_are_singular_and_valid() -> None:
    _, _, anchor, evidence = _context()
    output_request = build_output_resolution_request(
        "unit-0002",
        "The integration configuration.",
        [DependencyCandidate("unit-0001", "Creates the configuration.", ("req-0001",))],
        evidence={evidence.id: evidence},
        page_ordinal=1,
    )
    output_request.validate()
    first = _setup("setup-0001", "val-0001", anchor, title="Install the app")
    second = _setup("setup-0002", "val-0002", anchor, title="Configure the app")

    equivalence = build_setup_equivalence_request(first, second)
    equivalence.validate()
    assert output_request.question.kind == "resolve_prerequisite_output"
