from __future__ import annotations

import pytest

from powdrr_lift.core.acceptance_contract import (
    AcceptanceCriterion,
    CriterionAssertion,
)
from powdrr_lift.core.behavior_contract import CriterionQuality
from powdrr_lift.core.implementation_packet import compile_implementation_packet


def test_packet_renders_behavioral_test_descriptions_without_selectors() -> None:
    packet = compile_implementation_packet(
        objective="Add state data support.",
        obligations=("Data initializes on entry.",),
        required_tests=(
            {
                "description": "Verify entry initialization.",
                "provider": "pytest",
                "profile": "pytest",
                "selector": "tests/test_state.py::test_entry",
            },
        ),
        allowed_paths=("src/state.py", "tests/test_state.py"),
        validation_profiles=("pytest",),
        existing_tests=(
            {
                "provider": "pytest",
                "profile": "pytest",
                "selector": "tests/test_state.py::test_existing",
                "fingerprint": "sha256:test",
            },
        ),
    )

    rendered = packet.render()
    assert "Required behavioral tests:" in rendered
    assert "obligation 001" not in rendered
    assert "Add state data support." not in rendered
    assert "IMPORTANT:" not in rendered
    assert "add a focused test proving Verify entry initialization." in rendered
    assert "tests/test_state.py::test_entry" not in rendered
    assert "pytest/pytest" not in rendered
    assert "Verify entry initialization." in rendered
    assert "create the exact selectors" not in rendered
    assert "candidate 1: tests/test_state.py::test_existing" not in rendered
    assert "model-authored" not in rendered
    assert "repair the test and implementation as needed" in rendered
    assert "repository validation" not in rendered
    assert packet.from_data(packet.to_data()).to_data() == packet.to_data()


def test_packet_preserves_source_objective_without_rendering_product_work() -> None:
    objective = (
        "Add behavior.\n\nIMPORTANT: create a branch from main and commit everything."
    )
    packet = compile_implementation_packet(
        objective=objective,
        obligations=("The behavior exists.",),
        required_tests=(
            {
                "description": "Verify the behavior.",
                "provider": "pytest",
                "profile": "pytest",
                "selector": "tests/test_feature.py::test_behavior",
            },
        ),
        allowed_paths=("src",),
        validation_profiles=("pytest",),
    )

    assert packet.objective == objective
    assert "IMPORTANT:" not in packet.render()


def test_packet_renders_scoped_external_contract_and_unresolved_source_notes() -> None:
    packet = compile_implementation_packet(
        objective="Add incremental GraphQL support.",
        obligations=("Support @defer.",),
        required_tests=(
            {
                "description": "Verify deferred fragment behavior.",
                "provider": "pytest",
                "profile": "pytest",
                "selector": "tests/test_incremental.py::test_defer",
            },
        ),
        allowed_paths=("src",),
        validation_profiles=("pytest",),
        external_contract_requirements=(
            {
                "requirement": "Support the optional label argument on @defer.",
                "canonical_url": "https://spec.example.org/defer",
                "profile": "directive v1",
                "source_quote": "@defer accepts label.",
                "rationale": "It is needed to construct the requested directive.",
            },
        ),
        external_contract_notes=(
            {
                "rationale": "A newer profile may define a different payload format.",
                "claim": {
                    "candidate_requirement": "Use the newer pending/id payload format.",
                    "canonical_url": "https://spec.example.org/latest",
                    "profile": "latest draft",
                },
            },
        ),
    )

    rendered = packet.render()
    assert "Support the optional label argument on @defer." in rendered
    assert "https://spec.example.org/defer" in rendered
    assert "@defer accepts label." in rendered
    assert (
        "Unresolved external contract questions (do not assume an answer)" in rendered
    )
    assert "Use the newer pending/id payload format." in rendered
    assert packet.from_data(packet.to_data()).render() == rendered


def test_packet_requires_a_contract_for_each_obligation() -> None:
    with pytest.raises(ValueError, match="requires test contracts"):
        compile_implementation_packet(
            objective="Add behavior.",
            obligations=("The behavior exists.",),
            required_tests=(),
            allowed_paths=("src/feature.py",),
            validation_profiles=("pytest",),
        )


def test_packet_can_focus_one_obligation_and_its_matching_test() -> None:
    packet = compile_implementation_packet(
        objective="Add state data support.",
        obligations=("Initialize data.", "Reset data."),
        required_tests=(
            {
                "description": "Verify initialization.",
                "provider": "pytest",
                "profile": "pytest",
            },
            {
                "description": "Verify reset.",
                "provider": "pytest",
                "profile": "pytest",
            },
        ),
        allowed_paths=("src/state.py",),
        validation_profiles=("pytest",),
    )

    focused = packet.for_obligation(2)

    assert focused.obligations == ("Reset data.",)
    assert focused.required_tests[0]["description"] == "Verify reset."
    assert "Initialize data." not in focused.render()
    assert "Verify initialization." not in focused.render()


def test_packet_can_focus_a_compiled_code_task() -> None:
    packet = compile_implementation_packet(
        objective="Implement the entire feature.",
        obligations=("The entire feature exists.",),
        required_tests=({"description": "Verify the entire feature."},),
        allowed_paths=("src/state.py",),
        validation_profiles=("pytest",),
    )

    focused = packet.for_task(
        objective="Implement the reset behavior.",
        acceptance_criteria=("The reset behavior works.",),
    )

    assert focused.objective == "Implement the reset behavior."
    assert focused.obligations == ("Implement the reset behavior.",)
    assert focused.required_tests == ({"description": "The reset behavior works."},)
    assert "Implement the entire feature." not in focused.render()
    assert "Verify the entire feature." not in focused.render()
    assert "The reset behavior works." in focused.render()


def test_packet_renders_reviewed_criteria_by_contract_without_metadata() -> None:
    criteria = (
        AcceptanceCriterion(
            criterion_id="criterion-a",
            contract_id="contract-streaming",
            kind="state_transition",
            source_refs=("instruction-001",),
            setup={"result": {"items": ["first"]}},
            operation="process successive payloads",
            events=({"payload": {"items": ["second"]}},),
            assertions=(
                CriterionAssertion(
                    assertion_id="assertion-a",
                    observation="result.items",
                    relation="equals",
                    expected=["first", "second"],
                    source_refs=("instruction-001",),
                    basis="source_derived",
                ),
            ),
            unresolved_questions=(),
            quality=CriterionQuality(criterion_status="checkable"),
        ),
        AcceptanceCriterion(
            criterion_id="criterion-b",
            contract_id="contract-streaming",
            kind="invariant",
            source_refs=("instruction-002",),
            setup={"extensions": {"cursor": 1}},
            operation="process a later payload",
            events=(),
            assertions=(
                CriterionAssertion(
                    assertion_id="assertion-b",
                    observation="result.extensions",
                    relation="equals",
                    expected={"cursor": 2},
                    source_refs=("instruction-002",),
                    basis="source_derived",
                ),
            ),
            unresolved_questions=(),
            quality=CriterionQuality(criterion_status="checkable"),
        ),
    )
    packet = compile_implementation_packet(
        objective="Implement incremental payload handling.",
        obligations=("Accumulate data across payloads.",),
        required_tests=({"description": "Verify accumulated payload handling."},),
        allowed_paths=("src/", "tests/"),
        validation_profiles=("pytest",),
        behavior_scenarios=(
            {
                "scenario_id": "instruction-001",
                "subject": "result mapping",
                "given": "a prior result exists",
                "when": "another payload arrives",
                "then": "entries are accumulated",
                "dimensions": {
                    "normal_result": "entries are accumulated",
                    "error_behavior": "not_applicable",
                    "continuation": "not_applicable",
                    "unsupported_behavior": "not_applicable",
                    "cancellation_cleanup": "not_applicable",
                    "compatibility": "not_applicable",
                    "negative_boundaries": "not_applicable",
                },
                "evidence": ["The result mapping accumulates entries."],
                "validator": "pytest",
                "criterion_quality": {"criterion_status": "checkable"},
                "assumptions": [
                    {
                        "dimension": "compatibility",
                        "resolution": "Preserve the existing mapping type.",
                        "rationale": "Existing callers rely on mapping behavior.",
                        "basis": "repository_convention",
                        "basis_reference": "src/result.py:ResultMap",
                        "confidence": "high",
                    }
                ],
            },
        ),
        acceptance_criteria=tuple(item.to_data() for item in criteria),
    )

    rendered = packet.render()

    assert rendered.count("Contract 1: ") == 1
    assert '1. Start with {"result":{"items":["first"]}}.' in rendered
    assert 'Then apply {"payload":{"items":["second"]}}.' in rendered
    assert 'Check that result.items equals ["first","second"].' in rendered
    assert (
        '2. Start with {"extensions":{"cursor":1}}. Perform process a later payload.'
        in rendered
    )
    assert 'Check that result.extensions equals {"cursor":2}.' in rendered
    assert "criterion-a" not in rendered
    assert "instruction-001" not in rendered
    assert "[state_transition;" not in rendered
    assert "Necessary implementation choices and assumptions:" in rendered
    assert "Preserve the existing mapping type." in rendered
    assert "src/result.py:ResultMap" not in rendered


def test_packet_keeps_material_open_question_for_unresolved_criterion() -> None:
    criterion = AcceptanceCriterion(
        criterion_id="criterion-open",
        contract_id="contract-open",
        kind="invariant",
        source_refs=("instruction-003",),
        setup={"items": ["one"]},
        operation="process the optional field",
        events=(),
        assertions=(
            CriterionAssertion(
                assertion_id="assertion-open",
                observation="result.items",
                relation="equals",
                expected=["one"],
                source_refs=("instruction-003",),
                basis="source_derived",
            ),
        ),
        unresolved_questions=("Should absent values be preserved or cleared?",),
        quality=CriterionQuality(
            criterion_status="unresolved",
            failure_stage="criterion_review",
            failure_reason="a material implementation decision is unresolved",
        ),
    )
    packet = compile_implementation_packet(
        objective="Implement the optional field.",
        obligations=("Support the optional field.",),
        required_tests=({"description": "Verify optional field behavior."},),
        allowed_paths=("src/",),
        validation_profiles=("pytest",),
        acceptance_criteria=(criterion.to_data(),),
    )

    rendered = packet.render()

    assert "Material implementation questions left open:" in rendered
    assert "Should absent values be preserved or cleared?" in rendered
    assert "process the optional field" not in rendered


def test_packet_prompt_contains_every_required_test_without_compiler_metadata() -> None:
    packet = compile_implementation_packet(
        objective="Add state data support.",
        obligations=("Initialize data on entry.", "Reset data on re-entry."),
        required_tests=(
            {
                "description": "Verify initialization.",
                "provider": "pytest",
                "profile": "pytest",
                "name_hint": "test_state_data_initialization",
                "selector": "tests/test_state.py::test_initialization",
            },
            {
                "description": "Verify reset.",
                "provider": "pytest",
                "profile": "pytest",
                "name_hint": "test_state_data_reset",
                "selector": "tests/test_state.py::test_reset",
            },
        ),
        allowed_paths=("src/state.py", "tests/test_state.py"),
        validation_profiles=("pytest",),
    )

    rendered = packet.render()

    for expected in (
        "add a focused test proving Verify initialization.",
        "add a focused test proving Verify reset.",
    ):
        assert expected in rendered
    assert "tests/test_state.py::test_initialization" not in rendered
    assert "tests/test_state.py::test_reset" not in rendered
    assert "pytest/pytest" not in rendered

    assert "Initialize data on entry." not in rendered
    assert "Reset data on re-entry." not in rendered
